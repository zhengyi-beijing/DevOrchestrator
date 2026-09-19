"""JobService providing idempotent submission, status, logs, cancel, reconcile and retry."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.platform.process import is_pid_alive
from dev_orchestrator.storage.json_store import utc_now_iso

from .config import (
    DEFAULT_HEAD_LINES,
    DEFAULT_MAX_JOB_BYTES,
    DEFAULT_MAX_LINE_BYTES,
    DEFAULT_TAIL_LINES,
    JobsConfig,
    load_jobs_config,
    resolve_local_jobs_config_path,
    validate_and_resolve_execution,
)
from .models import (
    JobRecord,
    JobSpec,
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

        self.transports: dict[str, JobTransport] = dict(transports) if transports else {
            "local": LocalJobTransport(),
        }
        if "ssh" not in self.transports:
            ssh_t = self._resolve_ssh_transport()
            if ssh_t is not None:
                self.transports["ssh"] = ssh_t

    def _resolve_ssh_transport(self) -> Optional[JobTransport]:
        """Auto-wire SSHJobTransport from JobsConfig or aibroker-execution.json."""
        if self.config and self.config.ssh:
            from dev_orchestrator.ai.execution_transport import SSHTransportConfig
            s = self.config.ssh
            peer = s.get("peer") or s.get("host")
            if peer:
                ssh_cfg = SSHTransportConfig(
                    peer=str(peer).strip(),
                    user=str(s["user"]).strip() if s.get("user") else None,
                    port=int(s.get("port") or 22),
                    identity_file=Path(s["identity_file"]) if s.get("identity_file") else None,
                    known_hosts_file=Path(s["known_hosts_file"]) if s.get("known_hosts_file") else None,
                    strict_host_key_checking=str(s.get("strict_host_key_checking") or "yes"),
                    remote_python=str(s.get("remote_python") or "python3"),
                    path_mapping=dict(s.get("path_mapping") or {}),
                    expected_host_identity=str(s["expected_host_identity"]).strip() if s.get("expected_host_identity") else None,
                )
                return SSHJobTransport(ssh_cfg)

        aibroker_file = self.runtime_root / "aibroker-execution.json"
        if aibroker_file.is_file():
            from dev_orchestrator.storage.json_store import read_json
            data = read_json(aibroker_file, None)
            if isinstance(data, dict):
                raw_transport = data.get("transport")
                if isinstance(raw_transport, dict) and raw_transport.get("type") == "ssh":
                    from dev_orchestrator.ai.execution_transport import SSHTransportConfig
                    peer = raw_transport.get("peer") or raw_transport.get("host")
                    if peer:
                        ssh_cfg = SSHTransportConfig(
                            peer=str(peer).strip(),
                            user=str(raw_transport["user"]).strip() if raw_transport.get("user") else None,
                            port=int(raw_transport.get("port") or 22),
                            identity_file=Path(raw_transport["identity_file"]) if raw_transport.get("identity_file") else None,
                            known_hosts_file=Path(raw_transport["known_hosts_file"]) if raw_transport.get("known_hosts_file") else None,
                            strict_host_key_checking=str(raw_transport.get("strict_host_key_checking") or "yes"),
                            remote_python=str(raw_transport.get("remote_python") or "python3"),
                            path_mapping=dict(raw_transport.get("path_mapping") or {}),
                            expected_host_identity=str(raw_transport["expected_host_identity"]).strip() if raw_transport.get("expected_host_identity") else None,
                        )
                        return SSHJobTransport(ssh_cfg)
        return None

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
            caps = dict(self.config.log_caps) if self.config else {
                "max_line_bytes": DEFAULT_MAX_LINE_BYTES,
                "max_job_bytes": DEFAULT_MAX_JOB_BYTES,
                "head_lines": DEFAULT_HEAD_LINES,
                "tail_lines": DEFAULT_TAIL_LINES,
            }
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
                max_runtime_seconds=cmd_cfg.max_runtime_seconds,
                heartbeat_interval_seconds=cmd_cfg.heartbeat_interval_seconds,
                log_caps=caps,
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

        remote_polled_ok = False
        rem_status: dict[str, Any] | None = None

        # If remote transport, poll status to sync evidence
        if record.transport != "local":
            try:
                transport = self._get_transport(record.transport)
                jdir = self.store._job_dir(job_id)
                polled = transport.job_status(job_id, jdir)
                if isinstance(polled, dict):
                    rem_status = polled
                    remote_polled_ok = True
                    rem_res = rem_status.get("result")
                    if isinstance(rem_res, dict):
                        self.store.save_result(job_id, rem_res)
                    rem_hb = rem_status.get("heartbeat")
                    if isinstance(rem_hb, dict):
                        self.store.save_heartbeat(job_id, rem_hb)
                    rem_job = rem_status.get("job")
                    if isinstance(rem_job, dict):
                        def _sync_remote_job(rec: JobRecord) -> None:
                            rem_sup = rem_job.get("supervisor")
                            if isinstance(rem_sup, dict):
                                for k, v in rem_sup.items():
                                    if v is not None:
                                        rec.supervisor[k] = v
                            rem_ts = rem_job.get("timestamps")
                            if isinstance(rem_ts, dict):
                                for k, v in rem_ts.items():
                                    if v is not None:
                                        rec.timestamps[k] = v
                            if rem_job.get("state") == "running" and rec.state == "queued":
                                rec.transition_to(
                                    "running",
                                    reason=None,
                                    failure_kind=None,
                                    timestamp=rec.timestamps.get("started_at") or utc_now_iso(),
                                )
                            host_id = rem_status.get("host_identity")
                            if host_id:
                                rec.host_identity = host_id
                        self.store.update(job_id, _sync_remote_job)
                        record = self.store.get(job_id) or record
            except Exception:
                pass

        result_data = self.store.get_result(job_id)
        if result_data is not None:
            # Result exists: promote to terminal state
            exit_code = result_data.get("exit_code")
            outcome = result_data.get("outcome")
            err = result_data.get("error")

            def _promote_terminal(rec: JobRecord) -> None:
                if rec.state in ("completed", "failed", "cancelled"):
                    return
                if rec.state == "queued" and outcome != "cancelled" and exit_code == 0:
                    rec.transition_to("running", timestamp=rec.timestamps.get("started_at") or utc_now_iso())
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

        # Result does not exist; check supervisor liveness and heartbeat evidence
        hb = self.store.get_heartbeat(job_id)
        pid = record.supervisor.get("pid")
        start_token = record.supervisor.get("start_token")
        live_pid = (hb.get("pid") if hb else None) or pid
        hb_token = (hb.get("start_token") if hb else None) or start_token

        # Check start_token mismatch
        if hb and start_token and hb.get("start_token") and hb.get("start_token") != start_token:
            def _fail_token_mismatch(rec: JobRecord) -> None:
                if rec.state in ("completed", "failed", "cancelled"):
                    return
                rec.transition_to(
                    "unknown_recovery",
                    reason="start_token mismatch in heartbeat evidence",
                    failure_kind="start_token_mismatch",
                    timestamp=utc_now_iso(),
                )
                rec.recovery["recovery_safe_retry"] = False
            return self.store.update(job_id, _fail_token_mismatch)

        # Durably consume heartbeat if sequence has advanced or first observation
        if hb:
            hb_seq = int(hb.get("heartbeat_sequence") or hb.get("sequence") or 0)
            prev_seq = int(record.heartbeat.get("heartbeat_sequence") or record.heartbeat.get("sequence") or 0)
            now_iso = utc_now_iso()
            if hb_seq > prev_seq or not record.heartbeat.get("observed_at"):
                updated_hb = dict(hb)
                updated_hb["sequence"] = hb_seq
                updated_hb["heartbeat_sequence"] = hb_seq
                updated_hb["observed_at"] = now_iso
                self.store.save_heartbeat(job_id, updated_hb)
                record = self.store.get(job_id) or record

        # Determine supervisor liveness
        if record.transport == "local":
            is_alive = bool(live_pid and is_pid_alive(live_pid) and (not start_token or hb_token == start_token))
        else:
            # Remote/SSH transport: do NOT check is_pid_alive locally against a remote PID!
            # Judge liveness by remote status and locally observed heartbeat sequence
            if remote_polled_ok and rem_status is not None:
                if rem_status.get("supervisor_alive") is not None:
                    is_alive = bool(rem_status.get("supervisor_alive"))
                elif rem_status.get("job") and rem_status["job"].get("state") == "running":
                    is_alive = True
                elif rem_status.get("heartbeat"):
                    is_alive = True
                else:
                    is_alive = False
            else:
                # Remote poll failed (e.g. transport interruption / network blip):
                # If job was running and last heartbeat was observed within stalled timeout, consider in-flight
                last_obs = record.heartbeat.get("observed_at")
                if last_obs and record.state == "running":
                    try:
                        from datetime import datetime, timezone
                        obs_dt = datetime.fromisoformat(str(last_obs).replace("Z", "+00:00"))
                        now_dt = datetime.now(timezone.utc)
                        hb_interval = float(getattr(record, "heartbeat_interval_seconds", 5.0) or 5.0)
                        stalled_timeout = max(15.0, hb_interval * 3)
                        is_alive = (now_dt - obs_dt).total_seconds() <= stalled_timeout
                    except Exception:
                        is_alive = False
                else:
                    is_alive = False

        if is_alive:
            # Process appears alive; check if heartbeat sequence has stalled
            hb_interval = float(getattr(record, "heartbeat_interval_seconds", 5.0) or 5.0)
            stalled_timeout = max(15.0, hb_interval * 3)
            last_observed_iso = record.heartbeat.get("observed_at")
            if last_observed_iso and hb:
                hb_seq = int(hb.get("heartbeat_sequence") or hb.get("sequence") or 0)
                prev_seq = int(record.heartbeat.get("heartbeat_sequence") or record.heartbeat.get("sequence") or 0)
                try:
                    from datetime import datetime, timezone
                    obs_dt = datetime.fromisoformat(str(last_observed_iso).replace("Z", "+00:00"))
                    now_dt = datetime.now(timezone.utc)
                    elapsed = (now_dt - obs_dt).total_seconds()
                    if hb_seq <= prev_seq and elapsed > stalled_timeout:
                        def _promote_stalled(rec: JobRecord) -> None:
                            if rec.state in ("completed", "failed", "cancelled"):
                                return
                            rec.transition_to(
                                "unknown_recovery",
                                reason=f"heartbeat stalled: sequence {hb_seq} did not advance for {elapsed:.1f}s",
                                failure_kind="heartbeat_stalled",
                                timestamp=utc_now_iso(),
                            )
                            rec.recovery["recovery_safe_retry"] = False
                        return self.store.update(job_id, _promote_stalled)
                except Exception:
                    pass

            return record

        # Process is not alive and no result.json exists
        if record.state == "unknown_recovery":
            return record

        # Check positive evidence of never started
        has_started_evidence = bool(
            record.timestamps.get("started_at")
            or record.supervisor.get("start_token")
            or (hb and (hb.get("heartbeat_sequence") or hb.get("sequence") or hb.get("start_token")))
            or record.state == "running"
        )

        if not has_started_evidence:
            if record.transport == "local":
                # Local job with no start evidence and no alive process
                def _fail_never_started_local(rec: JobRecord) -> None:
                    if rec.state in ("completed", "failed", "cancelled"):
                        return
                    rec.transition_to(
                        "failed",
                        reason="supervisor process never started",
                        failure_kind="never_started",
                        timestamp=utc_now_iso(),
                    )
                    rec.recovery["recovery_safe_retry"] = True

                return self.store.update(job_id, _fail_never_started_local)
            else:
                # SSH/remote job: require POSITIVE confirmation from remote host that job was never started
                if remote_polled_ok and (rem_status is None or not rem_status.get("job") or rem_status.get("job", {}).get("state") == "queued"):
                    def _fail_never_started_remote(rec: JobRecord) -> None:
                        if rec.state in ("completed", "failed", "cancelled"):
                            return
                        rec.transition_to(
                            "failed",
                            reason="remote supervisor process never started",
                            failure_kind="never_started",
                            timestamp=utc_now_iso(),
                        )
                        rec.recovery["recovery_safe_retry"] = True

                    return self.store.update(job_id, _fail_never_started_remote)
                else:
                    # Remote poll failed or ambiguous: do NOT set recovery_safe_retry=True!
                    def _fail_ambiguous_remote(rec: JobRecord) -> None:
                        if rec.state in ("completed", "failed", "cancelled"):
                            return
                        rec.transition_to(
                            "failed",
                            reason="remote host unreachable or ambiguous start evidence",
                            failure_kind="transport_unreachable",
                            timestamp=utc_now_iso(),
                        )
                        rec.recovery["recovery_safe_retry"] = False

                    return self.store.update(job_id, _fail_ambiguous_remote)

        # Process started and died without writing result.json: ambiguous
        # Check result.json one more time under lock before promoting to unknown_recovery!
        def _promote_unknown(rec: JobRecord) -> None:
            if rec.state in ("completed", "failed", "cancelled"):
                return
            res = self.store.get_result(job_id)
            if res is not None:
                res_exit = res.get("exit_code")
                res_outcome = res.get("outcome")
                res_err = res.get("error")
                if res_outcome == "cancelled":
                    rec.transition_to("cancelled", reason=res_err or "cancelled", failure_kind="cancelled")
                elif res_exit == 0:
                    rec.transition_to("completed", reason=None, failure_kind=None)
                else:
                    rec.transition_to("failed", reason=res_err or f"exit code {res_exit}", failure_kind="non_zero_exit")
                rec.exit_code = res_exit
                rec.terminal = dict(res)
                rec.timestamps["finished_at"] = res.get("finished_at") or utc_now_iso()
                return

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
