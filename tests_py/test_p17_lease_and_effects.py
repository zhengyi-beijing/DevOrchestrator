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

    def test_dead_lease_with_answered_human_request_emits_and_admits_execute(self) -> None:
        """Finding 1: Answered human_request on dead lease emits EXECUTE and ActuatorGuard admits it without deadlock."""
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
            "human_request": {
                "question_id": "q-dead-lease-resume",
                "question_revision": 1,
                "request_type": "product_choice",
                "concrete_question": "Resume after worker exit?",
                "options": [
                    {"option_id": "opt-resume", "description": "Resume execution", "effect": "resume"}
                ],
                "answer": {
                    "question_id": "q-dead-lease-resume",
                    "question_revision": 1,
                    "option_id": "opt-resume",
                    "owner_id": "owner-1",
                    "command_id": "cmd-ans-1",
                    "answered_at": "2026-09-27T00:02:00Z",
                },
            },
        })
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
        policy = build_policy()
        # 1. Evaluator emits EXECUTE
        decision = decide(work_rec, dead_evidence, policy)
        self.assertEqual(decision.kind, DecisionKind.EXECUTE)
        self.assertIn("HUMAN_REQUEST_DISCHARGEABLE", decision.invariant_citations)

        # 2. ActuatorGuard admits EXECUTE on demonstrably dead lease (no LEASE_ALREADY_ACTIVE deadlock)
        guard = ActuatorGuard()
        verdict = guard.validate(decision, work_rec, dead_evidence, expected_anchor_head="head-1")
        self.assertTrue(verdict.accepted, f"Guard rejected EXECUTE on dead lease: {verdict.reason}")
        self.assertIsNone(verdict.rejection_code)

    def test_live_lease_with_answered_human_request_emits_noop_and_rejects_execute(self) -> None:
        """Finding 1: Live lease with answered human_request produces NOOP_ACTIVE and guard rejects EXECUTE."""
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
            "human_request": {
                "question_id": "q-live-lease-resume",
                "question_revision": 1,
                "request_type": "product_choice",
                "concrete_question": "Resume while running?",
                "options": [
                    {"option_id": "opt-resume", "description": "Resume execution", "effect": "resume"}
                ],
                "answer": {
                    "question_id": "q-live-lease-resume",
                    "question_revision": 1,
                    "option_id": "opt-resume",
                    "owner_id": "owner-1",
                    "command_id": "cmd-ans-1",
                    "answered_at": "2026-09-27T00:02:00Z",
                },
            },
        })
        live_evidence = EvidenceSnapshot(
            items=(
                EvidenceItem(
                    source="process_probe",
                    source_id="probe-live-1",
                    timestamp="2026-09-27T00:01:00Z",
                    anchor="head-1",
                    data={"role": "worker", "alive": True},
                    digest="sha256:live",
                    read_status="OK",
                ),
            ),
            conflicts=(),
            shared_leases=(),
            exact_anchors={"head": "head-1"},
            emergency_pause_asserted=False,
            metadata={},
        )
        policy = build_policy()
        # 1. Evaluator emits NOOP_ACTIVE (worker is already live)
        decision = decide(work_rec, live_evidence, policy)
        self.assertEqual(decision.kind, DecisionKind.NOOP_ACTIVE)
        self.assertIn("SINGLE_ACTIVE_LEASE", decision.invariant_citations)

        # 2. Guard rejects raw EXECUTE attempt against live lease
        guard = ActuatorGuard()
        exec_decision = Decision(
            kind=DecisionKind.EXECUTE,
            reason="Concurrent execution attempt",
            idempotency_key="idemp-exec-live",
        )
        verdict = guard.validate(exec_decision, work_rec, live_evidence, expected_anchor_head="head-1")
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.rejection_code, "LEASE_ALREADY_ACTIVE")

    def test_contradictory_process_probes_fails_closed_evaluator_and_guard(self) -> None:
        """Finding 2: Contradictory process_probe rows for same role fail closed to REQUEST_HUMAN in evaluator and are rejected by guard."""
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
            "current_problem": {
                "problem_id": "p-1",
                "failure_class": "IMPLEMENTATION_DEFECT",
                "normalized_fingerprint": "fp-1",
            },
        })
        # Two contradictory process_probe rows: one says alive=True, other says alive=False
        contradictory_evidence = EvidenceSnapshot(
            items=(
                EvidenceItem(
                    source="process_probe",
                    source_id="probe-1",
                    timestamp="2026-09-27T00:01:00Z",
                    anchor="head-1",
                    data={"role": "worker", "alive": True},
                    digest="sha256:p1",
                    read_status="OK",
                ),
                EvidenceItem(
                    source="process_probe",
                    source_id="probe-2",
                    timestamp="2026-09-27T00:01:05Z",
                    anchor="head-1",
                    data={"role": "worker", "alive": False},
                    digest="sha256:p2",
                    read_status="OK",
                ),
            ),
            conflicts=(),
            shared_leases=(),
            exact_anchors={"head": "head-1"},
            emergency_pause_asserted=False,
            metadata={},
        )
        policy = build_policy()
        # 1. Evaluator fails closed on contradictory authority
        decision = decide(work_rec, contradictory_evidence, policy)
        self.assertEqual(decision.kind, DecisionKind.REQUEST_HUMAN)
        self.assertIn("FAIL_CLOSED_AMBIGUITY", decision.invariant_citations)
        self.assertEqual(decision.problem_id, "ambiguous_lease_liveness")

        # 2. Guard rejects execution / recovery against ambiguous/contradictory lease
        guard = ActuatorGuard()
        for kind in (DecisionKind.EXECUTE, DecisionKind.RETRY_NEW_STRATEGY):
            d = Decision(kind=kind, reason="attempt", idempotency_key=f"idemp-{kind.value}")
            verdict = guard.validate(d, work_rec, contradictory_evidence, expected_anchor_head="head-1")
            self.assertFalse(verdict.accepted)
            self.assertEqual(verdict.rejection_code, "LEASE_ALREADY_ACTIVE")

    def test_terminal_success_lease_emits_verify_and_guard_admits_it(self) -> None:
        """Finding 3: Active lease with broker terminal success emits VERIFY and guard admits it."""
        from dev_orchestrator.convergence.evidence import EvidenceItem

        succ_evidence = EvidenceSnapshot(
            items=(
                EvidenceItem(
                    source="broker_effect",
                    source_id="broker-1",
                    timestamp="2026-09-27T00:01:00Z",
                    anchor="head-succ",
                    data={"execution_id": "exec-1", "role": "worker", "state": "succeeded"},
                    digest="sha256:succ-1",
                    read_status="OK",
                ),
            ),
            conflicts=(),
            shared_leases=(),
            exact_anchors={"head": "head-succ"},
            emergency_pause_asserted=False,
            metadata={},
        )
        work_rec = validate_work_record({
            "schema_version": 1,
            "project_id": "test-succ",
            "goal_id": "P17",
            "goal_revision": 1,
            "goal_spec_digest": "s",
            "predecessor_goal_id": "p0",
            "repository_identity": {"repo_path": ".", "branch": "main"},
            "status": "OPEN",
            "acceptance": {"kind": "NONE"},
            "authority_revision": "rev-1",
            "active_lease": self.active_lease,
            "attempts": [
                {
                    "attempt_id": "att-1",
                    "problem_id": "p1",
                    "strategy_id": "worker_run",
                    "typed_outcome": "done",
                }
            ],
        })
        policy = build_policy()
        # 1. Evaluator emits VERIFY rather than failing closed to REQUEST_HUMAN
        decision = decide(work_rec, succ_evidence, policy)
        self.assertEqual(decision.kind, DecisionKind.VERIFY)
        self.assertTrue(decision.parameters.get("terminal_success"))
        self.assertEqual(decision.parameters.get("role"), "worker")

        # 2. Guard admits VERIFY on terminal_success lease
        guard = ActuatorGuard(work_record=work_rec)
        verdict = guard.validate(
            decision,
            work_rec,
            succ_evidence,
            expected_anchor_head="head-succ",
            expected_goal_id="P17",
            expected_authority_revision="rev-1",
        )
        self.assertTrue(verdict.accepted, f"Guard rejected VERIFY: {verdict.reason}")
