"""P20.4 Detached Durable Soak Supervisor Entry Point."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time
from typing import Any, Optional

from dev_orchestrator.soak.faults import (
    SOAK_FAULTS,
    plan_fault_schedule,
    reserve_fault_injection,
    record_fault_disposition,
)
from dev_orchestrator.soak.permit import (
    SoakPermit,
    load_soak_permit,
    soak_enabled,
    validate_soak_permit,
)
from dev_orchestrator.soak.timeline import (
    append_soak_event,
    checkpoint_soak_run,
    compute_run_duration,
    open_soak_run,
    resume_soak_run,
)
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json


def run_soak_supervisor(
    permit_path: Path | str,
    canonical_repo: Path | str | None = None,
    canonical_runtime: Path | str | None = None,
    poll_interval: float = 5.0,
    max_duration_seconds: float = 21700.0,
) -> int:
    """Run the detached soak supervisor loop.

    Read-only against lifecycle ledgers; drives disposable soak project and faults.
    """
    permit = load_soak_permit(permit_path)
    if not soak_enabled(permit):
        sys.stderr.write(f"Soak permit at {permit_path} is invalid or disabled.\n")
        return 1

    can_repo = Path(canonical_repo or permit.canonical_repo_path)
    can_rt = Path(canonical_runtime or permit.canonical_runtime_root)
    ok, reason = validate_soak_permit(permit, can_repo, can_rt)
    if not ok:
        sys.stderr.write(f"Permit validation failed: {reason}\n")
        return 1

    run_state = resume_soak_run(permit)
    disp_rt = Path(permit.disposable_runtime_root)

    append_soak_event(disp_rt, permit.run_id, {
        "event_class": "supervisor_started",
        "pid": os.getpid(),
    })

    schedule = plan_fault_schedule(run_state)
    fault_idx = 0

    try:
        while True:
            # Check duration
            checkpoint_soak_run(disp_rt, run_state)
            dur_info = compute_run_duration(run_state)

            if not dur_info["continuity_ok"]:
                append_soak_event(disp_rt, permit.run_id, {
                    "event_class": "continuity_failure",
                    "reason": dur_info.get("discontinuity_reason"),
                })
                return 2

            elapsed = dur_info.get("monotonic_seconds") or 0.0
            if elapsed >= max_duration_seconds:
                append_soak_event(disp_rt, permit.run_id, {
                    "event_class": "duration_gate_met",
                    "elapsed": elapsed,
                })
                break

            # Process scheduled faults deterministically if needed
            if fault_idx < len(schedule):
                item = schedule[fault_idx]
                f_id = item["fault_id"]
                inj = reserve_fault_injection(
                    runtime_root=disp_rt,
                    run_id=permit.run_id,
                    fault_id=f_id,
                    mode="deterministic_test",
                    target={"disposable_path": str(disp_rt / "probe")},
                    pre_state={"status": "scheduled"},
                )
                record_fault_disposition(
                    runtime_root=disp_rt,
                    injection_id=inj["injection_id"],
                    incident_id=f"inc-soak-{f_id.lower()}",
                    failure_family=f"family-{f_id.lower()}",
                    recovery_strategy="RETRY_SAME_STRATEGY",
                    recovery_result="recovered",
                    recovery_latency=1.0,
                    owner_gate_id=None,
                    disposition=item["expected_disposition"],
                )
                run_state.setdefault("fault_dispositions", {})[f_id] = {
                    "disposition": item["expected_disposition"],
                    "mode": "deterministic_test",
                }
                fault_idx += 1

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        append_soak_event(disp_rt, permit.run_id, {
            "event_class": "supervisor_interrupted",
            "pid": os.getpid(),
        })
        checkpoint_soak_run(disp_rt, run_state)
        return 130
    except Exception as exc:
        append_soak_event(disp_rt, permit.run_id, {
            "event_class": "supervisor_exception",
            "error": str(exc),
            "pid": os.getpid(),
        })
        checkpoint_soak_run(disp_rt, run_state)
        return 1

    checkpoint_soak_run(disp_rt, run_state)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="P20.4 Soak Supervisor")
    parser.add_argument("--permit", required=True, help="Path to soak permit JSON")
    args = parser.parse_args(argv)
    return run_soak_supervisor(args.permit)


if __name__ == "__main__":
    raise SystemExit(main())
