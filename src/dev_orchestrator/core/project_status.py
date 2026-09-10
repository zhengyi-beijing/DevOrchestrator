"""Project-local operational status mirror for managed repositories.

The authoritative orchestration state remains under DevOrchestrator runtime.
This module writes a compact derived mirror to ``<repo>/.devorch/status.json``
so humans and tools can inspect one file without reconstructing the ledgers.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

STATUS_RELATIVE_PATH = Path(".devorch") / "status.json"
STATUS_SCHEMA_VERSION = 1


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
    return {
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
    current["worker"] = {
        "kind": "task", "state": record.get("state"),
        "process_alive": record.get("state") in {"launching", "running"},
        "pid": record.get("pid"), "started_at": record.get("started_at"),
        "updated_at": record.get("completed_at") or record.get("started_at"),
        "exit_code": record.get("exit_code"),
        "command": "managed {0}".format(record.get("backend_id") or "worker"),
    }
    write_json(target, current, indent=2)
    return target
