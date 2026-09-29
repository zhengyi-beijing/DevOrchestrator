"""Native local machine execution transport implementing MachineTransport."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Any, Mapping, Optional
import uuid

from dev_orchestrator.jobs.config import (
    JobsConfig,
    is_path_contained,
    load_jobs_config,
    resolve_execution_policy,
    resolve_remote_jobs_config_path,
)
from dev_orchestrator.jobs.models import JobRecord, JobSpec, job_id_for
from dev_orchestrator.jobs.store import ExecutionJobStore
from dev_orchestrator.jobs.transport import LocalJobTransport
from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.storage.json_store import utc_now_iso
from dev_orchestrator.transport.contracts import (
    FileReadRequest,
    FileResult,
    FileWriteRequest,
    HostCapabilities,
    MachineOperation,
    MachineOperationResult,
    StagedWriteContent,
    StatResult,
    TransportError,
    TransportRejectedError,
    TransportUnavailableError,
    WriteContentUpload,
)
from dev_orchestrator.transport.hosts import discover_local_capabilities


class LocalMachineTransport:
    """Native local machine execution transport implementing MachineTransport."""

    def __init__(
        self,
        jobs_config_path: Optional[Path | str] = None,
        *,
        host_id: str = "local",
        jobs_config: Optional[JobsConfig] = None,
    ) -> None:
        self.host_id = host_id
        if jobs_config is not None:
            self.jobs_cfg = jobs_config
        else:
            cfg_path = jobs_config_path or resolve_remote_jobs_config_path()
            loaded = load_jobs_config(cfg_path)
            if loaded is None:
                rt_dir = Path(jobs_config_path).parent if jobs_config_path else Path.cwd()
                self.jobs_cfg = JobsConfig(runtime_root=rt_dir, enabled=False, projects={})
            else:
                self.jobs_cfg = loaded

        self.store = ExecutionJobStore(self.jobs_cfg.runtime_root)
        self.local_job_transport = LocalJobTransport()
        self.staging_store = self.store.write_store

    def exec(self, request: MachineOperation) -> MachineOperationResult:
        """Execute a read-only command synchronously. Effectful commands fail closed."""
        ok, failure_kind, resolved = resolve_execution_policy(
            self.jobs_cfg,
            request.project_id,
            request.command_ref,
            parameters=request.parameters,
            expected_working_directory=request.expected_working_directory,
        )
        if not ok:
            if failure_kind and "hardware" in failure_kind:
                raise TransportRejectedError("hardware_execution_not_supported_in_p18")
            raise TransportRejectedError(f"execution policy resolution failed: {failure_kind}")
        assert resolved is not None
        if resolved.effect_class == "hardware":
            raise TransportRejectedError("hardware_execution_not_supported_in_p18")
        if resolved.effect_class != "read_only":
            raise TransportRejectedError(
                f"LocalMachineTransport.exec only supports read_only commands, found effect_class: {resolved.effect_class!r}"
            )

        requested_timeout = request.timeout_seconds
        if requested_timeout is not None and float(requested_timeout) <= 0:
            raise TransportRejectedError("timeout_seconds must be positive")
        timeout = min(float(requested_timeout), resolved.max_runtime_seconds) if requested_timeout is not None else resolved.max_runtime_seconds
        start_t = time.monotonic()
        try:
            completed = subprocess.run(
                resolved.resolved_argv,
                cwd=resolved.resolved_cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                **hidden_subprocess_kwargs(),
            )
            duration = time.monotonic() - start_t
            status = "ok" if completed.returncode == 0 else "failed"
            max_output_bytes = max(128, min(2 * 1024 * 1024, int(self.jobs_cfg.log_caps.get("max_job_bytes", 2 * 1024 * 1024))))
            stdout_raw = completed.stdout.encode("utf-8", errors="replace")
            stderr_raw = completed.stderr.encode("utf-8", errors="replace")
            stdout_budget = min(len(stdout_raw), max_output_bytes)
            stderr_budget = min(len(stderr_raw), max(0, max_output_bytes - stdout_budget))
            stdout = stdout_raw[:stdout_budget].decode("utf-8", errors="ignore")
            stderr = stderr_raw[:stderr_budget].decode("utf-8", errors="ignore")
            truncated_bytes = (len(stdout_raw) - stdout_budget) + (len(stderr_raw) - stderr_budget)
            return MachineOperationResult(
                operation_id=request.idempotency_key or str(uuid.uuid4()),
                command_ref=request.command_ref,
                status=status,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                host_identity=socket.gethostname(),
                duration_seconds=duration,
                parameters_digest=resolved.parameters_digest,
                execution_policy_digest=resolved.execution_policy_digest,
                resolution_digest=resolved.resolution_digest,
                raw_evidence={
                    "cwd": str(resolved.resolved_cwd),
                    "output_truncated": truncated_bytes > 0,
                    "truncated_bytes": truncated_bytes,
                    "output_limit_bytes": max_output_bytes,
                },
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start_t
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            max_output_bytes = max(128, min(2 * 1024 * 1024, int(self.jobs_cfg.log_caps.get("max_job_bytes", 2 * 1024 * 1024))))
            stdout_raw = stdout.encode("utf-8", errors="replace")
            stderr_raw = stderr.encode("utf-8", errors="replace")
            stdout_budget = min(len(stdout_raw), max_output_bytes)
            stderr_budget = min(len(stderr_raw), max(0, max_output_bytes - stdout_budget))
            stdout = stdout_raw[:stdout_budget].decode("utf-8", errors="ignore")
            stderr = stderr_raw[:stderr_budget].decode("utf-8", errors="ignore")
            truncated_bytes = (len(stdout_raw) - stdout_budget) + (len(stderr_raw) - stderr_budget)
            return MachineOperationResult(
                operation_id=request.idempotency_key or str(uuid.uuid4()),
                command_ref=request.command_ref,
                status="timeout",
                exit_code=-1,
                stdout=stdout,
                stderr=stderr,
                host_identity=socket.gethostname(),
                duration_seconds=duration,
                error=f"command timed out after {timeout} seconds",
                parameters_digest=resolved.parameters_digest,
                execution_policy_digest=resolved.execution_policy_digest,
                resolution_digest=resolved.resolution_digest,
                raw_evidence={
                    "cwd": str(resolved.resolved_cwd),
                    "output_truncated": truncated_bytes > 0,
                    "truncated_bytes": truncated_bytes,
                    "output_limit_bytes": max_output_bytes,
                },
            )
        except Exception as exc:
            duration = time.monotonic() - start_t
            return MachineOperationResult(
                operation_id=request.idempotency_key or str(uuid.uuid4()),
                command_ref=request.command_ref,
                status="failed",
                error=str(exc),
                host_identity=socket.gethostname(),
                duration_seconds=duration,
                parameters_digest=resolved.parameters_digest,
                execution_policy_digest=resolved.execution_policy_digest,
                resolution_digest=resolved.resolution_digest,
            )

    def spawn(self, request: MachineOperation) -> MachineOperationResult:
        """Spawn an idempotent or effectful command via durable JobService."""
        ok, failure_kind, resolved = resolve_execution_policy(
            self.jobs_cfg,
            request.project_id,
            request.command_ref,
            parameters=request.parameters,
            expected_working_directory=request.expected_working_directory,
        )
        if not ok:
            if failure_kind and "hardware" in failure_kind:
                raise TransportRejectedError("hardware_execution_not_supported_in_p18")
            raise TransportRejectedError(f"execution policy resolution failed: {failure_kind}")
        assert resolved is not None
        if resolved.effect_class == "hardware":
            raise TransportRejectedError("hardware_execution_not_supported_in_p18")

        spec = JobSpec(
            project_id=request.project_id,
            command_ref=request.command_ref,
            idempotency_key=request.idempotency_key,
            expected_working_directory=request.expected_working_directory,
            input_digest=request.input_digest,
            parameters_digest=resolved.parameters_digest,
            execution_policy_digest=resolved.execution_policy_digest,
            resolution_digest=resolved.resolution_digest,
        )
        target_job_id = job_id_for(spec)

        def _factory(jid: str, shash: str) -> JobRecord:
            now = utc_now_iso()
            jdir = self.store._job_dir(jid)
            resolved_argv = [arg.replace("{job_dir}", str(jdir)) for arg in resolved.resolved_argv]
            return JobRecord(
                job_id=jid,
                idempotency_key=spec.idempotency_key,
                spec_hash=shash,
                kind=spec.kind,
                project_id=spec.project_id,
                command_ref=spec.command_ref,
                resolved_argv=resolved_argv,
                working_directory=str(resolved.resolved_cwd),
                transport="local",
                host_identity=socket.gethostname(),
                duration_class=resolved.duration_class,
                max_runtime_seconds=resolved.max_runtime_seconds,
                heartbeat_interval_seconds=resolved.heartbeat_interval_seconds,
                log_caps=dict(self.jobs_cfg.log_caps),
                input_digest=spec.input_digest,
                submission_spec=spec.to_canonical_dict(),
                parameters=resolved.canonical_parameters,
                parameters_digest=resolved.parameters_digest,
                execution_policy_digest=resolved.execution_policy_digest,
                resolution_digest=resolved.resolution_digest,
                state="queued",
                timestamps={"created_at": now, "queued_at": now, "updated_at": now},
            )

        record, is_new = self.store.claim_or_get(
            spec,
            _factory,
            target_job_id=target_job_id,
            max_active_jobs=self.jobs_cfg.max_concurrent_jobs,
        )
        if is_new:
            start_res = self.local_job_transport.job_start(spec, self.store._job_dir(record.job_id))
            sup_pid = start_res.get("supervisor_pid") if isinstance(start_res, dict) else None
            if isinstance(sup_pid, int) and sup_pid > 0:
                def _record_pid(rec: JobRecord) -> None:
                    rec.supervisor["pid"] = sup_pid
                self.store.update(record.job_id, _record_pid)

        return MachineOperationResult(
            operation_id=record.job_id,
            command_ref=request.command_ref,
            status="ok" if record.state in ("queued", "running", "completed") else record.state,
            job_id=record.job_id,
            host_identity=socket.gethostname(),
            parameters_digest=resolved.parameters_digest,
            execution_policy_digest=resolved.execution_policy_digest,
            resolution_digest=resolved.resolution_digest,
            raw_evidence={"already_exists": not is_new, "state": record.state},
        )

    def poll(self, operation_id: str, host_id: Optional[str] = None) -> MachineOperationResult:
        """Poll the current status of a spawned durable operation."""
        record = self.store.get(operation_id)
        if record is None:
            raise TransportRejectedError(f"job {operation_id!r} not found")
        status_res = self.local_job_transport.job_status(operation_id, self.store._job_dir(operation_id))
        st = status_res.get("status") or record.state
        return MachineOperationResult(
            operation_id=operation_id,
            command_ref=record.command_ref,
            status=st,
            exit_code=record.exit_code,
            host_identity=record.host_identity or socket.gethostname(),
            job_id=operation_id,
            parameters_digest=record.parameters_digest,
            execution_policy_digest=record.execution_policy_digest,
            resolution_digest=record.resolution_digest,
            raw_evidence=status_res,
        )

    def cancel(
        self, operation_id: str, host_id: Optional[str] = None, *, reason: str = "cancelled"
    ) -> MachineOperationResult:
        """Cancel an active spawned durable operation."""
        record = self.store.get(operation_id)
        if record is None:
            raise TransportRejectedError(f"job {operation_id!r} not found")
        if record.state not in ("completed", "failed", "cancelled"):
            self.local_job_transport.job_cancel(
                operation_id, self.store._job_dir(operation_id), reason=reason
            )

            def _mark_cancelled(current: JobRecord) -> None:
                if current.state in ("completed", "failed", "cancelled"):
                    return
                current.transition_to(
                    "cancelled",
                    reason=reason,
                    failure_kind="cancelled",
                    timestamp=utc_now_iso(),
                )
                current.terminal["outcome"] = "cancelled"
                current.terminal["error"] = reason

            record = self.store.update(operation_id, _mark_cancelled)
        return MachineOperationResult(
            operation_id=operation_id,
            command_ref=record.command_ref,
            status=record.state,
            host_identity=record.host_identity or socket.gethostname(),
            job_id=operation_id,
            raw_evidence={"job_id": operation_id, "status": record.state, "reason": reason},
        )

    def read_file(self, request: FileReadRequest) -> FileResult:
        """Read a scoped file within configured file roots."""
        proj = self.jobs_cfg.projects.get(request.project_id)
        if proj is None:
            raise TransportRejectedError(f"unknown project_id: {request.project_id!r}")
        target = Path(request.path)
        allowed_roots = [proj.repo_path] + list(proj.file_roots)
        resolved_target: Optional[Path] = None
        if target.is_absolute():
            for root in allowed_roots:
                if is_path_contained(root, target):
                    resolved_target = target
                    break
        else:
            cand = (proj.repo_path / target).resolve()
            for root in allowed_roots:
                if is_path_contained(root, cand):
                    resolved_target = cand
                    break
        if resolved_target is None:
            raise TransportRejectedError(f"path {request.path!r} escapes project containment")
        if not resolved_target.is_file():
            return FileResult(
                status="failed",
                path=request.path,
                error=f"file not found: {request.path}",
                host_identity=socket.gethostname(),
            )

        with open(resolved_target, "rb") as f:
            if request.offset_bytes:
                f.seek(request.offset_bytes)
            data = f.read(request.max_bytes)

        sha = "sha256:" + hashlib.sha256(data).hexdigest()
        return FileResult(
            status="ok",
            path=str(resolved_target),
            content_sha256=sha,
            size_bytes=len(data),
            content_bytes=data,
            host_identity=socket.gethostname(),
        )

    def stage_write_content(self, upload: WriteContentUpload) -> StagedWriteContent:
        """Stage arbitrary binary content and return an opaque digest-verified content ref."""
        proj = self.jobs_cfg.projects.get(upload.project_id)
        max_bytes = proj.max_file_write_bytes if proj else 8 * 1024 * 1024
        return self.store.stage_write_content(upload, max_bytes=max_bytes)

    def write_file(self, request: FileWriteRequest) -> FileResult:
        """Commit a staged binary write under CAS preconditions and canonical path locking."""
        proj = self.jobs_cfg.projects.get(request.project_id)
        if proj is None:
            raise TransportRejectedError(f"unknown project_id: {request.project_id!r}")
        target = Path(request.target_path)
        allowed_roots = [proj.repo_path] + list(proj.file_roots)
        resolved_target: Optional[Path] = None
        target_root: Optional[Path] = None
        if target.is_absolute():
            for root in allowed_roots:
                if is_path_contained(root, target):
                    resolved_target = target
                    target_root = root
                    break
        else:
            cand = (proj.repo_path / target).resolve()
            for root in allowed_roots:
                if is_path_contained(root, cand):
                    resolved_target = cand
                    target_root = root
                    break
        if resolved_target is None or target_root is None:
            raise TransportRejectedError(f"target path {request.target_path!r} escapes project containment")

        req = dataclasses.replace(request, target_path=str(resolved_target))
        return self.store.apply_file_write(
            req,
            [str(r) for r in allowed_roots],
            host_identity=socket.gethostname(),
        )

    def stat(
        self, path: str, host_id: Optional[str] = None, project_id: Optional[str] = None
    ) -> StatResult:
        """Stat a path within configured repository or file roots."""
        allowed_roots: list[Path] = []
        if project_id is not None:
            proj = self.jobs_cfg.projects.get(project_id)
            if proj is None:
                raise TransportRejectedError(f"unknown project_id: {project_id!r}")
            allowed_roots = [proj.repo_path] + list(proj.file_roots)
        else:
            for p in self.jobs_cfg.projects.values():
                allowed_roots.append(p.repo_path)
                allowed_roots.extend(p.file_roots)

        cand = Path(path)
        resolved_cand: Optional[Path] = None
        if cand.is_absolute():
            for root in allowed_roots:
                if is_path_contained(root, cand):
                    resolved_cand = cand
                    break
        else:
            if project_id and project_id in self.jobs_cfg.projects:
                c = (self.jobs_cfg.projects[project_id].repo_path / cand).resolve()
                for root in allowed_roots:
                    if is_path_contained(root, c):
                        resolved_cand = c
                        break
            else:
                for root in allowed_roots:
                    c = (root / cand).resolve()
                    if is_path_contained(root, c):
                        resolved_cand = c
                        break

        if resolved_cand is None:
            raise TransportRejectedError(f"path {path!r} escapes allowed roots containment")

        if not resolved_cand.exists():
            return StatResult(
                status="ok",
                path=path,
                exists=False,
                host_identity=socket.gethostname(),
            )

        is_file = resolved_cand.is_file()
        is_dir = resolved_cand.is_dir()
        st = resolved_cand.stat()
        mtime_iso = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        sha: Optional[str] = None
        if is_file and st.st_size <= 10 * 1024 * 1024:
            sha = "sha256:" + hashlib.sha256(resolved_cand.read_bytes()).hexdigest()

        return StatResult(
            status="ok",
            path=str(resolved_cand),
            exists=True,
            is_file=is_file,
            is_dir=is_dir,
            size_bytes=st.st_size if is_file else None,
            modified_at=mtime_iso,
            sha256=sha,
            host_identity=socket.gethostname(),
        )

    def capabilities(self, host_id: Optional[str] = None) -> HostCapabilities:
        """Discover host capability and configuration status."""
        return discover_local_capabilities(self.jobs_cfg, host_id=host_id or self.host_id)
