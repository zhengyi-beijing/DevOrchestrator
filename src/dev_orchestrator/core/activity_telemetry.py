"""Additive activity and telemetry resolution for DevOrchestrator.

Provides real-time visibility into orchestrator vs implementation activity,
active roles, continuation disposition, and task state authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.core.execution_context import get_context, resolve_next_action
from dev_orchestrator.core.execution_intent import get_active_intent
from dev_orchestrator.core.task_status import parse_task_status


def resolve_activity_telemetry(
    snapshot: dict[str, Any],
    runtime_root: Path | str,
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve unified additive activity telemetry object."""
    if not isinstance(snapshot, dict):
        snapshot = {}
    pid = str(project_id or snapshot.get("project_id") or snapshot.get("id") or "").strip()

    # Worker & code execution state
    worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
    w_state = worker.get("state")
    code_exec = bool(worker.get("process_alive")) or w_state in {"launching", "running"}

    # Active AI role
    active_role: Optional[str] = None
    if code_exec:
        active_role = "worker"
    else:
        planner = snapshot.get("planner") if isinstance(snapshot.get("planner"), dict) else {}
        p_state = planner.get("state")
        if p_state in {"planning", "reviewing", "applying", "remediating"}:
            active_role = "planner"
        else:
            reviewer = snapshot.get("reviewer") if isinstance(snapshot.get("reviewer"), dict) else {}
            r_state = reviewer.get("state")
            if r_state in {"launching", "running"}:
                active_role = "reviewer"

    # Stage
    raw_stage = str(snapshot.get("lifecycle_state") or snapshot.get("state") or "IDLE").lower()
    stage_map = {
        "planning": "planning",
        "reviewing_plan": "planning",
        "remediating_plan": "planning",
        "applying_plan": "planning",
        "executing": "executing",
        "worker_running": "executing",
        "reviewing": "reviewing",
        "waiting_review": "waiting_review",
        "owner_gate": "owner_gate",
        "ready_to_run": "ready_to_run",
        "idle": "idle",
        "completed": "completed",
        "blocked": "blocked",
    }
    stage = stage_map.get(raw_stage, raw_stage)

    # Context & Intent
    context = get_context(runtime_root, pid) if pid else None
    intent = get_active_intent(runtime_root, pid) if pid else None

    disposition = context.get("disposition") if isinstance(context, dict) else "hold"
    next_action = context.get("next_action") if isinstance(context, dict) else "none"

    if next_action in {"none", None}:
        resolved_action, resolved_disp = resolve_next_action(snapshot, context)
        next_action = resolved_action
        if context is None:
            disposition = resolved_disp

    intent_state = str(intent.get("state")) if isinstance(intent, dict) and intent.get("state") else "none"

    # Task state source
    readiness = snapshot.get("readiness")
    if isinstance(readiness, dict):
        if readiness.get("source") == "structured" and readiness.get("valid"):
            task_state_source = "structured"
        else:
            task_state_source = "markdown_fallback"
    else:
        task_state_source = "markdown_fallback"

    # Orchestrator activity description
    if active_role == "worker":
        orch_activity = "monitoring_worker"
    elif active_role == "planner":
        orch_activity = "planning_task"
    elif active_role == "reviewer":
        orch_activity = "reviewing_task"
    elif disposition == "advance":
        orch_activity = f"dispatching_{next_action}"
    elif disposition == "remediate":
        orch_activity = "remediating_blocker"
    elif disposition == "owner_gate":
        orch_activity = "waiting_owner_gate"
    elif disposition == "terminal_success":
        orch_activity = "terminal_closure"
    else:
        orch_activity = "idle"

    return {
        "stage": stage,
        "active_role": active_role,
        "worker_state": w_state,
        "code_execution": code_exec,
        "orchestrator_activity": orch_activity,
        "next_action": next_action,
        "intent_state": intent_state,
        "disposition": disposition,
        "task_state_source": task_state_source,
    }
