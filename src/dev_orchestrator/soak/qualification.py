"""P20.4 Unattended Soak Qualification Verifier."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.soak.faults import SOAK_FAULTS, CPF_COVERAGE_MAP, disposition_allowed
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

    valid, refusal = validate_soak_permit(
        permit, permit.canonical_repo_path, production_runtime_root,
    )
    if not valid:
        reasons.append('permit_invalid: ' + str(refusal))
    if run_state.get('run_id') != permit.run_id or run_state.get('project_id') != permit.soak_project_id:
        reasons.append('run_identity_mismatch')
    if run_state.get('status') == 'FAILED':
        reasons.append('original_run_failed')
    from dev_orchestrator.soak.evidence import accepted_tasks, observed_faults, measured_metrics
    from dev_orchestrator.soak.verification_sources import cpf_retry_proofs, continuous_escape_metric
    declared_tasks = list(run_state.get('completed_tasks') or [])
    try:
        actual_tasks = accepted_tasks(permit)
        actual_faults = observed_faults(permit, run_state)
        values, coverage, _ = measured_metrics(permit, run_state)
        values['lifecycle_invariant_escape'], coverage['lifecycle_invariant_escape'] = continuous_escape_metric(permit, run_state)
        actual_cpfs = cpf_retry_proofs(permit, run_state)
    except (OSError, ValueError, TypeError, KeyError):
        actual_tasks, actual_faults, values, coverage, actual_cpfs = {}, {}, {}, {}, {}
        reasons.append('source_evidence_unavailable_or_corrupt')
    for task in declared_tasks:
        if task not in actual_tasks:
            reasons.append('uncorrelated_task: ' + str(task))
    run_state = {**run_state, 'completed_tasks': list(actual_tasks), 'task_evidence': actual_tasks,
                 'fault_dispositions': actual_faults, 'hard_zeros': values,
                 'metric_coverage': coverage, 'cpf_convergences': actual_cpfs}

    # 1. Duration & Clock Continuity Gate
    duration_info = compute_run_duration(run_state)
    if not duration_info["continuity_ok"]:
        reasons.append("clock_discontinuity")
        if duration_info.get("discontinuity_reason"):
            reasons.append(str(duration_info["discontinuity_reason"]))

    mono_sec = float(duration_info.get("monotonic_seconds") or 0.0)
    if (mono_sec < REQUIRED_SOAK_DURATION_SECONDS
            or float(duration_info.get('wall_seconds') or 0.) < REQUIRED_SOAK_DURATION_SECONDS):
        reasons.append("duration_gate_failed")

    # 2. Tasks Gate (>= 5 tasks)
    completed_tasks = list(run_state.get("completed_tasks") or [])
    task_count = len(completed_tasks)
    if task_count < REQUIRED_SOAK_TASKS_COUNT:
        reasons.append("tasks_gate_failed")

    task_evidence = run_state.get("task_evidence") or {}
    if len(set(completed_tasks)) != task_count:
        reasons.append('duplicate_task_labels')
    for task in completed_tasks:
        evidence = task_evidence.get(task)
        if not isinstance(evidence, dict) or not all(evidence.get(key) for key in (
            'planner_id', 'worker_source_request_id', 'review_id', 'commit',
            'worker_provider', 'reviewer_provider', 'ledger_verified',
        )):
            reasons.append('uncorrelated_task: ' + str(task))
        elif evidence['worker_provider'] == evidence['reviewer_provider']:
            reasons.append('reviewer_not_independent: ' + str(task))

    # 3. Fault Dispositions Gate (15/15)
    fault_disps = run_state.get("fault_dispositions") or {}
    expected_fault_ids = {f.fault_id for f in SOAK_FAULTS}
    # Check if all 15 faults are recorded (or match prefix F01..F15)
    recorded_fault_keys = set(fault_disps.keys())
    missing_faults = []
    for f in SOAK_FAULTS:
        # Check by full id or short id
        if f.fault_id not in recorded_fault_keys:
            missing_faults.append(f.fault_id)
    if missing_faults:
        reasons.append(f"missing_fault_dispositions: {sorted(missing_faults)}")
    for fault_id, evidence in fault_disps.items():
        if not isinstance(evidence, dict) or not all(evidence.get(key) for key in (
            'injection_id', 'incident_id', 'failure_family', 'evidence_verified',
        )):
            reasons.append('uncorrelated_fault: ' + str(fault_id))
        elif evidence.get('disposition') not in {'automatic_recovery', 'owner_gate', 'fail_closed'}:
            reasons.append('invalid_fault_disposition: ' + str(fault_id))
        elif not disposition_allowed(fault_id, evidence.get('disposition')):
            reasons.append('unexpected_fault_disposition: ' + str(fault_id))
        elif evidence.get('mode') != 'live_injection':
            reasons.append('fault_not_injected_during_run: ' + str(fault_id))

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
        'fabricated_successor',
        'lost_lifecycle_authority',
    ]
    for zk in expected_zero_keys:
        val = hard_zeros.get(zk)
        if val is None or val != 0:
            reasons.append(f"hard_zero_breach: {zk}={val}")
        coverage = (run_state.get('metric_coverage') or {}).get(zk)
        if not isinstance(coverage, dict) or str(coverage.get('coverage')).upper() != 'FULL' or not coverage.get('source'):
            reasons.append('hard_zero_coverage_unknown: ' + zk)

    verdict = "ACCEPT" if not reasons else "FAIL"

    return {
        "verdict": verdict,
        "reasons": reasons,
        "duration": duration_info,
        "tasks": {
            "completed_count": task_count,
            "completed_tasks": completed_tasks,
            "evidence": task_evidence,
            'declared_count': len(declared_tasks),
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
