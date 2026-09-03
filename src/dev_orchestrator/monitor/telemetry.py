"""Pure telemetry/state policy — Python parity of the accepted PowerShell P1 policy.

No I/O beyond reading the DevOrchestrator-owned runs history; never touches an
observed repository.
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.storage.json_store import (
    parse_utc,
    read_jsonl,
    utc_now_iso,
)

_TASK_ID_RE = re.compile(r"\bP\d+(?:\.\d+)*(?:[a-z])?\b", re.IGNORECASE)
_TERMINAL_STATES = ("completed", "failed")
_ACTIVE_STATES = ("starting", "running")


def extract_task_id(title: Any) -> Optional[str]:
    """Extract a ``P4.2.3b``-style task identifier from a title, or None."""
    if title is None:
        return None
    text = str(title).strip()
    if not text:
        return None
    match = _TASK_ID_RE.search(text)
    return match.group(0) if match else None


def _eta_minutes(project: dict) -> tuple[float, float]:
    eta = project.get("eta") or {}
    defaults = eta.get("default_worker_minutes") or {}
    try:
        minimum = float(defaults.get("min"))
        maximum = float(defaults.get("max"))
    except (TypeError, ValueError):
        minimum, maximum = 30.0, 90.0
    return minimum, maximum


def eta_policy(
    project: dict, task_id: Any, runs_path: Path | str
) -> dict[str, Any]:
    """Compute the ETA policy for a task.

    Precedence: task override, then historical completed-run median once the
    sample threshold is met, then project defaults.
    """
    eta = project.get("eta") or {}
    minimum, maximum = _eta_minutes(project)
    source = "project_default"
    confidence = "low"
    for override in eta.get("task_overrides") or []:
        if isinstance(override, dict) and override.get("task_id") == task_id:
            try:
                minimum = float(override.get("min"))
                maximum = float(override.get("max"))
            except (TypeError, ValueError):
                continue
            source = "task_override"
            confidence = "medium"
            break
    if source == "project_default":
        durations: list[float] = []
        project_id = project.get("id")
        for run in read_jsonl(runs_path):
            if not isinstance(run, dict):
                continue
            if run.get("project_id") != project_id or run.get("result") != "completed":
                continue
            try:
                durations.append(float(run.get("duration_seconds")) / 60.0)
            except (TypeError, ValueError):
                continue
        try:
            needed = int(eta.get("historical_min_samples") or 0)
        except (TypeError, ValueError):
            needed = 0
        if durations and len(durations) >= needed:
            median = statistics.median(durations)
            minimum = max(5.0, round(median * 0.70, 1))
            maximum = max(minimum, round(median * 1.35, 1))
            source = "project_history_median"
            confidence = "high" if len(durations) >= 8 else "medium"
    return {
        "min_minutes": minimum,
        "max_minutes": maximum,
        "source": source,
        "confidence": confidence,
    }


def get_run_id(project_id: Any, worker: dict) -> Optional[str]:
    """Stable DevOrchestrator run id from worker start time + pid."""
    started = parse_utc(worker.get("started_at"))
    if started is None:
        return None
    pid_part = "na"
    if worker.get("pid") is not None:
        try:
            pid_part = str(int(worker["pid"]))
        except (TypeError, ValueError):
            pid_part = "na"
    stamp = started.strftime("%Y%m%dT%H%M%S")
    millis = started.microsecond // 1000
    return "{0}-{1}{2:03d}Z-{3}".format(project_id, stamp, millis, pid_part)


def _to_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(max(0.0, float(value)), 1)
    except (TypeError, ValueError):
        return None


def worker_telemetry(
    project: dict,
    worker: dict,
    task_id: Any,
    last_activity_at: Any,
    runs_path: Path | str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Derive worker telemetry exactly per the P1 policy.

    ``now`` is injectable for deterministic tests; default is UTC now.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    policy = eta_policy(project, task_id, runs_path)
    is_task = worker.get("kind") == "task"
    state = worker.get("state")
    eta = project.get("eta") or {}

    started = parse_utc(worker.get("started_at")) if is_task else None
    updated = parse_utc(worker.get("updated_at")) if is_task else None
    activity = parse_utc(last_activity_at)

    elapsed: Optional[float] = None
    if started is not None:
        endpoint = now
        if state in _TERMINAL_STATES and updated is not None:
            endpoint = updated
        elapsed = _to_seconds((endpoint - started).total_seconds())

    activity_age = _to_seconds((now - activity).total_seconds()) if activity is not None else None

    min_total = float(policy["min_minutes"]) * 60.0
    max_total = float(policy["max_minutes"]) * 60.0
    if elapsed is None:
        remaining_min = min_total
        remaining_max = max_total
    else:
        remaining_min = max(0.0, min_total - elapsed)
        remaining_max = max(0.0, max_total - elapsed)

    health = "OK"
    if is_task and state in _ACTIVE_STATES:
        try:
            hard_timeout = float(eta.get("hard_timeout_minutes")) * 60.0
            stall_warning = float(eta.get("stall_warning_minutes")) * 60.0
        except (TypeError, ValueError):
            hard_timeout, stall_warning = math.inf, math.inf
        if elapsed is not None and elapsed >= hard_timeout:
            health = "TIMEOUT"
        elif activity_age is not None and activity_age >= stall_warning:
            health = "STALLED_WARNING"

    return {
        "run_id": get_run_id(project.get("id"), worker) if is_task else None,
        "task_id": task_id,
        "elapsed_seconds": elapsed,
        "last_activity_age_seconds": activity_age,
        "health": health,
        "eta": {
            "total_min_seconds": round(min_total, 1),
            "total_max_seconds": round(max_total, 1),
            "remaining_min_seconds": round(remaining_min, 1),
            "remaining_max_seconds": round(remaining_max, 1),
            "source": str(policy["source"]),
            "confidence": str(policy["confidence"]),
            "overrun": elapsed is not None and elapsed > max_total,
        },
    }


def new_run_record(project: dict, snapshot: dict) -> Optional[dict]:
    """Build a terminal task run record, or None when not applicable.

    Only ``headless next`` task runs that reached a terminal state produce
    history records; utility runs never pollute run history.
    """
    worker = snapshot.get("worker") or {}
    telemetry = snapshot.get("telemetry")
    if worker.get("kind") != "task" or worker.get("state") not in _TERMINAL_STATES:
        return None
    if not isinstance(telemetry, dict) or not telemetry.get("run_id"):
        return None
    git = snapshot.get("git") or {}
    return {
        "run_id": telemetry["run_id"],
        "project_id": project.get("id"),
        "task_id": telemetry.get("task_id"),
        "task_title": snapshot.get("next_title"),
        "engine": "dsh",
        "command": worker.get("command"),
        "started_at": worker.get("started_at"),
        "completed_at": worker.get("updated_at"),
        "duration_seconds": telemetry.get("elapsed_seconds"),
        "result": "completed" if worker.get("state") == "completed" else "failed",
        "exit_code": worker.get("exit_code"),
        "git_branch_observed": git.get("branch"),
        "git_head_observed": git.get("head"),
        "git_dirty_observed": bool(git.get("dirty")),
        "recorded_at": utc_now_iso(),
        "estimate": telemetry.get("eta"),
    }


def is_run_recorded(runs_path: Path | str, run_id: Any) -> bool:
    """Whether a stable terminal run id is already present in history."""
    if not run_id:
        return False
    for run in read_jsonl(runs_path):
        if isinstance(run, dict) and run.get("run_id") == run_id:
            return True
    return False


def new_state_event(previous: Optional[dict], current: dict) -> Optional[dict]:
    """State transition event, or None to suppress noise.

    No event is emitted when the state is unchanged and the run id is missing
    or unchanged — a utility/stale run id disappearing must not produce noise.
    """
    old_state = previous.get("state") if previous else None
    new_state = current.get("state") if current else None
    old_run = None
    new_run = None
    if previous and isinstance(previous.get("telemetry"), dict):
        old_run = previous["telemetry"].get("run_id")
    if current and isinstance(current.get("telemetry"), dict):
        new_run = current["telemetry"].get("run_id")
    if old_state == new_state and (not new_run or old_run == new_run):
        return None
    telemetry = current.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
    return {
        "observed_at": utc_now_iso(),
        "project_id": current.get("id"),
        "task_id": telemetry.get("task_id"),
        "run_id": new_run,
        "from_state": old_state,
        "to_state": new_state,
    }
