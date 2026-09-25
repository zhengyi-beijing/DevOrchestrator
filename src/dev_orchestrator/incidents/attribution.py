"""Owner intervention attribution and classification."""
from __future__ import annotations

from typing import Any


def classify_owner_action(
    action: str,
    command_record: dict[str, Any] | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> str:
    """Classify an owner command as NEW_INFORMATION, CONTROL_ONLY, or EXPLICIT_STOP.

    A command is CONTROL_ONLY if it supplied no new information (such as identical parameters,
    branch, HEAD, and gate decisions) but restored forward progress.
    """
    act = str(action or "").lower().strip()
    if act in {"stop", "pause"}:
        return "EXPLICIT_STOP"

    if act not in {"continue", "retry", "rereview", "reconcile", "restart"}:
        return "NEW_INFORMATION"

    if not isinstance(before, dict) or not before:
        return "NEW_INFORMATION"

    record = command_record or {}
    target = record.get("target") if isinstance(record.get("target"), dict) else {}
    expected = record.get("expected") if isinstance(record.get("expected"), dict) else {}

    # Check for parameter modifications or explicit decisions
    if record.get("parameters") or record.get("prompt_override") or target.get("decision"):
        return "NEW_INFORMATION"

    # Compare Git anchors if present
    before_git = before.get("git") if isinstance(before.get("git"), dict) else {}
    before_head = before.get("head") or before_git.get("head")
    before_branch = before.get("branch") or before_git.get("branch")

    exp_head = expected.get("head") or (expected.get("git") or {}).get("head")
    exp_branch = expected.get("branch") or (expected.get("git") or {}).get("branch")

    if exp_head and before_head and exp_head != before_head:
        return "NEW_INFORMATION"
    if exp_branch and before_branch and exp_branch != before_branch:
        return "NEW_INFORMATION"

    # Compare gate decisions
    before_gate = before.get("gate_id")
    target_gate = target.get("gate_id")
    if target.get("decision") or (target_gate and before_gate and target_gate != before_gate):
        return "NEW_INFORMATION"

    # Check if 'after' shows restored progress
    if isinstance(after, dict) and after:
        after_state = str(after.get("lifecycle_state") or after.get("state") or after.get("status") or "").upper()
        before_state = str(before.get("lifecycle_state") or before.get("state") or before.get("status") or "").upper()
        # Progress restored if state changed forward, active execution launched, or progress made
        if after.get("active_execution") or after.get("worker_running") or (after_state and after_state != before_state):
            return "CONTROL_ONLY"
        if after.get("restored_progress") or after.get("progress_restored"):
            return "CONTROL_ONLY"

    # Default to CONTROL_ONLY if identical inputs were given for an unattended recovery command
    return "CONTROL_ONLY"
