"""P17 Convergence Invariants registry and evaluators tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.evaluator import DecisionKind, decide
from dev_orchestrator.convergence.evidence import (
    ConflictClaim,
    EvidenceItem,
    EvidenceSnapshot,
)
from dev_orchestrator.convergence.invariants import (
    CONVERGENCE_INVARIANT_CODES,
    CPF_SCENARIO_MAP,
    LIFECYCLE_INVARIANT_MAP,
    evaluate_convergence_invariants,
)
from dev_orchestrator.convergence.policy import build_policy
from dev_orchestrator.convergence.verification import is_goal_satisfied
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

    def test_unresolved_ambiguity_blocks_satisfy_and_publish(self) -> None:
        """Finding 3: Unresolved ambiguity in evidence snapshot must fail closed before satisfaction or publication."""
        done_record = validate_work_record({
            **self.work_record.to_dict(),
            "status": "DONE",
            "acceptance": {
                "kind": "VERIFIED",
                "reviewer_verdict_id": "v-1",
                "anchor_head": "head-clean",
            },
            "verification": {
                "verification_id": "v-1",
                "exact_head": "head-clean",
                "clean_status_fingerprint": "clean",
                "acceptance_criterion_results": {"crit-1": True},
                "executed_checks": [{"command": "pytest", "exit_status": 0}],
                "reviewer_decision": "ACCEPT",
                "structured_findings": [],
            },
            "successor": {
                "successor_goal_id": "P18",
                "successor_spec_digest": "sha256:p18",
                "publication_state": "PENDING",
                "handoff_idempotency_key": "idemp-p18",
            },
        })

        # Evidence snapshot with unresolved conflict
        ambiguous_ev = EvidenceSnapshot(
            items=(),
            conflicts=(
                ConflictClaim(
                    source_a="repo_truth",
                    source_b="broker_effect",
                    conflict_type="HEAD_DIVERGENCE",
                    details="Unreconciled head divergence between repo and broker",
                    resolved=False,
                ),
            ),
            shared_leases=(),
            exact_anchors={"head": "head-clean"},
            emergency_pause_asserted=False,
            metadata={},
        )

        # 1. is_goal_satisfied must evaluate to False
        satisfied, reason = is_goal_satisfied(done_record, evidence=ambiguous_ev)
        self.assertFalse(satisfied)
        self.assertIn("ambiguity", reason.lower())

        # 2. decide() must fail closed to REQUEST_HUMAN, citing FAIL_CLOSED_AMBIGUITY
        decision = decide(done_record, ambiguous_ev, self.policy)
        self.assertEqual(decision.kind, DecisionKind.REQUEST_HUMAN)
        self.assertIn("FAIL_CLOSED_AMBIGUITY", decision.invariant_citations)
        self.assertIn("ambiguity", decision.reason.lower())
