"""Observed-project monitor Core: read-only Git helpers and tick logic.

Core responsibilities live here: shared read-only Git helpers, monitor state
policy, and the per-project monitor tick that selects a ``ProjectAdapter``
from configuration and writes DevOrchestrator-owned runtime projections.
Project-specific snapshot/Worker projection lives in the adapters package;
``agent_files`` is the default adapter. The monitor only ever reads the
observed repository (Git porcelain via argument-array subprocess calls and
worker runtime ``status.json``). It never writes into the observed repository,
starts agents, or advances phases.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.monitor.telemetry import (
    is_run_recorded,
    new_run_record,
    new_state_event,
)
from dev_orchestrator.storage.json_store import (
    append_jsonl,
    parse_utc,
    read_text_strict,
    utc_now,
    write_json,
)

_GIT_ENV = dict(os.environ)
_GIT_ENV["GIT_OPTIONAL_LOCKS"] = "0"
_GIT_QUOTE_OFF = "-c"
_GIT_CORE_QUOTEPATH = "core.quotepath=false"


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    """Invoke the Git CLI with an argument array — never a shell string."""
    argv = ["git", "--no-optional-locks", "-C", str(root), _GIT_QUOTE_OFF, _GIT_CORE_QUOTEPATH, *arguments]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        env=_GIT_ENV,
        check=False,
        **hidden_subprocess_kwargs(),
    )


def git_info(root: Path) -> dict[str, Any]:
    """Branch, HEAD, dirty flag and changed-entry count for ``root``."""
    branch_proc = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    head_proc = _run_git(root, "rev-parse", "HEAD")
    status_proc = _run_git(root, "status", "--porcelain")
    changes = [line for line in status_proc.stdout.splitlines() if line.strip()]
    return {
        "branch": (branch_proc.stdout.splitlines()[0].strip() if branch_proc.stdout.splitlines() else ""),
        "head": (head_proc.stdout.splitlines()[0].strip() if head_proc.stdout.splitlines() else ""),
        "dirty": bool(changes),
        "changed_entries": len(changes),
    }


def git_changed_activity_utc(root: Path) -> Optional[datetime]:
    """Most recent LastWriteTime of any file reported changed by Git."""
    proc = _run_git(root, "status", "--porcelain", "--untracked-files=all")
    latest: Optional[datetime] = None
    for line in proc.stdout.splitlines():
        # Porcelain v1 entries are fixed-column "<XY> <path>": the two status
        # columns (index, worktree) must be preserved, so slice at index 3 on
        # the raw line instead of stripping it first.
        if len(line) < 4 or line[2] != " ":
            continue
        relative = line[3:]
        if " -> " in relative:
            relative = relative.split(" -> ")[-1]
        relative = relative.strip('"')
        if not relative:
            continue
        candidate = root / relative
        try:
            if candidate.is_file():
                mtime = datetime.fromtimestamp(candidate.stat().st_mtime).astimezone()
                if latest is None or mtime > latest:
                    latest = mtime
        except OSError:
            continue
    return latest


def resolve_monitor_state(
    worker: dict, next_status: Optional[str], next_updated_at: Optional[str]
) -> str:
    """Derive the public monitor state from worker + next.md status text."""
    kind = worker.get("kind")
    state = worker.get("state")
    alive = bool(worker.get("process_alive"))
    status_text = next_status or ""

    if kind == "task" and state in ("starting", "running") and not alive:
        return "WORKER_LOST"
    if kind == "task" and state in ("starting", "running"):
        return "WORKER_RUNNING"
    if kind == "task" and state == "failed":
        return "WORKER_FAILED"
    if re.search(r"BLOCKED", status_text, re.IGNORECASE):
        return "BLOCKED"
    if re.search(r"ACCEPTED|awaiting", status_text, re.IGNORECASE):
        return "WAITING_PHASE_GATE"
    if kind == "task" and state == "completed":
        if re.search(r"DESIGN READY|EXECUTABLE", status_text, re.IGNORECASE):
            worker_updated = parse_utc(worker.get("updated_at"))
            next_updated = parse_utc(next_updated_at)
            if (
                worker_updated is not None
                and next_updated is not None
                and next_updated > worker_updated
            ):
                return "READY_TO_RUN"
        return "WAITING_REVIEW"
    if re.search(r"DESIGN READY|EXECUTABLE", status_text, re.IGNORECASE):
        return "READY_TO_RUN"
    return "IDLE"


def _adapter_for_project(project: dict) -> Any:
    """Select the configured ProjectAdapter; unknown ids fail closed."""
    from dev_orchestrator.adapters import get_project_adapter

    adapter_id = str(project.get("adapter") or "agent_files")
    return get_project_adapter(adapter_id)


def _monitor_error_snapshot(
    project: dict, observed_at: datetime, exc: Exception
) -> dict[str, Any]:
    """Fail-closed snapshot envelope when a project cannot be projected."""
    project_id = str(project.get("project_id") or "")
    repo_path = str(project.get("repo_path") or "")
    return {
        "id": project_id,
        "project_id": project_id,
        "name": project.get("name"),
        "root": repo_path,
        "repo_path": repo_path,
        "adapter": project.get("adapter"),
        "conversation_binding": project.get("conversation_binding"),
        "orchestration_ready": bool(project.get("orchestration_ready")),
        "observed_at": observed_at.isoformat(),
        "state": "MONITOR_ERROR",
        "error": str(exc),
    }


def run_monitor_once(
    config_path: Path | str, runtime_root: Path | str, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Execute one full monitor tick and return the normalized summary.

    Writes only DevOrchestrator-owned runtime projections. Terminal task run
    history is idempotent by stable run id.
    """
    from dev_orchestrator.config import load_projects_config

    config = load_projects_config(config_path)
    runtime = Path(runtime_root)
    projects_dir = runtime / "projects"
    history_dir = runtime / "history"
    projects_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)
    events_path = history_dir / "events.jsonl"
    runs_path = history_dir / "runs.jsonl"
    tick_now = now or utc_now()

    snapshots: list[dict[str, Any]] = []
    for project in config.get("projects") or []:
        project_id = str(project.get("project_id") or "")
        snapshot_path = projects_dir / (project_id + ".json")
        previous: Optional[dict] = None
        if snapshot_path.is_file():
            try:
                loaded = json.loads(read_text_strict(snapshot_path))
                if isinstance(loaded, dict):
                    previous = loaded
            except (OSError, ValueError):
                previous = None
        try:
            adapter = _adapter_for_project(project)
            snapshot = adapter.snapshot(project, runs_path, now=tick_now)
        except Exception as exc:  # noqa: BLE001 - match PS MONITOR_ERROR catch-all
            snapshot = _monitor_error_snapshot(project, tick_now, exc)
        try:
            from dev_orchestrator.core.project_context import context_status, resolve_project_context
            snapshot["project_context"] = context_status(resolve_project_context(project))
        except Exception as exc:  # noqa: BLE001
            snapshot["project_context"] = {
                "schema_version": 1,
                "state": "invalid",
                "reason": f"context resolution failed: {exc}",
                "digest": None,
                "updated_at": None,
                "document_path": None,
                "supplement_used": False,
                "domains": {},
            }
        event = new_state_event(previous, snapshot)
        if event is not None:
            append_jsonl(events_path, event)
        if isinstance(snapshot.get("telemetry"), dict):
            record = new_run_record(project, snapshot)
            if record is not None and not is_run_recorded(runs_path, record.get("run_id")):
                append_jsonl(runs_path, record)
        snapshots.append(snapshot)
        write_json(snapshot_path, snapshot)

    summary = {
        "observed_at": tick_now.isoformat(),
        "project_count": len(snapshots),
        "projects": snapshots,
    }
    write_json(runtime / "summary.json", summary)
    return summary
