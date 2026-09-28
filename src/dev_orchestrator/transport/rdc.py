"""RDC escalation fallback descriptor transport."""

from __future__ import annotations

from typing import Optional

from dev_orchestrator.transport.contracts import (
    FileReadRequest,
    FileResult,
    FileWriteRequest,
    HostCapabilities,
    MachineOperation,
    MachineOperationResult,
    StagedWriteContent,
    StatResult,
    TransportRejectedError,
    WriteContentUpload,
)


class RDCTransport:
    """Escalation fallback descriptor transport indicating RDC intervention required."""

    def __init__(self, host_id: str = "rdc") -> None:
        self.host_id = host_id

    def exec(self, request: MachineOperation) -> MachineOperationResult:
        return MachineOperationResult(
            operation_id=request.idempotency_key,
            command_ref=request.command_ref,
            status="rdc_fallback_required",
            error="RDC transport requires interactive escalation; unattended execution not supported",
        )

    def spawn(self, request: MachineOperation) -> MachineOperationResult:
        return MachineOperationResult(
            operation_id=request.idempotency_key,
            command_ref=request.command_ref,
            status="rdc_fallback_required",
            error="RDC transport requires interactive escalation; unattended execution not supported",
        )

    def poll(self, operation_id: str, host_id: Optional[str] = None) -> MachineOperationResult:
        return MachineOperationResult(
            operation_id=operation_id,
            command_ref="",
            status="rdc_fallback_required",
            error="RDC transport requires interactive escalation; unattended execution not supported",
        )

    def cancel(
        self, operation_id: str, host_id: Optional[str] = None, *, reason: str = "cancelled"
    ) -> MachineOperationResult:
        return MachineOperationResult(
            operation_id=operation_id,
            command_ref="",
            status="rdc_fallback_required",
            error="RDC transport requires interactive escalation; unattended execution not supported",
        )

    def read_file(self, request: FileReadRequest) -> FileResult:
        return FileResult(
            status="rdc_fallback_required",
            path=request.path,
            error="RDC transport requires interactive escalation; unattended execution not supported",
        )

    def stage_write_content(self, upload: WriteContentUpload) -> StagedWriteContent:
        raise TransportRejectedError(
            "RDC transport cannot stage write content directly; interactive intervention required"
        )

    def write_file(self, request: FileWriteRequest) -> FileResult:
        return FileResult(
            status="rdc_fallback_required",
            path=request.target_path,
            error="RDC transport requires interactive escalation; unattended execution not supported",
        )

    def stat(
        self, path: str, host_id: Optional[str] = None, project_id: Optional[str] = None
    ) -> StatResult:
        return StatResult(
            status="rdc_fallback_required",
            path=path,
            exists=False,
            error="RDC transport requires interactive escalation; unattended execution not supported",
        )

    def capabilities(self, host_id: Optional[str] = None) -> HostCapabilities:
        return HostCapabilities(
            host_id=host_id or self.host_id,
            os_family="unknown",
            path_style="posix",
            helper_version="none",
            jobs_config_valid=False,
            supported_operations=[],
        )
