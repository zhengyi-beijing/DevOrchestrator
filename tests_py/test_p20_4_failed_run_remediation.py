"""Negative live-run findings reproduced in isolated fixtures; not soak acceptance."""
import json
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.incidents.reliability_runs import open_reliability_run
from dev_orchestrator.incidents.reliability_telemetry import compute_reliability_telemetry
from dev_orchestrator.soak import timeline
from dev_orchestrator.storage.json_store import write_json, utc_now_iso, parse_utc
from test_transition_executor import make_repo, ready_summary, valid_project


def test_changed_head_cannot_static_relaunch_completed_task(tmp_path):
    repo = tmp_path / 'repo'
    make_repo(repo)
    executor = TransitionExecutor(tmp_path / 'runtime')
    row = {'project_id': 'p1', 'task_id': 'P1', 'source_request_id': 'first',
           'head': 'pre-worker-head', 'state': 'completed', 'started_at': utc_now_iso()}
    write_json(executor.runtime_root / 'transition-executor.json', {'executions': {'first': row}})
    project = {'project_id': 'p1', 'repo_path': str(repo), **valid_project()}
    project['execution'].pop('bootstrap')
    snapshot = ready_summary(repo, 'P1')['projects'][0]
    snapshot['next_status'] = '**READY TO RUN**'
    with patch.object(executor, '_launch') as launch:
        executor._advance_unlaunched_ready({'p1': project}, {'p1': snapshot})
    launch.assert_not_called()


def test_pending_review_fences_launch_even_if_authority_projection_lags():
    ledger = {'executions': {'first': {'project_id': 'p1', 'task_id': 'P1',
              'source_request_id': 'first', 'state': 'completed', 'review_state': 'pending'}}}
    _, reason = TransitionExecutor._lifecycle_launch_guard(ledger, 'p1', 'P1', None)
    assert reason and 'review' in reason.lower()


@pytest.mark.parametrize('plan_state,expected', [('failed', 'PLAN_FAILED'), ('recovery_required', 'OWNER_GATE'), ('owner_gate', 'OWNER_GATE')])
def test_current_failed_planner_is_not_hidden_by_pending_design(tmp_path, plan_state, expected):
    executor = TransitionExecutor(tmp_path)
    summary = {'projects': [{'project_id': 'p1', 'next_status': '**PENDING DESIGN**',
               'state': 'IDLE', 'telemetry': {'task_id': 'P1'}}]}
    planner = {'plans': {'plan': {'plan_id': 'plan', 'project_id': 'p1', 'task_id': 'P1',
               'state': plan_state, 'reason': 'provider unavailable', 'completed_at': utc_now_iso()}}}
    authority = executor.reconcile_lifecycle_authority(summary, planner_state=planner)['lifecycle']['p1']
    assert authority['lifecycle_state'] == expected
    if expected == 'OWNER_GATE':
        assert authority['owner_gate']['reason']
    assert executor.overlay_lifecycle_authority(summary)['projects'][0]['lifecycle_state'] == expected


def test_old_task_failure_does_not_regress_current_pending_design(tmp_path):
    executor = TransitionExecutor(tmp_path)
    summary = {'projects': [{'project_id': 'p1', 'next_status': '**PENDING DESIGN**',
               'state': 'IDLE', 'telemetry': {'task_id': 'P2'}}]}
    plans = {'plans': {'old': {'project_id': 'p1', 'task_id': 'P1', 'state': 'failed'}}}
    assert executor.reconcile_lifecycle_authority(summary, planner_state=plans)['lifecycle']['p1']['lifecycle_state'] == 'PENDING_DESIGN'


def test_clock_identity_does_not_depend_on_psutil_wall_estimate():
    with patch('psutil.boot_time', return_value=1000):
        first = timeline._current_boot_id()
    with patch('psutil.boot_time', return_value=1001.705):
        second = timeline._current_boot_id()
    assert first == second
    assert first.startswith(('windows-boot:', 'linux-boot:'))


