"""Regressions from the 2026-10-06 live P20.4 qualification run."""
from __future__ import annotations

import json
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult
from dev_orchestrator.control.command_store import ControlCommandStore, EXPECTED_IDENTITY_FIELDS
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.soak.evidence import (
    broker_retry_recovered,
    daemon_restart_active_recovered,
    duplicate_retry_idempotent,
    lost_handoff_recovered,
    worker_interrupt_recovered,
    unexpected_dirt_fail_closed,
)
from dev_orchestrator.soak.fault_port import FaultPort
from dev_orchestrator.soak.faults import get_soak_fault
from dev_orchestrator.soak.live_faults import LiveFaultDriver
from dev_orchestrator.soak.permit import create_soak_permit
from dev_orchestrator.soak.template import generate_soak_project
from dev_orchestrator.storage.json_store import read_json, write_json
from dev_orchestrator.storage.json_store import parse_utc, utc_now_iso


def _permit(tmp_path: Path, fault_id: str):
    canonical = tmp_path / 'canonical'
    canonical.mkdir()
    (canonical / 'runtime').mkdir()
    repo = tmp_path / 'case/repo'
    runtime = tmp_path / 'case/runtime'
    repo.mkdir(parents=True)
    runtime.mkdir(parents=True)
    return create_soak_permit('run', 'project', repo, runtime, canonical,
                              canonical / 'runtime', [fault_id])


def _request(permit, role: str, stage: str):
    return AIRoleRequest(project_id=permit.soak_project_id, role=role, prompt='test',
                         working_directory=Path(permit.disposable_repo_path),
                         request_id=f'{role}-{stage}', stage_run_id=stage)


def _result(request, status='succeeded'):
    return AIRoleResult(request_id=request.request_id, role_run_id='role-run', status=status,
                        output='{}' if status == 'succeeded' else None,
                        error=None if status == 'succeeded' else 'no eligible resource',
                        dispatch_id='dispatch', decision_id='decision', execution_id='execution')


class _SequencePort:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return _result(request, self.statuses.pop(0))


def test_reviewer_reject_targets_technical_review_only(tmp_path):
    permit = _permit(tmp_path, 'F04_REVIEWER_REJECT')
    port = _SequencePort(['succeeded', 'succeeded'])
    fault = FaultPort(port, permit, 'F04_REVIEWER_REJECT')

    plan_review = _request(permit, 'reviewer', 'plan_review')
    assert fault.execute(plan_review).output == '{}'
    assert not (Path(permit.disposable_runtime_root) / 'fault-effect.json').exists()

    technical_review = _request(permit, 'reviewer', 'review')
    changed = fault.execute(technical_review)
    effect = read_json(Path(permit.disposable_runtime_root) / 'fault-effect.json', {})
    assert 'P20 injected rejection' in changed.output
    assert effect['state'] == 'injected' and effect['attempt'] == 1


def test_failed_upstream_does_not_consume_repeated_signature_attempt(tmp_path):
    permit = _permit(tmp_path, 'F15_REPEATED_SIGNATURE')
    port = _SequencePort(['failed', 'succeeded'])
    fault = FaultPort(port, permit, 'F15_REPEATED_SIGNATURE')
    request = _request(permit, 'planner', 'plan')

    assert fault.execute(request).status == 'failed'
    assert not (Path(permit.disposable_runtime_root) / 'fault-effect.json').exists()
    changed = fault.execute(request)
    effect = read_json(Path(permit.disposable_runtime_root) / 'fault-effect.json', {})
    assert changed.output.startswith('P20 isolated malformed response')
    assert effect['state'] == 'injected' and effect['attempt'] == 1


class _DelayedRunningPort:
    def __init__(self):
        self.polls = 0
        self.interrupted = threading.Event()

    def execute(self, request):
        assert self.interrupted.wait(3)
        return _result(request, 'cancelled')

    def status(self, request_id):
        self.polls += 1
        return {'request_id': request_id, 'status': 'queued' if self.polls < 3 else 'running'}

    def interrupt(self, request_id, reason):
        self.interrupted.set()
        return {'request_id': request_id, 'status': 'cancelled', 'reason': reason}


