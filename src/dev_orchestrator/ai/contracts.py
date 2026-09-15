"""Provider-neutral contracts between DevOrchestrator and AIResourceBroker."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

_QUALITIES = {"economy", "balanced", "high"}
_INDEPENDENCE = {"none", "resource", "account", "provider"}
_STATUSES = {"succeeded", "failed", "cancelled", "no_candidate"}
_RESOURCE_FAILURE_CLASSIFICATIONS = {
    "quota_exhausted",
    "rate_limited",
    "provider_temporarily_unavailable",
    "resource_unavailable",
}


def _nonblank(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank")
    return value


def _utc_time(name: str, value: str | None) -> datetime | None:
    if value is None:
        return None
    _nonblank(name, value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be UTC")
    return parsed


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
    excluded_resource_ids: tuple[str, ...] = ()
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
        excluded = self.excluded_resource_ids
        if not isinstance(excluded, (tuple, list)):
            raise ValueError("excluded_resource_ids must be a sequence")
        if any(not isinstance(resource_id, str) or not resource_id.strip() for resource_id in excluded):
            raise ValueError("excluded_resource_ids must contain nonblank strings")
        if len(set(excluded)) != len(excluded):
            raise ValueError("excluded_resource_ids must not contain duplicates")
        object.__setattr__(self, "excluded_resource_ids", tuple(excluded))


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
    started_at: str | None = None
    finished_at: str | None = None
    first_output_at: str | None = None
    quota_observation: Mapping[str, Any] | None = None
    rate_limit_observation: Mapping[str, Any] | None = None
    failure_classification: str | None = None

    def __post_init__(self) -> None:
        _nonblank("request_id", self.request_id)
        if self.role_run_id:
            _nonblank("role_run_id", self.role_run_id)
        if self.status not in _STATUSES:
            raise ValueError(f"invalid AI role result status: {self.status}")
        if self.usage_source not in {"reported", "derived", "unknown"}:
            raise ValueError("invalid usage_source")
        started = _utc_time("started_at", self.started_at)
        finished = _utc_time("finished_at", self.finished_at)
        first_output = _utc_time("first_output_at", self.first_output_at)
        if started is not None and finished is not None and finished < started:
            raise ValueError("finished_at must not precede started_at")
        if started is not None and first_output is not None and first_output < started:
            raise ValueError("first_output_at must not precede started_at")
        if finished is not None and first_output is not None and first_output > finished:
            raise ValueError("first_output_at must not follow finished_at")
        for name in ("quota_observation", "rate_limit_observation"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Mapping):
                raise ValueError(f"{name} must be an object when present")
        if self.failure_classification is not None:
            _nonblank("failure_classification", self.failure_classification)
            if self.failure_classification not in _RESOURCE_FAILURE_CLASSIFICATIONS:
                raise ValueError("invalid failure_classification")
