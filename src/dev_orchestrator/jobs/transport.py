"""JobTransport protocol and implementations (LocalJobTransport, SSHJobTransport)."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable
from uuid import uuid4

from dev_orchestrator.ai.execution_transport import (
    ExecutionTransportError,
    SSHTransport,
    SSHTransportConfig,
)
from dev_orchestrator.platform.process import (
    is_pid_alive,
    spawn_detached,
    terminate_process_tree,
)
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

from .logs import BoundedNDJSONLog
from .models import JobSpec, job_id_for


@runtime_checkable
class JobTransport(Protocol):
    """Protocol for job execution transports (Local and SSH)."""

    def job_start(self, spec: JobSpec, job_dir: Path) -> dict[str, Any]:
        """Start detached job execution on target host."""
        ...

    def job_status(self, job_id: str, job_dir: Optional[Path] = None) -> dict[str, Any]:
        """Poll job status on target host."""
        ...

    def job_logs(
        self, job_id: str, job_dir: Optional[Path] = None, *, cursor: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """Read paginated bounded logs on target host."""
        ...

    def job_cancel(
        self, job_id: str, job_dir: Optional[Path] = None, *, reason: str = "cancelled"
    ) -> dict[str, Any]:
        """Cancel active job on target host."""
        ...

    def job_artifact(
        self, job_id: str, job_dir: Optional[Path] = None, *, name: str
    ) -> dict[str, Any]:
        """Retrieve output artifact descriptor and content."""
        ...


class LocalJobTransport:
    """Default local detached supervisor transport."""

    def __init__(self, *, subprocess_module: Any = None) -> None:
        self._subprocess = subprocess_module or subprocess

    def job_start(self, spec: JobSpec, job_dir: Path) -> dict[str, Any]:
        target_dir = Path(job_dir).resolve()
        argv = [
            sys.executable,
            "-m",
            "dev_orchestrator.jobs.supervisor",
            "--job-dir",
            str(target_dir),
        ]
        proc = spawn_detached(argv)
        return {
            "job_id": job_id_for(spec),
            "status": "started",
            "supervisor_pid": proc.pid,
            "host_identity": socket.gethostname(),
        }

    def job_status(self, job_id: str, job_dir: Optional[Path] = None) -> dict[str, Any]:
        if job_dir is None:
            raise ValueError("job_dir is required for LocalJobTransport.job_status")
        target_dir = Path(job_dir)
        job_data = read_json(target_dir / "job.json", None)
        heartbeat = read_json(target_dir / "heartbeat.json", None)
        result = read_json(target_dir / "result.json", None)
        sup_alive = None
        if isinstance(job_data, dict):
            sup = job_data.get("supervisor", {})
            if isinstance(sup, dict):
                pid = sup.get("pid")
                if isinstance(pid, int) and pid > 0:
                    sup_alive = is_pid_alive(pid)
        return {
            "job_id": job_id,
            "host_identity": socket.gethostname(),
            "job": job_data,
            "heartbeat": heartbeat,
            "result": result,
            "supervisor_alive": sup_alive,
        }

    def job_logs(
        self, job_id: str, job_dir: Optional[Path] = None, *, cursor: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        if job_dir is None:
            raise ValueError("job_dir is required for LocalJobTransport.job_logs")
        log_path = Path(job_dir) / "log.ndjson"
        reader = BoundedNDJSONLog(log_path)
        return reader.read_paginated(cursor, limit)

    def job_cancel(
        self, job_id: str, job_dir: Optional[Path] = None, *, reason: str = "cancelled"
    ) -> dict[str, Any]:
        if job_dir is None:
            raise ValueError("job_dir is required for LocalJobTransport.job_cancel")
        target_dir = Path(job_dir)
        write_json(target_dir / "cancel.json", {
            "cancelled_at": utc_now_iso(),
            "reason": reason,
        }, indent=2)

        job_data = read_json(target_dir / "job.json", {})
        if isinstance(job_data, dict):
            sup = job_data.get("supervisor", {})
            if isinstance(sup, dict):
                pid = sup.get("pid")
                if pid and is_pid_alive(pid):
                    terminate_process_tree(pid)

        return {
            "job_id": job_id,
            "status": "cancelled",
            "host_identity": socket.gethostname(),
        }

    def job_artifact(
        self, job_id: str, job_dir: Optional[Path] = None, *, name: str
    ) -> dict[str, Any]:
        if job_dir is None:
            raise ValueError("job_dir is required for LocalJobTransport.job_artifact")
        from .store import ExecutionJobStore
        store = ExecutionJobStore(job_dir.parent.parent, read_only=True)
        art = store.get_output_artifact(job_id, name)
        if art is None:
            raise ValueError(f"artifact {name!r} not found for job {job_id}")
        return {
            "job_id": job_id,
            "host_identity": socket.gethostname(),
            "artifact": {
                "name": art["name"],
                "sha256": art["sha256"],
                "size_bytes": art["size_bytes"],
            },
            "content": art["content"],
        }


class SSHJobTransport:
    """Remote job transport reusing SSH helper execution and correlation checks."""

    def __init__(self, ssh_config: SSHTransportConfig, *, subprocess_module: Any = None) -> None:
        self.ssh_config = ssh_config
        self._subprocess = subprocess_module or subprocess
        self._delegate = SSHTransport(ssh_config, subprocess_module=self._subprocess)

    def _execute_op(self, envelope: dict[str, Any], timeout_seconds: float = 30.0) -> dict[str, Any]:
        resp = self._delegate._run_remote_helper(envelope, timeout_seconds)
        status = resp.get("status")
        if status != "success":
            err_msg = resp.get("error") or "remote execution helper failed"
            raise ExecutionTransportError(f"remote helper error for {envelope.get('operation')}: {err_msg}")
        payload = resp.get("payload")
        if not isinstance(payload, dict):
            raise ExecutionTransportError("remote helper payload must be a JSON object")
        return payload

    def job_start(
        self, spec: JobSpec, job_dir: Optional[Path] = None, *, job_id: Optional[str] = None
    ) -> dict[str, Any]:
        req_id = f"job-start-{uuid4().hex[:12]}"
        actual_job_id = job_id or (job_dir.name if job_dir is not None else job_id_for(spec))
        envelope = {
            "operation": "job_start",
            "request_id": req_id,
            "job_id": actual_job_id,
            "project_id": spec.project_id,
            "command_ref": spec.command_ref,
            "idempotency_key": spec.idempotency_key,
        }
        if spec.expected_working_directory:
            envelope["expected_working_directory"] = spec.expected_working_directory
        if spec.input_digest:
            envelope["input_digest"] = spec.input_digest
            if job_dir is not None:
                from .store import ExecutionJobStore
                store = ExecutionJobStore(job_dir.parent.parent, read_only=True)
                inp = store.get_input_artifact(actual_job_id)
                if inp is not None:
                    envelope["input_payload"] = inp
        return self._execute_op(envelope)

    def job_status(self, job_id: str, job_dir: Optional[Path] = None) -> dict[str, Any]:
        req_id = f"job-status-{uuid4().hex[:12]}"
        envelope = {
            "operation": "job_status",
            "request_id": req_id,
            "job_id": job_id,
        }
        return self._execute_op(envelope)

    def job_logs(
        self, job_id: str, job_dir: Optional[Path] = None, *, cursor: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        req_id = f"job-logs-{uuid4().hex[:12]}"
        envelope = {
            "operation": "job_logs",
            "request_id": req_id,
            "job_id": job_id,
            "cursor": cursor,
            "limit": limit,
        }
        return self._execute_op(envelope)

    def job_cancel(
        self, job_id: str, job_dir: Optional[Path] = None, *, reason: str = "cancelled"
    ) -> dict[str, Any]:
        req_id = f"job-cancel-{uuid4().hex[:12]}"
        envelope = {
            "operation": "job_cancel",
            "request_id": req_id,
            "job_id": job_id,
            "reason": reason,
        }
        return self._execute_op(envelope)

    def job_artifact(
        self, job_id: str, job_dir: Optional[Path] = None, *, name: str
    ) -> dict[str, Any]:
        req_id = f"job-art-{uuid4().hex[:12]}"
        envelope = {
            "operation": "job_artifact",
            "request_id": req_id,
            "job_id": job_id,
            "artifact_name": name,
        }
        res = self._execute_op(envelope)
        if res.get("status") == "not_found":
            raise ValueError(f"artifact {name!r} not found for job {job_id}")
        return res