def test_same_pid_replacement_is_not_continuous(tmp_path):
    from types import SimpleNamespace
    permit = SimpleNamespace(disposable_runtime_root=tmp_path, run_id='process', soak_project_id='p1')
    run = timeline.open_soak_run(permit)
    run['supervisor_create_time'] = 1.0
    result = timeline.compute_run_duration(run)
    assert not result['continuity_ok']
    assert result['discontinuity_reason'] == 'supervisor_process_discontinuity'


def test_terminal_different_source_same_task_is_counted(tmp_path):
    run = open_reliability_run(tmp_path, 'p1')
    rows = {key: {'project_id': 'p1', 'task_id': 'P1', 'source_request_id': key,
                 'state': state, 'started_at': run['opened_at'], 'source_kind': kind}
            for key, state, kind in [('initial', 'completed', 'control'), ('auto', 'failed', 'ready')]}
    write_json(tmp_path / 'transition-executor.json', {'executions': rows})
    metric = compute_reliability_telemetry(tmp_path, run['run_id']).metrics['duplicate_execution_count']
    assert metric.value == 1 and metric.coverage == 'full'


def test_compact_observations_do_not_repeat_unbounded_watchdog_history(tmp_path):
    from dev_orchestrator.incidents.observations import record_post_tick_observations
    executor = TransitionExecutor(tmp_path)
    summary = {'projects': [{'project_id': 'p1', 'state': 'READY_TO_RUN', 'telemetry': {'task_id': 'P1'}}]}
    executor.reconcile_lifecycle_authority(summary)
    run = open_reliability_run(tmp_path, 'p1')
    write_json(tmp_path / 'watchdog.json', {'projects': {'p1': {'last_checked_at': utc_now_iso(),
        'attempt_counts': {}, 'attempts': {'huge': 'x' * 200000}}}})
    record_post_tick_observations(tmp_path, summary, executor)
    assert (tmp_path / 'reliability/observations' / run['run_id'] / 'invariant-observations.jsonl').stat().st_size < 10000


def test_observation_rotation_retains_and_verifies_every_segment(tmp_path):
    from dev_orchestrator.incidents import observations
    executor = TransitionExecutor(tmp_path)
    summary = {'projects': [{'project_id': 'p1', 'state': 'READY_TO_RUN', 'telemetry': {'task_id': 'P1'}}]}
    executor.reconcile_lifecycle_authority(summary)
    run = open_reliability_run(tmp_path, 'p1')
    with patch.object(observations, 'SEGMENT_BYTES', 8000):
        for _ in range(8):
            observations.record_post_tick_observations(tmp_path, summary, executor)
    rows = observations.read_observations(tmp_path, run['run_id'])
    assert len(rows) == 56
    directory = observations.observation_directory(tmp_path, run['run_id'])
    index = json.loads((directory / 'observations-index.json').read_text())
    assert len(index['segments']) >= 2
    sealed = directory / index['segments'][0]['name']
    sealed.write_text('corrupt\n')
    with pytest.raises(ValueError):
        observations.read_observations(tmp_path, run['run_id'])


def test_observation_failure_opens_stable_authority_gate(tmp_path):
    executor = TransitionExecutor(tmp_path)
    summary = {'projects': [{'project_id': 'p1', 'state': 'READY_TO_RUN', 'telemetry': {'task_id': 'P1'}}]}
    executor.reconcile_lifecycle_authority(summary)
    executor.record_observation_failure(summary, ValueError('capacity exhausted'))
    first = executor.state()['lifecycle']['p1']['owner_gate']
    executor.record_observation_failure(summary, ValueError('capacity exhausted'))
    executor.reconcile_lifecycle_authority(summary)
    assert first == executor.state()['lifecycle']['p1']['owner_gate']
    assert first['code'] == 'RELIABILITY_OBSERVATION_UNAVAILABLE'


