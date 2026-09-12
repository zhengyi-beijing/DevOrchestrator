"""Project-local operational status mirror for managed repositories.

The authoritative orchestration state remains under DevOrchestrator runtime.
This module writes a compact derived mirror to ``<repo>/.devorch/status.json``
so humans and tools can inspect one file without reconstructing the ledgers.
"""
from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.storage.json_store import (
    parse_utc,
    read_json,
    utc_now,
    utc_now_iso,
    write_json,
)

STATUS_RELATIVE_PATH = Path(".devorch") / "status.json"
STATUS_SCHEMA_VERSION = 1


def _watchdog_view(runtime: Path, project_id: str) -> Optional[dict[str, Any]]:
    state_file = runtime / "watchdog.json"
    if not state_file.is_file():
        return None

    data = read_json(state_file, None)
    if not isinstance(data, dict):
        return {
            "schema_version": 1,
            "state": "degraded",
            "degraded_reason": "unreadable watchdog state",
        }

    if data.get("degraded"):
        return {
            "schema_version": 1,
            "state": "degraded",
            "degraded_reason": data.get("degraded_reason") or "coordinator state degraded",
        }

    quarantined = data.get("quarantined_projects")
    if isinstance(quarantined, dict) and project_id in quarantined:
        return {
            "schema_version": 1,
            "state": "degraded",
            "degraded_reason": quarantined[project_id],
        }

    projects = data.get("projects")
    prow = projects.get(project_id) if isinstance(projects, dict) else None
    if not isinstance(prow, dict):
        return None

    now_dt = utc_now()
    sig_sources = prow.get("signal_sources") if isinstance(prow.get("signal_sources"), dict) else {}
    ev_state = prow.get("activity_evidence", "available")
    ev_reason = prow.get("activity_evidence_reason")

    attempts_dict = prow.get("attempts") if isinstance(prow.get("attempts"), dict) else {}
    total_attempts = len(attempts_dict)

    stall = prow.get("stall") if isinstance(prow.get("stall"), dict) else None
    run_scope_key = stall.get("run_scope_key") if stall else None
    attempt_counts = prow.get("attempt_counts") if isinstance(prow.get("attempt_counts"), dict) else {}
    attempts_this_run = int(attempt_counts.get(run_scope_key, 0)) if run_scope_key else 0

    is_diagnosing = any(
        isinstance(a, dict) and a.get("state") == "running"
        for a in attempts_dict.values()
    )

    cooldown_until_str = prow.get("cooldown_until")
    cooldown_until_dt = parse_utc(cooldown_until_str)
    in_cooldown = cooldown_until_dt is not None and now_dt < cooldown_until_dt

    owner_gate = prow.get("owner_gate")

    if prow.get("disabled"):
        wd_state = "disabled"
    elif ev_state != "available":
        wd_state = "evidence_unavailable"
    elif owner_gate:
        wd_state = "owner_gate"
    elif is_diagnosing:
        wd_state = "diagnosing"
    elif in_cooldown:
        wd_state = "cooldown"
    elif stall and stall.get("no_progress_seconds") is not None:
        thresh = float(stall.get("threshold_minutes", 15)) * 60.0
        if float(stall["no_progress_seconds"]) >= thresh:
            wd_state = "stalled"
        else:
            wd_state = "ok"
    else:
        wd_state = "ok"

    last_recovery = None
    recoveries = [
        a.get("recovery")
        for a in attempts_dict.values()
        if isinstance(a, dict) and isinstance(a.get("recovery"), dict)
    ]
    if recoveries:
        rec = max(
            recoveries,
            key=lambda r: str(r.get("resolved_at") or r.get("requested_at") or r.get("reserved_at") or "")
        )
        last_recovery = {
            "action": rec.get("action"),
            "state": rec.get("state"),
            "reason": rec.get("reason"),
            "command_id": rec.get("command_id"),
        }

    excluded_paths = sig_sources.get("excluded_paths") or []
    excluded_count = sig_sources.get("excluded_count", len(excluded_paths))
    newest_path = None
    sources_dict = sig_sources.get("sources") if isinstance(sig_sources.get("sources"), dict) else {}
    for s_val in sources_dict.values():
        if isinstance(s_val, dict) and s_val.get("path"):
            newest_path = s_val["path"]

    snapshot_activity = read_json(runtime / "projects" / f"{project_id}.json", {}).get("activity", {})
    runtime_scope = (
        snapshot_activity.get("watchdog_safe", {}).get("runtime_scope")
        if isinstance(snapshot_activity, dict)
        else "unknown"
    )

    return {
        "schema_version": 1,
        "state": wd_state,
        "last_progress_at": prow.get("last_progress_at"),
        "no_progress_seconds": stall.get("no_progress_seconds") if stall else None,
        "threshold_minutes": stall.get("threshold_minutes") if stall else prow.get("no_progress_threshold_minutes", 15),
        "activity_evidence": {
            "state": ev_state,
            "reason": ev_reason,
            "newest_path": newest_path,
            "excluded_count": excluded_count,
            "runtime_scope": runtime_scope,
        },
        "last_diagnosis": prow.get("last_diagnosis"),
        "evidence_hash": prow.get("last_evidence_hash"),
        "attempts": total_attempts,
        "attempts_this_run": attempts_this_run,
        "cooldown_until": cooldown_until_str,
        "last_recovery": last_recovery,
        "degraded_reason": prow.get("last_error"),
    }


