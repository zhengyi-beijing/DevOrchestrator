"""P20.4 Soak Run Timeline, Clock and Checkpoint Management."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import math
from typing import Any, Optional

from dev_orchestrator.soak.permit import SoakPermit
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now, utc_now_iso, write_json


def _current_boot_id() -> str:
    """Return a boot identifier or stable string for current system boot."""
    try:
        import psutil
        return str(psutil.boot_time())
    except Exception:
        # Fallback to process creation time
        return f"boot-{int(time.time() - time.monotonic())}"


def _same_boot(recorded: Any, current: str) -> bool:
    if recorded == current:
        return True
    # psutil's Windows boot_time estimate fluctuates by up to one second
    # between processes (psutil issue 1007). Preserve the original clock and
    # apply only that documented precision to legacy numeric boot identities.
    if os.name == 'nt' and isinstance(recorded, str):
        try:
            left, right = float(recorded), float(current)
            return math.isfinite(left) and math.isfinite(right) and abs(left - right) <= 1.
        except ValueError:
            pass
    return False


def open_soak_run(permit: SoakPermit) -> dict[str, Any]:
    """Open a new soak run and create initial state and timeline log."""
    rt = Path(permit.disposable_runtime_root)
    rt.mkdir(parents=True, exist_ok=True)
    existing = load_soak_run(rt, permit.run_id)
    if existing is not None:
        if existing.get('run_id') != permit.run_id or existing.get('project_id') != permit.soak_project_id:
            raise ValueError('existing run identity mismatch')
        return existing

    boot_id = _current_boot_id()
    now_mono = time.monotonic()
    now_iso = utc_now_iso()

    run_state = {
        "run_id": permit.run_id,
        "project_id": permit.soak_project_id,
        "started_at": now_iso,
        "updated_at": now_iso,
        "supervisor_pid": os.getpid(),
        "status": "RUNNING",
        "segments": [
            {
                "boot_id": boot_id,
                "start_mono": now_mono,
                "end_mono": now_mono,
                "started_at": now_iso,
            }
        ],
        "completed_tasks": [],
        "task_evidence": {},
        "fault_dispositions": {},
        "cpf_convergences": {},
        "hard_zeros": {
            "manual_continue": 0,
            "lifecycle_repair": 0,
            "duplicate_worker": 0,
            "duplicate_job": 0,
            "duplicate_handoff": 0,
            "duplicate_successor": 0,
            "wrong_task_execution": 0,
            "lifecycle_invariant_escape": 0,
            "silent_stall": 0,
            "infinite_retry": 0,
            "restart_regression": 0,
        },
    }
    run_state['hard_zeros'] = {key: None for key in run_state['hard_zeros']}
    run_state['metric_coverage'] = {}

    checkpoint_soak_run(rt, run_state)
    append_soak_event(rt, permit.run_id, {
        "event_class": "run_opened",
        "run_id": permit.run_id,
        "supervisor_pid": os.getpid(),
    })
    return run_state


def resume_soak_run(permit: SoakPermit) -> dict[str, Any]:
    """Resume an existing soak run, preserving run_id and original started_at."""
    rt = Path(permit.disposable_runtime_root)
    run_file = rt / f"run-{permit.run_id}.json"
    existing = read_json(run_file, None)
    if not isinstance(existing, dict):
        raise ValueError('run has not started; resume cannot start or reset a run')
    if existing.get('run_id') != permit.run_id or existing.get('project_id') != permit.soak_project_id:
        raise ValueError('run identity mismatch')
    return existing


def append_soak_event(runtime_root: Path | str, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """Append a typed event to timeline.jsonl."""
    rt = Path(runtime_root)
    rt.mkdir(parents=True, exist_ok=True)
    timeline_file = rt / "timeline.jsonl"

    from dev_orchestrator.incidents.sanitize import sanitize_incident_payload
    row = sanitize_incident_payload(dict(event))
    row.setdefault("run_id", run_id)
    row.setdefault("timestamp", utc_now_iso())
    row.setdefault("monotonic", time.monotonic())

    line = json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
    from dev_orchestrator.accounting.events import InterProcessFileLock
    with InterProcessFileLock(rt / 'soak-timeline.lock'):
        if timeline_file.exists() and timeline_file.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('bounded soak timeline capacity exhausted')
        with open(timeline_file, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    return row


def checkpoint_soak_run(
    runtime_root: Path | str,
    run_state: dict[str, Any],
    update_monotonic: bool = True,
) -> dict[str, Any]:
    """Atomically checkpoint the current run state."""
    rt = Path(runtime_root)
    rt.mkdir(parents=True, exist_ok=True)
    run_id = str(run_state.get("run_id") or "")
    if run_state.get('supervisor_pid') != os.getpid():
        raise ValueError('only the original supervisor may checkpoint its run')
    run_file = rt / f"run-{run_id}.json"

    if update_monotonic:
        segments = run_state.get("segments")
        if isinstance(segments, list) and segments:
            last_seg = segments[-1]
            if isinstance(last_seg, dict):
                now_m = time.monotonic()
                if "start_mono" in last_seg and now_m >= last_seg["start_mono"]:
                    last_seg["end_mono"] = now_m

    run_state["updated_at"] = utc_now_iso()
    write_json(run_file, run_state, indent=2)
    return run_state


def load_soak_run(runtime_root: Path | str, run_id: str) -> dict[str, Any] | None:
    rt = Path(runtime_root)
    run_file = rt / f"run-{run_id}.json"
    if not run_file.is_file():
        return None
    return read_json(run_file, None)


def compute_run_duration(run_state: dict[str, Any]) -> dict[str, Any]:
    """Compute run duration across monotonic segments with continuity checking."""
    started_at_str = str(run_state.get("started_at") or "")
    started_dt = parse_utc(started_at_str)
    wall_seconds = 0.0
    if started_dt is not None:
        now_dt = parse_utc(run_state.get('updated_at')) or started_dt
        wall_seconds = max(0.0, (now_dt - started_dt).total_seconds())

    segments = run_state.get("segments") or []
    monotonic_seconds = 0.0
    continuity_ok = True
    discontinuity_reason: Optional[str] = None

    if not isinstance(segments, list) or not segments:
        return {
            "wall_seconds": wall_seconds,
            "monotonic_seconds": 0.0,
            "segments": [],
            "continuity_ok": False,
            "discontinuity_reason": "no_segments_found",
        }
    if len(segments) != 1:
        return {'wall_seconds': wall_seconds, 'monotonic_seconds': 0., 'segments': segments,
                'continuity_ok': False, 'discontinuity_reason': 'multiple_discontinuous_segments'}

    for idx, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continuity_ok = False
            discontinuity_reason = f"malformed_segment_{idx}"
            break
        s_mono = seg.get("start_mono")
        e_mono = seg.get("end_mono")
        if s_mono is None or e_mono is None:
            continuity_ok = False
            discontinuity_reason = f"missing_monotonic_in_segment_{idx}"
            break
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in (s_mono, e_mono)):
            continuity_ok = False
            discontinuity_reason = 'invalid_monotonic_value'
            break
        if e_mono < s_mono:
            continuity_ok = False
            discontinuity_reason = f"monotonic_went_backwards_in_segment_{idx}"
            break
        if not _same_boot(seg.get('boot_id'), _current_boot_id()):
            continuity_ok = False
            discontinuity_reason = 'system_boot_discontinuity'
            break
        delta = float(e_mono - s_mono)
        if started_dt is None or abs(delta - wall_seconds) > 10.:
            continuity_ok = False
            discontinuity_reason = 'wall_monotonic_discontinuity'
            break
        monotonic_seconds += delta

    return {
        "wall_seconds": wall_seconds,
        "monotonic_seconds": monotonic_seconds,
        "segments": segments,
        "continuity_ok": continuity_ok,
        "discontinuity_reason": discontinuity_reason,
    }
