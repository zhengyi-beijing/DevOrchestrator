"""Failure incident contract, validation, and taxonomy for P20.1.

Defines the canonical FailureIncident dataclass, the 19 owner-authorized failure classes,
schema versioning, and validation functions.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

INCIDENT_SCHEMA_VERSION = 1

FAILURE_CLASSES = frozenset({
    "malformed_ai_output",
    "planner_timeout",
    "worker_process_lost",
    "reviewer_rejected",
    "reviewer_timeout",
    "provider_quota_exhausted",
    "provider_unavailable",
    "aibroker_unavailable",
    "daemon_restart_during_execution",
    "daemon_restart_during_handoff",
    "stale_head",
    "unexpected_dirty_worktree",
    "missing_execution_record",
    "lost_handoff",
    "duplicate_request",
    "lifecycle_projection_mismatch",
    "retry_without_progress",
    "transport_unavailable",
    "unknown_requires_owner",
})


@dataclass
class FailureIncident:
    """Canonical failure incident record."""

    schema_version: int
    incident_id: str
    project_id: str
    failure_signature: str
    failure_family: str
    failure_class: str
    first_seen_at: str
    last_seen_at: str
    occurrence_count: int
    task_id: str | None = None
    execution_id: str | None = None
    plan_id: str | None = None
    review_id: str | None = None
    lifecycle_generation: int | None = None
    lifecycle_transition_id: str | None = None
    recovery_epoch_id: str | None = None
    stage: str | None = None
    role: str | None = None
    provider: str | None = None
    model: str | None = None
    resource: str | None = None
    account: str | None = None
    evidence: dict[str, Any] | None = None
    root_cause_hint: str | None = None
    previous_attempts: list[dict[str, Any]] | None = None
    recovery_strategy: str | None = None
    recovery_attempt: int | None = None
    recovery_result: str | None = None
    recovery_latency: float | None = None
    terminal_disposition: str | None = None
    owner_gate_id: str | None = None
    regression_candidate_id: str | None = None
    related_incident_ids: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FailureIncident:
        raw = dict(data)
        return cls(
            schema_version=int(raw.get("schema_version", INCIDENT_SCHEMA_VERSION)),
            incident_id=str(raw["incident_id"]),
            project_id=str(raw["project_id"]),
            failure_signature=str(raw["failure_signature"]),
            failure_family=str(raw["failure_family"]),
            failure_class=str(raw["failure_class"]),
            first_seen_at=str(raw["first_seen_at"]),
            last_seen_at=str(raw["last_seen_at"]),
            occurrence_count=int(raw.get("occurrence_count", 1)),
            task_id=raw.get("task_id"),
            execution_id=raw.get("execution_id"),
            plan_id=raw.get("plan_id"),
            review_id=raw.get("review_id"),
            lifecycle_generation=raw.get("lifecycle_generation"),
            lifecycle_transition_id=raw.get("lifecycle_transition_id"),
            recovery_epoch_id=raw.get("recovery_epoch_id"),
            stage=raw.get("stage"),
            role=raw.get("role"),
            provider=raw.get("provider"),
            model=raw.get("model"),
            resource=raw.get("resource"),
            account=raw.get("account"),
            evidence=copy.deepcopy(raw.get("evidence")),
            root_cause_hint=raw.get("root_cause_hint"),
            previous_attempts=copy.deepcopy(raw.get("previous_attempts")),
            recovery_strategy=raw.get("recovery_strategy"),
            recovery_attempt=raw.get("recovery_attempt"),
            recovery_result=raw.get("recovery_result"),
            recovery_latency=float(raw["recovery_latency"]) if raw.get("recovery_latency") is not None else None,
            terminal_disposition=raw.get("terminal_disposition"),
            owner_gate_id=raw.get("owner_gate_id"),
            regression_candidate_id=raw.get("regression_candidate_id"),
            related_incident_ids=list(raw.get("related_incident_ids") or []) if raw.get("related_incident_ids") is not None else None,
        )


def validate_failure_incident(incident: Mapping[str, Any] | FailureIncident) -> list[str]:
    """Validate failure incident structure, schema version, and required fields."""
    errors: list[str] = []
    data = incident.to_dict() if isinstance(incident, FailureIncident) else dict(incident)

    version = data.get("schema_version")
    if version != INCIDENT_SCHEMA_VERSION:
        errors.append(f"unsupported schema_version {version}; expected {INCIDENT_SCHEMA_VERSION}")

    required_strings = ("incident_id", "project_id", "failure_signature", "failure_family", "failure_class", "first_seen_at", "last_seen_at")
    for field_name in required_strings:
        val = data.get(field_name)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"missing or empty required string field: {field_name}")

    fc = data.get("failure_class")
    if fc is not None and fc not in FAILURE_CLASSES:
        errors.append(f"unknown failure_class '{fc}'; must be one of {sorted(FAILURE_CLASSES)}")

    oc = data.get("occurrence_count")
    if not isinstance(oc, int) or isinstance(oc, bool) or oc < 1:
        errors.append(f"occurrence_count must be a positive integer, got: {oc}")

    lat = data.get("recovery_latency")
    if lat is not None and not isinstance(lat, (int, float)):
        errors.append(f"recovery_latency must be numeric, got {type(lat)}")

    ev = data.get("evidence")
    if ev is not None and not isinstance(ev, dict):
        errors.append(f"evidence must be a dictionary or None, got {type(ev)}")

    return errors


def normalize_failure_incident(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize failure incident payload into a validated canonical dictionary."""
    raw = dict(data)
    incident = FailureIncident.from_dict(raw)
    errors = validate_failure_incident(incident)
    if errors:
        raise ValueError(f"Invalid FailureIncident: {'; '.join(errors)}")
    return incident.to_dict()
