"""JobService providing idempotent submission, status, logs, cancel, reconcile and retry."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, Mapping, Optional

from dev_orchestrator.platform.process import is_pid_alive
from dev_orchestrator.storage.json_store import utc_now_iso

from .config import (
    JobsConfig,
    load_jobs_config,
    resolve_local_jobs_config_path,
    validate_and_resolve_execution,
)
from .models import (
    JobRecord,
    JobSpec,
    job_id_for,
    retry_successor_id,
)
from .store import ExecutionJobStore
from .transport import JobTransport, LocalJobTransport, SSHJobTransport


class JobService:
    """Service facade coordinating ExecutionJobStore and JobTransports."""

    def __init__(
        self,
        runtime_root: Path | str,
        transports: Optional[dict[str, JobTransport]] = None,
        config: Optional[JobsConfig] = None,
        config_path: Optional[Path | str] = None,
        accounting: Any = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.store = ExecutionJobStore(self.runtime_root)
        self.accounting = accounting

        if config is not None:
            self.config = config
        else:
            cfg_p = Path(config_path) if config_path else resolve_local_jobs_config_path(self.runtime_root)
            self.config = load_jobs_config(cfg_p)

        self.transports: dict[str, JobTransport] = transports or {
            "local": LocalJobTransport(),
        }

    def _get_transport(self, transport_name: str) -> JobTransport:
        if transport_name not in self.transports:
            raise ValueError(f"transport {transport_name!r} not configured in JobService")
        return self.transports[transport_name]

    def submit(
        self,
        spec: JobSpec,
        *,
        target_job_id: Optional[str] = None,
        retry_of: Optional[str] = None,
        attempt: int = 1,
    ) -> JobRecord:
        """Submit a job idempotently. If newly claimed, spawns exactly one supervisor."""
        if self.config is None:
            raise RuntimeError("execution-jobs.json is missing or jobs runtime is disabled")

        ok, failure_kind, resolved_cwd, cmd_cfg = validate_and_resolve_execution(
            self.config,
            spec.project_id,
            spec.command_ref,
            expected_working_directory=spec.expected_working_directory,
        )
        if not ok or cmd_cfg is None or resolved_cwd is None:
            raise ValueError(f"execution validation failed: {failure_kind}")

        def _factory(jid: str, shash: str) -> JobRecord:
            now = utc_now_iso()
            rec = JobRecord(
                job_id=jid,
                idempotency_key=spec.idempotency_key,
                spec_hash=shash,
                kind=spec.kind,
                project_id=spec.project_id,
                command_ref=spec.command_ref,
                resolved_argv=cmd_cfg.argv,
                working_directory=str(resolved_cwd),
                transport=spec.transport,
                host_identity=socket.gethostname(),
                duration_class=cmd_cfg.duration_class,
                state="queued",
                task_id=spec.task_id,
                stage_run_id=spec.stage_run_id,
                role_run_id=spec.role_run_id,
                source_request_id=spec.source_request_id,
                broker_request_id=spec.broker_request_id,
                timestamps={
                    "created_at": now,
                    "queued_at": now,
                    "started_at": None,
                    "finished_at": None,
                    "updated_at": now,
                },
                retry={
                    "retry_of": retry_of,
                    "attempt": attempt,
                    "retry_request_id": None,
                    "retry_request_hash": None,
                    "successor_job_id": None,
                    "successor_spawned_at": None,
                },
            )
            return rec

        record, is_new = self.store.claim_or_get(spec, _factory, target_job_id=target_job_id)
        if is_new:
            transport = self._get_transport(spec.transport)
            jdir = self.store._job_dir(record.job_id)
            start_res = transport.job_start(spec, jdir)
            sup_pid = start_res.get("supervisor_pid") if isinstance(start_res, dict) else None
            if isinstance(sup_pid, int) and sup_pid > 0:
                def _record_pid(rec: JobRecord) -> None:
                    rec.supervisor["pid"] = sup_pid
                record = self.store.update(record.job_id, _record_pid)

        return record

    def status(self, job_id: str) -> JobRecord | None:
        """Get job record from store."""
        return self.store.get(job_id)

    def logs(self, job_id: str, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        """Read paginated secret-redacted logs."""
        record = self.store.get(job_id)
        if record is None:
            raise ValueError(f"job {job_id} not found")
        transport = self._get_transport(record.transport)
        jdir = self.store._job_dir(job_id)
        return transport.job_logs(job_id, jdir, cursor=cursor, limit=limit)

    def cancel(self, job_id: str, reason: str = "cancelled") -> JobRecord:
        """Cancel active job."""
        record = self.store.get(job_id)
        if record is None:
            raise ValueError(f"job {job_id} not found")

        if record.state in ("completed", "failed", "cancelled"):
            return record

        transport = self._get_transport(record.transport)
        jdir = self.store._job_dir(job_id)
        transport.job_cancel(job_id, jdir, reason=reason)

        def _mutate(rec: JobRecord) -> None:
            rec.transition_to("cancelled", reason=reason, failure_kind="cancelled", timestamp=utc_now_iso())
            rec.terminal["outcome"] = "cancelled"
            rec.terminal["error"] = reason

        return self.store.update(job_id, _mutate)

    def reconcile(self, job_id: str) -> JobRecord:
        """Reconcile job state strictly from durable evidence."""
        record = self.store.get(job_id)
        if record is None:
            raise ValueError(f"job {job_id} not found")

        # Terminal records remain terminal
        if record.state in ("completed", "failed", "cancelled"):
            return record

        result_data = self.store.get_result(job_id)
        if result_data is not None:
            # Result exists: promote to terminal state
            exit_code = result_data.get("exit_code")
            outcome = result_data.get("outcome")
            err = result_data.get("error")

            def _promote_terminal(rec: JobRecord) -> None:
                if outcome == "cancelled":
                    rec.transition_to("cancelled", reason=err or "cancelled", failure_kind="cancelled")
                elif exit_code == 0:
                    rec.transition_to("completed", reason=None, failure_kind=None)
                else:
                    rec.transition_to("failed", reason=err or f"exit code {exit_code}", failure_kind="non_zero_exit")
                rec.exit_code = exit_code
                rec.terminal = dict(result_data)
                rec.timestamps["finished_at"] = result_data.get("finished_at") or utc_now_iso()

            return self.store.update(job_id, _promote_terminal)

        # Result does not exist; check supervisor liveness
        hb = self.store.get_heartbeat(job_id)
        pid = record.supervisor.get("pid")
        start_token = record.supervisor.get("start_token")
        live_pid = (hb.get("pid") if hb else None) or pid
        hb_token = (hb.get("start_token") if hb else None) or start_token

        if live_pid and is_pid_alive(live_pid) and hb_token == start_token:
            # Process is currently alive with matching start_token
            return record

        # Process is not alive and no result.json exists
        if record.state == "unknown_recovery":
            return record

        started_at = record.timestamps.get("started_at")
        if record.state == "queued" or (not started_at and (not pid or pid <= 0)):
            # Process never started (or died before transitioning to running)
            def _fail_never_started(rec: JobRecord) -> None:
                rec.transition_to(
                    "failed",
                    reason="supervisor process never started",
                    failure_kind="never_started",
                    timestamp=utc_now_iso(),
                )
                rec.recovery["recovery_safe_retry"] = True

            return self.store.update(job_id, _fail_never_started)

        # Process started and died without writing result.json: ambiguous
        def _promote_unknown(rec: JobRecord) -> None:
            rec.transition_to(
                "unknown_recovery",
                reason="supervisor died mid-run without terminal result",
                failure_kind="supervisor_died_without_result",
                timestamp=utc_now_iso(),
            )
            rec.recovery["recovery_safe_retry"] = False

        return self.store.update(job_id, _promote_unknown)

    def retry(self, job_id: str, retry_request_id: str) -> JobRecord:
        """Retry a job after reconciling predecessor and claiming write-once intent."""
        if not retry_request_id or not retry_request_id.strip():
            raise ValueError("retry_request_id must be nonblank and stable across replays")

        # Reconcile predecessor first
        pred = self.reconcile(job_id)

        is_terminal = pred.state in ("completed", "failed", "cancelled")
        is_safe = bool(pred.recovery.get("recovery_safe_retry", False))
        if not is_terminal and not is_safe:
            raise ValueError(
                f"cannot retry job {job_id} in state {pred.state!r}; "
                "job is neither terminal nor marked recovery_safe_retry"
            )

        successor_id, is_new = self.store.claim_retry(job_id, retry_request_id)

        if not is_new:
            # Replay: successor was already claimed
            succ = self.store.get(successor_id)
            if succ is not None:
                return succ

        # Spawn successor
        new_attempt = int(pred.retry.get("attempt") or 1) + 1
        succ_spec = JobSpec(
            project_id=pred.project_id,
            command_ref=pred.command_ref,
            idempotency_key=f"{pred.idempotency_key}:retry:{retry_request_id}",
            kind=pred.kind,
            task_id=pred.task_id,
            stage_run_id=pred.stage_run_id,
            role_run_id=pred.role_run_id,
            source_request_id=pred.source_request_id,
            broker_request_id=pred.broker_request_id,
            transport=pred.transport,
            expected_working_directory=pred.working_directory,
        )

        succ_record = self.submit(
            succ_spec,
            target_job_id=successor_id,
            retry_of=job_id,
            attempt=new_attempt,
        )

        def _record_spawn(rec: JobRecord) -> None:
            rec.retry["successor_spawned_at"] = utc_now_iso()

        self.store.update(job_id, _record_spawn)
        return succ_record
