"""In-memory execution-effect reconciliation port and five-state effect model.

Enforces NO_ORPHAN_OWNER: a lease is active progress only while demonstrably live.
Converts non-live leases deterministically to typed outcomes and fails closed on ambiguity.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class EffectState(str, Enum):
    LIVE = "LIVE"
    TERMINAL_SUCCESS = "TERMINAL_SUCCESS"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    CANCELLED = "CANCELLED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class ExecutionEffectPort:
    """In-memory port representing an external execution effect boundary."""
    effect_id: str
    broker_request_id: str
    status: EffectState
    probe_details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "broker_request_id": self.broker_request_id,
            "status": self.status.value,
            "probe_details": dict(self.probe_details),
        }


def reconcile_lease(
    active_lease: Mapping[str, Any] | None,
    effect_state: EffectState | str,
    *,
    probe_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile an active execution lease against observed effect state.

    Returns a typed reconciliation result:
    - outcome: "RETAIN_LIVE" | "CLEARED_SUCCESS" | "CLEARED_FAILED" | "CLEARED_CANCELLED" | "FAIL_CLOSED_AMBIGUOUS"
    - new_active_lease: dict | None
    - failure_class: str | None
    - explanation: str
    """
    if active_lease is None:
        return {
            "outcome": "NO_LEASE",
            "new_active_lease": None,
            "failure_class": None,
            "explanation": "No active lease was present to reconcile",
        }

    st = EffectState(effect_state) if isinstance(effect_state, str) else effect_state
    lease_dict = dict(active_lease)

    if st == EffectState.LIVE:
        return {
            "outcome": "RETAIN_LIVE",
            "new_active_lease": lease_dict,
            "failure_class": None,
            "explanation": "Execution is demonstrably live; retain active lease",
        }

    if st == EffectState.TERMINAL_SUCCESS:
        return {
            "outcome": "CLEARED_SUCCESS",
            "new_active_lease": None,
            "failure_class": None,
            "explanation": "Execution completed successfully; lease cleared",
        }

    if st == EffectState.TERMINAL_FAILURE:
        return {
            "outcome": "CLEARED_FAILED",
            "new_active_lease": None,
            "failure_class": "IMPLEMENTATION_DEFECT",
            "explanation": "Execution failed terminally; lease cleared for recovery",
        }

    if st == EffectState.CANCELLED:
        return {
            "outcome": "CLEARED_CANCELLED",
            "new_active_lease": None,
            "failure_class": "RESOURCE_TRANSIENT",
            "explanation": "Execution was cancelled; lease cleared cleanly",
        }

    # AMBIGUOUS: cannot verify if effect ran or not; fail closed without duplicate effect
    return {
        "outcome": "FAIL_CLOSED_AMBIGUOUS",
        "new_active_lease": lease_dict,
        "failure_class": "INTEGRITY_OR_IDENTITY_AMBIGUITY",
        "explanation": "External effect state is ambiguous; refusing to clear or duplicate effect",
    }