def test_worker_interrupt_polls_for_entire_dispatch(tmp_path):
    permit = _permit(tmp_path, 'F03_WORKER_KILLED')
    port = _DelayedRunningPort()
    result = FaultPort(port, permit, 'F03_WORKER_KILLED').execute(
        _request(permit, 'worker', 'control'))
    effect = read_json(Path(permit.disposable_runtime_root) / 'fault-effect.json', {})
    assert result.status == 'cancelled'
    assert port.polls >= 3
    assert effect['state'] == 'injected'
    assert effect['effect_mode'] == 'owned_broker_worker_interrupt'


def test_worker_interrupt_recovery_accepts_reviewed_control_retry(tmp_path):
    permit = _permit(tmp_path, 'F03_WORKER_KILLED')
    runtime = Path(permit.disposable_runtime_root)
    repo = Path(permit.disposable_repo_path)
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo, check=True)
    (repo / 'tracked.txt').write_text('reviewed\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'tracked.txt'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'reviewed'], cwd=repo, check=True)
    reviewed_head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, check=True,
                                   capture_output=True, text=True).stdout.strip()
    effect = {'fault_id': 'F03_WORKER_KILLED', 'state': 'injected',
              'injected_at': '2026-10-07T01:00:00+00:00',
              'request_id': 'ai-worker:initial:execute',
              'effect_mode': 'owned_broker_worker_interrupt'}
    write_json(runtime / 'transition-executor.json', {'executions': {
        'initial:execute': {'project_id': 'project', 'task_id': 'P204.1',
            'state': 'failed', 'completed_at': '2026-10-07T01:00:01+00:00',
            'broker_request_id': 'ai-worker:initial:execute'},
        'retry-worker': {'project_id': 'project', 'task_id': 'P204.1',
            'state': 'completed', 'completed_at': '2026-10-07T01:02:00+00:00',
            'engine': 'aibroker', 'dispatch_id': 'wd', 'execution_id': 'we',
            'decision_id': 'wdec', 'head': reviewed_head,
            'resource_context': {'provider': 'agy'}},
        'ai_review:retry-worker': {'project_id': 'project', 'task_id': 'P204.1',
            'state': 'settled', 'outcome': 'task_complete'},
    }})
    write_json(runtime / 'ai-reviewer.json', {'reviews': {
        'ai_review:retry-worker': {'project_id': 'project', 'task_id': 'P204.1',
            'source_request_id': 'retry-worker', 'state': 'completed', 'decision': 'next',
            'completed_at': '2026-10-07T01:03:00+00:00', 'head': reviewed_head,
            'dispatch_id': 'rd', 'execution_id': 're', 'decision_id': 'rdec',
            'resource_context': {'provider': 'anthropic'}},
    }})
    write_json(runtime / 'review-decisions.json', {'decisions': {
        'ai_review:retry-worker': {'decision': 'next', 'head': reviewed_head},
    }})

    proof = worker_interrupt_recovered(permit, effect)
    assert proof['worker_source_request_id'] == 'retry-worker'
    assert proof['review_id'] == 'ai_review:retry-worker'
    assert proof['worker_provider'] == 'agy'
    assert proof['reviewer_provider'] == 'anthropic'
    ledger = read_json(runtime / 'transition-executor.json', {})
    ledger['executions']['initial:execute']['broker_request_id'] = 'different-request'
    write_json(runtime / 'transition-executor.json', ledger)
    assert worker_interrupt_recovered(permit, effect) is None
    ledger['executions']['initial:execute']['broker_request_id'] = effect['request_id']
    write_json(runtime / 'transition-executor.json', ledger)
    ledger['executions']['initial:execute']['task_id'] = 'P204.other'
    write_json(runtime / 'transition-executor.json', ledger)
    assert worker_interrupt_recovered(permit, effect) is None
    ledger['executions']['initial:execute']['task_id'] = 'P204.1'
    write_json(runtime / 'transition-executor.json', ledger)
    reviews = read_json(runtime / 'ai-reviewer.json', {})
    reviews['reviews']['ai_review:retry-worker']['resource_context']['provider'] = 'agy'
    write_json(runtime / 'ai-reviewer.json', reviews)
    assert worker_interrupt_recovered(permit, effect) is None
    reviews['reviews']['ai_review:retry-worker']['resource_context']['provider'] = 'anthropic'
    reviews['reviews']['ai_review:retry-worker']['head'] = 'missing-head'
    write_json(runtime / 'ai-reviewer.json', reviews)
    decisions = read_json(runtime / 'review-decisions.json', {})
    decisions['decisions']['ai_review:retry-worker']['head'] = 'missing-head'
    write_json(runtime / 'review-decisions.json', decisions)
    assert worker_interrupt_recovered(permit, effect) is None


