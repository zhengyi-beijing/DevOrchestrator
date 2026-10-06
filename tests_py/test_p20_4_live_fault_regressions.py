"""Regressions from the 2026-10-06 live P20.4 qualification run."""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult
from dev_orchestrator.control.command_store import ControlCommandStore, EXPECTED_IDENTITY_FIELDS
from dev_orchestrator.soak.evidence import (
    duplicate_retry_idempotent,
    lost_handoff_recovered,
    unexpected_dirt_fail_closed,
)
from dev_orchestrator.soak.fault_port import FaultPort
from dev_orchestrator.soak.live_faults import LiveFaultDriver
from dev_orchestrator.soak.permit import create_soak_permit
from dev_orchestrator.soak.template import generate_soak_project
from dev_orchestrator.storage.json_store import read_json, write_json


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


def test_lost_handoff_proof_uses_rebuilt_transition_not_deleted_worker(tmp_path):
    permit = _permit(tmp_path, 'F13_LOST_HANDOFF')
    runtime = Path(permit.disposable_runtime_root)
    transition = {'transition_id': 'handoff-1', 'project_id': 'project',
                  'source_task_id': 'P204.1', 'target_task_id': 'P204.2', 'state': 'ready'}
    write_json(runtime / 'fault-lost-handoff-ledger-backup.json',
               {'transitions': {'handoff-1': transition}})
    write_json(runtime / 'transition-executor.json', {'transitions': {'handoff-1': transition},
        'executions': {'review': {'project_id': 'project', 'source_task_id': 'P204.1',
            'target_task_id': 'P204.2', 'state': 'handoff', 'handoff_consumed': True}},
        'lifecycle': {'project': {'current_task_id': 'P204.2', 'generation': 1,
            'active_owner': {'role': 'planner', 'task_id': 'P204.2'},
            'invariants': [{'code': 'SINGLE_ACTIVE_LIFECYCLE_OWNER', 'holds': True}]}}})
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
