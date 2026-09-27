"""P17 Convergence Invariants registry and evaluators tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.evidence import EvidenceSnapshot
from dev_orchestrator.convergence.invariants import (
    CONVERGENCE_INVARIANT_CODES,
    CPF_SCENARIO_MAP,
    LIFECYCLE_INVARIANT_MAP,
    evaluate_convergence_invariants,
)
from dev_orchestrator.convergence.policy import build_policy
from dev_orchestrator.convergence.work_record import validate_work_record


class TestP17Invariants(unittest.TestCase):
    def setUp(self) -> None:
        self.work_record = validate_work_record({
            "schema_version": 1,
            "project_id": "devorchestrator",
            "goal_id": "P17",
            "goal_revision": 1,
            "goal_spec_digest": "sha256:spec-1",
            "predecessor_goal_id": "P16.14",
            "repository_identity": {"repo_path": "C:\\repo", "branch": "main"},
            "status": "OPEN",
            "acceptance": {"kind": "NONE"},
            "authority_revision": "rev-1",
        })
        self.evidence = EvidenceSnapshot()
        self.policy = build_policy()

    def test_twenty_one_invariant_codes_exist(self) -> None:
        self.assertEqual(len(CONVERGENCE_INVARIANT_CODES), 21)
        expected = {
            "SINGLE_AUTHORITY",
            "SINGLE_ACTIVE_LEASE",
            "PROGRESS_TOTALITY",
            "ACCEPTANCE_BEFORE_ADVANCE",
            "OWNER_OVERRIDE_EXPLICIT",
            "HUMAN_REQUEST_DISCHARGEABLE",
            "MARKDOWN_NON_AUTHORITY",
            "ANCHOR_BINDING",
            "IDEMPOTENT_REPLAY",
            "STABLE_PROBLEM_IDENTITY",
            "BOUNDED_PROBLEM",
            "COMPLETE_EXHAUSTION",
            "HUMAN_TYPED",
            "EMERGENCY_BRAKE",
            "FAIL_CLOSED_AMBIGUITY",
            "LEARNED_CONSTRAINT_CONSUMPTION",
            "LEARNING_REGRESSION",
            "SUCCESSOR_DETERMINISM",
            "NO_HUMAN_CLOCK",
            "WAIT_IS_NOT_PROGRESS",
            "INVARIANT_CODE_STABILITY",
        }
        self.assertEqual(set(CONVERGENCE_INVARIANT_CODES), expected)

    def test_lifecycle_and_cpf_mappings(self) -> None:
        # All 21 codes must be mapped to at least one lifecycle invariant
        for code in CONVERGENCE_INVARIANT_CODES:
            self.assertIn(code, LIFECYCLE_INVARIANT_MAP, f"Code {code} missing lifecycle mapping")
            self.assertTrue(len(LIFECYCLE_INVARIANT_MAP[code]) > 0)

        # CPF-01 through CPF-10 must all be mapped
        for i in range(1, 11):
            cpf_id = f"CPF-{i:02d}"
            self.assertIn(cpf_id, CPF_SCENARIO_MAP)
            for inv in CPF_SCENARIO_MAP[cpf_id]:
                self.assertIn(inv, CONVERGENCE_INVARIANT_CODES)

    def test_pure_evaluator_evaluates_all_invariants(self) -> None:
        verdicts = evaluate_convergence_invariants(self.work_record, self.evidence, self.policy)
        self.assertEqual(len(verdicts), 21)
        for code in CONVERGENCE_INVARIANT_CODES:
            self.assertIn(code, verdicts)
            v = verdicts[code]
            self.assertEqual(v.code, code)
            self.assertIsInstance(v.holds, bool)
            self.assertIsInstance(v.details, str)
            self.assertTrue(bool(v.details))
