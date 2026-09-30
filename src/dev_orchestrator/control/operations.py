"""Shared control service operations for REST and MCP interfaces.

Extracts the core transport operations (spawn, poll, cancel, read_file) into
a single authoritative service layer. REST routes and MCP endpoints delegate
here rather than duplicating host resolution, policy evaluation, transport
selection, and observability logging.
"""

from __future__ import annotations

import base64
import dataclasses
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from dev_orchestrator.transport.contracts import (
    FileReadRequest,
    MachineOperation,
    TransportRejectedError,
)
from dev_orchestrator.transport.hosts import (
    get_transport_for_host,
    load_transport_hosts_config,
)
from dev_orchestrator.transport.observability import log_transport_operation


class ControlOperationError(Exception):
    """Raised when a control operation cannot be performed."""

    def __init__(self, status_code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.message = message


def local_job_belongs_to_project(runtime_root: Path | str, operation_id: Any, project_id: Any) -> tuple[bool, str]:
    """Verify that a local job exists and belongs to the requested project."""
    if not isinstance(operation_id, str) or not operation_id.strip():
        return False, "operation_id is required"
    if not isinstance(project_id, str) or not project_id.strip():
        return False, "project_id is required"
    from dev_orchestrator.jobs.store import ExecutionJobStore
    runtime = Path(runtime_root)
    record = ExecutionJobStore(runtime, read_only=True).get(operation_id.strip())
    if record is None:
        return False, "job not found"
    if record.project_id != project_id.strip():
        return False, "job does not belong to requested project"
    return True, "ok"


def _verify_selected_transport(transport: Any) -> str:
    """Ensure transport selection is strictly local or ssh; fail closed otherwise."""
    sel = getattr(getattr(transport, "last_selection", None), "selected_transport", "local")
    if sel not in ("local", "ssh"):
        raise ControlOperationError(
            400,
            "Transport Rejected",
            f"forbidden transport selection: {sel} (only local or ssh permitted; RDC fallback forbidden)",
        )
    return str(sel)


def spawn_job(
    runtime_root: Path | str,
    *,
    project_id: str,
    command_ref: str,
    idempotency_key: str,
    host_id: str = "local",
    parameters: Optional[dict[str, Any]] = None,
    expected_working_directory: Optional[str] = None,
    input_digest: Optional[str] = None,
    request_id: Optional[str] = None,
    capability_id: Optional[str] = None,
) -> dict[str, Any]:
    """Start one durable host-allowlisted background job."""
    runtime = Path(runtime_root)
    if not project_id or not str(project_id).strip():
        raise ControlOperationError(400, "Bad Request", "project_id is required")
    if not command_ref or not str(command_ref).strip():
        raise ControlOperationError(400, "Bad Request", "command_ref is required")
    if not idempotency_key or not str(idempotency_key).strip():
        raise ControlOperationError(400, "Bad Request", "idempotency_key is required")

    h_cfg = load_transport_hosts_config(runtime)
    is_local = (host_id == "local")
    p_digest = None
    ep_digest = None
    res_digest = None
    effect_cls = "effectful"

    if not is_local:
        profile = h_cfg.hosts.get(host_id)
        if profile is None:
            raise ControlOperationError(400, "Bad Request", f"unknown host_id: {host_id}")
        from dev_orchestrator.transport.ssh import SSHMachineTransport
        ssh_t = SSHMachineTransport(profile)
        op_probe = MachineOperation(
            project_id=project_id,
            command_ref=command_ref,
            idempotency_key=f"resolve-{idempotency_key}",
            parameters=parameters,
            expected_working_directory=expected_working_directory,
        )
        try:
            resolved_info = ssh_t.resolve(op_probe)
        except Exception as exc:
            raise ControlOperationError(400, "Bad Request", f"remote resolution failed: {exc}") from exc
        ep_digest = resolved_info.get("execution_policy_digest")
        res_digest = resolved_info.get("resolution_digest")
        p_digest = resolved_info.get("parameters_digest")
        effect_cls = resolved_info.get("effect_class", "effectful")
    else:
        from dev_orchestrator.jobs.config import load_jobs_config, resolve_execution_policy
        jobs_cfg = load_jobs_config(runtime / "execution-jobs.json")
        if jobs_cfg and project_id and command_ref:
            ok_pol, _, resolved = resolve_execution_policy(
                jobs_cfg,
                project_id,
                command_ref,
                parameters=parameters,
                expected_working_directory=expected_working_directory,
            )
            if ok_pol and resolved:
                p_digest = resolved.parameters_digest
                ep_digest = resolved.execution_policy_digest
                res_digest = resolved.resolution_digest
                effect_cls = resolved.effect_class

    try:
        transport = get_transport_for_host(
            runtime,
            host_id,
            operation="spawn",
            command_ref=command_ref,
            effect_class=effect_cls,
            policy_digest=ep_digest,
        )
        sel_transport = _verify_selected_transport(transport)
        op = MachineOperation(
            project_id=project_id,
            command_ref=command_ref,
            idempotency_key=idempotency_key,
            parameters=parameters,
            expected_working_directory=expected_working_directory,
            input_digest=input_digest,
        )
        res = transport.spawn(op)
    except TransportRejectedError as exc:
        raise ControlOperationError(400, "Bad Request", str(exc)) from exc
    except ControlOperationError:
        raise
    except Exception as exc:
        raise ControlOperationError(400, "Bad Request", str(exc)) from exc

    try:
        log_transport_operation(
            runtime,
            operation_id=getattr(res, "operation_id", idempotency_key),
            request_id=request_id,
            operation="spawn",
            host_id=host_id,
            selected_transport=sel_transport,
            status=res.status,
            project_id=project_id,
            command_ref=command_ref,
            parameters_names=sorted((parameters or {}).keys()),
            parameters_digest=getattr(res, "parameters_digest", p_digest),
            execution_policy_digest=getattr(res, "execution_policy_digest", ep_digest),
            resolution_digest=getattr(res, "resolution_digest", res_digest),
            candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
            capability_id=capability_id,
            duration_seconds=getattr(res, "duration_seconds", None),
            exit_code=getattr(res, "exit_code", None),
            error=res.error,
        )
    except Exception:
        pass

    res_data = dataclasses.asdict(res)
    res_data["selected_transport"] = sel_transport
    return res_data


def poll_job(
    runtime_root: Path | str,
    *,
    project_id: str,
    operation_id: Optional[str] = None,
    job_id: Optional[str] = None,
    host_id: str = "local",
    request_id: Optional[str] = None,
    capability_id: Optional[str] = None,
) -> dict[str, Any]:
    """Poll durable state and result for one background job."""
    runtime = Path(runtime_root)
    op_id = (operation_id or job_id or "").strip()
    if not op_id:
        raise ControlOperationError(400, "Bad Request", "operation_id or job_id is required")
    if not project_id or not str(project_id).strip():
        raise ControlOperationError(400, "Bad Request", "project_id is required")

    if host_id == "local":
        belongs, reason = local_job_belongs_to_project(runtime, op_id, project_id)
        if not belongs:
            raise ControlOperationError(
                404 if reason == "job not found" else 403,
                "Not Found" if reason == "job not found" else "Forbidden",
                reason,
            )

    try:
        transport = get_transport_for_host(runtime, host_id, operation="poll", effect_class="read_only")
        sel_transport = _verify_selected_transport(transport)
        res = transport.poll(op_id, host_id=host_id)
    except TransportRejectedError as exc:
        raise ControlOperationError(400, "Bad Request", str(exc)) from exc
    except ControlOperationError:
        raise
    except Exception as exc:
        raise ControlOperationError(400, "Bad Request", str(exc)) from exc

    try:
        log_transport_operation(
            runtime,
            operation_id=str(op_id),
            request_id=request_id,
            operation="poll",
            host_id=host_id,
            selected_transport=sel_transport,
            status=res.status,
            project_id=project_id,
            candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
            capability_id=capability_id,
            exit_code=res.exit_code,
            error=res.error,
        )
    except Exception:
        pass

    res_data = dataclasses.asdict(res)
    res_data["selected_transport"] = sel_transport
    return res_data


def cancel_job(
    runtime_root: Path | str,
    *,
    project_id: str,
    operation_id: Optional[str] = None,
    job_id: Optional[str] = None,
    host_id: str = "local",
    reason: str = "cancelled",
    request_id: Optional[str] = None,
    capability_id: Optional[str] = None,
) -> dict[str, Any]:
    """Idempotently cancel one exact durable job without cross-job cancellation."""
    runtime = Path(runtime_root)
    op_id = (operation_id or job_id or "").strip()
    if not op_id:
        raise ControlOperationError(400, "Bad Request", "operation_id or job_id is required")
    if not project_id or not str(project_id).strip():
        raise ControlOperationError(400, "Bad Request", "project_id is required")

    if host_id == "local":
        belongs, owner_reason = local_job_belongs_to_project(runtime, op_id, project_id)
        if not belongs:
            raise ControlOperationError(
                404 if owner_reason == "job not found" else 403,
                "Not Found" if owner_reason == "job not found" else "Forbidden",
                owner_reason,
            )

    try:
        transport = get_transport_for_host(runtime, host_id, operation="cancel", effect_class="effectful")
        sel_transport = _verify_selected_transport(transport)
        res = transport.cancel(op_id, host_id=host_id, reason=reason)
    except TransportRejectedError as exc:
        raise ControlOperationError(400, "Bad Request", str(exc)) from exc
    except ControlOperationError:
        raise
    except Exception as exc:
        raise ControlOperationError(400, "Bad Request", str(exc)) from exc

    try:
        log_transport_operation(
            runtime,
            operation_id=str(op_id),
            request_id=request_id,
            operation="cancel",
            host_id=host_id,
            selected_transport=sel_transport,
            status=res.status,
            project_id=project_id,
            candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
            capability_id=capability_id,
            error=res.error,
        )
    except Exception:
        pass

    res_data = dataclasses.asdict(res)
    res_data["selected_transport"] = sel_transport
    return res_data


def read_file(
    runtime_root: Path | str,
    *,
    project_id: str,
    path: str,
    host_id: str = "local",
    max_bytes: int = 10 * 1024 * 1024,
    offset_bytes: int = 0,
    request_id: Optional[str] = None,
    capability_id: Optional[str] = None,
) -> dict[str, Any]:
    """Read bounded content from a configured project file root."""
    runtime = Path(runtime_root)
    if not project_id or not str(project_id).strip():
        raise ControlOperationError(400, "Bad Request", "project_id is required")
    if not path or not str(path).strip():
        raise ControlOperationError(400, "Bad Request", "path is required")

    clamped_max = max(1, min(10 * 1024 * 1024, int(max_bytes)))
    clamped_offset = max(0, int(offset_bytes))

    try:
        transport = get_transport_for_host(runtime, host_id, operation="read_file", effect_class="read_only")
        sel_transport = _verify_selected_transport(transport)
        file_res = transport.read_file(
            FileReadRequest(
                project_id=project_id,
                path=path,
                host_id=host_id,
                max_bytes=clamped_max,
                offset_bytes=clamped_offset,
            )
        )
    except TransportRejectedError as exc:
        raise ControlOperationError(400, "Bad Request", str(exc)) from exc
    except ControlOperationError:
        raise
    except Exception as exc:
        raise ControlOperationError(400, "Bad Request", str(exc)) from exc

    try:
        log_transport_operation(
            runtime,
            operation_id=f"read-{uuid4().hex[:12]}",
            request_id=request_id,
            operation="read_file",
            host_id=host_id,
            selected_transport=sel_transport,
            status=file_res.status,
            project_id=project_id,
            candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
            capability_id=capability_id,
            error=file_res.error,
        )
    except Exception:
        pass

    b64_str = base64.b64encode(file_res.content_bytes).decode("ascii") if file_res.content_bytes else None
    content_text = None
    if file_res.content_bytes is not None and b"\x00" not in file_res.content_bytes:
        try:
            content_text = file_res.content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content_text = None

    return {
        "status": file_res.status,
        "path": file_res.path,
        "content_sha256": file_res.content_sha256,
        "size_bytes": file_res.size_bytes,
        "content_base64": b64_str,
        "content_text": content_text,
        "host_identity": file_res.host_identity,
        "error": file_res.error,
        "selected_transport": sel_transport,
    }
