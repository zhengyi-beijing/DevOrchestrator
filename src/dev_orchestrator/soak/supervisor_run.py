"""Original-process durable observer of real AI tasks and isolated fault effects."""
from pathlib import Path
import os
import sys
import time
from dev_orchestrator.soak.permit import load_soak_permit, validate_soak_permit
from dev_orchestrator.soak.timeline import open_soak_run, load_soak_run, checkpoint_soak_run, append_soak_event, compute_run_duration
from dev_orchestrator.soak.evidence import accepted_tasks, primary_active, measured_metrics
from dev_orchestrator.storage.json_store import read_json, utc_now_iso


def run_soak_supervisor(permit_path, canonical_repo=None, canonical_runtime=None,
                        poll_interval=5., max_duration_seconds=43200.):
    permit = load_soak_permit(permit_path)
    if permit is None:
        return 1
    ok, reason = validate_soak_permit(permit, canonical_repo or permit.canonical_repo_path,
                                    canonical_runtime or permit.canonical_runtime_root)
    if not ok:
        sys.stderr.write('permit refused: ' + str(reason))
        return 1
    runtime = Path(permit.disposable_runtime_root)
    if load_soak_run(runtime, permit.run_id) is not None:
        sys.stderr.write('An original run exists. Replacement supervisors cannot reset or accumulate clocks.')
        return 2
    manifest = read_json(runtime / 'soak-project-manifest.json', {})
    from dev_orchestrator.control.command_store import ControlCommandStore
    initial = ControlCommandStore(runtime).get(manifest.get('initial_control_id') or 'missing')
    active = primary_active(permit)
    if not initial or initial.get('state') != 'accepted' or not active:
        sys.stderr.write('Primary real-AI pipeline has not started normally.')
        return 3
    state = open_soak_run(permit)
    from dev_orchestrator.incidents.reliability_runs import open_reliability_run
    state['reliability_run_id'] = open_reliability_run(runtime, permit.soak_project_id,
                                                    opener_source='p20_durable_supervisor')['run_id']
    state['source_revision'] = read_repository_head(permit.canonical_repo_path)
    state['pending_faults'] = list(permit.allowed_fault_ids)
    state['supervisor_create_time'] = __import__('psutil').Process(os.getpid()).create_time()
    append_soak_event(runtime, permit.run_id, {'event_class': 'real_pipeline_observed', **active})
    try:
        from dev_orchestrator.soak.evidence import start_cpf_verification, cpf_proofs
        state['cpf_job_id'] = start_cpf_verification(permit, state['source_revision'])
        from dev_orchestrator.soak.live_faults import LiveFaultDriver
        faults = LiveFaultDriver(permit, permit_path)
        while True:
            state['current_execution'] = primary_active(permit)
            proof = accepted_tasks(permit)
            for task, evidence in proof.items():
                if task not in state['task_evidence']:
                    append_soak_event(runtime, permit.run_id, {'event_class': 'task_accepted', 'task_id': task, **evidence})
            state['task_evidence'], state['completed_tasks'] = proof, list(proof)
            state['fault_dispositions'], state['pending_faults'] = faults.advance()
            state['cpf_convergences'] = cpf_proofs(permit, state)
            values, coverage, telemetry = measured_metrics(permit, state)
            state['hard_zeros'].update(values)
            state['metric_coverage'].update(coverage)
            state['reliability_telemetry'] = telemetry
            checkpoint_soak_run(runtime, state)
            duration = compute_run_duration(state)
            if not duration['continuity_ok']:
                state.update(status='FAILED', ended_at=utc_now_iso(), failure_reason=duration['discontinuity_reason'])
                checkpoint_soak_run(runtime, state)
                return 4
            if duration['monotonic_seconds'] >= 21600.:
                from dev_orchestrator.soak.qualification import evaluate_soak_qualification
                report = evaluate_soak_qualification(permit.canonical_runtime_root, permit)
                if report['verdict'] == 'ACCEPT':
                    state.update(status='QUALIFIED', ended_at=utc_now_iso(), qualification=report)
                    checkpoint_soak_run(runtime, state)
                    return 0
            if duration['monotonic_seconds'] >= max_duration_seconds:
                state.update(status='FAILED', ended_at=utc_now_iso(), failure_reason='bounded qualification deadline exhausted')
                checkpoint_soak_run(runtime, state)
                return 5
            time.sleep(poll_interval)
    except Exception as exc:
        state.update(status='FAILED', ended_at=utc_now_iso(), failure_reason=type(exc).__name__)
        append_soak_event(runtime, permit.run_id, {'event_class': 'supervisor_failed', 'error_type': type(exc).__name__})
        checkpoint_soak_run(runtime, state)
        return 1


def read_repository_head(repo):
    from dev_orchestrator.core.repository import read_repository_truth
    return read_repository_truth(repo).head
