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

import hashlib
from dataclasses import dataclass
from dev_orchestrator.adapters.base import DEFAULT_ADAPTER_ID, ProjectAdapter
from dev_orchestrator.core.watchdog import (
    canonical_path,
    is_watchdog_owned_path,
    path_contains,
)
from dev_orchestrator.monitor.project import (
    git_changed_entries,
    git_info,
    resolve_monitor_state,
)
from dev_orchestrator.monitor.telemetry import extract_task_id, worker_telemetry
from dev_orchestrator.platform.process import is_pid_alive
from dev_orchestrator.storage.json_store import parse_utc, read_text_strict, utc_now

_NEXT_TITLE_RE = re.compile(r"^# ")
_NEXT_STATUS_RE = re.compile(r"^Status:", re.IGNORECASE)
_PHASE_HINT_RE = re.compile(r"^- P[0-9].*(?:DESIGN READY|NOT STARTED|BLOCKED|RUNNING)", re.IGNORECASE)
_WORKER_COPY_FIELDS = ("pid", "started_at", "updated_at", "exit_code", "command", "model")


@dataclass(frozen=True)
class ActivityEntry:
    kind: str
    relative: str
    path: str
    mtime: datetime


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


def _collect_activity_entries(root: Path, relative_runtime: str) -> list[ActivityEntry]:
    """Collect raw path-identifiable activity entries in one pass."""
    entries: list[ActivityEntry] = []
    run_root = root / relative_runtime
    worker_files = [
        run_root / "status.json",
        run_root / "stdout.log",
        run_root / "stderr.log",
    ]
    for p in worker_files:
        try:
            if p.is_file():
                mtime = datetime.fromtimestamp(p.stat().st_mtime).astimezone()
                rel = os.path.relpath(p, root).replace("\\", "/")
                entries.append(ActivityEntry("worker_runtime", rel, str(p.resolve()), mtime))
        except OSError:
            pass

    agent_files = [
        root / "agent" / "CURRENT.md",
        root / "agent" / "next.md",
        root / "agent" / "result.md",
    ]
    for p in agent_files:
        try:
            if p.is_file():
                mtime = datetime.fromtimestamp(p.stat().st_mtime).astimezone()
                rel = os.path.relpath(p, root).replace("\\", "/")
                entries.append(ActivityEntry("agent_file", rel, str(p.resolve()), mtime))
        except OSError:
            pass

    for row in git_changed_entries(root):
        try:
            mtime = datetime.fromisoformat(row["mtime_iso"])
            entries.append(ActivityEntry("git_changed", row["relative"], row["path"], mtime))
        except (KeyError, ValueError):
            pass

    return entries


def aggregate_activity(
    entries: list[ActivityEntry],
    *,
    exclude: Optional[Callable[[ActivityEntry], bool]] = None,
) -> tuple[Optional[str], Optional[ActivityEntry], dict[str, Any], list[ActivityEntry]]:
    """Pure aggregation of activity entries; order-independent."""
    per_kind: dict[str, Any] = {
        "worker_runtime": {"last_activity_at": None, "path": None, "considered": 0, "excluded_self": 0},
        "agent_file": {"last_activity_at": None, "path": None, "considered": 0, "excluded_self": 0},
        "git_changed": {"last_activity_at": None, "path": None, "considered": 0, "excluded_self": 0},
    }
    excluded: list[ActivityEntry] = []
    considered: list[ActivityEntry] = []

    for entry in entries:
        if exclude is not None and exclude(entry):
            excluded.append(entry)
            kind_stats = per_kind.setdefault(entry.kind, {
                "last_activity_at": None, "path": None, "considered": 0, "excluded_self": 0
            })
            kind_stats["excluded_self"] += 1
            continue

        considered.append(entry)
        kind_stats = per_kind.setdefault(entry.kind, {
            "last_activity_at": None, "path": None, "considered": 0, "excluded_self": 0
        })
        kind_stats["considered"] += 1
        curr_kind_latest = kind_stats["last_activity_at"]
        if curr_kind_latest is None:
            kind_stats["last_activity_at"] = entry.mtime.isoformat()
            kind_stats["path"] = entry.relative
        else:
            prev_dt = parse_utc(curr_kind_latest)
            if prev_dt is not None and entry.mtime > prev_dt:
                kind_stats["last_activity_at"] = entry.mtime.isoformat()
                kind_stats["path"] = entry.relative

    newest: Optional[ActivityEntry] = None
    if considered:
        newest = max(considered, key=lambda e: e.mtime)

    last_activity_at = newest.mtime.isoformat() if newest is not None else None
    return last_activity_at, newest, per_kind, excluded