def test_same_task_blocked_review_obligation_is_authoritative_gate(tmp_path):
    executor = TransitionExecutor(tmp_path)
    write_json(tmp_path / 'transition-executor.json', {'executions': {
        'worker': {'project_id': 'p1', 'task_id': 'P1', 'source_request_id': 'worker',
                   'state': 'completed', 'review_state': 'pending'},
        'ai_review:worker': {'project_id': 'p1', 'task_id': 'P1', 'source_request_id': 'ai_review:worker', 'state': 'blocked'}}})
    reviews = {'reviews': {'ai_review:worker': {'review_id': 'ai_review:worker', 'project_id': 'p1', 'task_id': 'P1',
                 'source_request_id': 'worker', 'state': 'completed', 'decision': 'next'}}}
    summary = {'projects': [{'project_id': 'p1', 'state': 'READY_TO_RUN', 'telemetry': {'task_id': 'P1'}}]}
    first = executor.reconcile_lifecycle_authority(summary, reviewer_state=reviews)['lifecycle']['p1']
    assert first['lifecycle_state'] == 'OWNER_GATE'
    second = executor.reconcile_lifecycle_authority(summary, reviewer_state=reviews)['lifecycle']['p1']
    assert first['owner_gate'] == second['owner_gate']


def test_two_model_failures_switch_to_heterogeneous_resource(tmp_path):
    from dataclasses import replace
    from dev_orchestrator.ai.contracts import ResourceContext
    from test_ai_planner import ReviewerResourceFailoverPort
    from test_p20_3_planner_bounds import exercise

    class Heterogeneous(ReviewerResourceFailoverPort):
        def resource_snapshot(self):
            return {'resources': [{'resource_id': f'agy/{i}/opus', 'provider': 'agy', 'model': 'opus'}
                                  for i in (1, 2, 3)] + [{'resource_id': 'other/review', 'provider': 'other', 'model': 'review'}]}

        def execute(self, request):
            if request.role == 'reviewer' and self.review_calls == 2:
                assert 'agy/3/opus' in request.excluded_resource_ids
                assert request.quality == 'high'
                assert request.independence == 'provider'
            result = super().execute(request)
            if request.role == 'reviewer':
                resource = (ResourceContext(f'agy/{self.review_calls}/opus', 'agy', str(self.review_calls), 'opus')
                            if self.review_calls <= 2 else ResourceContext('other/review', 'other', '1', 'review'))
                return replace(result, resource_context=resource)
            return result

    port = Heterogeneous(failures=2)
    result = exercise(port, tmp_path)
    assert port.review_calls == 3
    assert result['state'] == 'ready'


def test_case_cleanup_runs_on_failure_without_stopping_unrelated_runtime(tmp_path):
    import threading
    from dev_orchestrator.soak.live_faults import LiveFaultDriver
    from dev_orchestrator.soak.permit import create_soak_permit
    from dev_orchestrator.soak.faults import SOAK_FAULTS
    canonical = tmp_path / 'canonical'
    canonical.mkdir()
    driver = LiveFaultDriver.__new__(LiveFaultDriver)
    driver.permit = create_soak_permit('parent', 'primary', tmp_path / 'repo', tmp_path / 'runtime', canonical, canonical / 'runtime')
    driver.permit_path = str(tmp_path / 'permit.json')
    driver.stop_event = threading.Event()
    with patch.object(driver, '_run_case', side_effect=RuntimeError('bounded failure')):
        with patch('dev_orchestrator.soak.live_faults.stop_owned_daemon') as stop:
            with pytest.raises(RuntimeError):
                driver._case(0, SOAK_FAULTS[0])
            stop.assert_called_once()
            assert Path(stop.call_args.args[0].disposable_runtime_root).is_relative_to(tmp_path / 'runtime/fault-cases')


def test_bounded_recovery_gate_promoted_only_for_current_task(tmp_path):
    executor = TransitionExecutor(tmp_path)
    summary = {'projects': [{'project_id': 'p1', 'next_status': '**PENDING DESIGN**',
               'state': 'IDLE', 'telemetry': {'task_id': 'P2'}}]}
    plans = {'plans': {'plan': {'project_id': 'p1', 'task_id': 'P2', 'state': 'failed'}}}
    executor.owner_store.record_gate(project_id='p1', gate_id='recovery', reason='resources exhausted', active=True)
    problem = {'task_id': 'P1', 'state': 'exhausted'}
    write_json(tmp_path / 'watchdog.json', {'projects': {'p1': {'planning_recovery_problems': {'problem': problem}}}})
    assert executor.reconcile_lifecycle_authority(summary, planner_state=plans)['lifecycle']['p1']['lifecycle_state'] == 'PLAN_FAILED'
    problem['task_id'] = 'P2'
    write_json(tmp_path / 'watchdog.json', {'projects': {'p1': {'planning_recovery_problems': {'problem': problem}}}})
    gate = executor.reconcile_lifecycle_authority(summary, planner_state=plans)['lifecycle']['p1']['owner_gate']
    assert gate['code'] == 'PLANNING_RECOVERY_EXHAUSTED'
    assert gate['gate_id'] == 'recovery'