def _git_path(repo: Path, relative: str) -> Optional[Path]:
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), "rev-parse", "--git-path", relative],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10, check=False, **hidden_subprocess_kwargs(),
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    path = Path(proc.stdout.strip())
    return path if path.is_absolute() else repo / path


def ensure_status_is_git_ignored(repo: Path) -> None:
    """Locally exclude ``.devorch/`` without modifying tracked project files."""
    exclude = _git_path(repo, "info/exclude")
    if exclude is None:
        raise RuntimeError("repository git exclude path is unavailable")
    exclude.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    except OSError as exc:
        raise RuntimeError("cannot read git exclude: {0}".format(exc)) from exc
    lines = {line.strip() for line in text.splitlines() if line.strip()}
    if ".devorch/" in lines or "/.devorch/" in lines:
        return
    prefix = "" if not text or text.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(prefix + ".devorch/\n")


def _latest_timestamp(record: dict[str, Any]) -> str:
    for key in ("completed_at", "consumed_at", "dispatched_at", "prepared_at", "started_at", "recorded_at"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _latest_decision(runtime: Path, project_id: str) -> Optional[dict[str, Any]]:
    data = read_json(runtime / "websol-decisions.json", {})
    decisions = data.get("decisions") if isinstance(data, dict) else None
    if not isinstance(decisions, dict):
        return None
    rows = [r for r in decisions.values() if isinstance(r, dict) and r.get("project_id") == project_id]
    return max(rows, key=_latest_timestamp) if rows else None


def _latest_actuation(runtime: Path, project_id: str) -> Optional[dict[str, Any]]:
    data = read_json(runtime / "transition-executor.json", {})
    executions = data.get("executions") if isinstance(data, dict) else None
    if not isinstance(executions, dict):
        return None
    rows = [r for r in executions.values() if isinstance(r, dict) and r.get("project_id") == project_id]
    return max(rows, key=_latest_timestamp) if rows else None


def _latest_dispatch(runtime: Path, project_id: str) -> Optional[dict[str, Any]]:
    data = read_json(runtime / "dispatcher-state.json", {})
    worker_done = data.get("worker_done") if isinstance(data, dict) else None
    project = worker_done.get(project_id) if isinstance(worker_done, dict) else None
    occurrences = project.get("occurrences") if isinstance(project, dict) else None
    if not isinstance(occurrences, dict):
        return None
    rows = [dict(r, run_id=run_id) for run_id, r in occurrences.items() if isinstance(r, dict)]
    return max(rows, key=_latest_timestamp) if rows else None


def _safe_git(snapshot: dict[str, Any]) -> dict[str, Any]:
    git = snapshot.get("git")
    if not isinstance(git, dict):
        return {}
    return {key: git.get(key) for key in ("branch", "head", "dirty", "changed_entries")}


def _safe_worker(snapshot: dict[str, Any]) -> dict[str, Any]:
    worker = snapshot.get("worker")
    if not isinstance(worker, dict):
        return {}
    return {key: worker.get(key) for key in (
        "kind", "state", "process_alive", "pid", "started_at", "updated_at", "exit_code", "command", "model"
    )}


def build_project_status(
    snapshot: dict[str, Any], runtime_root: Path | str, *, phase: str,
    daemon_state: str, pid: Optional[int], last_error: Optional[str] = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root)
    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    decision = _latest_decision(runtime, project_id)
    dispatch = _latest_dispatch(runtime, project_id)
    actuation = _latest_actuation(runtime, project_id)
    web_sol = None
    if decision is not None:
        web_sol = {
            "state": "consumed", "request_id": decision.get("request_id"),
            "decision": decision.get("decision"),
            "disposition": decision.get("disposition"),
            "next_action": decision.get("next_action"),
            "consumed_at": decision.get("consumed_at"),
        }
    elif dispatch is not None:
        web_sol = {
            "state": dispatch.get("state"), "request_id": dispatch.get("request_id"),
            "delivery_state": dispatch.get("delivery_state"),
            "run_id": dispatch.get("run_id"),
        }
    actuation_view = None
    if actuation is not None:
        actuation_view = {key: actuation.get(key) for key in (
            "source_request_id", "source_kind", "source_task_id", "task_id",
            "backend_id", "state", "pid", "started_at", "completed_at", "exit_code", "reason"
        )}
    result = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "project_id": project_id,
        "updated_at": utc_now_iso(),
        "daemon": {"state": daemon_state, "pid": pid, "last_error": last_error},
        "phase": phase,
        "status": snapshot.get("lifecycle_state") or snapshot.get("state"),
        "monitor_status": snapshot.get("state"),
        "orchestration_ready": bool(snapshot.get("orchestration_ready")),
        "task_id": telemetry.get("task_id"),
        "run_id": telemetry.get("run_id"),
        "stage_id": snapshot.get("stage_id"),
        "next_title": snapshot.get("next_title"),
        "git": _safe_git(snapshot),
        "worker": _safe_worker(snapshot),
        "web_sol": web_sol,
        "actuation": actuation_view,
    }
    if "project_context" in snapshot:
        result["project_context"] = copy.deepcopy(snapshot["project_context"])
    watchdog_view = _watchdog_view(runtime, project_id)
    if watchdog_view is not None:
        result["watchdog"] = watchdog_view
    return result


