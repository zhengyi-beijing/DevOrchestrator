"""Default ``agent_files`` project adapter.

Owns the read-only projection of the existing agent-file project contract:
``agent/CURRENT.md``, ``agent/next.md``, ``agent/result.md``, the configured
Worker runtime ``status.json``, and Git porcelain state. This adapter never
writes into the observed repository, never starts Workers and never advances
tasks/stages; it only reads.

The projection engine was previously inline in the monitor; it now lives here
so Core selects a ProjectAdapter from configuration and stays adapter-neutral.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.adapters.base import DEFAULT_ADAPTER_ID, ProjectAdapter
from dev_orchestrator.monitor.project import (
    git_changed_activity_utc,
    git_info,
    resolve_monitor_state,
)
from dev_orchestrator.monitor.telemetry import extract_task_id, worker_telemetry
from dev_orchestrator.platform.process import is_pid_alive
from dev_orchestrator.storage.json_store import read_text_strict, utc_now

_NEXT_TITLE_RE = re.compile(r"^# ")
_NEXT_STATUS_RE = re.compile(r"^Status:", re.IGNORECASE)
_PHASE_HINT_RE = re.compile(r"^- P[0-9].*(?:DESIGN READY|NOT STARTED|BLOCKED|RUNNING)", re.IGNORECASE)
_WORKER_COPY_FIELDS = ("pid", "started_at", "updated_at", "exit_code", "command", "model")


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


def _worker_info(root: Path, relative_runtime: str) -> dict[str, Any]:
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
    return [
        run_root / "status.json",
        run_root / "stdout.log",
        run_root / "stderr.log",
        root / "agent" / "CURRENT.md",
        root / "agent" / "next.md",
        root / "agent" / "result.md",
    ]


def _last_activity_utc(root: Path, relative_runtime: str) -> Optional[str]:
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


class AgentFilesAdapter(ProjectAdapter):
    """Default adapter for the ``agent/*.md + worker status.json`` contract."""

    @property
    def adapter_id(self) -> str:
        return DEFAULT_ADAPTER_ID

    def snapshot(
        self,
        project: dict[str, Any],
        runs_path: Path | str,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """One read-only projection for a canonically normalized project."""
        now = now or utc_now()
        observed_at = now.isoformat()
        project_id = str(project.get("project_id") or "")
        project_name = project.get("name")
        root_text = str(project.get("repo_path") or "")
        root = Path(os.path.abspath(root_text))

        common = {
            "id": project_id,
            "project_id": project_id,
            "name": project_name,
            "root": root_text,
            "repo_path": root_text,
            "adapter": self.adapter_id,
            "conversation_binding": project.get("conversation_binding"),
            "orchestration_ready": bool(project.get("orchestration_ready")),
        }
        if not root.is_dir():
            return {
                **common,
                "observed_at": observed_at,
                "state": "UNAVAILABLE",
            }

        relative_runtime = str(project.get("worker_runtime") or "")
        next_path = root / "agent" / "next.md"
        current_path = root / "agent" / "CURRENT.md"

        next_title: Optional[str] = None
        next_status: Optional[str] = None
        next_text = ""
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

        worker = _worker_info(root, relative_runtime)
        state = resolve_monitor_state(worker, next_status, next_updated_at)
        last_activity = _last_activity_utc(root, relative_runtime)
        task_id = extract_task_id(next_title)
        if task_id is None:
            task_id = extract_task_id(next_text)
        telemetry = worker_telemetry(
            project, worker, task_id, last_activity, runs_path, now=now
        )
        return {
            **common,
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
