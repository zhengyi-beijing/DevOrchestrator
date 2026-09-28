"""SSH-backed machine execution transport communicating with remote_helper."""

from __future__ import annotations

import base64
from pathlib import Path
import subprocess
from typing import Any, Mapping, Optional
import uuid

from dev_orchestrator.ai.execution_transport import SSHTransportConfig
from dev_orchestrator.transport.contracts import (
    FileReadRequest,
    FileResult,
    FileWriteRequest,
    HostCapabilities,
    MachineOperation,
    MachineOperationResult,
    StagedWriteContent,
    StatResult,
    TransportAmbiguousError,
    TransportError,
    TransportRejectedError,
    TransportUnavailableError,
    WriteContentUpload,
)
from dev_orchestrator.transport.hosts import TransportHostProfile
from dev_orchestrator.transport.ssh_channel import (
    DEFAULT_MAX_SSH_RESPONSE_BYTES,
    run_remote_helper_envelope,
)


class SSHMachineTransport:
    """SSH-backed machine execution transport communicating with remote_helper."""

    def __init__(
        self,
        config: SSHTransportConfig | TransportHostProfile,
        *,
        subprocess_module: Any = None,
    ) -> None:
        if isinstance(config, TransportHostProfile):
            ssh_dict = config.ssh or {}
            self.host_id = config.host_id
            self.ssh_cfg = SSHTransportConfig(
                peer=ssh_dict.get("peer") or config.host_id,
                user=ssh_dict.get("user"),
                port=int(ssh_dict.get("port") or 22),
                identity_file=Path(ssh_dict["identity_file"]) if ssh_dict.get("identity_file") else None,
                known_hosts_file=Path(ssh_dict["known_hosts_file"]) if ssh_dict.get("known_hosts_file") else None,
                strict_host_key_checking=ssh_dict.get("strict_host_key_checking", "yes"),
                remote_python=ssh_dict.get("remote_python", "python3"),
                remote_helper_module=ssh_dict.get("remote_helper_module", "dev_orchestrator.ai.remote_helper"),
                ssh_executable=ssh_dict.get("ssh_executable", "ssh"),
                connect_timeout_seconds=float(ssh_dict.get("connect_timeout_seconds", 30.0)),
                expected_host_identity=config.expected_host_identity or ssh_dict.get("expected_host_identity"),
            )
            self.max_response_bytes = config.response_limits.get("max_response_bytes", DEFAULT_MAX_SSH_RESPONSE_BYTES)
        else:
            self.host_id = config.peer
            self.ssh_cfg = config
            self.max_response_bytes = DEFAULT_MAX_SSH_RESPONSE_BYTES
        self._subprocess = subprocess_module or subprocess

    def _call_helper(self, request_envelope: dict[str, Any], timeout_seconds: float = 60.0) -> dict[str, Any]:
        resp = run_remote_helper_envelope(
            self.ssh_cfg,
            request_envelope,
            subprocess_module=self._subprocess,
            timeout_seconds=timeout_seconds,
            max_response_bytes=self.max_response_bytes,
        )
        if resp.get("status") == "error":
            err_msg = str(resp.get("error") or "remote helper error")
            if (
                "hardware" in err_msg
                or "reject" in err_msg
                or "forbidden" in err_msg
                or "escapes" in err_msg
                or "validation failed" in err_msg
                or "only supports read_only" in err_msg
            ):
                raise TransportRejectedError(err_msg)
            raise TransportError(err_msg)
        payload = resp.get("payload")
        if not isinstance(payload, dict):
            raise TransportError("remote helper returned non-dict payload")
        return payload

    def exec(self, request: MachineOperation) -> MachineOperationResult:
        req_id = request.idempotency_key or str(uuid.uuid4())
        req_env = {
            "operation": "op_exec",
            "request_id": req_id,
            "project_id": request.project_id,
            "command_ref": request.command_ref,
            "parameters": request.parameters,
            "expected_working_directory": request.expected_working_directory,
            "timeout_seconds": request.timeout_seconds,
        }
        timeout = (request.timeout_seconds or 300.0) + self.ssh_cfg.connect_timeout_seconds + 10.0
        payload = self._call_helper(req_env, timeout_seconds=timeout)
        return MachineOperationResult(
            operation_id=req_id,
            command_ref=request.command_ref,
            status=payload.get("status", "ok"),
            exit_code=payload.get("exit_code"),
            stdout=payload.get("stdout"),
            stderr=payload.get("stderr"),
            host_identity=self.ssh_cfg.expected_host_identity or self.ssh_cfg.peer,
            parameters_digest=payload.get("parameters_digest"),
            execution_policy_digest=payload.get("execution_policy_digest"),
            resolution_digest=payload.get("resolution_digest"),
            raw_evidence=payload,
        )

    def spawn(self, request: MachineOperation) -> MachineOperationResult:
        req_id = request.idempotency_key or str(uuid.uuid4())
        req_env = {
            "operation": "op_spawn",
            "request_id": req_id,
            "project_id": request.project_id,
            "command_ref": request.command_ref,
            "idempotency_key": request.idempotency_key,
            "parameters": request.parameters,
            "expected_working_directory": request.expected_working_directory,
            "input_digest": request.input_digest,
        }
        payload = self._call_helper(req_env, timeout_seconds=self.ssh_cfg.connect_timeout_seconds + 30.0)
        return MachineOperationResult(
            operation_id=payload.get("job_id", req_id),
            command_ref=request.command_ref,
            status=payload.get("status", "ok"),
            job_id=payload.get("job_id"),
            host_identity=self.ssh_cfg.expected_host_identity or self.ssh_cfg.peer,
            parameters_digest=payload.get("parameters_digest"),
            execution_policy_digest=payload.get("execution_policy_digest"),
            resolution_digest=payload.get("resolution_digest"),
            raw_evidence=payload,
        )

    def poll(self, operation_id: str, host_id: Optional[str] = None) -> MachineOperationResult:
        req_id = str(uuid.uuid4())
        req_env = {
            "operation": "op_poll",
            "request_id": req_id,
            "job_id": operation_id,
        }
        payload = self._call_helper(req_env, timeout_seconds=self.ssh_cfg.connect_timeout_seconds + 15.0)
        return MachineOperationResult(
            operation_id=operation_id,
            command_ref=payload.get("details", {}).get("command_ref", ""),
            status=payload.get("state") or payload.get("status", "unknown"),
            exit_code=payload.get("exit_code"),
            job_id=operation_id,
            host_identity=self.ssh_cfg.expected_host_identity or self.ssh_cfg.peer,
            parameters_digest=payload.get("parameters_digest"),
            execution_policy_digest=payload.get("execution_policy_digest"),
            resolution_digest=payload.get("resolution_digest"),
            raw_evidence=payload,
        )

    def cancel(
        self, operation_id: str, host_id: Optional[str] = None, *, reason: str = "cancelled"
    ) -> MachineOperationResult:
        req_id = str(uuid.uuid4())
        req_env = {
            "operation": "op_cancel",
            "request_id": req_id,
            "job_id": operation_id,
            "reason": reason,
        }
        payload = self._call_helper(req_env, timeout_seconds=self.ssh_cfg.connect_timeout_seconds + 15.0)
        return MachineOperationResult(
            operation_id=operation_id,
            command_ref="",
            status=payload.get("details", {}).get("status", "cancelled"),
            job_id=operation_id,
            host_identity=self.ssh_cfg.expected_host_identity or self.ssh_cfg.peer,
            raw_evidence=payload,
        )

    def read_file(self, request: FileReadRequest) -> FileResult:
        req_id = str(uuid.uuid4())
        req_env = {
            "operation": "op_read_file",
            "request_id": req_id,
            "project_id": request.project_id,
            "path": request.path,
            "max_bytes": request.max_bytes,
            "offset_bytes": request.offset_bytes,
        }
        payload = self._call_helper(req_env, timeout_seconds=self.ssh_cfg.connect_timeout_seconds + 30.0)
        raw_b64 = payload.get("content_base64")
        content_bytes = base64.b64decode(raw_b64) if raw_b64 is not None else None
        return FileResult(
            status=payload.get("status", "ok"),
            path=payload.get("path", request.path),
            content_sha256=payload.get("sha256"),
            size_bytes=payload.get("size_bytes"),
            content_bytes=content_bytes,
            host_identity=self.ssh_cfg.expected_host_identity or self.ssh_cfg.peer,
            error=payload.get("error"),
            raw_evidence=payload,
        )

    def stage_write_content(self, upload: WriteContentUpload) -> StagedWriteContent:
        req_id = str(uuid.uuid4())
        req_env = {
            "operation": "op_stage_write",
            "request_id": req_id,
            "project_id": upload.project_id,
            "content_base64": upload.content_base64,
            "decoded_size_bytes": upload.decoded_size_bytes,
            "content_sha256": upload.content_sha256,
        }
        payload = self._call_helper(req_env, timeout_seconds=self.ssh_cfg.connect_timeout_seconds + 60.0)
        return StagedWriteContent(
            content_ref=payload["content_ref"],
            content_sha256=payload["content_sha256"],
            decoded_size_bytes=payload["decoded_size_bytes"],
            project_id=payload["project_id"],
            host_id=payload["host_id"],
            staged_at=payload["staged_at"],
            expires_at=payload.get("expires_at"),
        )

    def write_file(self, request: FileWriteRequest) -> FileResult:
        req_id = str(uuid.uuid4())
        req_env = {
            "operation": "op_write_file",
            "request_id": req_id,
            "project_id": request.project_id,
            "target_path": request.target_path,
            "idempotency_key": request.idempotency_key,
            "content_ref": request.content_ref,
            "content_sha256": request.content_sha256,
            "decoded_size_bytes": request.decoded_size_bytes,
            "if_absent": request.if_absent,
            "expected_sha256": request.expected_sha256,
            "expected_file_policy_digest": request.expected_file_policy_digest,
        }
        payload = self._call_helper(req_env, timeout_seconds=self.ssh_cfg.connect_timeout_seconds + 30.0)
        return FileResult(
            status=payload.get("status", "ok"),
            path=payload.get("target_path", request.target_path),
            content_sha256=payload.get("content_sha256"),
            size_bytes=payload.get("size_bytes"),
            pre_digest=payload.get("pre_digest"),
            post_digest=payload.get("post_digest"),
            applied_at=payload.get("applied_at"),
            host_identity=self.ssh_cfg.expected_host_identity or self.ssh_cfg.peer,
            error=payload.get("error"),
            raw_evidence=payload,
        )

    def stat(
        self, path: str, host_id: Optional[str] = None, project_id: Optional[str] = None
    ) -> StatResult:
        req_id = str(uuid.uuid4())
        req_env = {
            "operation": "op_stat",
            "request_id": req_id,
            "path": path,
            "project_id": project_id,
        }
        payload = self._call_helper(req_env, timeout_seconds=self.ssh_cfg.connect_timeout_seconds + 15.0)
        return StatResult(
            status=payload.get("status", "ok"),
            path=payload.get("path", path),
            exists=bool(payload.get("exists", False)),
            is_file=bool(payload.get("is_file", False)),
            is_dir=bool(payload.get("is_dir", False)),
            size_bytes=payload.get("size_bytes"),
            modified_at=payload.get("modified_at"),
            sha256=payload.get("sha256"),
            host_identity=self.ssh_cfg.expected_host_identity or self.ssh_cfg.peer,
            error=payload.get("error"),
        )

    def capabilities(self, host_id: Optional[str] = None) -> HostCapabilities:
        req_id = str(uuid.uuid4())
        req_env = {
            "operation": "op_capabilities",
            "request_id": req_id,
        }
        payload = self._call_helper(req_env, timeout_seconds=self.ssh_cfg.connect_timeout_seconds + 15.0)
        caps = payload.get("capabilities", {})
        return HostCapabilities(
            host_id=caps.get("host_id", self.host_id),
            os_family=caps.get("os_family", "linux"),
            path_style=caps.get("path_style", "posix"),
            helper_version=caps.get("helper_version", "unknown"),
            jobs_config_valid=bool(caps.get("jobs_config_valid", False)),
            approved_policy_pins=caps.get("approved_policy_pins", {}),
            response_limits=caps.get("response_limits", {}),
            supported_operations=caps.get("supported_operations", []),
            probed_at=caps.get("probed_at"),
        )
