"""Project orchestration lifecycle overlay for UI/status projection."""
from __future__ import annotations

import copy
from typing import Any


def _latest(rows: Any, project_id: str) -> dict[str, Any] | None:
    if not isinstance(rows, dict):
        return None
    matches = [
        row for row in rows.values()
        if isinstance(row, dict) and str(row.get("project_id") or "") == project_id
    ]
    if not matches:
        return None
    def key(row: dict[str, Any]) -> tuple[str, str]:
        stamp = ""
        for name in (
            "completed_at", "review_completed_at", "worker_launched_at",
            "ready_at", "planner_completed_at", "recovered_at", "started_at",
        ):
            value = row.get(name)
            if isinstance(value, str) and value:
                stamp = value
                break
        return stamp, str(row.get("plan_id") or row.get("review_id") or "")
    return max(matches, key=key)


def overlay_orchestration_lifecycle(
    summary: Any, *, planner_state: Any = None, reviewer_state: Any = None,
) -> Any:
    """Add lifecycle_state without changing raw monitor state semantics."""
    projected = copy.deepcopy(summary)
    if not isinstance(projected, dict) or not isinstance(projected.get("projects"), list):
        return projected
    plans = planner_state.get("plans") if isinstance(planner_state, dict) else None
    reviews = reviewer_state.get("reviews") if isinstance(reviewer_state, dict) else None
    for snapshot in projected["projects"]:
        if not isinstance(snapshot, dict):
            continue
        project_id = str(snapshot.get("project_id") or "")
        base = str(snapshot.get("state") or "UNKNOWN")
        lifecycle = base
        plan = _latest(plans, project_id)
        review = _latest(reviews, project_id)

        if base == "WORKER_RUNNING":
            lifecycle = "EXECUTING"
        elif isinstance(plan, dict) and plan.get("state") == "planning":
            lifecycle = "PLANNING"
        elif isinstance(plan, dict) and plan.get("state") == "reviewing":
            lifecycle = "REVIEWING_PLAN"
        elif isinstance(plan, dict) and plan.get("state") == "applying":
            lifecycle = "APPLYING_PLAN"
        elif isinstance(plan, dict) and plan.get("state") == "ready":
            lifecycle = "READY_TO_RUN"
        if isinstance(review, dict):
            review_state = str(review.get("state") or "")
            if lifecycle != "EXECUTING" and review_state in {"launching", "running"}:
                lifecycle = "REVIEWING"
            elif lifecycle != "EXECUTING" and review_state == "recovery_required":
                lifecycle = "RECOVERY_REQUIRED"
            elif lifecycle != "EXECUTING" and review_state == "failed":
                lifecycle = "REVIEW_FAILED"
            elif lifecycle != "EXECUTING" and review_state == "completed" and review.get("decision") == "owner_gate":
                lifecycle = "OWNER_GATE"
        if isinstance(plan, dict):
            plan_state = str(plan.get("state") or "")
            if plan_state == "recovery_required" and lifecycle == base:
                lifecycle = "RECOVERY_REQUIRED"
            elif plan_state == "owner_gate" and lifecycle == base:
                lifecycle = "OWNER_GATE"
            elif plan_state == "failed" and lifecycle == base:
                lifecycle = "PLAN_FAILED"
        snapshot["lifecycle_state"] = lifecycle
    return projected