def test_broker_retry_recovery_stops_at_recovered_boundary(tmp_path):
    permit = _permit(tmp_path, 'F07_BROKER_UNAVAILABLE')
    runtime = Path(permit.disposable_runtime_root)
    effect = {'fault_id': 'F07_BROKER_UNAVAILABLE', 'state': 'injected',
              'injected_at': '2026-10-07T01:00:00+00:00',
              'request_id': 'plan:planner',
              'effect_mode': 'injected_transport_boundary'}
    write_json(runtime / 'ai-planner.json', {'plans': {'plan': {
        'project_id': 'project', 'task_id': 'P204.1', 'state': 'worker_launched',
        'worker_source_request_id': 'plan:execute',
            'planner_attempts': [
                {'request_id': 'plan:planner', 'status': None,
                 'completed_at': '2026-10-07T01:00:01+00:00'},
                {'request_id': 'plan:planner:retry-1', 'status': 'succeeded',
                 'completed_at': '2026-10-07T01:00:05+00:00',
             'dispatch_id': 'pd', 'execution_id': 'pe',
             'resource': {'provider': 'anthropic'}},
        ],
    }}})
    write_json(runtime / 'transition-executor.json', {'executions': {
        'plan:execute': {'project_id': 'project', 'task_id': 'P204.1',
            'state': 'running'},
    }})

    proof = broker_retry_recovered(permit, effect)
    assert proof['planner_id'] == 'plan'
    assert proof['worker_source_request_id'] == 'plan:execute'
    assert proof['recovered_attempt'] == 2
    effect['request_id'] = 'unrelated:planner'
    assert broker_retry_recovered(permit, effect) is None
    effect['request_id'] = 'plan:planner'
    effect['effect_mode'] = 'wrong-boundary'
    assert broker_retry_recovered(permit, effect) is None
    effect['effect_mode'] = 'injected_transport_boundary'
    plans = read_json(runtime / 'ai-planner.json', {})
    plans['plans']['plan']['planner_attempts'][1]['request_id'] = effect['request_id']
    write_json(runtime / 'ai-planner.json', plans)
    assert broker_retry_recovered(permit, effect) is None
    plans['plans']['plan']['planner_attempts'][1]['request_id'] = 'plan:planner:retry-1'
    write_json(runtime / 'ai-planner.json', plans)
    effect['injected_at'] = '2026-10-07T01:00:06+00:00'
    assert broker_retry_recovered(permit, effect) is None