def write_project_status(snapshot: dict[str, Any], runtime_root: Path | str, **kwargs: Any) -> Path:
    repo = Path(str(snapshot.get("repo_path") or snapshot.get("root") or ""))
    if not repo.is_dir():
        raise RuntimeError("project repo_path is unavailable for status mirror")
    ensure_status_is_git_ignored(repo)
    target = repo / STATUS_RELATIVE_PATH
    write_json(target, build_project_status(snapshot, runtime_root, **kwargs), indent=2)
    return target


def write_project_statuses(
    summary: Any, runtime_root: Path | str, *, phase: str,
    daemon_state: str, pid: Optional[int], last_error: Optional[str] = None,
) -> list[Path]:
    if not isinstance(summary, dict) or not isinstance(summary.get("projects"), list):
        return []
    written: list[Path] = []
    for snapshot in summary["projects"]:
        if not isinstance(snapshot, dict):
            continue
        written.append(write_project_status(
            snapshot, runtime_root, phase=phase, daemon_state=daemon_state,
            pid=pid, last_error=last_error,
        ))
    return written


def write_execution_status(record: dict[str, Any], runtime_root: Path | str) -> Optional[Path]:
    repo_text = record.get("repo_path")
    project_id = record.get("project_id")
    if not isinstance(repo_text, str) or not repo_text or not isinstance(project_id, str) or not project_id:
        return None
    repo = Path(repo_text)
    if not repo.is_dir():
        return None
    ensure_status_is_git_ignored(repo)
    target = repo / STATUS_RELATIVE_PATH
    current = read_json(target, {})
    if not isinstance(current, dict):
        current = {}
    current.update({
        "schema_version": STATUS_SCHEMA_VERSION, "project_id": project_id,
        "updated_at": utc_now_iso(), "phase": "worker",
        "task_id": record.get("task_id"),
        "actuation": {key: record.get(key) for key in (
            "source_request_id", "source_kind", "source_task_id", "task_id", "backend_id",
            "state", "pid", "started_at", "completed_at", "exit_code", "reason"
        )},
    })
    run_state = record.get("state")
    if run_state in {"launching", "running"}:
        current["status"] = "EXECUTING"
        current["lifecycle_state"] = "EXECUTING"
    elif run_state == "completed":
        current["status"] = "WAITING_REVIEW"
        current["lifecycle_state"] = "WAITING_REVIEW"
    elif run_state == "failed":
        current["status"] = "WORKER_FAILED"
        current["lifecycle_state"] = "WORKER_FAILED"
    current["worker"] = {
        "kind": "task", "state": run_state,
        "process_alive": run_state in {"launching", "running"},
        "pid": record.get("pid"), "started_at": record.get("started_at"),
        "updated_at": record.get("completed_at") or record.get("started_at"),
        "exit_code": record.get("exit_code"),
        "command": "managed {0}".format(record.get("backend_id") or "worker"),
    }
    if record.get("engine") == "aibroker":
        current["worker"]["engine"] = "aibroker"
        current["broker_execution"] = {
            "broker_request_id": record.get("broker_request_id"),
            "role_run_id": record.get("role_run_id"),
            "state": run_state,
            "started_at": record.get("started_at"),
            "engine": "aibroker",
        }
    write_json(target, current, indent=2)
    return target