def test_legitimate_remediation_counted_only_when_nonoverlapping(tmp_path):
    run = open_reliability_run(tmp_path, 'p1')
    start = parse_utc(run['opened_at'])
    rows = {'first': {'project_id': 'p1', 'task_id': 'P1', 'source_request_id': 'first',
                     'state': 'completed', 'started_at': start.isoformat(), 'completed_at': (start + timedelta(seconds=1)).isoformat()},
            'remedy': {'project_id': 'p1', 'task_id': 'P1', 'source_request_id': 'remedy',
                      'source_kind': 'remediation', 'review_decision_id': 'review', 'state': 'completed',
                      'started_at': (start + timedelta(seconds=2)).isoformat()}}
    write_json(tmp_path / 'ai-reviewer.json', {'reviews': {'review': {'source_request_id': 'first'}}})
    write_json(tmp_path / 'review-decisions.json', {'decisions': {'review': {
        'project_id': 'p1', 'task_id': 'P1', 'decision': 'remediate'}}})
    write_json(tmp_path / 'transition-executor.json', {'executions': rows})
    assert compute_reliability_telemetry(tmp_path, run['run_id']).metrics['duplicate_execution_count'].value == 0
    rows['remedy']['started_at'] = start.isoformat()
    write_json(tmp_path / 'transition-executor.json', {'executions': rows})
    assert compute_reliability_telemetry(tmp_path, run['run_id']).metrics['duplicate_execution_count'].value == 1


def test_verifier_reads_full_twelve_hour_fixture_across_segments(tmp_path):
    from types import SimpleNamespace
    from dev_orchestrator.incidents import observations
    from dev_orchestrator.core.lifecycle_authority import INVARIANT_CODES
    from dev_orchestrator.soak.verification_sources import continuous_escape_metric
    start = parse_utc('2026-10-05T00:00:00Z')
    directory = tmp_path / 'reliability'
    directory.mkdir()
    rows = [{'project_id': 'p1', 'source': 'normal_post_tick',
             'recorded_at': (start + timedelta(seconds=second)).isoformat(),
             'invariant_code': code, 'holds': True, 'escaped_to_authority': False}
            for second in range(0, 43201, 30) for code in INVARIANT_CODES]
    with patch.object(observations, 'SEGMENT_BYTES', 256 * 1024):
        for offset in range(0, len(rows), 100):
            observations._append_rows(directory, rows[offset:offset + 100])
    permit = SimpleNamespace(disposable_runtime_root=tmp_path, soak_project_id='p1')
    state = {'started_at': start.isoformat(), 'updated_at': (start + timedelta(hours=12)).isoformat()}
    value, coverage = continuous_escape_metric(permit, state)
    assert value == 0 and coverage['coverage'] == 'full'
    assert len(json.loads((directory / 'observations-index.json').read_text())['segments']) > 1


def test_guarded_shutdown_refuses_pid_reuse(tmp_path):
    import argparse
    from dev_orchestrator.cli import cmd_stop_daemon, CliError
    (tmp_path / 'daemon.pid').write_text(str(os.getpid()))
    args = argparse.Namespace(runtime_root=str(tmp_path), expected_pid=os.getpid(), expected_create_time=1.0)
    with patch('dev_orchestrator.cli.terminate_process_tree') as terminate:
        with pytest.raises(CliError, match='generation mismatch'):
            cmd_stop_daemon(args)
    terminate.assert_not_called()
    assert (tmp_path / 'daemon.pid').exists()


