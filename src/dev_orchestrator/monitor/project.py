"""Observed-project monitor: read-only Git/Worker projections and tick logic.

The monitor only ever reads the observed repository (Git porcelain via
argument-array subprocess calls and worker runtime ``status.json``). It never
writes into the observed repository, starts agents, or advances phases.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.monitor.telemetry import (
    extract_task_id,
    is_run_recorded,
    new_run_record,
    new_state_event,
    worker_telemetry,
)
from dev_orchestrator.platform.process import is_pid_alive
from dev_orchestrator.storage.json_store import (
    append_jsonl,
    parse_utc,
    read_text_strict,
    utc_now,
    write_json,
)

_NEXT_TITLE_RE = re.compile(r"^# ")
_NEXT_STATUS_RE = re.compile(r"^Status:", re.IGNORECASE)
_PHASE_HINT_RE = re.compile(r"^- P[0-9].*(?:DESIGN READY|NOT STARTED|BLOCKED|RUNNING)", re.IGNORECASE)
_WORKER_COPY_FIELDS = ("pid", "started_at", "updated_at", "exit_code", "command", "model")

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


def _first_matching_line(text: str, pattern: re.Pattern) -> Optional[str]:
    for line in text.splitlines():
        if pattern.search(line):
            return line
    return None


def _read_tolerant(path: Path) -> str:
    """Read text with replacement decoding (PowerShell ``-Encoding UTF8`` parity)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def worker_info(root: Path, relative_runtime: str) -> dict[str, Any]:
    """Read the worker projection from ``<root>/<runtime>/status.json``.

    A missing status file means ``not_started``. The file is read-only; the
    worker runtime directory is never created or modified here.
    """
    status_path = root / relative_runtime / "status.json"
    if not status_path.is_file():
        return {"state": "not_started", "kind": "none", "process_alive": False}
    try:
        status = json.loads(read_text_strict(status_path))
    except (OSError, ValueError) as exc:
        raise RuntimeError("invalid worker status.json: {0}".format(exc)) from exc
    if not isinstance(status, dict):
        raise RuntimeError("worker status.json must contain a JSON object")
    command = status.get("command")
    kind = "task" if command and re.search(r"headless next", str(command)) else "utility"
    info: dict[str, Any] = {
        "state": str(status.get("state") or ""),
        "kind": kind,
        "process_alive": is_pid_alive(status.get("pid")),
    }
    for name in _WORKER_COPY_FIELDS:
        if name in status:
            info[name] = status[name]
    return info


def _activity_candidates(root: Path, relative_runtime: str) -> list[Path]:
    run_root = root / relative_runtime
    candidates = [
        run_root / "status.json",
        run_root / "stdout.log",
        run_root / "stderr.log",
        root / "agent" / "CURRENT.md",
        root / "agent" / "next.md",
        root / "agent" / "result.md",
    ]
    return candidates


def last_activity_utc(root: Path, relative_runtime: str) -> Optional[str]:
    """Newest mtime across worker logs, agent files and changed Git files."""
    latest: Optional[datetime] = None
    for path in _activity_candidates(root, relative_runtime):
        try:
            if path.is_file():
                mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
                if latest is None or mtime > latest:
                    latest = mtime
        except OSError:
            continue
    git_activity = git_changed_activity_utc(root)
    if git_activity is not None and (latest is None or git_activity > latest):
        latest = git_activity
    if latest is None:
        return None
    return latest.astimezone().isoformat()


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


def project_snapshot(
    project: dict, runs_path: Path, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """One read-only projection for a configured project."""
    now = now or utc_now()
    observed_at = now.isoformat()
    project_id = str(project.get("id") or "")
    project_name = project.get("name")
    root_text = str(project.get("root") or "")
    root = Path(os.path.abspath(root_text))

    if not root.is_dir():
        return {
            "id": project_id,
            "name": project_name,
            "root": root_text,
            "observed_at": observed_at,
            "state": "UNAVAILABLE",
        }

    relative_runtime = str(project.get("worker_runtime") or "")
    next_path = root / "agent" / "next.md"
    current_path = root / "agent" / "CURRENT.md"

    next_title: Optional[str] = None
    next_status: Optional[str] = None
    if next_path.is_file():
        next_text = _read_tolerant(next_path)
        title_line = _first_matching_line(next_text, _NEXT_TITLE_RE)
        if title_line is not None:
            next_title = title_line[2:].strip() or None
        status_line = _first_matching_line(next_text, _NEXT_STATUS_RE)
        if status_line is not None:
            status_text = status_line[7:].strip()
            next_status = status_text or None

    next_updated_at: Optional[str] = None
    try:
        if next_path.is_file():
            next_updated_at = datetime.fromtimestamp(next_path.stat().st_mtime).astimezone().isoformat()
    except OSError:
        next_updated_at = None

    phase_hint: Optional[str] = None
    if current_path.is_file():
        current_text = _read_tolerant(current_path)
        hint_line = _first_matching_line(current_text, _PHASE_HINT_RE)
        if hint_line is not None:
            phase_hint = hint_line.strip()

    worker = worker_info(root, relative_runtime)
    state = resolve_monitor_state(worker, next_status, next_updated_at)
    last_activity = last_activity_utc(root, relative_runtime)
    task_id = extract_task_id(next_title)
    telemetry = worker_telemetry(
        project, worker, task_id, last_activity, runs_path, now=now
    )
    return {
        "id": project_id,
        "name": project_name,
        "root": root_text,
        "observed_at": observed_at,
        "state": state,
        "git": git_info(root),
        "worker": worker,
        "telemetry": telemetry,
        "last_activity_at": last_activity,
        "next_title": next_title,
        "next_status": next_status,
        "next_updated_at": next_updated_at,
        "phase_hint": phase_hint,
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
        project_id = str(project.get("id") or "")
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
            snapshot = project_snapshot(project, runs_path, now=tick_now)
        except Exception as exc:  # noqa: BLE001 - match PS MONITOR_ERROR catch-all
            snapshot = {
                "id": project_id,
                "name": project.get("name"),
                "root": str(project.get("root") or ""),
                "observed_at": tick_now.isoformat(),
                "state": "MONITOR_ERROR",
                "error": str(exc),
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
