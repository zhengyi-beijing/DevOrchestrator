"""Real short-lived JobService processes; these unit runs are never live acceptance."""
import sys
import time
from pathlib import Path
import test_p20_4_external_binding as fixtures
from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.jobs.models import JobSpec
from dev_orchestrator.soak.binding import observe_owned_job
from dev_orchestrator.storage.json_store import write_json


def test_owned_job_process_and_source_generation_are_observed():
    fixture = fixtures.TestExternalExecutionBinding()
    fixture.setUp()
    job = None
    try:
        runtime = fixture.canonical_runtime
        script = ('import time; from dev_orchestrator.soak.permit import load_soak_permit; '
                  'from dev_orchestrator.soak.timeline import open_soak_run; '
                  f'open_soak_run(load_soak_permit({str(fixture.permit_path)!r})); time.sleep(12)')
        write_json(runtime / 'execution-jobs.json', {'enabled': True, 'runtime_root': str(runtime),
            'projects': {'devorchestrator': {'repo_path': str(fixture.canonical_repo), 'commands': {
                'p20_soak_supervisor': {'argv': [sys.executable, '-c', script], 'cwd': '.',
                    'max_runtime_seconds': 20, 'heartbeat_interval_seconds': 1, 'effect_class': 'idempotent'}}}}})
        service = JobService(runtime)
        source = 'owned-unit-source'
        job = service.submit(JobSpec(project_id='devorchestrator', task_id='P20.4', kind='soak',
            source_request_id=source, command_ref='p20_soak_supervisor',
            idempotency_key=f'soak:{fixture.permit.run_id}:{source}', parameters_digest=fixture.permit.fingerprint))
        record = {'repo_path': str(fixture.canonical_repo), 'source_request_id': source, 'project_id': 'devorchestrator'}
        binding = {'job_id': job.job_id, 'permit_path': str(fixture.permit_path),
            'permit_fingerprint': fixture.permit.fingerprint, 'soak_run_id': fixture.permit.run_id,
            'soak_project_id': fixture.permit.soak_project_id}
        until = time.monotonic() + 8
        facts, reason = None, None
        while facts is None and time.monotonic() < until:
            facts, reason = observe_owned_job(runtime, record, binding)
            time.sleep(.1)
        assert facts is not None, reason
        assert facts['child_pid'] != facts['supervisor_pid']
        assert facts['supervisor_start_token']
        from dev_orchestrator.soak.timeline import load_soak_run, compute_run_duration
        duration = compute_run_duration(load_soak_run(fixture.disposable_runtime, fixture.permit.run_id))
        assert duration['continuity_ok'], duration
        from dev_orchestrator.core.execution_lifecycle import resolve_execution_liveness
        from unittest.mock import Mock
        port = Mock()
        port.status.return_value = {'request_id': 'finished-foreground', 'status': 'succeeded'}
        state = {'executions': {source: {**record, 'state': 'running', 'engine': 'aibroker',
            'broker_request_id': 'finished-foreground', 'external_execution_binding': binding}}}
        live = resolve_execution_liveness({'source_request_id': source, 'project_id': 'devorchestrator',
            'engine': 'aibroker', 'backend_handle': 'finished-foreground'}, executor_state=state,
            ai_execution_port=port, runtime_root=runtime)
        assert live['verdict'] == 'alive', live
        assert observe_owned_job(runtime, {**record, 'source_request_id': 'wrong-source'}, binding)[0] is None
        assert observe_owned_job(runtime, record, {**binding, 'supervisor_start_token': 'wrong-token'})[0] is None
    finally:
        if job is not None:
            service.cancel(job.job_id)
            time.sleep(.5)
        fixture.tearDown()