def test_monotonic_rollback_between_checkpoints_is_retained(tmp_path):
    from types import SimpleNamespace
    permit = SimpleNamespace(disposable_runtime_root=tmp_path, run_id='rollback', soak_project_id='p1')
    run = timeline.open_soak_run(permit)
    initial = run['segments'][0]['start_mono']
    with patch.object(timeline.time, 'monotonic', return_value=initial + 5):
        timeline.checkpoint_soak_run(tmp_path, run)
    with patch.object(timeline.time, 'monotonic', return_value=initial + 4):
        timeline.checkpoint_soak_run(tmp_path, run)
    assert run['segments'][0]['end_mono'] == initial + 5
    assert timeline.compute_run_duration(run)['discontinuity_reason'] == 'monotonic_went_backwards_between_checkpoints'


def test_clock_correction_retains_original_start_and_monotonic_evidence(tmp_path):
    from types import SimpleNamespace
    permit = SimpleNamespace(disposable_runtime_root=tmp_path, run_id='ntp', soak_project_id='p1')
    run = timeline.open_soak_run(permit)
    started = run['started_at']
    run['updated_at'] = (parse_utc(started) + timedelta(seconds=21601.705)).isoformat()
    run['segments'][0]['end_mono'] = run['segments'][0]['start_mono'] + 21600
    with patch('psutil.boot_time', return_value=2001.705):
        result = timeline.compute_run_duration(run)
    assert result['continuity_ok']
    assert result['monotonic_seconds'] == 21600
    assert result['wall_seconds'] == 21601.705
    assert run['started_at'] == started
    with patch.object(timeline, '_current_boot_id', return_value='actual-different-boot'):
        assert timeline.compute_run_duration(run)['discontinuity_reason'] == 'system_boot_discontinuity'


def test_current_failed_reviewer_remains_visible_to_bounded_retry(tmp_path):
    executor = TransitionExecutor(tmp_path)
    write_json(tmp_path / 'transition-executor.json', {'executions': {'worker': {
        'source_request_id': 'worker', 'project_id': 'p1', 'task_id': 'P1',
        'state': 'completed', 'review_state': 'pending'}}})
    reviews = {'reviews': {'ai_review:worker': {'review_id': 'ai_review:worker',
        'project_id': 'p1', 'task_id': 'P1', 'source_request_id': 'worker', 'state': 'failed'}}}
    summary = {'projects': [{'project_id': 'p1', 'state': 'READY_TO_RUN', 'telemetry': {'task_id': 'P1'}}]}
    authority = executor.reconcile_lifecycle_authority(summary, reviewer_state=reviews)['lifecycle']['p1']
    assert authority['lifecycle_state'] == 'REVIEW_FAILED'


@pytest.mark.skipif(os.name != 'nt', reason='Windows boot counter API')
def test_boot_counter_changes_identity_even_when_bcd_guid_is_unchanged():
    import winreg
    with patch('winreg.QueryValueEx', return_value=(55, winreg.REG_DWORD)):
        before = timeline._current_boot_id()
    with patch('winreg.QueryValueEx', return_value=(56, winreg.REG_DWORD)):
        after = timeline._current_boot_id()
    assert before != after


def test_compact_gate_preserves_identity_and_full_reason_digest():
    import hashlib
    from dev_orchestrator.incidents.observations import _compact_gate
    gate = {'gate_id': 'g', 'code': 'c', 'task_id': 'P1', 'reason': 'x' * 10000}
    compact = _compact_gate(gate)
    assert compact['task_id'] == 'P1'
    assert compact['reason_sha256'] == hashlib.sha256(gate['reason'].encode()).hexdigest()
    assert len(compact['reason']) <= 256


def test_unmeasured_project_does_not_append_or_gate_for_legacy_capacity(tmp_path):
    from dev_orchestrator.incidents import observations
    executor = TransitionExecutor(tmp_path)
    summary = {'projects': [{'project_id': 'p1', 'state': 'READY_TO_RUN', 'telemetry': {'task_id': 'P1'}}]}
    executor.reconcile_lifecycle_authority(summary)
    directory = tmp_path / 'reliability'
    directory.mkdir()
    legacy = directory / 'invariant-observations.jsonl'
    legacy.write_bytes(b'old evidence')
    with patch.object(observations, 'MAX_TOTAL_BYTES', 1):
        observations.record_post_tick_observations(tmp_path, summary, executor)
    assert legacy.read_bytes() == b'old evidence'
    assert not executor.state()['lifecycle']['p1'].get('owner_gate')