def _last_activity_utc(root: Path, relative_runtime: str) -> Optional[str]:
    """Legacy helper: newest mtime across worker logs, agent files and Git."""
    entries = _collect_activity_entries(root, relative_runtime)
    last_act, _, _, _ = aggregate_activity(entries)
    return last_act


class AgentFilesAdapter(ProjectAdapter):
    """Default adapter for the ``agent/*.md + worker status.json`` contract."""

    def __init__(self, *, runtime_root: Path | str | None = None) -> None:
        self.runtime_root = Path(runtime_root) if runtime_root is not None else None

    @property
    def adapter_id(self) -> str:
        return DEFAULT_ADAPTER_ID

    def snapshot(
        self,
        project: dict[str, Any],
        runs_path: Path | str,
        *,
        now: Optional[datetime] = None,
        runtime_root: Path | str | None = None,
    ) -> dict[str, Any]:
        """One read-only projection for a canonically normalized project."""
        now = now or utc_now()
        observed_at = now.isoformat()
        project_id = str(project.get("project_id") or "")
        project_name = project.get("name")
        root_text = str(project.get("repo_path") or "")
        root = Path(os.path.abspath(root_text))
        eff_runtime_root = runtime_root or self.runtime_root

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

        # Activity collection and dual aggregation (unfiltered vs watchdog_safe)
        entries = _collect_activity_entries(root, relative_runtime)
        unfiltered_last_activity, _, _, _ = aggregate_activity(entries)
        (
            filtered_last_activity,
            newest_filtered,
            per_kind_rollup,
            excluded_entries,
        ) = aggregate_activity(
            entries,
            exclude=lambda e: is_watchdog_owned_path(root, e.path, runtime_root=eff_runtime_root),
        )

        sorted_excluded = sorted({e.relative for e in excluded_entries})
        now_dt = now or utc_now()
        age_seconds: Optional[float] = None
        if filtered_last_activity:
            f_dt = parse_utc(filtered_last_activity)
            if f_dt:
                age_seconds = round(max(0.0, (now_dt - f_dt).total_seconds()), 1)

        c_root = canonical_path(root)
        repo_root_fp = hashlib.sha256(c_root.encode("utf-8")).hexdigest()[:16]
        repo_scope = "canonical"

        runtime_root_fp: Optional[str] = None
        runtime_scope = "unknown"
        if eff_runtime_root is not None:
            c_rt = canonical_path(eff_runtime_root)
            runtime_root_fp = hashlib.sha256(c_rt.encode("utf-8")).hexdigest()[:16]
            runtime_scope = "runtime-aware" if path_contains(root, eff_runtime_root) else "runtime-external"

        activity_block = {
            "schema_version": 1,
            "last_activity_at": unfiltered_last_activity,
            "watchdog_safe": {
                "last_activity_at": filtered_last_activity,
                "age_seconds": age_seconds,
                "newest_kind": newest_filtered.kind if newest_filtered else None,
                "newest_path": newest_filtered.relative if newest_filtered else None,
                "sources": per_kind_rollup,
                "changed_entries_considered": per_kind_rollup.get("git_changed", {}).get("considered", 0),
                "excluded_paths": sorted_excluded[:20],
                "excluded_count": len(excluded_entries),
                "repo_root_fingerprint": repo_root_fp,
                "repo_scope": repo_scope,
                "runtime_root_fingerprint": runtime_root_fp,
                "runtime_scope": runtime_scope,
            },
        }

        task_id = extract_task_id(next_title)
        if task_id is None:
            task_id = extract_task_id(next_text)
        telemetry = worker_telemetry(
            project,
            worker,
            task_id,
            unfiltered_last_activity,
            runs_path,
            now=now,
            watchdog_safe_activity_at=filtered_last_activity,
        )
        return {
            **common,
            "observed_at": observed_at,
            "state": state,
            "git": git_info(root),
            "worker": worker,
            "telemetry": telemetry,
            "last_activity_at": unfiltered_last_activity,
            "activity": activity_block,
            "next_title": next_title,
            "next_status": next_status,
            "next_updated_at": next_updated_at,
            "phase_hint": phase_hint,
        }
