"""Provider-neutral contracts between DevOrchestrator and AIResourceBroker."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

_QUALITIES = {"economy", "balanced", "high"}
_INDEPENDENCE = {"none", "resource", "account", "provider"}
_STATUSES = {"succeeded", "failed", "cancelled", "no_candidate"}


def _nonblank(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank")
    return value


@dataclass(frozen=True, slots=True)
class ResourceContext:
    """Observed broker resource identity; it is evidence, not routing policy."""

    resource_id: str | None = None
    provider: str | None = None
    account: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        for name in ("resource_id", "provider", "account", "model"):
            value = getattr(self, name)
            if value is not None:
                _nonblank(name, value)


@dataclass(frozen=True, slots=True)
class AIRoleRequest:
    """One orchestration-owned semantic request for an AI role."""

    project_id: str
    role: str
    prompt: str
    working_directory: Path
    task_run_id: str = ""
    stage_run_id: str = ""
    role_run_id: str = ""
    request_id: str = field(default_factory=lambda: str(uuid4()))
    quality: str = "balanced"
    independence: str = "none"
    previous_resource_context: ResourceContext | None = None
    timeout_seconds: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project_id", "role", "prompt", "request_id"):
            _nonblank(name, getattr(self, name))
        for name in ("task_run_id", "stage_run_id", "role_run_id"):
            value = getattr(self, name)
            if value:
                _nonblank(name, value)
        if not isinstance(self.working_directory, Path):
            object.__setattr__(self, "working_directory", Path(self.working_directory))
        if self.quality not in _QUALITIES:
            raise ValueError("quality must be economy, balanced, or high")
        if self.independence not in _INDEPENDENCE:
            raise ValueError("invalid independence")
        if self.timeout_seconds is not None:
            if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be positive")
        previous = self.previous_resource_context
        required = {
            "none": (), "resource": ("resource_id",),
            "account": ("provider", "account"), "provider": ("provider",),
        }[self.independence]
        if any(previous is None or not getattr(previous, name) for name in required):
            raise ValueError("independence requires prior resource evidence")


@dataclass(frozen=True, slots=True)
class AIRoleResult:
    """Terminal result from one broker dispatch."""

    request_id: str
    role_run_id: str
    status: str
    output: str | None = None
    error: str | None = None
    dispatch_id: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    session_id: str | None = None
    resource_context: ResourceContext | None = None
    usage: Mapping[str, Any] | None = None
    usage_source: str = "unknown"

    def __post_init__(self) -> None:
        _nonblank("request_id", self.request_id)
        if self.role_run_id:
            _nonblank("role_run_id", self.role_run_id)
        if self.status not in _STATUSES:
            raise ValueError(f"invalid AI role result status: {self.status}")
        if self.usage_source not in {"reported", "derived", "unknown"}:
            raise ValueError("invalid usage_source")
