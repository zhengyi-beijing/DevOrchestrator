"""Data models and state machine for durable asynchronous execution jobs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from .config import (
    DEFAULT_HEAD_LINES,
    DEFAULT_MAX_JOB_BYTES,
    DEFAULT_MAX_LINE_BYTES,
    DEFAULT_TAIL_LINES,
)

JOB_SCHEMA_VERSION = 1

JOB_STATES = (
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "unknown_recovery",
)

TERMINAL_JOB_STATES = (
    "completed",
    "failed",
    "cancelled",
)

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"completed", "failed", "cancelled", "unknown_recovery"}),
    "unknown_recovery": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class JobConflictError(RuntimeError):
    """Job id or retry successor already claimed with conflicting identity."""


class JobCorruptionError(RuntimeError):
    """Job record or log exists but cannot be safely read."""


class JobTransitionError(RuntimeError):
    """Illegal state transition attempted on job record."""


def _nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value.strip()


@dataclass(frozen=True)
class JobSpec:
    """Specification for submitting an asynchronous execution job."""

    project_id: str
    command_ref: str
    idempotency_key: str
    kind: str = "validation"
    task_id: Optional[str] = None
    stage_run_id: Optional[str] = None
    role_run_id: Optional[str] = None
    source_request_id: Optional[str] = None
    broker_request_id: Optional[str] = None
    transport: str = "local"
    expected_working_directory: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _nonblank(self.project_id, "project_id"))
        object.__setattr__(self, "command_ref", _nonblank(self.command_ref, "command_ref"))
        object.__setattr__(self, "idempotency_key", _nonblank(self.idempotency_key, "idempotency_key"))
        if self.transport not in ("local", "ssh"):
            raise ValueError(f"unsupported transport {self.transport!r}, must be 'local' or 'ssh'")

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": JOB_SCHEMA_VERSION,
            "project_id": self.project_id,
            "command_ref": self.command_ref,
            "idempotency_key": self.idempotency_key,
            "kind": self.kind,
            "task_id": self.task_id,
            "stage_run_id": self.stage_run_id,
            "role_run_id": self.role_run_id,
            "source_request_id": self.source_request_id,
            "broker_request_id": self.broker_request_id,
            "transport": self.transport,
            "expected_working_directory": self.expected_working_directory,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobSpec:
        allowed = {
            "project_id",
            "command_ref",
            "idempotency_key",
            "kind",
            "task_id",
            "stage_run_id",
            "role_run_id",
            "source_request_id",
            "broker_request_id",
            "transport",
            "expected_working_directory",
            "metadata",
        }
        filtered = {k: data[k] for k in allowed if k in data and data[k] is not None}
        return cls(**filtered)


def spec_hash(spec: JobSpec | Mapping[str, Any]) -> str:
    """Canonical sha256 hash mirroring control.command_store.request_hash."""
    canonical = spec.to_canonical_dict() if isinstance(spec, JobSpec) else JobSpec.from_dict(spec).to_canonical_dict()
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def job_id_for(spec: JobSpec | Mapping[str, Any]) -> str:
    """Derive deterministic job_id from project_id and idempotency_key."""
    if isinstance(spec, JobSpec):
        proj = spec.project_id
        key = spec.idempotency_key
    else:
        proj = _nonblank(spec.get("project_id"), "project_id")
        key = _nonblank(spec.get("idempotency_key"), "idempotency_key")
    digest = hashlib.sha256(f"{proj}:{key}".encode("utf-8")).hexdigest()[:16]
    return f"job-{digest}"


def retry_successor_id(predecessor_job_id: str, retry_request_id: str) -> str:
    """Deterministically map predecessor_job_id and retry_request_id to successor job_id."""
    pred_id = _nonblank(predecessor_job_id, "predecessor_job_id")
    req_id = _nonblank(retry_request_id, "retry_request_id")
    digest = hashlib.sha256(f"{pred_id}:retry:{req_id}".encode("utf-8")).hexdigest()[:16]
    return f"job-{digest}"


@dataclass
class JobRecord:
    """Durable execution job record (schema version 1)."""

    job_id: str
    idempotency_key: str
    spec_hash: str
    kind: str
    project_id: str
    command_ref: str
    resolved_argv: list[str]
    working_directory: str
    transport: str
    host_identity: str
    duration_class: str
    max_runtime_seconds: float = 300.0
    heartbeat_interval_seconds: float = 5.0
    log_caps: dict[str, int] = field(default_factory=lambda: {
        "max_line_bytes": DEFAULT_MAX_LINE_BYTES,
        "max_job_bytes": DEFAULT_MAX_JOB_BYTES,
        "head_lines": DEFAULT_HEAD_LINES,
        "tail_lines": DEFAULT_TAIL_LINES,
    })
    state: str = "queued"
    state_reason: Optional[str] = None
    failure_kind: Optional[str] = None
    schema_version: int = JOB_SCHEMA_VERSION
    task_id: Optional[str] = None
    stage_run_id: Optional[str] = None
    role_run_id: Optional[str] = None
    source_request_id: Optional[str] = None
    broker_request_id: Optional[str] = None
    exit_code: Optional[int] = None
    accounting_recorded_at: Optional[str] = None
    supervisor: dict[str, Any] = field(default_factory=lambda: {
        "pid": None,
        "start_token": None,
        "started_at": None,
    })
    heartbeat: dict[str, Any] = field(default_factory=lambda: {
        "sequence": 0,
        "heartbeat_sequence": 0,
        "reported_at": None,
        "observed_at": None,
        "progress": None,
    })
    timestamps: dict[str, Any] = field(default_factory=lambda: {
        "created_at": None,
        "queued_at": None,
        "started_at": None,
        "finished_at": None,
        "updated_at": None,
    })
    terminal: dict[str, Any] = field(default_factory=lambda: {
        "outcome": None,
        "error": None,
        "truncated_lines": 0,
        "truncated_bytes": 0,
    })
    retry: dict[str, Any] = field(default_factory=lambda: {
        "retry_of": None,
        "attempt": 1,
        "retry_request_id": None,
        "retry_request_hash": None,
        "successor_job_id": None,
        "successor_spawned_at": None,
    })
    recovery: dict[str, Any] = field(default_factory=lambda: {
        "recovery_safe_retry": False,
        "evidence": None,
    })

    def transition_to(
        self,
        new_state: str,
        *,
        reason: Optional[str] = None,
        failure_kind: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        """Enforce legal state transitions."""
        if new_state not in JOB_STATES:
            raise JobTransitionError(f"unknown job state: {new_state!r}")
        allowed = VALID_TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise JobTransitionError(
                f"illegal job transition from {self.state!r} to {new_state!r}"
            )
        self.state = new_state
        if reason is not None:
            self.state_reason = reason
        if failure_kind is not None:
            self.failure_kind = failure_kind
        ts = timestamp or self.timestamps.get("updated_at")
        if ts is not None:
            self.timestamps["updated_at"] = ts
            if new_state == "running" and not self.timestamps.get("started_at"):
                self.timestamps["started_at"] = ts
            elif new_state in TERMINAL_JOB_STATES and not self.timestamps.get("finished_at"):
                self.timestamps["finished_at"] = ts

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobRecord:
        raw = dict(data)
        fields_set = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in raw.items() if k in fields_set}
        return cls(**filtered)