def write_review_status(record: dict[str, Any], runtime_root: Path | str, repo_path: Path | str) -> Optional[Path]:
    repo = Path(repo_path)
    if not repo.is_dir():
        return None
    ensure_status_is_git_ignored(repo)
    target = repo / STATUS_RELATIVE_PATH
    current = read_json(target, {})
    if not isinstance(current, dict):
        current = {}
    state = record.get("state")
    project_id = str(record.get("project_id") or current.get("project_id") or "")
    current.update({
        "schema_version": STATUS_SCHEMA_VERSION,
        "project_id": project_id,
        "updated_at": utc_now_iso(),
        "phase": "reviewer",
        "task_id": record.get("task_id") or current.get("task_id"),
    })
    if state in {"launching", "running"}:
        current["status"] = "REVIEWING"
        current["lifecycle_state"] = "REVIEWING"
    elif state == "completed":
        decision = record.get("decision")
        if decision == "owner_gate":
            current["status"] = "OWNER_GATE"
            current["lifecycle_state"] = "OWNER_GATE"
        elif decision == "remediate":
            current["status"] = "REMEDIATING"
            current["lifecycle_state"] = "REMEDIATING"
        elif decision == "next":
            current["status"] = "REVIEW_ACCEPTED"
            current["lifecycle_state"] = "REVIEW_ACCEPTED"
    elif state == "failed":
        current["status"] = "REVIEW_FAILED"
        current["lifecycle_state"] = "REVIEW_FAILED"
    current["reviewer"] = {
        "review_id": record.get("review_id"),
        "state": state,
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "decision": record.get("decision"),
        "reason": record.get("reason"),
    }
    write_json(target, current, indent=2)
    return target


