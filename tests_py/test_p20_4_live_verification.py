"""Regression for verifier failures found in the original live run."""
import json
import hashlib
import sys
import time
from types import SimpleNamespace

from dev_orchestrator.incidents.reliability_runs import open_reliability_run
from dev_orchestrator.incidents.reliability_telemetry import compute_reliability_telemetry
from dev_orchestrator.core.lifecycle_authority import INVARIANT_CODES
from dev_orchestrator.soak.verification_sources import continuous_escape_metric
from dev_orchestrator.soak.verification_sources import cpf_retry_proofs
from dev_orchestrator.soak.cpf_runner import CPF_TEST_PATHS
from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.jobs.models import JobSpec
from dev_orchestrator.config import REPO_ROOT
from dev_orchestrator.storage.json_store import write_json, utc_now_iso


def test_run_scoped_escape_observations_are_read(tmp_path):
    run = open_reliability_run(tmp_path, 'fixture')
    path = tmp_path / 'reliability/observations' / run['run_id'] / 'invariant-observations.jsonl'
    path.parent.mkdir(parents=True)
    row = {'project_id': 'fixture', 'recorded_at': run['opened_at'],
           'invariant_code': 'CURRENT_TASK_MATCHES_ACTIVE_EXECUTION',
           'transition_id': 't1', 'holds': False, 'escaped_to_authority': True}
    path.write_text(json.dumps(row) + '\n', encoding='utf-8')
    metric = compute_reliability_telemetry(tmp_path, run['run_id']).metrics['lifecycle_invariant_escape_count']
    assert metric.coverage == 'full'
    assert metric.value == 1


def test_corrupt_escape_observations_cannot_claim_zero(tmp_path):
    run = open_reliability_run(tmp_path, 'fixture')
    path = tmp_path / 'reliability/observations' / run['run_id'] / 'invariant-observations.jsonl'
    path.parent.mkdir(parents=True)
    row = {'project_id': 'fixture', 'recorded_at': run['opened_at'],
           'holds': True, 'escaped_to_authority': False}
    path.write_text(json.dumps(row) + '\n{broken\n', encoding='utf-8')
    metric = compute_reliability_telemetry(tmp_path, run['run_id']).metrics['lifecycle_invariant_escape_count']
    assert metric.coverage == 'unknown'
    assert metric.value is None


def test_escape_coverage_requires_every_invariant_and_continuous_ticks(tmp_path):
    permit = SimpleNamespace(disposable_runtime_root=tmp_path, soak_project_id='fixture')
    state = {'started_at': '2026-10-04T00:00:00+00:00', 'updated_at': '2026-10-04T00:01:00+00:00'}
    path = tmp_path / 'reliability/invariant-observations.jsonl'
    path.parent.mkdir()
    rows = [{'project_id': 'fixture', 'source': 'normal_post_tick',
             'recorded_at': f'2026-10-04T00:00:{second:02d}+00:00',
             'invariant_code': code, 'holds': True, 'escaped_to_authority': False}
            for second in (10, 30, 50) for code in INVARIANT_CODES]
    path.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    value, coverage = continuous_escape_metric(permit, state)
    assert value == 0 and coverage['coverage'] == 'full'
    rows[0].update(holds=False, escaped_to_authority=True)
    path.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    assert continuous_escape_metric(permit, state)[0] == 1
    path.write_text('\n'.join(json.dumps(row) for row in rows if row['invariant_code'] != INVARIANT_CODES[0]), encoding='utf-8')
    assert continuous_escape_metric(permit, state)[0] is None
    path.write_text('\n'.join(json.dumps(row) for row in rows if ':30+' not in row['recorded_at']), encoding='utf-8')
    assert continuous_escape_metric(permit, state)[0] is None


def test_real_cpf_retry_preserves_failed_predecessor_and_all_ten_proofs(tmp_path):
    repo, runtime = tmp_path / 'repo', tmp_path / 'runtime'
    repo.mkdir()
    command = {'argv': [sys.executable, '-c', 'raise SystemExit(1)'], 'cwd': '.',
               'effect_class': 'read_only', 'max_runtime_seconds': 60, 'heartbeat_interval_seconds': 1}
    config = {'enabled': True, 'runtime_root': str(runtime), 'projects': {
        'fixture': {'repo_path': str(repo), 'commands': {'cpf_replay': command}}}}
    write_json(runtime / 'execution-jobs.json', config)
    permit = SimpleNamespace(disposable_runtime_root=runtime, soak_project_id='fixture', run_id='unit-cpf')
    state = {'started_at': utc_now_iso(), 'source_revision': 'unit-source'}
    payload = {'source_revision': state['source_revision'], 'file_hashes': {
        path: hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() for path in CPF_TEST_PATHS}}
    service = JobService(runtime)
    original = service.submit(JobSpec(project_id='fixture', command_ref='cpf_replay',
        idempotency_key='cpf:unit-cpf', source_request_id='unit-cpf', stage_run_id='CPF-verification',
        parameters_digest=state['source_revision']), input_payload=payload)
    state['cpf_job_id'] = original.job_id

    def terminal(job_id):
        until = time.monotonic() + 40
        while time.monotonic() < until:
            record = service.reconcile(job_id)
            if record.state in {'completed', 'failed', 'cancelled'}:
                return record
            time.sleep(.1)
        service.cancel(job_id)
        raise AssertionError('bounded unit CPF job did not finish')

    assert terminal(original.job_id).state == 'failed'
    state['updated_at'] = utc_now_iso()
    assert cpf_retry_proofs(permit, state) == {}
    command['argv'] = [sys.executable, '-m', 'dev_orchestrator.soak.cpf_runner', '--job-dir', '{job_dir}']
    write_json(runtime / 'execution-jobs.json', config)
    service = JobService(runtime)
    successor = service.retry(original.job_id, 'fix-corpus-working-directory')
    assert service.retry(original.job_id, 'fix-corpus-working-directory').job_id == successor.job_id
    assert terminal(successor.job_id).state == 'completed'
    state['updated_at'] = utc_now_iso()
    proofs = cpf_retry_proofs(permit, state)
    assert set(proofs) == {f'CPF-{i:02d}' for i in range(1, 11)}
    assert all(row['job_lineage'] == [original.job_id, successor.job_id] for row in proofs.values())
    assert service.status(original.job_id).state == 'failed'
    xml = service.store._job_dir(successor.job_id) / 'cpf.xml'
    xml.write_text('<broken', encoding='utf-8')
    assert cpf_retry_proofs(permit, state) == {}
