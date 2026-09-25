"""ProgressObligation resolution for waiting and active orchestration lifecycle states."""
from __future__ import annotations

from typing import Any, Optional

from dev_orchestrator.storage.json_store import parse_utc, utc_now_iso


def resolve_progress_obligation(
    snapshot: dict[str, Any],
    context: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve progress obligation for a project snapshot.

    Declared external waits, startup grace, pauses, and pending owner gates are legal waits.
    """
    lifecycle = str(
        snapshot.get("lifecycle_state")
        or snapshot.get("status")
        or snapshot.get("state")
        or ""
    ).strip().upper()

    gate = snapshot.get("owner_gate")
    paused = bool(snapshot.get("paused"))
    wait_type = snapshot.get("wait_type")
    startup_grace = bool(snapshot.get("startup_grace"))

    # Explicit legal wait conditions
    is_legal_wait = bool(gate or paused or wait_type or startup_grace)

    if lifecycle == "REVIEWING":
        return {
            "state": "REVIEWING",
            "expected_event": "review_decision",
            "successor_action": "advance_decision",
            "evidence_source": "ai-reviewer.json",
            "deadline_at": snapshot.get("deadline_at"),
            "tick_budget": 5,
            "timeout_classification": "reviewer_failed",
            "auto_recovery_allowed": True,
            "escalation": "owner_gate",
            "legal_wait": is_legal_wait,
        }

    if lifecycle in {"REVIEW_FAILED", "REMEDIATE_READY"}:
        return {
            "state": lifecycle,
            "expected_event": "remediation_or_retry",
            "successor_action": "retry",
            "evidence_source": "ai-reviewer.json",
            "deadline_at": snapshot.get("deadline_at"),
            "tick_budget": 3,
            "timeout_classification": "reviewer_failed",
            "auto_recovery_allowed": True,
            "escalation": "owner_gate",
            "legal_wait": is_legal_wait,
        }

    if lifecycle in {"READY_TO_RUN", "NEXT_TASK_READY"}:
        return {
            "state": lifecycle,
            "expected_event": "worker_launch",
            "successor_action": "continue",
            "evidence_source": "transition-executor.json",
            "deadline_at": snapshot.get("deadline_at"),
            "tick_budget": 3,
            "timeout_classification": "ready_to_run_unlaunched",
            "auto_recovery_allowed": True,
            "escalation": "owner_gate",
            "legal_wait": is_legal_wait,
        }

    if lifecycle == "EXECUTING":
        return {
            "state": "EXECUTING",
            "expected_event": "worker_done",
            "successor_action": "complete_run",
            "evidence_source": "execution-lineage.json",
            "deadline_at": snapshot.get("deadline_at"),
            "tick_budget": 10,
            "timeout_classification": "agent_stalled",
            "auto_recovery_allowed": True,
            "escalation": "owner_gate",
            "legal_wait": is_legal_wait,
        }

    if lifecycle in {"DONE", "COMPLETED", "TERMINAL"}:
        return {
            "state": lifecycle,
            "expected_event": "none",
            "successor_action": "none",
            "evidence_source": "monitor_summary",
            "deadline_at": None,
            "tick_budget": 0,
            "timeout_classification": "terminal",
            "auto_recovery_allowed": False,
            "escalation": "none",
            "legal_wait": True,
            "is_terminal": True,
        }

    if lifecycle in {"IDLE", "PAUSED", "WAITING"}:
        return {
            "state": lifecycle,
            "expected_event": "resume_or_dispatch",
            "successor_action": "wait",
            "evidence_source": "monitor_summary",
            "deadline_at": None,
            "tick_budget": 0,
            "timeout_classification": "idle",
            "auto_recovery_allowed": False,
            "escalation": "owner_gate" if not is_legal_wait else "none",
            "legal_wait": True,
            "is_terminal": False,
        }

    # Generic/fallback state
    return {
        "state": lifecycle or "UNKNOWN",
        "expected_event": "lifecycle_transition",
        "successor_action": "reconcile",
        "evidence_source": "monitor_summary",
        "deadline_at": None,
        "tick_budget": 5,
        "timeout_classification": "unknown",
        "auto_recovery_allowed": False,
        "escalation": "owner_gate",
        "legal_wait": is_legal_wait,
        "is_terminal": False,
    }