def test_measured_capacity_failure_gates_only_its_project(tmp_path):
    from dev_orchestrator.incidents import observations
    executor = TransitionExecutor(tmp_path)
    summary = {'projects': [{'project_id': p, 'state': 'READY_TO_RUN', 'telemetry': {'task_id': 'P1'}} for p in ('p1', 'p2')]}
    executor.reconcile_lifecycle_authority(summary)
    open_reliability_run(tmp_path, 'p1')
    with patch.object(observations, 'MAX_TOTAL_BYTES', 1):
        with pytest.raises(ValueError) as failed:
            observations.record_post_tick_observations(tmp_path, summary, executor)
    executor.record_observation_failure(summary, failed.value)
    assert executor.state()['lifecycle']['p1']['owner_gate']['code'] == 'RELIABILITY_OBSERVATION_UNAVAILABLE'
    assert not executor.state()['lifecycle']['p2'].get('owner_gate')


def test_current_ambiguous_recovery_is_stable_explicit_gate_after_restart(tmp_path):
    executor = TransitionExecutor(tmp_path)
    summary = {'projects': [{'project_id': 'p1', 'state': 'IDLE', 'telemetry': {'task_id': 'P1'}}]}
    plans = {'plans': {'plan': {'plan_id': 'plan', 'project_id': 'p1', 'task_id': 'P1', 'state': 'recovery_required'}}}
    first = executor.reconcile_lifecycle_authority(summary, planner_state=plans)['lifecycle']['p1']
    assert first['lifecycle_state'] == 'OWNER_GATE'
    assert first['owner_gate']['required_owner_decision']
    assert first['active_owner'] is None
    second = TransitionExecutor(tmp_path).reconcile_lifecycle_authority(summary, planner_state=plans)['lifecycle']['p1']
    assert first['owner_gate'] == second['owner_gate']


def test_closed_measurement_stops_append_and_fresh_run_preserves_legacy(tmp_path):
    from dev_orchestrator.incidents import observations
    from dev_orchestrator.incidents.reliability_runs import close_reliability_run
    executor = TransitionExecutor(tmp_path)
    summary = {'projects': [{'project_id': 'p1', 'state': 'READY_TO_RUN', 'telemetry': {'task_id': 'P1'}}]}
    executor.reconcile_lifecycle_authority(summary)
    legacy = tmp_path / 'reliability/invariant-observations.jsonl'
    legacy.parent.mkdir()
    legacy.write_bytes(b'preserved cold history')
    first = open_reliability_run(tmp_path, 'p1')
    observations.record_post_tick_observations(tmp_path, summary, executor)
    first_rows = observations.read_observations(tmp_path, first['run_id'])
    close_reliability_run(tmp_path, first['run_id'])
    observations.record_post_tick_observations(tmp_path, summary, executor)
    assert observations.read_observations(tmp_path, first['run_id']) == first_rows
    second = open_reliability_run(tmp_path, 'p1')
    observations.record_post_tick_observations(tmp_path, summary, executor)
    assert len(observations.read_observations(tmp_path, second['run_id'])) == 7
    assert legacy.read_bytes() == b'preserved cold history'


def test_current_ambiguous_reviewer_has_explicit_gate_without_physical_owner(tmp_path):
    executor = TransitionExecutor(tmp_path)
    write_json(tmp_path / 'transition-executor.json', {'executions': {'worker': {
        'source_request_id': 'worker', 'project_id': 'p1', 'task_id': 'P1', 'state': 'completed', 'review_state': 'pending'}}})
    reviews = {'reviews': {'ai_review:worker': {'review_id': 'ai_review:worker',
        'project_id': 'p1', 'task_id': 'P1', 'source_request_id': 'worker', 'state': 'recovery_required'}}}
    summary = {'projects': [{'project_id': 'p1', 'state': 'READY_TO_RUN', 'telemetry': {'task_id': 'P1'}}]}
    first = executor.reconcile_lifecycle_authority(summary, reviewer_state=reviews)['lifecycle']['p1']
    assert first['lifecycle_state'] == 'OWNER_GATE'
    assert first['owner_gate']['code'] == 'REVIEW_RECOVERY_REQUIRED'
    assert first['active_owner'] is None
    second = TransitionExecutor(tmp_path).reconcile_lifecycle_authority(summary, reviewer_state=reviews)['lifecycle']['p1']
    assert second['owner_gate'] == first['owner_gate']
    assert second['active_owner'] is None


