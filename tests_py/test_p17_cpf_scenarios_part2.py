"""P17 CPF-06 through CPF-10 scenarios coverage.

Validates that pure convergence evaluator and replay model faithfully represent
CPF-06 through CPF-10 transition boundaries and expectations.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from dev_orchestrator.convergence.evaluator import DecisionKind, decide
from dev_orchestrator.convergence.evidence import (
    ConflictClaim,
    EvidenceItem,
    EvidenceSnapshot,
)
from dev_orchestrator.convergence.invariants import evaluate_convergence_invariants
from dev_orchestrator.convergence.policy import build_policy
from dev_orchestrator.convergence.replay import ReplayHarness
from dev_orchestrator.convergence.work_record import validate_work_record


class TestP17CPFScenariosPart2(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = ReplayHarness()
        self.corpus_dir = Path("tests_py/data/p17_corpus")

    def test_cpf_06_duplicate_events_idempotent(self) -> None:
        """CPF-06: Repeated daemon ticks / events do not duplicate execution."""
        case = self.harness.load_case_file(self.corpus_dir / "case_05_replayed_duplicate_command_or_event.json")
        res = self.harness.run_case(case)
        self.assertTrue(res.passed, f"CPF-06 replay failed: {res.details}")
        self.assertIn("CPF-06", case.cpf_scenarios)
        self.assertEqual(res.actual_decision.kind, DecisionKind.NOOP_ACTIVE)

    def test_cpf_07_crash_after_intent_converges_to_single_owner(self) -> None:
        """CPF-07: Crash after intent before publication recovers to single owner."""
        case = self.harness.load_case_file(self.corpus_dir / "case_26_crash_injection_between_durable_writes.json")
        res = self.harness.run_case(case)
        self.assertTrue(res.passed, f"CPF-07 replay failed: {res.details}")
        self.assertIn("CPF-07", case.cpf_scenarios)
        self.assertEqual(res.actual_decision.kind, DecisionKind.PUBLISH_SUCCESSOR)

    def test_cpf_08_missing_declaration_and_bounded_problem_fails_closed(self) -> None:
        """CPF-08: Exhausted problem or missing declaration fails closed without spinning."""
        case = self.harness.load_case_file(self.corpus_dir / "case_01_p1614_remediation_budget_deadlock.json")
        res = self.harness.run_case(case)
        self.assertTrue(res.passed, f"CPF-08 replay failed: {res.details}")
        self.assertIn("CPF-08", case.cpf_scenarios)
        self.assertEqual(res.actual_decision.kind, DecisionKind.WRITE_HANDOFF)

    def test_cpf_09_repaired_head_reconciles_and_discharges(self) -> None:
        """CPF-09: Repaired HEAD / human discharge advances cleanly."""
        case = self.harness.load_case_file(self.corpus_dir / "case_24_human_question_discharged_across_head_change.json")
        res = self.harness.run_case(case)
        self.assertTrue(res.passed, f"CPF-09 replay failed: {res.details}")
        self.assertIn("CPF-09", case.cpf_scenarios)
        self.assertEqual(res.actual_decision.kind, DecisionKind.EXECUTE)

    def test_cpf_10_same_head_repeated_ticks_are_byte_stable(self) -> None:
        """CPF-10: Repeated ticks on same unchanged evidence produce identical decision and key."""
        case = self.harness.load_case_file(self.corpus_dir / "case_05_replayed_duplicate_command_or_event.json")
        res1 = self.harness.run_case(case)
        res2 = self.harness.run_case(case)
        self.assertEqual(res1.actual_decision.kind, res2.actual_decision.kind)
        self.assertEqual(res1.actual_decision.idempotency_key, res2.actual_decision.idempotency_key)
