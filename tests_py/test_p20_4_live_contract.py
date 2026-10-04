"""Adversarial acceptance checks; synthetic fixtures are not live soak evidence."""
from pathlib import Path
from unittest.mock import patch
import unittest

import test_p20_4_external_binding as binding_fixture
from dev_orchestrator.soak.faults import SOAK_FAULTS
from dev_orchestrator.soak.qualification import evaluate_soak_qualification
from dev_orchestrator.soak.timeline import open_soak_run, resume_soak_run, compute_run_duration
from dev_orchestrator.soak.permit import permit_fingerprint, validate_soak_permit


class LiveContractTests(unittest.TestCase):
    def test_remote_envelope_cannot_enable_soak_binding(self):
        from dev_orchestrator.control.command_store import canonical_request, EXPECTED_IDENTITY_FIELDS
        request = {'schema_version': 1, 'command_id': 'probe', 'project_id': 'devorchestrator',
                   'action': 'soak_bind', 'expected': {key: None for key in EXPECTED_IDENTITY_FIELDS},
                   'target': {'permit_path': 'missing', 'source_request_id': 'missing'}}
        with self.assertRaises(ValueError):
            canonical_request(request)
        self.assertEqual(canonical_request(request, local=True)['action'], 'soak_bind')

    def test_start_requires_existing_owned_execution(self):
        result = self.fixture.executor.start_and_bind_soak('missing', str(self.fixture.permit_path))
        self.assertEqual(result['status'], 'rejected')

    def setUp(self):
        self.fixture = binding_fixture.TestExternalExecutionBinding()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_missing_permit_and_nonexistent_job_cannot_bind(self):
        result = self.fixture.executor.bind_external_execution('req-p20-4-001', {
            'binding_kind': 'p20_soak_run', 'job_id': 'nonexistent-job',
            'soak_run_id': 'unproved-run', 'terminally_satisfied': True,
        })
        self.assertNotEqual(result.get('status'), 'bound')

    def test_permit_alone_does_not_prove_job_ownership(self):
        data = self.fixture.executor.state()
        data['executions']['req-p20-4-001']['state'] = 'running'
        self.fixture.executor._save_ledger(data)  # isolated test fixture only
        result = self.fixture.executor.bind_external_execution('req-p20-4-001', {
            'binding_kind': 'p20_soak_run', 'job_id': 'nonexistent-job',
            'permit_path': str(self.fixture.permit_path),
            'permit_fingerprint': self.fixture.permit.fingerprint,
            'soak_run_id': self.fixture.permit.run_id,
            'soak_project_id': self.fixture.permit.soak_project_id,
        })
        self.assertNotEqual(result.get('status'), 'bound')

    def test_labels_and_unobserved_zero_counts_do_not_qualify(self):
        permit = self.fixture.permit
        state = open_soak_run(permit)
        state['completed_tasks'] = [f'T{i}' for i in range(1, 6)]
        state['task_evidence'] = {}
        state['fault_dispositions'] = {
            fault.fault_id: {'disposition': 'automatic_recovery'} for fault in SOAK_FAULTS
        }
        state['cpf_convergences'] = {f'CPF-{i:02d}': 'unproved' for i in range(1, 11)}
        state['hard_zeros'] = {key: 0 for key in (
            'manual_continue', 'lifecycle_repair', 'duplicate_worker', 'duplicate_job',
            'duplicate_handoff', 'duplicate_successor', 'wrong_task_execution',
            'lifecycle_invariant_escape', 'silent_stall', 'infinite_retry', 'restart_regression',
        )}
        duration = {'continuity_ok': True, 'monotonic_seconds': 21601.,
                    'wall_seconds': 21601., 'segments': [], 'discontinuity_reason': None}
        with patch('dev_orchestrator.soak.qualification.load_soak_run', return_value=state), \
             patch('dev_orchestrator.soak.qualification.compute_run_duration', return_value=duration):
            report = evaluate_soak_qualification(self.fixture.canonical_runtime, permit)
        self.assertNotEqual(report['verdict'], 'ACCEPT')

    def test_self_declared_verification_flags_cannot_replace_source_ledgers(self):
        state = open_soak_run(self.fixture.permit)
        state['completed_tasks'] = [f'SOAK-T{i}' for i in range(1, 6)]
        state['task_evidence'] = {task: {'planner_id': 'invented-plan', 'worker_source_request_id': 'invented-worker',
            'review_id': 'invented-review', 'commit': 'invented-commit', 'worker_provider': 'a',
            'reviewer_provider': 'b', 'ledger_verified': True} for task in state['completed_tasks']}
        state['fault_dispositions'] = {fault.fault_id: {'injection_id': 'invented-injection',
            'incident_id': 'invented-incident', 'failure_family': 'invented-family',
            'evidence_verified': True, 'mode': 'live_injection', 'disposition': 'automatic_recovery'} for fault in SOAK_FAULTS}
        state['cpf_convergences'] = {f'CPF-{i:02d}': 'invented-convergence' for i in range(1, 11)}
        state['hard_zeros'] = {key: 0 for key in state['hard_zeros']}
        state['metric_coverage'] = {key: {'coverage': 'full', 'source': 'invented-source'} for key in state['hard_zeros']}
        duration = {'continuity_ok': True, 'monotonic_seconds': 21601., 'wall_seconds': 21601.}
        with patch('dev_orchestrator.soak.qualification.load_soak_run', return_value=state), \
             patch('dev_orchestrator.soak.qualification.compute_run_duration', return_value=duration):
            self.assertNotEqual(evaluate_soak_qualification(self.fixture.canonical_runtime, self.fixture.permit)['verdict'], 'ACCEPT')

    def test_permit_canonical_identity_cannot_be_self_declared(self):
        permit = self.fixture.permit
        permit.canonical_runtime_root = str(self.fixture.temp_dir / 'unrelated-runtime')
        permit.fingerprint = permit_fingerprint(permit)
        ok, _ = validate_soak_permit(permit, self.fixture.canonical_repo, self.fixture.canonical_runtime)
        self.assertFalse(ok)

    def test_permit_identifiers_cannot_escape_evidence_directory(self):
        permit = self.fixture.permit
        permit.run_id = '../escape'
        permit.fingerprint = permit_fingerprint(permit)
        # A matching untrusted marker cannot make a traversal-bearing identifier safe.
        import json
        (self.fixture.disposable_repo / '.devorch-disposable-soak').write_text(
            json.dumps({'run_id': permit.run_id, 'project_id': permit.soak_project_id}), encoding='utf-8')
        ok, _ = validate_soak_permit(permit, self.fixture.canonical_repo, self.fixture.canonical_runtime)
        self.assertFalse(ok)

    def test_resume_is_observation_without_new_clock_segment(self):
        first = open_soak_run(self.fixture.permit)
        path = self.fixture.disposable_runtime / ('run-' + self.fixture.permit.run_id + '.json')
        before = path.read_bytes()
        resumed = resume_soak_run(self.fixture.permit)
        self.assertEqual(resumed['segments'], first['segments'])
        self.assertEqual(path.read_bytes(), before)

    def test_discontinuous_boot_segments_cannot_be_summed(self):
        state = open_soak_run(self.fixture.permit)
        state['segments'] = [
            {'boot_id': 'old-boot', 'start_mono': 0., 'end_mono': 10800.},
            {'boot_id': 'new-boot', 'start_mono': 0., 'end_mono': 10800.},
        ]
        self.assertFalse(compute_run_duration(state)['continuity_ok'])