def test_daemon_restart_active_recovery_requires_identity_and_invariants(tmp_path):
    from dev_orchestrator.incidents import observations
    from dev_orchestrator.incidents.reliability_runs import open_reliability_run, record_daemon_boot
    permit = _permit(tmp_path, 'F08_DAEMON_RESTART_ACTIVE')
    runtime = Path(permit.disposable_runtime_root)
    effect = {'fault_id': 'F08_DAEMON_RESTART_ACTIVE', 'state': 'injected',
              'reserved_at': '2026-10-07T01:00:02+00:00',
              'injected_at': '2026-10-07T01:00:04+00:00',
              'effect_mode': 'isolated_native_boundary',
              'pre_restart_active': {'role_record_id': 'plan', 'task_id': 'P204.1',
                                     'state': 'planning'},
              'pre_restart_authority': {'current_task_id': 'P204.1', 'generation': 3},
              'daemon_before': {'state': 'running', 'pid': 100, 'create_time': 1.0},
              'daemon_after': {'state': 'starting', 'pid': 200, 'create_time': 2.0}}
    write_json(runtime / 'ai-planner.json', {'plans': {'plan': {
        'project_id': 'project', 'task_id': 'P204.1', 'state': 'planning'}}})
    write_json(runtime / 'transition-executor.json', {'executions': {}, 'lifecycle': {
        'project': {'current_task_id': 'P204.1', 'generation': 3,
            'lifecycle_state': 'PLANNING',
            'active_owner': {'role': 'planner', 'id': 'plan', 'task_id': 'P204.1'},
            'invariants': [{'code': 'SINGLE_ACTIVE_LIFECYCLE_OWNER', 'holds': True}]},
    }})
    write_json(runtime / 'daemon.json', {'state': 'running', 'pid': 200})
    run = open_reliability_run(runtime, 'project')
    write_json(runtime / 'soak-project-manifest.json', {'reliability_run_id': run['run_id']})
    record_daemon_boot(runtime, 200, '127.0.0.1', 1,
                       started_at='2026-10-07T01:00:03+00:00', boot_id='boot-after')
    directory = observations.observation_directory(runtime, run['run_id'])
    directory.mkdir(parents=True)
    observations._append_rows(directory, [{
        'project_id': 'project', 'observation_kind': 'authority_snapshot',
        'recorded_at': f'2026-10-07T01:00:{second:02d}+00:00',
        'task_id': 'P204.1', 'generation': 3, 'lifecycle_state': 'PLANNING',
        'owner_gate': None, 'active_owner': True,
    } for second in (4, 9, 14)])

    proof = daemon_restart_active_recovered(permit, effect)
    assert proof['old_pid'] == 100 and proof['new_pid'] == 200
    assert proof['current_task_id'] == 'P204.1'
    assert proof['generation'] == 3
    assert proof['single_owner_converged'] is True
    assert proof['restart_boot_id'] == 'boot-after'
    write_json(runtime / 'daemon.json', {'state': 'stopped', 'pid': 200,
                                         'stopped_at': '2026-10-07T01:00:20+00:00'})
    assert daemon_restart_active_recovered(permit, effect) == proof
    effect['reserved_at'] = '2026-10-07T01:00:03.500000+00:00'
    assert daemon_restart_active_recovered(permit, effect) is None
    effect['reserved_at'] = '2026-10-07T01:00:02+00:00'
    effect['daemon_after']['pid'] = 201
    assert daemon_restart_active_recovered(permit, effect) is None
    effect['daemon_after']['pid'] = 200
    effect['pre_restart_active']['state'] = 'completed'
    assert daemon_restart_active_recovered(permit, effect) is None
    effect['pre_restart_active']['state'] = 'planning'
    ledger = read_json(runtime / 'transition-executor.json', {})
    ledger['lifecycle']['project']['generation'] = 4
    write_json(runtime / 'transition-executor.json', ledger)
    assert daemon_restart_active_recovered(permit, effect) is None
    ledger['lifecycle']['project']['generation'] = 3
    ledger['lifecycle']['project']['lifecycle_state'] = 'PLAN_FAILED'
    ledger['lifecycle']['project']['active_owner'] = None
    write_json(runtime / 'transition-executor.json', ledger)
    assert daemon_restart_active_recovered(permit, effect) is None


