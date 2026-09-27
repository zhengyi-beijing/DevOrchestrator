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
class LeaseLiveness:
    found_liveness: bool
    lease_live: bool
    lease_ambiguous: bool
    terminal_success: bool = False


def resolve_lease_liveness(
    active_lease: Mapping[str, Any] | None,
    evidence: Any,
) -> LeaseLiveness:
    """Resolve active lease liveness across all matching evidence items.

    Enforces fail-closed behavior on contradictory authority:
    if multiple process_probe or broker_effect rows for the same role/execution
    yield conflicting or ambiguous liveness states, lease_ambiguous is set to True.
    """
    if not active_lease:
        return LeaseLiveness(found_liveness=False, lease_live=False, lease_ambiguous=False)

    role = active_lease.get("role")
    exec_id = active_lease.get("execution_id")

    items = getattr(evidence, "items", ())
    verdicts: set[str] = set()
    found_liveness = False
    terminal_success = False

    for item in items:
        source = getattr(item, "source", None)
        if source is None and isinstance(item, Mapping):
            source = item.get("source")
        data = getattr(item, "data", None)
        if data is None and isinstance(item, Mapping):
            data = item.get("data")
        if not isinstance(data, Mapping):
            continue

        if source == "process_probe" and role and data.get("role") == role:
            found_liveness = True
            alive = data.get("alive")
            if alive is True:
                verdicts.add("LIVE")
            elif alive is False:
                verdicts.add("DEAD")
            else:
                verdicts.add("AMBIGUOUS")

        if source == "broker_effect" and exec_id and data.get("execution_id") == exec_id:
            found_liveness = True
            st = data.get("state")
            if st in ("running", "live"):
                verdicts.add("LIVE")
            elif st in ("failed", "cancelled", "terminal"):
                verdicts.add("DEAD")
            elif st in ("succeeded", "completed"):
                verdicts.add("DEAD")
                terminal_success = True
            else:
                verdicts.add("AMBIGUOUS")

    if not found_liveness:
        return LeaseLiveness(found_liveness=False, lease_live=False, lease_ambiguous=False)

    if "AMBIGUOUS" in verdicts or len(verdicts) > 1:
        return LeaseLiveness(
            found_liveness=True,
            lease_live=False,
            lease_ambiguous=True,
            terminal_success=terminal_success,
        )

    if verdicts == {"LIVE"}:
        return LeaseLiveness(
            found_liveness=True,
            lease_live=True,
            lease_ambiguous=False,
            terminal_success=False,
        )

    # verdicts == {"DEAD"}
    return LeaseLiveness(
        found_liveness=True,
        lease_live=False,
        lease_ambiguous=False,
        terminal_success=terminal_success,
    )


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
