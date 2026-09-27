"""P17 Lease reconciliation and external effect model tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.effects import (
    EffectState,
    ExecutionEffectPort,
    reconcile_lease,
)


class TestP17LeaseAndEffects(unittest.TestCase):
    def setUp(self) -> None:
        self.active_lease = {
            "role": "worker",
            "attempt_id": "att-1",
            "execution_id": "exec-1",
            "effect_id": "broker-eff-1",
            "host": "localhost",
            "pid": 12345,
            "epoch": "epoch-1",
            "acquired_at": "2026-09-27T00:00:00Z",
        }

    def test_reconcile_live_lease_retains_active_lease(self) -> None:
        res = reconcile_lease(self.active_lease, EffectState.LIVE)
        self.assertEqual(res["outcome"], "RETAIN_LIVE")
        self.assertIsNotNone(res["new_active_lease"])
        self.assertEqual(res["new_active_lease"]["role"], "worker")

    def test_reconcile_terminal_success_clears_lease(self) -> None:
        res = reconcile_lease(self.active_lease, EffectState.TERMINAL_SUCCESS)
        self.assertEqual(res["outcome"], "CLEARED_SUCCESS")
        self.assertIsNone(res["new_active_lease"])

    def test_reconcile_terminal_failure_clears_lease_and_tags_defect(self) -> None:
        res = reconcile_lease(self.active_lease, EffectState.TERMINAL_FAILURE)
        self.assertEqual(res["outcome"], "CLEARED_FAILED")
        self.assertIsNone(res["new_active_lease"])
        self.assertEqual(res["failure_class"], "IMPLEMENTATION_DEFECT")

    def test_reconcile_cancelled_clears_lease(self) -> None:
        res = reconcile_lease(self.active_lease, EffectState.CANCELLED)
        self.assertEqual(res["outcome"], "CLEARED_CANCELLED")
        self.assertIsNone(res["new_active_lease"])

    def test_reconcile_ambiguous_fails_closed_without_clearing(self) -> None:
        res = reconcile_lease(self.active_lease, EffectState.AMBIGUOUS)
        self.assertEqual(res["outcome"], "FAIL_CLOSED_AMBIGUOUS")
        self.assertIsNotNone(res["new_active_lease"])
        self.assertEqual(res["failure_class"], "INTEGRITY_OR_IDENTITY_AMBIGUITY")
