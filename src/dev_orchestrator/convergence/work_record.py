"""Canonical Work Record target model for single-authority convergence.

Defines WorkRecord, validation, canonical serialization, digest generation,
and in-memory CAS transition helper.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

ALLOWED_STATUSES = frozenset({"OPEN", "NEEDS_HUMAN", "DONE"})
ALLOWED_GOAL_STATUSES = ALLOWED_STATUSES
ALLOWED_ACCEPTANCE_KINDS = frozenset({"NONE", "VERIFIED", "OWNER_OVERRIDE"})


class WorkRecordValidationError(ValueError):
    """Raised when a WorkRecord payload fails validation."""


class CASConflictError(RuntimeError):
    """Raised when an in-memory CAS comparison fails."""


@dataclass(frozen=True)
class WorkRecord:
    schema_version: int
    project_id: str
    goal_id: str
    goal_revision: int
    goal_spec_digest: str
    predecessor_goal_id: str | None
    repository_identity: dict[str, Any]
    status: str
    active_lease: dict[str, Any] | None = None
    current_problem: dict[str, Any] | None = None
    attempts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    acceptance: dict[str, Any] = field(default_factory=lambda: {"kind": "NONE"})
    wait: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    successor: dict[str, Any] | None = None
    human_request: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None
    authority_revision: str = "rev-1"
    consumed_idempotency_keys: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "goal_spec_digest": self.goal_spec_digest,
            "predecessor_goal_id": self.predecessor_goal_id,
            "repository_identity": dict(self.repository_identity),
            "status": self.status,
            "active_lease": dict(self.active_lease) if self.active_lease is not None else None,
            "current_problem": dict(self.current_problem) if self.current_problem is not None else None,
            "attempts": [dict(a) for a in self.attempts],
            "acceptance": dict(self.acceptance),
            "wait": dict(self.wait) if self.wait is not None else None,
            "verification": dict(self.verification) if self.verification is not None else None,
            "successor": dict(self.successor) if self.successor is not None else None,
            "human_request": dict(self.human_request) if self.human_request is not None else None,
            "handoff": dict(self.handoff) if self.handoff is not None else None,
            "authority_revision": self.authority_revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.consumed_idempotency_keys:
            d["consumed_idempotency_keys"] = list(self.consumed_idempotency_keys)
        return d


def canonical_json(record: WorkRecord | Mapping[str, Any]) -> str:
    """Return deterministic canonical JSON with sorted keys and tight separators."""
    if isinstance(record, WorkRecord):
        data = record.to_dict()
    elif isinstance(record, Mapping):
        data = dict(record)
    else:
        raise TypeError(f"Expected WorkRecord or Mapping, got {type(record).__name__}")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def work_record_digest(record: WorkRecord | Mapping[str, Any]) -> str:
    """Compute sha256 hex digest of the canonical JSON representation."""
    c_json = canonical_json(record)
    return hashlib.sha256(c_json.encode("utf-8")).hexdigest()


def validate_work_record(payload: Mapping[str, Any]) -> WorkRecord:
    """Validate a raw dictionary against the WorkRecord v0 specification.

    Raises WorkRecordValidationError on schema violations.
    """
    if not isinstance(payload, Mapping):
        raise WorkRecordValidationError("Work record payload must be a mapping")

    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or schema_version < 1:
        raise WorkRecordValidationError(f"Invalid schema_version: {schema_version!r}")

    for req_field in ("project_id", "goal_id", "goal_spec_digest"):
        val = payload.get(req_field)
        if not isinstance(val, str) or not val.strip():
            raise WorkRecordValidationError(f"Required field {req_field!r} must be a non-empty string")

    goal_rev = payload.get("goal_revision")
    if not isinstance(goal_rev, int) or goal_rev < 1:
        raise WorkRecordValidationError(f"Invalid goal_revision: {goal_rev!r}")

    pred_goal = payload.get("predecessor_goal_id")
    if pred_goal is not None and (not isinstance(pred_goal, str) or not pred_goal.strip()):
        raise WorkRecordValidationError(f"Invalid predecessor_goal_id: {pred_goal!r}")

    repo_id = payload.get("repository_identity")
    if not isinstance(repo_id, Mapping) or "repo_path" not in repo_id or "branch" not in repo_id:
        raise WorkRecordValidationError("repository_identity must contain 'repo_path' and 'branch'")

    status = payload.get("status")
    if status not in ALLOWED_STATUSES:
        raise WorkRecordValidationError(f"status must be one of {sorted(ALLOWED_STATUSES)}, got {status!r}")

    # Active lease validation
    active_lease = payload.get("active_lease")
    if active_lease is not None:
        if not isinstance(active_lease, Mapping):
            raise WorkRecordValidationError("active_lease must be a mapping or None")
        req_lease_keys = ("role", "attempt_id", "execution_id", "acquired_at")
        for k in req_lease_keys:
            if k not in active_lease:
                raise WorkRecordValidationError(f"active_lease missing required key: {k!r}")

    # Acceptance validation
    acceptance = payload.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise WorkRecordValidationError("acceptance must be a mapping")
    kind = acceptance.get("kind")
    if kind not in ALLOWED_ACCEPTANCE_KINDS:
        raise WorkRecordValidationError(f"acceptance.kind must be one of {sorted(ALLOWED_ACCEPTANCE_KINDS)}, got {kind!r}")

    if kind == "VERIFIED":
        for k in ("reviewer_verdict_id", "anchor_head"):
            if not acceptance.get(k):
                raise WorkRecordValidationError(f"VERIFIED acceptance missing required key {k!r}")
    elif kind == "OWNER_OVERRIDE":
        for k in ("owner_id", "command_id", "scope", "reason", "waived_obligations"):
            if k not in acceptance:
                raise WorkRecordValidationError(f"OWNER_OVERRIDE acceptance missing required key {k!r}")
        if not isinstance(acceptance.get("waived_obligations"), list):
            raise WorkRecordValidationError("waived_obligations must be a list")

    if status == "DONE" and kind not in ("VERIFIED", "OWNER_OVERRIDE"):
        raise WorkRecordValidationError(
            f"status 'DONE' requires acceptance.kind to be 'VERIFIED' or 'OWNER_OVERRIDE', got {kind!r}"
        )

    # Current problem validation
    current_problem = payload.get("current_problem")
    if current_problem is not None:
        if not isinstance(current_problem, Mapping):
            raise WorkRecordValidationError("current_problem must be a mapping or None")
        for k in ("problem_id", "failure_class", "normalized_fingerprint"):
            if not current_problem.get(k):
                raise WorkRecordValidationError(f"current_problem missing required key {k!r}")

    # Attempts validation
    attempts_raw = payload.get("attempts", [])
    if not isinstance(attempts_raw, (list, tuple)):
        raise WorkRecordValidationError("attempts must be a sequence")
    parsed_attempts = []
    for i, att in enumerate(attempts_raw):
        if not isinstance(att, Mapping):
            raise WorkRecordValidationError(f"attempts[{i}] must be a mapping")
        for k in ("attempt_id", "problem_id", "strategy_id", "typed_outcome"):
            if k not in att:
                raise WorkRecordValidationError(f"attempts[{i}] missing required key {k!r}")
        parsed_attempts.append(dict(att))

    # Wait validation
    wait = payload.get("wait")
    if wait is not None:
        if not isinstance(wait, Mapping):
            raise WorkRecordValidationError("wait must be a mapping or None")
        for k in ("not_before", "reset_source", "reason_code"):
            if k not in wait:
                raise WorkRecordValidationError(f"wait missing required key {k!r}")

    # Successor validation
    successor = payload.get("successor")
    if successor is not None:
        if not isinstance(successor, Mapping):
            raise WorkRecordValidationError("successor must be a mapping or None")
        for k in ("successor_goal_id", "successor_spec_digest", "publication_state", "handoff_idempotency_key"):
            if k not in successor:
                raise WorkRecordValidationError(f"successor missing required key {k!r}")

    # Human request validation
    human_req = payload.get("human_request")
    if human_req is not None:
        if not isinstance(human_req, Mapping):
            raise WorkRecordValidationError("human_request must be a mapping or None")
        for k in ("question_id", "question_revision", "request_type", "concrete_question", "options"):
            if k not in human_req:
                raise WorkRecordValidationError(f"human_request missing required key {k!r}")
        if not isinstance(human_req.get("options"), (list, tuple)) or len(human_req["options"]) == 0:
            raise WorkRecordValidationError("human_request.options must be a non-empty sequence")

    auth_rev = str(payload.get("authority_revision") or "rev-1")
    created_at = str(payload.get("created_at") or "")
    updated_at = str(payload.get("updated_at") or "")
    consumed_raw = payload.get("consumed_idempotency_keys") or payload.get("executed_idempotency_keys") or ()
    if not isinstance(consumed_raw, (list, tuple, set)):
        raise WorkRecordValidationError("consumed_idempotency_keys must be a sequence")
    parsed_consumed = tuple(str(k) for k in consumed_raw if k)

    return WorkRecord(
        schema_version=schema_version,
        project_id=str(payload["project_id"]),
        goal_id=str(payload["goal_id"]),
        goal_revision=goal_rev,
        goal_spec_digest=str(payload["goal_spec_digest"]),
        predecessor_goal_id=str(pred_goal) if pred_goal else None,
        repository_identity=dict(repo_id),
        status=str(status),
        active_lease=dict(active_lease) if active_lease else None,
        current_problem=dict(current_problem) if current_problem else None,
        attempts=tuple(parsed_attempts),
        acceptance=dict(acceptance),
        wait=dict(wait) if wait else None,
        verification=dict(payload["verification"]) if payload.get("verification") else None,
        successor=dict(successor) if successor else None,
        human_request=dict(human_req) if human_req else None,
        handoff=dict(payload["handoff"]) if payload.get("handoff") else None,
        authority_revision=auth_rev,
        consumed_idempotency_keys=parsed_consumed,
        created_at=created_at,
        updated_at=updated_at,
    )


def apply_work_record_cas(
    record: WorkRecord,
    expected_revision: str,
    updates: Mapping[str, Any],
    *,
    now: str | None = None,
) -> WorkRecord:
    """Pure, side-effect-free, model-only CAS helper.

    Compares record.authority_revision against expected_revision.
    If match: applies updates, advances authority_revision, and sets updated_at.
    If mismatch: raises CASConflictError.
    Does NOT perform file I/O or persistence.
    """
    if record.authority_revision != expected_revision:
        raise CASConflictError(
            f"CAS revision mismatch: record has {record.authority_revision!r}, "
            f"expected {expected_revision!r}"
        )

    current_data = record.to_dict()
    for k, v in updates.items():
        if k in ("schema_version", "project_id", "goal_id", "created_at"):
            # Immutable identity fields
            if v != current_data.get(k):
                raise WorkRecordValidationError(f"Cannot mutate immutable identity field {k!r}")
        current_data[k] = copy.deepcopy(v)

    # Bump revision
    old_rev = record.authority_revision
    if old_rev.startswith("rev-"):
        try:
            num = int(old_rev.split("-")[1])
            new_rev = f"rev-{num + 1}"
        except (ValueError, IndexError):
            new_rev = f"{old_rev}.next"
    else:
        new_rev = f"{old_rev}.next"

    iso_now = now or datetime.now(timezone.utc).isoformat()
    current_data["authority_revision"] = new_rev
    current_data["updated_at"] = iso_now
    if not current_data.get("created_at"):
        current_data["created_at"] = iso_now

    return validate_work_record(current_data)


def model_durable_decision_transition(
    record: WorkRecord,
    decision: Any,
    *,
    now: str | None = None,
) -> WorkRecord:
    """Model the durable intent / authority transition for an admitted decision.

    Pure and side-effect free: applies CAS updates to record to reflect the durable
    execution intent or terminal publication, persisting consumed idempotency keys,
    active lease, successor state, or handoff, and bumping authority revision.
    Non-mutative decisions (NOOP_ACTIVE, WAIT_UNTIL, REQUEST_HUMAN) return the record unchanged.
    """
    kind = getattr(decision, "kind", None)
    kind_val = kind.value if hasattr(kind, "value") else str(kind)
    if kind_val in ("NOOP_ACTIVE", "WAIT_UNTIL", "REQUEST_HUMAN"):
        return record

    iso_now = now or datetime.now(timezone.utc).isoformat()
    idemp_key = getattr(decision, "idempotency_key", "")
    params = getattr(decision, "parameters", {}) or {}

    new_consumed = set(record.consumed_idempotency_keys)
    if idemp_key:
        new_consumed.add(idemp_key)

    updates: dict[str, Any] = {
        "consumed_idempotency_keys": sorted(new_consumed),
    }

    if kind_val == "EXECUTE" or kind_val.startswith("RETRY_") or kind_val in ("FAILOVER_RESOURCE", "ESCALATE_CAPABILITY"):
        role = params.get("role", "worker")
        attempt_id = params.get("attempt_id") or f"att-{len(record.attempts) + 1}"
        exec_id = params.get("execution_id") or f"exec-{record.goal_id}-{idemp_key[:8]}"
        updates["active_lease"] = {
            "role": role,
            "attempt_id": attempt_id,
            "execution_id": exec_id,
            "acquired_at": iso_now,
            "idempotency_key": idemp_key,
        }

    elif kind_val == "VERIFY":
        role = params.get("role", "reviewer")
        attempt_id = params.get("attempt_id") or f"att-verify-{len(record.attempts) + 1}"
        exec_id = params.get("execution_id") or f"exec-verify-{record.goal_id}-{idemp_key[:8]}"
        updates["active_lease"] = {
            "role": role,
            "attempt_id": attempt_id,
            "execution_id": exec_id,
            "acquired_at": iso_now,
            "idempotency_key": idemp_key,
        }

    elif kind_val == "PUBLISH_SUCCESSOR":
        succ = dict(record.successor or {})
        succ["publication_state"] = "PUBLISHED"
        succ["handoff_idempotency_key"] = idemp_key or succ.get("handoff_idempotency_key") or ""
        updates["successor"] = succ
        updates["status"] = "DONE"
        updates["active_lease"] = None

    elif kind_val == "SATISFY_GOAL":
        updates["status"] = "DONE"
        updates["active_lease"] = None

    elif kind_val == "WRITE_HANDOFF":
        updates["status"] = "NEEDS_HUMAN"
        h = dict(record.handoff or {})
        h["created_at"] = iso_now
        h["idempotency_key"] = idemp_key
        updates["handoff"] = h

    return apply_work_record_cas(
        record,
        expected_revision=record.authority_revision,
        updates=updates,
        now=iso_now,
    )
