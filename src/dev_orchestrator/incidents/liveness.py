"""Three-role liveness resolution across worker, planner, and reviewer."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.platform.process import is_pid_alive
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now


def _check_worker_liveness(
    runtime_root: Path,
    project_id: str,
    snapshot: dict[str, Any],
    executor: Any,
) -> dict[str, Any]:
    worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
    pid = worker.get("pid")
    w_state = str(worker.get("state") or "").lower()
    sources_inspected = 0

    # Check lineage store
    lineage_file = runtime_root / "execution-lineage.json"
    if lineage_file.is_file():
        try:
            lineage_data = read_json(lineage_file, None)
            if not isinstance(lineage_data, dict):
                return {"alive": None, "state": "unknown", "source": "lineage_corrupt", "details": {}}
            sources_inspected += 1
            records = lineage_data.get("records") if isinstance(lineage_data.get("records"), dict) else {}
            for rec in records.values():
                if isinstance(rec, dict) and str(rec.get("project_id") or "") == project_id:
                    if rec.get("lifecycle_phase") in {"obligated", "launched", "running"}:
                        rec_pid = rec.get("pid")
                        if rec_pid and is_pid_alive(rec_pid):
                            return {"alive": True, "state": "running", "source": "lineage_active", "details": rec}
        except Exception as exc:
            return {"alive": None, "state": "unknown", "source": f"lineage_error: {exc}", "details": {}}

    # Check executor state
    if executor is not None:
        try:
            exec_data = None
            if isinstance(executor, dict):
                exec_data = executor
            elif hasattr(executor, "state") and callable(getattr(executor, "state")):
                exec_data = executor.state()
            else:
                return {"alive": None, "state": "unknown", "source": "executor_unreadable", "details": {}}

            if isinstance(exec_data, dict):
                sources_inspected += 1
                executions = exec_data.get("executions") if isinstance(exec_data.get("executions"), dict) else {}
                for row in executions.values():
                    if isinstance(row, dict) and str(row.get("project_id") or "") == project_id:
                        if row.get("state") in {"launching", "running"}:
                            r_pid = row.get("pid")
                            if r_pid and is_pid_alive(r_pid):
                                return {"alive": True, "state": "running", "source": "executor_active", "details": row}
                            elif not r_pid:
                                return {"alive": True, "state": str(row.get("state")), "source": "executor_active_no_pid", "details": row}
            else:
                return {"alive": None, "state": "unknown", "source": "executor_invalid_shape", "details": {}}
        except Exception as exc:
            return {"alive": None, "state": "unknown", "source": f"executor_error: {exc}", "details": {}}

    # Check snapshot worker process
    if isinstance(snapshot, dict) and isinstance(snapshot.get("worker"), dict) and snapshot.get("worker"):
        sources_inspected += 1
        if w_state in {"running", "starting"}:
            if pid and is_pid_alive(pid):
                return {"alive": True, "state": w_state, "source": "snapshot_pid_alive", "details": worker}
            if pid and not is_pid_alive(pid):
                return {"alive": False, "state": "dead", "source": "snapshot_pid_dead", "details": worker}

    if sources_inspected == 0:
        return {"alive": None, "state": "unknown", "source": "no_readable_sources", "details": {}}

    return {"alive": False, "state": "idle", "source": "no_active_worker", "details": worker}


def _check_coordinator_role(
    runtime_root: Path,
    project_id: str,
    role_name: str,
    ledger_filename: str,
    coordinator: Any,
) -> dict[str, Any]:
    # 1. Thread inspection on coordinator
    coord_alive: Optional[bool] = None
    if coordinator is not None:
        live_fn = getattr(coordinator, "has_live_role", None)
        if callable(live_fn):
            try:
                coord_alive = live_fn(project_id)
            except Exception:
                coord_alive = None

    if coord_alive is True:
        return {"alive": True, "state": "running", "source": f"{role_name}_thread_alive", "details": {}}

    # 2. Durable ledger inspection
    ledger_path = runtime_root / ledger_filename
    if not ledger_path.is_file():
        # Ledger not found; if coordinator is present and reported False, dead; else unknown
        if coordinator is not None and coord_alive is False:
            return {"alive": False, "state": "idle", "source": f"{role_name}_no_ledger_and_thread_idle", "details": {}}
        return {"alive": None, "state": "unknown", "source": f"{role_name}_ledger_missing", "details": {}}

    try:
        data = read_json(ledger_path, None)
    except Exception as exc:
        return {"alive": None, "state": "unknown", "source": f"{role_name}_ledger_corrupt: {exc}", "details": {}}

    if not isinstance(data, dict):
        return {"alive": None, "state": "unknown", "source": f"{role_name}_ledger_invalid_shape", "details": {}}

    records_key = "plans" if "planner" in role_name else "reviews"
    records = data.get(records_key)
    if not isinstance(records, dict):
        records = {}

    matching = [
        rec for rec in records.values()
        if isinstance(rec, dict) and str(rec.get("project_id") or "") == project_id
    ]

    active_states = {"launching", "running", "remediating"}
    active_matches = [m for m in matching if m.get("state") in active_states]

    if not active_matches:
        # Every source readable and negative: coordinator must be present AND report False
        if coordinator is not None and coord_alive is False:
            return {"alive": False, "state": "idle", "source": f"{role_name}_idle_in_ledger", "details": {}}
        return {"alive": None, "state": "unknown", "source": f"{role_name}_coordinator_thread_unavailable", "details": {}}

    latest_active = max(
        active_matches,
        key=lambda m: str(m.get("updated_at") or m.get("started_at") or m.get("requested_at") or ""),
    )

    # If coordinator explicitly verified no thread is alive for this active record:
    if coordinator is not None and coord_alive is False:
        # Thread is dead despite active ledger status -> dead/abandoned
        return {"alive": False, "state": "abandoned", "source": f"{role_name}_thread_dead_active_record", "details": latest_active}

    if coord_alive is None:
        return {"alive": None, "state": "unknown", "source": f"{role_name}_active_record_thread_unknown", "details": latest_active}

    return {"alive": True, "state": str(latest_active.get("state")), "source": f"{role_name}_active", "details": latest_active}


def resolve_role_liveness(
    runtime_root: Path | str,
    project_id: str,
    snapshot: dict[str, Any],
    planner: Any = None,
    reviewer: Any = None,
    executor: Any = None,
    policy: dict[str, Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Resolve liveness across worker, planner, and reviewer roles.

    Returns:
        worker: dict
        planner: dict
        reviewer: dict
        all_dead: bool (True only when worker, planner, reviewer are all False)
        any_unknown: bool (True if any role is None)
    """
    runtime = Path(runtime_root)
    worker_res = _check_worker_liveness(runtime, project_id, snapshot, executor)
    planner_res = _check_coordinator_role(runtime, project_id, "planner", "ai-planner.json", planner)
    reviewer_res = _check_coordinator_role(runtime, project_id, "reviewer", "ai-reviewer.json", reviewer)

    w_alive = worker_res.get("alive")
    p_alive = planner_res.get("alive")
    r_alive = reviewer_res.get("alive")

    all_dead = (w_alive is False) and (p_alive is False) and (r_alive is False)
    any_unknown = (w_alive is None) or (p_alive is None) or (r_alive is None)
    any_alive = (w_alive is True) or (p_alive is True) or (r_alive is True)

    return {
        "worker": worker_res,
        "planner": planner_res,
        "reviewer": reviewer_res,
        "all_dead": all_dead,
        "any_unknown": any_unknown,
        "any_alive": any_alive,
    }
