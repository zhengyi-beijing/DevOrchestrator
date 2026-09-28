"""Contracts, data models, and typed errors for MachineTransport."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from dev_orchestrator.ai.execution_transport import ExecutionTransportError


def canonical_sha256(digest_or_bytes: str | bytes) -> str:
    """Return a canonical 'sha256:' prefixed lowercase hex digest."""
    if isinstance(digest_or_bytes, bytes):
        return "sha256:" + hashlib.sha256(digest_or_bytes).hexdigest().lower()
    if not isinstance(digest_or_bytes, str):
        raise TypeError(f"expected str or bytes, got {type(digest_or_bytes).__name__}")
    val = digest_or_bytes.strip()
    if val.startswith("sha256:"):
        return "sha256:" + val[7:].lower()
    return "sha256:" + val.lower()


class TransportError(ExecutionTransportError):
    """Base error for execution transport failures."""


class TransportUnavailableError(TransportError):
    """Target transport or host is unavailable before dispatch (e.g. connection refused, network down)."""


class TransportRejectedError(TransportError):
    """Operation rejected by host policy, capability fence, parameter validation, or hardware block."""


class TransportAmbiguousError(TransportError):
    """Operation failed during or after dispatch; actual completion status is ambiguous and must not be retried blindly."""


@dataclass(frozen=True)
class MachineOperation:
    """Specification for executing or spawning a machine operation."""

    project_id: str
    command_ref: str
    idempotency_key: str
    parameters: Optional[dict[str, Any]] = None
    host_id: Optional[str] = None
    expected_working_directory: Optional[str] = None
    input_digest: Optional[str] = None
    timeout_seconds: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MachineOperationResult:
    """Outcome of a machine operation execution or status poll."""

    operation_id: str
    command_ref: str
    status: str  # "ok", "failed", "timeout", "cancelled", "ambiguous", "unavailable", "rejected"
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    host_identity: str = ""
    duration_seconds: Optional[float] = None
    error: Optional[str] = None
    parameters_digest: Optional[str] = None
    execution_policy_digest: Optional[str] = None
    resolution_digest: Optional[str] = None
    job_id: Optional[str] = None
    raw_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FileReadRequest:
    """Request to read a scoped file from a target host."""

    project_id: str
    path: str
    host_id: Optional[str] = None
    max_bytes: int = 10 * 1024 * 1024
    offset_bytes: int = 0


@dataclass(frozen=True)
class WriteContentUpload:
    """Strict Base64 binary payload for pre-mutation content staging."""

    project_id: str
    host_id: str
    content_base64: str
    decoded_size_bytes: int
    content_sha256: str


@dataclass(frozen=True)
class StagedWriteContent:
    """Opaque reference to verified staged content bytes."""

    content_ref: str
    content_sha256: str
    decoded_size_bytes: int
    project_id: str
    host_id: str
    staged_at: str
    expires_at: Optional[str] = None


@dataclass(frozen=True)
class FileWriteRequest:
    """Compare-and-swap file write intent referencing pre-staged content."""

    project_id: str
    host_id: str
    target_path: str
    idempotency_key: str
    content_ref: str
    content_sha256: str
    decoded_size_bytes: int
    if_absent: Optional[bool] = None
    expected_sha256: Optional[str] = None
    expected_file_policy_digest: Optional[str] = None


@dataclass(frozen=True)
class FileResult:
    """Result of a file read or durable write operation."""

    status: str  # "ok", "failed", "ambiguous", "unavailable", "rejected"
    path: str
    content_sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    content_bytes: Optional[bytes] = None
    pre_digest: Optional[str] = None
    post_digest: Optional[str] = None
    applied_at: Optional[str] = None
    host_identity: str = ""
    error: Optional[str] = None
    raw_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StatResult:
    """Filesystem metadata for a scoped path."""

    status: str
    path: str
    exists: bool
    is_file: bool = False
    is_dir: bool = False
    size_bytes: Optional[int] = None
    modified_at: Optional[str] = None
    sha256: Optional[str] = None
    host_identity: str = ""
    error: Optional[str] = None


@dataclass(frozen=True)
class HostCapabilities:
    """Discovered or configured capabilities for an execution host."""

    host_id: str
    os_family: str  # "windows", "linux", "darwin"
    path_style: str  # "windows" or "posix"
    helper_version: str
    jobs_config_valid: bool
    approved_policy_pins: dict[str, str] = field(default_factory=dict)
    response_limits: dict[str, int] = field(default_factory=dict)
    supported_operations: list[str] = field(default_factory=list)
    probed_at: Optional[str] = None


@dataclass(frozen=True)
class TransportSelection:
    """Decision output from evidence-based transport selection."""

    selected_transport: str  # "local", "ssh", "rdc_fallback_required"
    candidate_order: list[str]
    evidence_source: str
    reason_code: str
    candidate_rejections: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class MachineTransport(Protocol):
    """Protocol for first-class DevOrchestrator machine execution transports."""

    def exec(self, request: MachineOperation) -> MachineOperationResult:
        """Execute a read-only command synchronously. Effectful commands fail closed."""
        ...

    def spawn(self, request: MachineOperation) -> MachineOperationResult:
        """Spawn an idempotent or effectful command via durable JobService."""
        ...

    def poll(self, operation_id: str, host_id: Optional[str] = None) -> MachineOperationResult:
        """Poll the current status of a spawned durable operation."""
        ...

    def cancel(
        self, operation_id: str, host_id: Optional[str] = None, *, reason: str = "cancelled"
    ) -> MachineOperationResult:
        """Cancel an active spawned durable operation."""
        ...

    def read_file(self, request: FileReadRequest) -> FileResult:
        """Read a scoped file within configured file roots."""
        ...

    def stage_write_content(self, upload: WriteContentUpload) -> StagedWriteContent:
        """Stage arbitrary binary content and return an opaque digest-verified content ref."""
        ...

    def write_file(self, request: FileWriteRequest) -> FileResult:
        """Commit a staged binary write under CAS preconditions and canonical path locking."""
        ...

    def stat(
        self, path: str, host_id: Optional[str] = None, project_id: Optional[str] = None
    ) -> StatResult:
        """Stat a path within configured repository or file roots."""
        ...

    def capabilities(self, host_id: Optional[str] = None) -> HostCapabilities:
        """Discover host capability and configuration status."""
        ...
