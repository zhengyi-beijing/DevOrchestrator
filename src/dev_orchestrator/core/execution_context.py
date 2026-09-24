"""Durable execution context and continuation disposition ledger.

Maintains runtime/execution-context.json (schema_version 1), preserving
bounded continuity across role transitions, daemon restarts, and
git-anchor adjustments.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.core.task_status import parse_task_status
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

EXECUTION_CONTEXT_SCHEMA_VERSION = 1

CONTINUATION_DISPOSITIONS = frozenset({
    "advance",
    "hold",
    "remediate",
    "owner_gate",
    "owner_stop",
    "terminal_success",
    "terminal_failure",
    "exhausted",
})


def _context_lock(runtime_root: Path | str) -> InterProcessFileLock:
    return InterProcessFileLock(Path(runtime_root) / "execution-context.lock")


def load_execution_contexts(runtime_root: Path | str) -> dict[str, Any]:
    """Load runtime/execution-context.json or return empty envelope."""
    context_file = Path(runtime_root) / "execution-context.json"
    data = read_json(context_file, None)
    if (
        isinstance(data, dict)
        and data.get("schema_version") == EXECUTION_CONTEXT_SCHEMA_VERSION
        and isinstance(data.get("contexts"), dict)
    ):
        return data
    return {"schema_version": EXECUTION_CONTEXT_SCHEMA_VERSION, "contexts": {}}


def get_context(runtime_root: Path | str, project_id: str) -> Optional[dict[str, Any]]:
    """Retrieve execution context for project_id, if any."""
    data = load_execution_contexts(runtime_root)
    ctx = data.get("contexts", {}).get(project_id)
    if isinstance(ctx, dict):
        return copy.deepcopy(ctx)
    return None


def update_context(
    runtime_root: Path | str,
    project_id: str,
    *,
    task_id: Optional[str] = None,
    disposition: Optional[str] = None,
    next_action: Optional[str] = None,
    git_anchor: Optional[str] = None,
    active_role: Optional[str] = None,
    idle_ticks: Optional[int] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create or update execution context for project_id under lock."""
    runtime = Path(runtime_root)
    with _context_lock(runtime):
        context_file = runtime / "execution-context.json"
        data = load_execution_contexts(runtime)
        contexts = data.setdefault("contexts", {})
        ctx = contexts.get(project_id)
        now_iso = utc_now_iso()
        if not isinstance(ctx, dict):
            ctx = {
                "schema_version": EXECUTION_CONTEXT_SCHEMA_VERSION,
                "project_id": project_id,
                "task_id": task_id,
                "disposition": disposition or "hold",
                "next_action": next_action or "none",
                "git_anchor": git_anchor,
                "active_role": active_role,
                "role_history": [],
                "recovery_snapshot": {},
                "idle_ticks": idle_ticks if idle_ticks is not None else 0,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        else:
            if task_id is not None:
                ctx["task_id"] = task_id
            if disposition is not None:
                if disposition not in CONTINUATION_DISPOSITIONS:
                    raise ValueError(f"Invalid disposition {disposition}; expected one of {sorted(CONTINUATION_DISPOSITIONS)}")
                ctx["disposition"] = disposition
            if next_action is not None:
                ctx["next_action"] = next_action
            if git_anchor is not None:
                ctx["git_anchor"] = git_anchor
            if active_role is not None:
                ctx["active_role"] = active_role if active_role != "none" else None
            if idle_ticks is not None:
                ctx["idle_ticks"] = idle_ticks
            ctx["updated_at"] = now_iso

        for k, v in kwargs.items():
            if v is not None:
                ctx[k] = v

        contexts[project_id] = ctx
        write_json(context_file, data, indent=2)
        return copy.deepcopy(ctx)


def record_role_completion(
    runtime_root: Path | str,
    project_id: str,
    role: str,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    """Record a role completion event into context role history under lock."""
    runtime = Path(runtime_root)
    with _context_lock(runtime):
        context_file = runtime / "execution-context.json"
        data = load_execution_contexts(runtime)
        contexts = data.setdefault("contexts", {})
        ctx = contexts.get(project_id)
        now_iso = utc_now_iso()
        if not isinstance(ctx, dict):
            ctx = {
                "schema_version": EXECUTION_CONTEXT_SCHEMA_VERSION,
                "project_id": project_id,
                "task_id": outcome.get("task_id"),
                "disposition": "hold",
                "next_action": "none",
                "git_anchor": outcome.get("git_anchor"),
                "active_role": None,
                "role_history": [],
                "recovery_snapshot": {},
                "idle_ticks": 0,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        history = ctx.setdefault("role_history", [])
        history.append({
            "role": role,
            "outcome": outcome,
            "completed_at": now_iso,
        })
        if ctx.get("active_role") == role:
            ctx["active_role"] = None
        ctx["updated_at"] = now_iso
        contexts[project_id] = ctx
        write_json(context_file, data, indent=2)
        return copy.deepcopy(ctx)


def set_next_action(
    runtime_root: Path | str,
    project_id: str,
    next_action: str,
    *,
    disposition: str = "advance",
    **kwargs: Any,
) -> dict[str, Any]:
    """Set next scheduled continuation action and disposition."""
    return update_context(
        runtime_root,
        project_id,
        next_action=next_action,
        disposition=disposition,
        idle_ticks=0,
        **kwargs,
    )


def clear_next_action(
    runtime_root: Path | str,
    project_id: str,
) -> dict[str, Any]:
    """Clear next scheduled action, setting next_action to 'none'."""
    return update_context(
        runtime_root,
        project_id,
        next_action="none",
        idle_ticks=0,
    )


def sync_recovery_snapshot(
    runtime_root: Path | str,
    project_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Synchronize latest recovery snapshot into execution context."""
    runtime = Path(runtime_root)
    with _context_lock(runtime):
        context_file = runtime / "execution-context.json"
        data = load_execution_contexts(runtime)
        contexts = data.setdefault("contexts", {})
        ctx = contexts.get(project_id)
        now_iso = utc_now_iso()
        if not isinstance(ctx, dict):
            ctx = {
                "schema_version": EXECUTION_CONTEXT_SCHEMA_VERSION,
                "project_id": project_id,
                "task_id": snapshot.get("task_id"),
                "disposition": "advance",
                "next_action": "none",
                "git_anchor": snapshot.get("git_anchor"),
                "active_role": None,
                "role_history": [],
                "recovery_snapshot": copy.deepcopy(snapshot),
                "idle_ticks": 0,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        else:
            ctx["recovery_snapshot"] = copy.deepcopy(snapshot)
            ctx["updated_at"] = now_iso
        contexts[project_id] = ctx
        write_json(context_file, data, indent=2)
        return copy.deepcopy(ctx)


def context_is_stale(
    context: dict[str, Any],
    current_head: Optional[str],
    current_task_id: Optional[str],
) -> bool:
    """Return True if git anchor or task ID diverged from repository truth."""
    if not isinstance(context, dict):
        return True
    ctx_anchor = context.get("git_anchor")
    if ctx_anchor and current_head and ctx_anchor != current_head:
        return True
    ctx_task = context.get("task_id")
    if ctx_task and current_task_id and ctx_task != current_task_id:
        return True
    return False


def resolve_next_action(
    snapshot: dict[str, Any],
    context: Optional[dict[str, Any]] = None,
) -> tuple[str, str]:
    """Pure resolution of next continuation action and disposition from snapshot and context."""
    if not isinstance(snapshot, dict):
        return "none", "hold"

    owner = snapshot.get("owner_control") or {}
    if bool(owner.get("paused")):
        return "none", "owner_stop"

    gate = snapshot.get("gate")
    if isinstance(gate, dict) and gate.get("state") == "owner_gate":
        return "none", "owner_gate"

    parsed_status = parse_task_status(snapshot.get("next_status"))
    if parsed_status.is_completed():
        return "none", "terminal_success"

    # Check for actively executing roles
    worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
    if worker.get("kind") == "task" and worker.get("state") in {"starting", "running"}:
        return "none", "hold"

    planner = snapshot.get("planner") if isinstance(snapshot.get("planner"), dict) else {}
    if planner.get("state") in {"planning", "reviewing", "applying", "remediating"}:
        return "none", "hold"

    reviewer = snapshot.get("reviewer") if isinstance(snapshot.get("reviewer"), dict) else {}
    if reviewer.get("state") in {"launching", "running"}:
        return "none", "hold"

    if isinstance(context, dict) and context.get("disposition") == "hold":
        ctx_act = context.get("next_action") or "none"
        if ctx_act == "none":
            if parsed_status.is_pending_design():
                ctx_act = "plan"
            elif parsed_status.is_ready_to_run():
                ctx_act = "execute"
        return ctx_act, "hold"

    lifecycle = str(snapshot.get("lifecycle_state") or snapshot.get("state") or "").upper()
    if lifecycle == "WAITING_REVIEW" or (worker.get("state") == "completed" and not reviewer.get("state")):
        return "review", "advance"

    if parsed_status.is_pending_design():
        return "plan", "advance"

    if parsed_status.is_ready_to_run() or lifecycle == "READY_TO_RUN":
        return "execute", "advance"

    if isinstance(context, dict):
        ctx_action = context.get("next_action")
        ctx_disp = context.get("disposition")
        if ctx_action and ctx_action != "none" and ctx_disp:
            return ctx_action, ctx_disp

    return "none", "hold"


def classify_continuation(
    snapshot: dict[str, Any],
    intent: Optional[dict[str, Any]],
    blockers: list[Any],
    context: Optional[dict[str, Any]] = None,
    project_config: Optional[dict[str, Any]] = None,
) -> tuple[str, str, str]:
    """Classify project state into (disposition, next_action, reason)."""
    # 1. Owner control & gate blockers
    owner_gate_blocker = next(
        (b for b in blockers if getattr(b, "code", "") in {"OWNER_GATE_PRESENT", "OWNER_PAUSED"} or getattr(b, "failure_class", "") == "owner_gate"),
        None,
    )
    if owner_gate_blocker is not None:
        return "owner_gate", "none", getattr(owner_gate_blocker, "code", "OWNER_GATE")

    # 2. Terminal task status
    parsed_status = parse_task_status(snapshot.get("next_status") if isinstance(snapshot, dict) else None)
    if parsed_status.is_completed():
        return "terminal_success", "none", "task status is completed"

    # 3. Active role execution: hold without burning recovery budget
    worker = snapshot.get("worker") if isinstance(snapshot, dict) and isinstance(snapshot.get("worker"), dict) else {}
    if worker.get("kind") == "task" and worker.get("state") in {"starting", "running"}:
        return "hold", "none", "worker task is currently running"

    planner = snapshot.get("planner") if isinstance(snapshot, dict) and isinstance(snapshot.get("planner"), dict) else {}
    if planner.get("state") in {"planning", "reviewing", "applying", "remediating"}:
        return "hold", "none", f"planner is currently {planner.get('state')}"

    reviewer = snapshot.get("reviewer") if isinstance(snapshot, dict) and isinstance(snapshot.get("reviewer"), dict) else {}
    if reviewer.get("state") in {"launching", "running"}:
        return "hold", "none", "reviewer is currently active"

    # 4. Blockers classification
    if blockers:
        top = blockers[0]
        f_class = getattr(top, "failure_class", "")
        code = getattr(top, "code", "")
        if f_class == "terminal":
            return "terminal_failure", "none", f"terminal blocker: {code}"

        roles = project_config.get("ai_roles") if isinstance(project_config, dict) else None
        planner_config = roles.get("planner") if isinstance(roles, dict) else None
        planning_ready = isinstance(planner_config, dict) and planner_config.get("enabled") is True
        if parsed_status.is_pending_design() and planning_ready:
            return "advance", "plan", "pending design task advances to planning"

        if f_class == "lifecycle" or code == "READINESS_NOT_READY_TO_RUN":
            return "lifecycle_hold", "none", f"task lifecycle state not ready to run: {getattr(top, 'observed', code)}"
        if f_class == "transient_infrastructure":
            return "transient_infrastructure", "none", f"transient blocker: {code}"
        if f_class in {"recoverable_orchestration", "readiness", "remediation", "execution_policy"} or code in {
            "PROJECT_NOT_REGISTERED", "ORPHANED_PROJECT_STATE", "READINESS_TOKEN_UNSTRUCTURED", "READINESS_TASK_ID_MISMATCH"
        }:
            return "remediate", "remediate", f"recoverable blocker: {code}"

    # 5. Normal forward progression
    action, disp = resolve_next_action(snapshot, context)
    return disp, action, f"resolved action {action} with disposition {disp}"
