"""P20.4 Unattended Soak Qualification Verifier."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.soak.faults import SOAK_FAULTS, CPF_COVERAGE_MAP
from dev_orchestrator.soak.permit import SoakPermit, validate_soak_permit
from dev_orchestrator.soak.timeline import compute_run_duration, load_soak_run

REQUIRED_SOAK_DURATION_SECONDS = 21600.0
REQUIRED_SOAK_TASKS_COUNT = 5


def evaluate_soak_qualification(
    production_runtime_root: Path | str,
    permit: SoakPermit | None,
) -> dict[str, Any]:
    """Evaluate whether the soak run meets all hard qualification criteria."""
    reasons: list[str] = []

    if permit is None:
        return {
            "verdict": "FAIL",
            "reasons": ["permit_absent"],
            "duration": None,
            "tasks": None,
            "faults": None,
            "cpf_scenarios": None,
            "hard_zeros": None,
        }

    run_state = load_soak_run(permit.disposable_runtime_root, permit.run_id)
    if not isinstance(run_state, dict):
        return {
            "verdict": "FAIL",
            "reasons": ["run_state_not_found"],
            "duration": None,
            "tasks": None,
            "faults": None,
            "cpf_scenarios": None,
            "hard_zeros": None,
        }

    # 1. Duration & Clock Continuity Gate
    duration_info = compute_run_duration(run_state)
    if not duration_info["continuity_ok"]:
        reasons.append("clock_discontinuity")
        if duration_info.get("discontinuity_reason"):
            reasons.append(str(duration_info["discontinuity_reason"]))

    mono_sec = float(duration_info.get("monotonic_seconds") or 0.0)
    if mono_sec < REQUIRED_SOAK_DURATION_SECONDS:
        reasons.append("duration_gate_failed")

    # 2. Tasks Gate (>= 5 tasks)
    completed_tasks = list(run_state.get("completed_tasks") or [])
    task_count = len(completed_tasks)
    if task_count < REQUIRED_SOAK_TASKS_COUNT:
        reasons.append("tasks_gate_failed")

    task_evidence = run_state.get("task_evidence") or {}

    # 3. Fault Dispositions Gate (15/15)
    fault_disps = run_state.get("fault_dispositions") or {}
    expected_fault_ids = {f.fault_id for f in SOAK_FAULTS}
    # Check if all 15 faults are recorded (or match prefix F01..F15)
    recorded_fault_keys = set(fault_disps.keys())
    missing_faults = []
    for f in SOAK_FAULTS:
        # Check by full id or short id
        short_id = f.fault_id.split("_")[0]
        if f.fault_id not in recorded_fault_keys and short_id not in recorded_fault_keys:
            missing_faults.append(f.fault_id)
    if missing_faults:
        reasons.append(f"missing_fault_dispositions: {sorted(missing_faults)}")

    # 4. CPF Scenarios Gate (10/10)
    cpf_convergences = run_state.get("cpf_convergences") or {}
    expected_cpfs = {f"CPF-{i:02d}" for i in range(1, 11)}
    missing_cpfs = expected_cpfs - set(cpf_convergences.keys())
    if missing_cpfs:
        reasons.append(f"missing_cpf_convergences: {sorted(missing_cpfs)}")

    # 5. Hard Zeros Gate
    hard_zeros = run_state.get("hard_zeros") or {}
    expected_zero_keys = [
        "manual_continue",
        "lifecycle_repair",
        "duplicate_worker",
        "duplicate_job",
        "duplicate_handoff",
        "duplicate_successor",
        "wrong_task_execution",
        "lifecycle_invariant_escape",
        "silent_stall",
        "infinite_retry",
        "restart_regression",
    ]
    for zk in expected_zero_keys:
        val = hard_zeros.get(zk)
        if val is None or val != 0:
            reasons.append(f"hard_zero_breach: {zk}={val}")

    verdict = "ACCEPT" if not reasons else "FAIL"

    return {
        "verdict": verdict,
        "reasons": reasons,
        "duration": duration_info,
        "tasks": {
            "completed_count": task_count,
            "completed_tasks": completed_tasks,
            "evidence": task_evidence,
        },
        "faults": {
            "dispositions_count": len(fault_disps),
            "dispositions": fault_disps,
        },
        "cpf_scenarios": {
            "convergences_count": len(cpf_convergences),
            "convergences": cpf_convergences,
        },
        "hard_zeros": hard_zeros,
    }