def test_observed_fault_verifies_compact_gate_after_actual_restart_records(tmp_path):
    from dev_orchestrator.incidents import observations
    from dev_orchestrator.incidents.reliability_runs import record_daemon_boot
    from dev_orchestrator.soak.evidence import observed_faults
    from dev_orchestrator.soak.permit import create_soak_permit, save_soak_permit
    from dev_orchestrator.soak.faults import SOAK_FAULTS
    canonical = tmp_path / 'canonical'
    canonical.mkdir()
    fault_id = SOAK_FAULTS[0].fault_id
    parent = create_soak_permit('parent', 'primary', tmp_path / 'repo', tmp_path / 'runtime', canonical, canonical / 'runtime')
    case = Path(parent.disposable_runtime_root) / 'fault-cases' / fault_id / 'runtime'
    permit = create_soak_permit('case', 'case-project', case.parent / 'repo', case, canonical, canonical / 'runtime', [fault_id])
    permit_path = case.parent / 'permit.json'
    save_soak_permit(permit_path, permit)
    write_json(Path(permit.disposable_repo_path) / permit.disposable_marker_name,
               {'run_id': permit.run_id, 'project_id': permit.soak_project_id})
    now = parse_utc(utc_now_iso())
    start, injected, restarted = (now - timedelta(seconds=s) for s in (60, 40, 20))
    run = open_reliability_run(case, 'case-project')
    write_json(case / 'soak-project-manifest.json', {'reliability_run_id': run['run_id']})
    gate = {'gate_id': 'stable', 'code': 'RESOURCE_EXHAUSTED', 'task_id': 'P204.1', 'reason': 'x' * 10000}
    write_json(case / 'transition-executor.json', {'lifecycle': {'case-project': {'owner_gate': gate, 'active_owner': None}}})
    record_daemon_boot(case, 123, '127.0.0.1', 1, started_at=restarted.isoformat())
    effect = {'state': 'injected', 'fault_id': fault_id, 'injection_id': 'i1', 'injected_at': injected.isoformat()}
    write_json(case / 'fault-effect.json', effect)
    write_json(case / 'soak-faults/i1.json', {'mode': 'live_injection', 'fault_id': fault_id, 'run_id': 'case'})
    write_json(case / 'incident-packets/index.json', {'families': {'family': {'family_id': 'family',
        'project_id': 'case-project', 'packet_ids': ['packet'], 'occurrences': [{'occurrence_key': 'i1'}]}}})
    declared = {'case_runtime_root': str(case), 'case_permit_path': str(permit_path), 'failure_family': 'family',
        'incident_id': 'packet', 'disposition': 'owner_gate', 'evidence': {'owner_gate': gate, 'stable_after_restart': True}}
    write_json(Path(parent.disposable_runtime_root) / 'fault-driver.json', {'dispositions': {fault_id: declared}})
    directory = observations.observation_directory(case, run['run_id'])
    directory.mkdir(parents=True)
    rows = [{'project_id': 'case-project', 'observation_kind': 'authority_snapshot', 'recorded_at': now.isoformat(),
             'active_owner': False, 'owner_gate': observations._compact_gate(gate)} for _ in range(3)]
    observations._append_rows(directory, rows)
    state = {'started_at': start.isoformat(), 'updated_at': now.isoformat()}
    assert fault_id in observed_faults(parent, state)
    rows[0]['owner_gate']['reason_sha256'] = 'changed'
    (directory / 'invariant-observations.jsonl').write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    assert fault_id not in observed_faults(parent, state)