def test_observed_faults_revalidates_f08_after_case_daemon_cleanup(tmp_path):
    from dev_orchestrator.incidents import observations
    from dev_orchestrator.incidents.reliability_runs import open_reliability_run, record_daemon_boot
    from dev_orchestrator.soak import evidence as evidence_module
    from dev_orchestrator.soak.permit import save_soak_permit

    canonical = tmp_path / 'canonical'
    (canonical / 'runtime').mkdir(parents=True)
    parent = create_soak_permit('parent', 'primary', tmp_path / 'primary/repo',
        tmp_path / 'primary/runtime', canonical, canonical / 'runtime',
        ['F08_DAEMON_RESTART_ACTIVE'])
    case_runtime = (Path(parent.disposable_runtime_root) / 'fault-cases'
                    / 'F08_DAEMON_RESTART_ACTIVE' / 'runtime')
    case = create_soak_permit('case', 'case-project', case_runtime.parent / 'repo',
        case_runtime, canonical, canonical / 'runtime', ['F08_DAEMON_RESTART_ACTIVE'])
    Path(case.disposable_repo_path).mkdir(parents=True)
    case_runtime.mkdir(parents=True)
    case_permit_path = case_runtime.parent / 'permit.json'
    save_soak_permit(case_permit_path, case)
    write_json(Path(case.disposable_repo_path) / case.disposable_marker_name,
               {'run_id': case.run_id, 'project_id': case.soak_project_id})

    effect = {'fault_id': 'F08_DAEMON_RESTART_ACTIVE', 'state': 'injected',
        'injection_id': 'injection', 'reserved_at': '2026-10-07T01:00:02+00:00',
        'injected_at': '2026-10-07T01:00:04+00:00',
        'effect_mode': 'isolated_native_boundary',
        'pre_restart_active': {'role_record_id': 'plan', 'task_id': 'P204.1',
                               'state': 'planning'},
        'pre_restart_authority': {'current_task_id': 'P204.1', 'generation': 3},
        'daemon_before': {'state': 'running', 'pid': 100, 'create_time': 1.0},
        'daemon_after': {'state': 'starting', 'pid': 200, 'create_time': 2.0}}
    write_json(case_runtime / 'fault-effect.json', effect)
    write_json(case_runtime / 'soak-faults/injection.json', {
        'mode': 'live_injection', 'fault_id': effect['fault_id'], 'run_id': case.run_id})
    write_json(case_runtime / 'ai-planner.json', {'plans': {'plan': {
        'project_id': case.soak_project_id, 'task_id': 'P204.1', 'state': 'planning'}}})
    write_json(case_runtime / 'transition-executor.json', {'executions': {}, 'lifecycle': {
        case.soak_project_id: {'current_task_id': 'P204.1', 'generation': 3,
            'lifecycle_state': 'PLANNING',
            'active_owner': {'role': 'planner', 'id': 'plan', 'task_id': 'P204.1'},
            'invariants': [{'code': 'SINGLE_ACTIVE_LIFECYCLE_OWNER', 'holds': True}]},
    }})
    write_json(case_runtime / 'daemon.json', {'state': 'stopped', 'pid': 200,
        'stopped_at': '2026-10-07T01:00:20+00:00', 'tree_stopped': True})
    run = open_reliability_run(case_runtime, case.soak_project_id)
    write_json(case_runtime / 'soak-project-manifest.json', {'reliability_run_id': run['run_id']})
    record_daemon_boot(case_runtime, 200, '127.0.0.1', 1,
        started_at='2026-10-07T01:00:03+00:00', boot_id='boot-after')
    directory = observations.observation_directory(case_runtime, run['run_id'])
    directory.mkdir(parents=True)
    observations._append_rows(directory, [{
        'project_id': case.soak_project_id, 'observation_kind': 'authority_snapshot',
        'recorded_at': f'2026-10-07T01:00:{second:02d}+00:00',
        'task_id': 'P204.1', 'generation': 3, 'lifecycle_state': 'PLANNING',
        'owner_gate': None, 'active_owner': True,
    } for second in (5, 10, 15)])
    write_json(case_runtime / 'incident-packets/index.json', {'families': {'family': {
        'family_id': 'family', 'project_id': case.soak_project_id,
        'packet_ids': ['packet'], 'occurrences': [{'occurrence_key': 'injection'}],
    }}})
    proof = daemon_restart_active_recovered(case, effect)
    assert proof is not None
    declared = {'case_runtime_root': str(case_runtime),
        'case_permit_path': str(case_permit_path), 'failure_family': 'family',
        'incident_id': 'packet', 'disposition': 'automatic_recovery', 'evidence': proof}
    write_json(Path(parent.disposable_runtime_root) / 'fault-driver.json', {
        'dispositions': {effect['fault_id']: declared}})
    state = {'started_at': '2026-10-07T01:00:00+00:00',
             'updated_at': '2026-10-07T01:01:00+00:00'}

    assert effect['fault_id'] in evidence_module.observed_faults(parent, state)
    declared['evidence']['restart_boot_id'] = 'tampered'
    write_json(Path(parent.disposable_runtime_root) / 'fault-driver.json', {
        'dispositions': {effect['fault_id']: declared}})
    assert effect['fault_id'] not in evidence_module.observed_faults(parent, state)


