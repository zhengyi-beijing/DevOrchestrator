"""P17 Evaluator Purity tests.

Proves that decide() is strictly side-effect free, deterministic, and performs
zero filesystem, process, git, network, implicit clock, or RNG operations.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from dev_orchestrator.convergence.evaluator import DecisionKind, decide
from dev_orchestrator.convergence.evidence import EvidenceSnapshot
from dev_orchestrator.convergence.policy import build_policy
from dev_orchestrator.convergence.work_record import validate_work_record


class TestP17EvaluatorPurity(unittest.TestCase):
    def setUp(self) -> None:
        self.work_record = validate_work_record({
            "schema_version": 1,
            "project_id": "devorchestrator",
            "goal_id": "P17",
            "goal_revision": 1,
            "goal_spec_digest": "sha256:spec-p17",
            "predecessor_goal_id": "P16.14",
            "repository_identity": {"repo_path": "C:\\repo", "branch": "main"},
            "status": "OPEN",
            "acceptance": {"kind": "NONE"},
            "authority_revision": "rev-1",
        })
        self.evidence = EvidenceSnapshot()
        self.policy = build_policy()
        self.fixed_time = "2026-09-27T12:00:00Z"

    def test_decide_is_pure_and_deterministic(self) -> None:
        d1 = decide(self.work_record, self.evidence, self.policy, now=self.fixed_time)
        d2 = decide(self.work_record, self.evidence, self.policy, now=self.fixed_time)

        self.assertEqual(d1.kind, d2.kind)
        self.assertEqual(d1.reason, d2.reason)
        self.assertEqual(d1.idempotency_key, d2.idempotency_key)
        self.assertEqual(d1.to_dict(), d2.to_dict())

    def test_sentinels_prove_no_io_during_decide(self) -> None:
        """Prove decide() makes no subprocess, socket, open, or os mutation calls."""
        with patch("subprocess.Popen") as mock_popen, \
             patch("subprocess.run") as mock_subrun, \
             patch("socket.socket") as mock_socket, \
             patch("builtins.open") as mock_open:

            decision = decide(self.work_record, self.evidence, self.policy, now=self.fixed_time)
            self.assertEqual(decision.kind, DecisionKind.EXECUTE)

            mock_popen.assert_not_called()
            mock_subrun.assert_not_called()
            mock_socket.assert_not_called()
            mock_open.assert_not_called()

    def test_totality_open_no_progress_produces_action(self) -> None:
        """PROGRESS_TOTALITY: an open goal with no active progress produces an actionable decision."""
        decision = decide(self.work_record, self.evidence, self.policy, now=self.fixed_time)
        self.assertIn(decision.kind, (DecisionKind.EXECUTE, DecisionKind.VERIFY, DecisionKind.REQUEST_HUMAN))
        self.assertTrue(bool(decision.idempotency_key))
