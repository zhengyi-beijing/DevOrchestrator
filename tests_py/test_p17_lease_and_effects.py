"""P17 Lease reconciliation and external effect model tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.actuator_guard import ActuatorGuard
from dev_orchestrator.convergence.effects import (
    EffectState,
    ExecutionEffectPort,
    reconcile_lease,
)
from dev_orchestrator.convergence.evaluator import Decision, DecisionKind, decide
from dev_orchestrator.convergence.evidence import EvidenceSnapshot
from dev_orchestrator.convergence.policy import build_policy
from dev_orchestrator.convergence.work_record import validate_work_record


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

    def test_lease_without_liveness_evidence_is_not_active_progress(self) -> None:
        """Finding 5: Lease without positive liveness evidence fails closed to REQUEST_HUMAN, not NOOP_ACTIVE."""
        work_rec = validate_work_record({
            "schema_version": 1,
            "project_id": "devorchestrator",
            "goal_id": "P17",
            "goal_revision": 1,
            "goal_spec_digest": "sha256:spec",
            "predecessor_goal_id": "P16.14",
            "repository_identity": {"repo_path": "C:\\repo", "branch": "main"},
            "status": "OPEN",
            "acceptance": {"kind": "NONE"},
            "authority_revision": "rev-1",
            "active_lease": self.active_lease,
        })
        # Empty evidence snapshot - no process_probe or broker_effect
        empty_evidence = EvidenceSnapshot(
            items=(),
            conflicts=(),
            shared_leases=(),
            exact_anchors={"head": "head-1"},
            emergency_pause_asserted=False,
            metadata={},
        )
        policy = build_policy()
        decision = decide(work_rec, empty_evidence, policy)
        # Must fail closed to REQUEST_HUMAN, NOT NOOP_ACTIVE
        self.assertNotEqual(decision.kind, DecisionKind.NOOP_ACTIVE)
        self.assertEqual(decision.kind, DecisionKind.REQUEST_HUMAN)
        self.assertIn("NO_ORPHAN_OWNER", decision.invariant_citations)
        self.assertIn("PROGRESS_TOTALITY", decision.invariant_citations)

    def test_guard_rejects_failover_and_escalation_while_lease_active(self) -> None:
        """Finding 6: Guard rejects FAILOVER_RESOURCE, ESCALATE_CAPABILITY, and VERIFY while a lease is active."""
        work_rec = validate_work_record({
            "schema_version": 1,
            "project_id": "devorchestrator",
            "goal_id": "P17",
            "goal_revision": 1,
            "goal_spec_digest": "sha256:spec",
            "predecessor_goal_id": "P16.14",
            "repository_identity": {"repo_path": "C:\\repo", "branch": "main"},
            "status": "OPEN",
            "acceptance": {"kind": "NONE"},
            "authority_revision": "rev-1",
            "active_lease": self.active_lease,
        })
        evidence = EvidenceSnapshot(
            items=(),
            conflicts=(),
            shared_leases=(),
            exact_anchors={"head": "head-1"},
            emergency_pause_asserted=False,
            metadata={},
        )
        guard = ActuatorGuard()

        for kind in (DecisionKind.FAILOVER_RESOURCE, DecisionKind.ESCALATE_CAPABILITY, DecisionKind.VERIFY):
            decision = Decision(
                kind=kind,
                reason=f"Attempting {kind.value}",
                idempotency_key=f"idemp-{kind.value}",
            )
            verdict = guard.validate(
                decision,
                work_rec,
                evidence,
                expected_anchor_head="head-1",
            )
            self.assertFalse(verdict.accepted, f"Expected {kind.value} to be rejected while lease active")
            self.assertEqual(verdict.rejection_code, "LEASE_ALREADY_ACTIVE")

    def test_dead_lease_recovery_decision_is_admitted_by_guard(self) -> None:
        """Finding 2: ActuatorGuard admits recovery decisions when lease is demonstrably dead."""
        from dev_orchestrator.convergence.evidence import EvidenceItem

        work_rec = validate_work_record({
            "schema_version": 1,
            "project_id": "devorchestrator",
            "goal_id": "P17",
            "goal_revision": 1,
            "goal_spec_digest": "sha256:spec",
            "predecessor_goal_id": "P16.14",
            "repository_identity": {"repo_path": "C:\\repo", "branch": "main"},
            "status": "OPEN",
            "acceptance": {"kind": "NONE"},
            "authority_revision": "rev-1",
            "active_lease": self.active_lease,
        })
        # Evidence with process_probe reporting alive=False
        dead_evidence = EvidenceSnapshot(
            items=(
                EvidenceItem(
                    source="process_probe",
                    source_id="probe-dead-1",
                    timestamp="2026-09-27T00:01:00Z",
                    anchor="head-1",
                    data={"role": "worker", "alive": False},
                    digest="sha256:dead",
                    read_status="OK",
                ),
            ),
            conflicts=(),
            shared_leases=(),
            exact_anchors={"head": "head-1"},
            emergency_pause_asserted=False,
            metadata={},
        )
        guard = ActuatorGuard()

        for kind in (
            DecisionKind.RETRY_NEW_STRATEGY,
            DecisionKind.RETRY_SAME_STRATEGY,
            DecisionKind.FAILOVER_RESOURCE,
            DecisionKind.ESCALATE_CAPABILITY,
        ):
            decision = Decision(
                kind=kind,
                reason=f"Recovery attempt {kind.value}",
                idempotency_key=f"idemp-dead-{kind.value}",
            )
            verdict = guard.validate(
                decision,
                work_rec,
                dead_evidence,
                expected_anchor_head="head-1",
            )
            self.assertTrue(verdict.accepted, f"Expected {kind.value} to be accepted for dead lease: {verdict.reason}")
            self.assertIsNone(verdict.rejection_code)