@pytest.mark.parametrize(('fault_id', 'proof_name'), (
    ('F03_WORKER_KILLED', 'worker_interrupt_recovered'),
    ('F07_BROKER_UNAVAILABLE', 'broker_retry_recovered'),
    ('F08_DAEMON_RESTART_ACTIVE', 'daemon_restart_active_recovered'),
))
def test_observed_faults_revalidates_boundary_specific_evidence(
        tmp_path, monkeypatch, fault_id, proof_name):
    from dev_orchestrator.soak import evidence as evidence_module
    from dev_orchestrator.soak.permit import save_soak_permit
    canonical = tmp_path / 'canonical'
    (canonical / 'runtime').mkdir(parents=True)
    parent = create_soak_permit('parent', 'primary', tmp_path / 'primary/repo',
        tmp_path / 'primary/runtime', canonical, canonical / 'runtime', [fault_id])
    case = Path(parent.disposable_runtime_root) / 'fault-cases' / fault_id / 'runtime'
    permit = create_soak_permit('case', 'case-project', case.parent / 'repo', case,
        canonical, canonical / 'runtime', [fault_id])
    Path(permit.disposable_repo_path).mkdir(parents=True)
    case.mkdir(parents=True)
    permit_path = case.parent / 'permit.json'
    save_soak_permit(permit_path, permit)
    write_json(Path(permit.disposable_repo_path) / permit.disposable_marker_name,
               {'run_id': permit.run_id, 'project_id': permit.soak_project_id})
    now = parse_utc(utc_now_iso())
    start, injected = now - timedelta(seconds=30), now - timedelta(seconds=20)
    effect = {'state': 'injected', 'fault_id': fault_id,
              'injection_id': 'injection', 'injected_at': injected.isoformat()}
    write_json(case / 'fault-effect.json', effect)
    write_json(case / 'soak-faults/injection.json', {
        'mode': 'live_injection', 'fault_id': fault_id, 'run_id': permit.run_id})
    write_json(case / 'incident-packets/index.json', {'families': {'family': {
        'family_id': 'family', 'project_id': permit.soak_project_id,
        'packet_ids': ['packet'], 'occurrences': [{'occurrence_key': 'injection'}],
    }}})
    proof = {'fault_id': fault_id, 'boundary_verified': True}
    declared = {'case_runtime_root': str(case), 'case_permit_path': str(permit_path),
        'failure_family': 'family', 'incident_id': 'packet',
        'disposition': 'automatic_recovery', 'evidence': dict(proof)}
    write_json(Path(parent.disposable_runtime_root) / 'fault-driver.json', {
        'dispositions': {fault_id: declared}})
    monkeypatch.setattr(evidence_module, proof_name,
                        lambda case_permit, current_effect: dict(proof))
    state = {'started_at': start.isoformat(), 'updated_at': now.isoformat()}

    assert fault_id in evidence_module.observed_faults(parent, state)
    declared['evidence']['boundary_verified'] = False
    write_json(Path(parent.disposable_runtime_root) / 'fault-driver.json', {
        'dispositions': {fault_id: declared}})
    assert fault_id not in evidence_module.observed_faults(parent, state)


def test_duplicate_retry_proof_requires_one_durable_record(tmp_path):
    permit = _permit(tmp_path, 'F14_DUPLICATE_RETRY')
    request = {'schema_version': 1, 'command_id': 'retry-one', 'project_id': 'project',
               'action': 'retry', 'expected': {key: None for key in EXPECTED_IDENTITY_FIELDS},
               'target': {'target_id': 'review'}}
    store = ControlCommandStore(permit.disposable_runtime_root)
    first = store.submit(request, source='automatic_p20_fault_duplicate_retry')
    second = store.submit(request, source='automatic_p20_fault_duplicate_retry')
    assert first == second
    proof = duplicate_retry_idempotent(permit, {'effect_mode': 'real_idempotent_duplicate_retry_request',
        'duplicate_same_record': True, 'retry_command_id': 'retry-one'})
    assert proof['durable_record_count'] == 1
    assert proof['request_hash'] == first['request_hash']


def test_duplicate_retry_structural_effect_preserves_injection_budget(tmp_path):
    permit = _permit(tmp_path, 'F14_DUPLICATE_RETRY')
    runtime = Path(permit.disposable_runtime_root)
    request = {'schema_version': 1, 'command_id': 'retry-one', 'project_id': 'project',
               'action': 'retry', 'expected': {key: None for key in EXPECTED_IDENTITY_FIELDS},
               'target': {'target_id': 'review'}}
    ControlCommandStore(runtime).submit(request, source='automatic_p20_fault_duplicate_retry')
    write_json(runtime / 'fault-effect.json', {'fault_id': 'F14_DUPLICATE_RETRY',
        'attempt': 1, 'state': 'triggered', 'injection_id': 'initial'})
    driver = LiveFaultDriver.__new__(LiveFaultDriver)
    effect = driver._structural_effect(permit, {}, get_soak_fault('F14_DUPLICATE_RETRY'),
                                       {'executions': {}}, {})
    assert effect['state'] == 'injected'
    assert effect['attempt'] == 1

    port = _SequencePort(['succeeded'])
    result = FaultPort(port, permit, 'F14_DUPLICATE_RETRY').execute(
        _request(permit, 'reviewer', 'review'))
    assert result.status == 'succeeded'
    assert len(port.requests) == 1
    assert read_json(runtime / 'fault-effect.json', {})['injection_id'] == effect['injection_id']


