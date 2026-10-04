"""P20.4 Soak Run Timeline, Clock and Checkpoint Management."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
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


def open_soak_run(permit: SoakPermit) -> dict[str, Any]:
    """Open a new soak run and create initial state and timeline log."""
    rt = Path(permit.disposable_runtime_root)
    rt.mkdir(parents=True, exist_ok=True)

    boot_id = _current_boot_id()
    now_mono = time.monotonic()
    now_iso = utc_now_iso()

    run_state = {
        "run_id": permit.run_id,
        "project_id": permit.soak_project_id,
        "started_at": now_iso,
        "updated_at": now_iso,
        "supervisor_pid": os.getpid(),
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
        return open_soak_run(permit)

    boot_id = _current_boot_id()
    now_mono = time.monotonic()
    now_iso = utc_now_iso()

    segments = list(existing.get("segments") or [])
    segments.append({
        "boot_id": boot_id,
        "start_mono": now_mono,
        "end_mono": now_mono,
        "resumed_at": now_iso,
    })

    existing["segments"] = segments
    existing["supervisor_pid"] = os.getpid()
    existing["updated_at"] = now_iso

    checkpoint_soak_run(rt, existing)
    append_soak_event(rt, permit.run_id, {
        "event_class": "run_resumed",
        "run_id": permit.run_id,
        "supervisor_pid": os.getpid(),
    })
    return existing


def append_soak_event(runtime_root: Path | str, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """Append a typed event to timeline.jsonl."""
    rt = Path(runtime_root)
    rt.mkdir(parents=True, exist_ok=True)
    timeline_file = rt / "timeline.jsonl"

    row = dict(event)
    row.setdefault("run_id", run_id)
    row.setdefault("timestamp", utc_now_iso())
    row.setdefault("monotonic", time.monotonic())

    line = json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
    with open(timeline_file, "a", encoding="utf-8") as f:
        f.write(line)
    return row


def checkpoint_soak_run(
    runtime_root: Path | str,
    run_state: dict[str, Any],
    update_monotonic: bool = False,
) -> dict[str, Any]:
    """Atomically checkpoint the current run state."""
    rt = Path(runtime_root)
    rt.mkdir(parents=True, exist_ok=True)
    run_id = str(run_state.get("run_id") or "")
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
        now_dt = utc_now()
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
        if e_mono < s_mono:
            continuity_ok = False
            discontinuity_reason = f"monotonic_went_backwards_in_segment_{idx}"
            break
        delta = float(e_mono - s_mono)
        monotonic_seconds += delta

    return {
        "wall_seconds": wall_seconds,
        "monotonic_seconds": monotonic_seconds,
        "segments": segments,
        "continuity_ok": continuity_ok,
        "discontinuity_reason": discontinuity_reason,
    }
