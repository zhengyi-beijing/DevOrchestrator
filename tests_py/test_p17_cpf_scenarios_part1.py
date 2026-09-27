"""P17 CPF-01 through CPF-05 scenarios coverage.

Validates that pure convergence evaluator and replay model faithfully represent
CPF-01 through CPF-05 transition boundaries and expectations.
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


class TestP17CPFScenariosPart1(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = ReplayHarness()
        self.corpus_dir = Path("tests_py/data/p17_corpus")

    def test_cpf_01_stale_predecessor_worker_fenced(self) -> None:
        """CPF-01: Stale predecessor worker ownership fences target successor."""
        case = self.harness.load_case_file(self.corpus_dir / "case_06_worker_process_death_stale_active_ownership.json")
        res = self.harness.run_case(case)
        self.assertTrue(res.passed, f"CPF-01 replay failed: {res.details}")
        self.assertIn("CPF-01", case.cpf_scenarios)
        self.assertEqual(res.actual_decision.kind, DecisionKind.RETRY_NEW_STRATEGY)

    def test_cpf_02_zero_touch_lost_handoff_recovers(self) -> None:
        """CPF-02: Lost successor handoff after acceptance recovers to publish successor."""
        case = self.harness.load_case_file(self.corpus_dir / "case_02_lost_successor_handoff.json")
        res = self.harness.run_case(case)
        self.assertTrue(res.passed, f"CPF-02 replay failed: {res.details}")
        self.assertIn("CPF-02", case.cpf_scenarios)
        self.assertEqual(res.actual_decision.kind, DecisionKind.PUBLISH_SUCCESSOR)

    def test_cpf_03_restart_mid_transition_replays_journal(self) -> None:
        """CPF-03: Restart during effect / mid-transition replays deterministically."""
        case = self.harness.load_case_file(self.corpus_dir / "case_21_restart_during_broker_effect.json")
        res = self.harness.run_case(case)
        self.assertTrue(res.passed, f"CPF-03 replay failed: {res.details}")
        self.assertIn("CPF-03", case.cpf_scenarios)
        self.assertEqual(res.actual_decision.kind, DecisionKind.NOOP_ACTIVE)

    def test_cpf_04_contradictory_authority_fails_closed(self) -> None:
        """CPF-04: Contradictory authority claims fail closed to human request."""
        case = self.harness.load_case_file(self.corpus_dir / "case_10_multi_owner_contradictory_authority_ambiguity.json")
        res = self.harness.run_case(case)
        self.assertTrue(res.passed, f"CPF-04 replay failed: {res.details}")
        self.assertIn("CPF-04", case.cpf_scenarios)
        self.assertEqual(res.actual_decision.kind, DecisionKind.REQUEST_HUMAN)

    def test_cpf_05_dirty_worktree_defers_without_mutation(self) -> None:
        """CPF-05: Dirty worktree waits without mutating repository."""
        case = self.harness.load_case_file(self.corpus_dir / "case_13_dirty_worktree_at_transition_boundary.json")
        res = self.harness.run_case(case)
        self.assertTrue(res.passed, f"CPF-05 replay failed: {res.details}")
        self.assertIn("CPF-05", case.cpf_scenarios)
        self.assertEqual(res.actual_decision.kind, DecisionKind.WAIT_UNTIL)