def test_lost_handoff_proof_uses_rebuilt_transition_not_deleted_worker(tmp_path):
    permit = _permit(tmp_path, 'F13_LOST_HANDOFF')
    runtime = Path(permit.disposable_runtime_root)
    transition = {'transition_id': 'handoff-1', 'project_id': 'project',
                  'source_task_id': 'P204.1', 'target_task_id': 'P204.2', 'state': 'ready'}
    write_json(runtime / 'fault-lost-handoff-ledger-backup.json',
               {'transitions': {'handoff-1': transition}})
    write_json(runtime / 'transition-executor.json', {'transitions': {'handoff-1': transition},
        'executions': {'review': {'project_id': 'project', 'source_task_id': 'P204.1',
            'target_task_id': 'P204.2', 'state': 'handoff',
            'lifecycle_transition_id': 'handoff-1', 'transition_generation': 1}},
        'lifecycle': {'project': {'current_task_id': 'P204.1', 'generation': 0,
            'active_owner': None,
            'invariants': [{'code': 'SINGLE_ACTIVE_LIFECYCLE_OWNER', 'holds': True}]}}})
    TransitionExecutor(runtime).mark_handoff_consumed('review', 'continue-1', 'plan-1')
    current = read_json(runtime / 'transition-executor.json', {})
    current['lifecycle']['project']['active_owner'] = {'role': 'planner', 'task_id': 'P204.2'}
    write_json(runtime / 'transition-executor.json', current)
    assert current['transitions']['handoff-1']['state'] == 'published'
    current_path = runtime / 'transition-executor.json'
    current = read_json(current_path, {})
    current['lifecycle']['project']['current_task_id'] = 'P204.3'
    current['lifecycle']['project']['active_owner']['task_id'] = 'P204.3'
    write_json(current_path, current)
    assert lost_handoff_recovered(permit) is None

    current['lifecycle']['project']['current_task_id'] = 'P204.2'
    current['lifecycle']['project']['active_owner']['task_id'] = 'P204.2'
    write_json(current_path, current)
    proof = lost_handoff_recovered(permit)
    assert proof['target_task_id'] == 'P204.2'
    assert proof['single_owner_converged'] is True


def test_unexpected_dirt_is_a_verified_fail_closed_wait(tmp_path):
    permit = _permit(tmp_path, 'F11_UNEXPECTED_DIRT')
    repo = Path(permit.disposable_repo_path)
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo, check=True)
    (repo / 'tracked.txt').write_text('base\n', encoding='utf-8')
    subprocess.run(['git', 'add', 'tracked.txt'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'base'], cwd=repo, check=True)
    (repo / 'injected-dirty-worktree.txt').write_text(
        'P20 explicitly permitted unsafe dirt fault\n', encoding='utf-8')
    write_json(Path(permit.disposable_runtime_root) / 'transition-executor.json',
        {'lifecycle': {'project': {'lifecycle_state': 'PLAN_FAILED', 'active_owner': None}}})
    proof = unexpected_dirt_fail_closed(permit)
    assert proof['wait_without_mutation'] is True
    assert proof['dirty_path'] == 'injected-dirty-worktree.txt'


def test_single_task_case_removes_successor_specs(tmp_path):
    permit = _permit(tmp_path, 'F04_REVIEWER_REJECT')
    manifest = generate_soak_project(permit)
    LiveFaultDriver._bound_single_task(permit, manifest)
    agent = Path(permit.disposable_repo_path) / 'agent'
    roadmap = json.loads((agent / 'staged/roadmap.json').read_text(encoding='utf-8'))
    assert manifest['task_ids'] == ['P204.1']
    assert roadmap['tasks'] == [{'task_id': 'P204.1', 'successor': None, 'successor_spec_path': None}]
    assert not any((agent / f'staged/P204.{number}.md').exists() for number in range(2, 6))
