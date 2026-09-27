"""Typed failure classes, stable problem fingerprints, and problem tracking.

Derives stable problem identity excluding volatile values (HEAD, lifecycle phase,
PID, timestamps, retry IDs, provider account unless cause is provider-specific).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from dev_orchestrator.convergence.policy import ProblemBudget


class FailureClass(str, Enum):
    OUTPUT_INVALID = "OUTPUT_INVALID"
    RESOURCE_TRANSIENT = "RESOURCE_TRANSIENT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    AUTH_OR_PERMISSION = "AUTH_OR_PERMISSION"
    ENVIRONMENT_CONSTRAINT = "ENVIRONMENT_CONSTRAINT"
    IMPLEMENTATION_DEFECT = "IMPLEMENTATION_DEFECT"
    REASONING_OR_STRATEGY_DEFECT = "REASONING_OR_STRATEGY_DEFECT"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    INTEGRITY_OR_IDENTITY_AMBIGUITY = "INTEGRITY_OR_IDENTITY_AMBIGUITY"
    SAFETY_OR_IRREVERSIBLE_AUTHORIZATION = "SAFETY_OR_IRREVERSIBLE_AUTHORIZATION"
    CONTROL_PLANE_DEFECT = "CONTROL_PLANE_DEFECT"


ALLOWED_PROBLEM_FIELDS = frozenset({
    "goal_id",
    "criterion_or_invariant_id",
    "failure_class",
    "semantic_error_family",
    "capability_selector",
    "stable_scope",
})

FORBIDDEN_VOLATILE_KEYS = frozenset({
    "head",
    "commit",
    "sha",
    "lifecycle_state",
    "lifecycle_phase",
    "epoch",
    "pid",
    "process_id",
    "timestamp",
    "timestamps",
    "time",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "retry_id",
    "request_id",
    "execution_id",
    "attempt_id",
})

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _validate_non_volatile(value: Any, key_name: str = "") -> None:
    if isinstance(value, str):
        if _UUID_RE.match(value.strip()):
            raise ValueError(f"Volatile UUID detected in problem fingerprint for {key_name!r}")
        if _ISO_TIMESTAMP_RE.match(value.strip()):
            raise ValueError(f"Volatile timestamp detected in problem fingerprint for {key_name!r}")
    elif isinstance(value, (int, float)):
        if value > 1_000_000_000:
            raise ValueError(f"Volatile epoch timestamp detected in problem fingerprint for {key_name!r}")
    elif isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in FORBIDDEN_VOLATILE_KEYS:
                raise ValueError(f"Forbidden volatile key {k!r} detected in problem fingerprint")
            _validate_non_volatile(v, key_name=str(k))
    elif isinstance(value, list):
        for item in value:
            _validate_non_volatile(item, key_name=key_name)


def normalized_problem_fingerprint(
    *,
    goal_id: str,
    criterion_or_invariant_id: str,
    failure_class: FailureClass | str,
    semantic_error_family: str,
    capability_selector: str = "default",
    stable_scope: str = "project",
    extra_semantic: Mapping[str, Any] | None = None,
) -> str:
    """Compute stable truncated SHA-256 fingerprint for a problem.

    Strictly rejects volatile keys (HEAD, lifecycle phase, PID, timestamps, retry IDs).
    """
    fc_val = failure_class.value if isinstance(failure_class, FailureClass) else str(failure_class)
    if fc_val not in FailureClass._value2member_map_:
        raise ValueError(f"Invalid failure_class: {fc_val!r}")

    semantic: dict[str, Any] = {
        "goal_id": str(goal_id).strip(),
        "criterion_or_invariant_id": str(criterion_or_invariant_id).strip(),
        "failure_class": fc_val,
        "semantic_error_family": str(semantic_error_family).strip(),
        "capability_selector": str(capability_selector).strip(),
        "stable_scope": str(stable_scope).strip(),
    }

    if extra_semantic:
        for k, v in extra_semantic.items():
            if str(k).lower() in FORBIDDEN_VOLATILE_KEYS:
                raise ValueError(f"Forbidden volatile key {k!r} in extra_semantic")
            semantic[k] = v

    # Check for forbidden volatile keys and values
    for k, v in semantic.items():
        if str(k).lower() in FORBIDDEN_VOLATILE_KEYS:
            raise ValueError(f"Forbidden volatile key {k!r} in problem payload")
        _validate_non_volatile(v, key_name=str(k))

    c_json = json.dumps(semantic, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(c_json.encode("utf-8")).hexdigest()[:32]


@dataclass
class ProblemTracker:
    problem_id: str
    failure_class: FailureClass
    normalized_fingerprint: str
    first_seen_at: str
    resource_attempts: int = 0
    strategy_attempts: int = 0
    capability_escalations: int = 0
    total_attempts: int = 0
    output_invalid_repairs: int = 0
    current_tier_index: int = 0
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "failure_class": self.failure_class.value,
            "normalized_fingerprint": self.normalized_fingerprint,
            "first_seen_at": self.first_seen_at,
            "resource_attempts": self.resource_attempts,
            "strategy_attempts": self.strategy_attempts,
            "capability_escalations": self.capability_escalations,
            "total_attempts": self.total_attempts,
            "output_invalid_repairs": self.output_invalid_repairs,
            "current_tier_index": self.current_tier_index,
            "resolved": self.resolved,
        }

    def is_exhausted(self, budget: ProblemBudget) -> bool:
        if self.total_attempts >= budget.max_total_attempts:
            return True
        if self.strategy_attempts >= budget.max_strategy_attempts and self.capability_escalations >= budget.max_capability_escalations:
            return True
        return False


def select_next_problem(
    problems: Sequence[ProblemTracker | dict[str, Any]],
    order: str = "stable_hash",
) -> ProblemTracker | dict[str, Any] | None:
    """Select the next unresolved problem using deterministic stable ordering."""
    unresolved = [p for p in problems if not (p.resolved if isinstance(p, ProblemTracker) else p.get("resolved", False))]
    if not unresolved:
        return None

    def sort_key(p: ProblemTracker | dict[str, Any]) -> str:
        if isinstance(p, ProblemTracker):
            p_id = p.problem_id
            fp = p.normalized_fingerprint
        else:
            p_id = str(p.get("problem_id", ""))
            fp = str(p.get("normalized_fingerprint", ""))
        if order == "first_seen":
            seen = p.first_seen_at if isinstance(p, ProblemTracker) else str(p.get("first_seen_at", ""))
            return f"{seen}:{p_id}"
        # Default: stable hash
        return f"{fp}:{p_id}"

    return sorted(unresolved, key=sort_key)[0]