def project_runtime_status(snapshot: dict[str, Any], runtime_root: Path | str) -> dict[str, Any]:
    """Project live broker-native execution/reviewer/planner state onto a project snapshot."""
    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
    if not project_id:
        return snapshot
    runtime = Path(runtime_root)
    projected = copy.deepcopy(snapshot)

    # 1. Check transition executor for active worker runs
    actuation_data = read_json(runtime / "transition-executor.json", {})
    executions = actuation_data.get("executions") if isinstance(actuation_data, dict) else None
    if isinstance(executions, dict):
        matching_runs = [
            r for r in executions.values()
            if isinstance(r, dict) and r.get("project_id") == project_id
        ]
        active_runs = [
            r for r in matching_runs
            if r.get("state") in {"launching", "running"}
        ]
        if active_runs:
            latest_run = max(active_runs, key=lambda r: str(r.get("started_at") or ""))
            run_state = str(latest_run.get("state") or "")
            worker_state = "running" if run_state == "running" else "starting"
            projected["state"] = "WORKER_RUNNING"
            projected["lifecycle_state"] = "EXECUTING"
            worker_info = {
                "kind": "task",
                "state": worker_state,
                "process_alive": True,
                "pid": latest_run.get("pid"),
                "started_at": latest_run.get("started_at"),
                "updated_at": latest_run.get("started_at"),
                "command": "managed {0}".format(latest_run.get("backend_id") or "worker"),
            }
            if latest_run.get("engine") == "aibroker":
                worker_info["engine"] = "aibroker"
                projected["broker_execution"] = {
                    "broker_request_id": latest_run.get("broker_request_id"),
                    "role_run_id": latest_run.get("role_run_id"),
                    "state": run_state,
                    "started_at": latest_run.get("started_at"),
                    "engine": "aibroker",
                }
            projected["worker"] = worker_info

    # 2. Check ai-reviewer for active reviews
    reviewer_data = read_json(runtime / "ai-reviewer.json", {})
    reviews = reviewer_data.get("reviews") if isinstance(reviewer_data, dict) else None
    if isinstance(reviews, dict):
        matching_reviews = [
            r for r in reviews.values()
            if isinstance(r, dict) and r.get("project_id") == project_id
        ]
        active_reviews = [
            r for r in matching_reviews
            if r.get("state") in {"launching", "running"}
        ]
        if active_reviews and projected.get("lifecycle_state") != "EXECUTING":
            latest_review = max(active_reviews, key=lambda r: str(r.get("started_at") or ""))
            projected["lifecycle_state"] = "REVIEWING"
            if projected.get("state") in {"READY_TO_RUN", "WAITING_REVIEW", "WORKER_RUNNING"}:
                projected["state"] = "REVIEWING"
            projected["reviewer"] = {
                "review_id": latest_review.get("review_id"),
                "state": latest_review.get("state"),
                "started_at": latest_review.get("started_at"),
                "role": "reviewer",
            }

    # 3. Check ai-planner for active planning
    planner_data = read_json(runtime / "ai-planner.json", {})
    plans = planner_data.get("plans") if isinstance(planner_data, dict) else None
    if isinstance(plans, dict):
        matching_plans = [
            p for p in plans.values()
            if isinstance(p, dict) and p.get("project_id") == project_id
        ]
        active_plans = [
            p for p in matching_plans
            if p.get("state") in {"planning", "reviewing", "applying"}
        ]
        if active_plans and projected.get("lifecycle_state") != "EXECUTING":
            latest_plan = max(active_plans, key=lambda p: str(p.get("started_at") or ""))
            p_state = latest_plan.get("state")
            lifecycle = "PLANNING" if p_state == "planning" else ("REVIEWING_PLAN" if p_state == "reviewing" else "APPLYING_PLAN")
            projected["lifecycle_state"] = lifecycle
            if projected.get("state") in {"READY_TO_RUN", "IDLE"}:
                projected["state"] = lifecycle
            projected["planner"] = {
                "plan_id": latest_plan.get("plan_id"),
                "state": p_state,
                "started_at": latest_plan.get("started_at"),
            }

    return projected
