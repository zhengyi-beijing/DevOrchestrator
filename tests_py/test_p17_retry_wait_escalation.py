"""P17 Bounded retry, wait, failover, escalation, and exhaustion tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.evaluator import DecisionKind, decide
from dev_orchestrator.convergence.evidence import EvidenceSnapshot
from dev_orchestrator.convergence.policy import ProblemBudget, build_policy
from dev_orchestrator.convergence.problems import FailureClass
from dev_orchestrator.convergence.work_record import validate_work_record


class TestP17RetryWaitEscalation(unittest.TestCase):
    def setUp(self) -> None:
        self.base_record = {
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
        }
        self.policy = build_policy(
            default_budget=ProblemBudget(
                max_resource_attempts=2,
                max_strategy_attempts=2,
                max_capability_escalations=2,
                max_total_attempts=5,
            ),
            quota_reset_rules={
                "rate_limit": {"reset_at": "2026-09-27T16:00:00Z"},
            },
        )

    def test_known_quota_reset_produces_budget_free_wait(self) -> None:
        rec = validate_work_record({
            **self.base_record,
            "current_problem": {
                "problem_id": "p-quota",
                "failure_class": FailureClass.RESOURCE_TRANSIENT.value,
                "criterion_or_invariant_id": "rate_limit",
                "normalized_fingerprint": "fp-rate",
            },
            "attempts": [],
        })
        decision = decide(rec, EvidenceSnapshot(), self.policy, now="2026-09-27T12:00:00Z")
        self.assertEqual(decision.kind, DecisionKind.WAIT_UNTIL)
        self.assertIn("Quota or resource transient failure", decision.reason)
        self.assertEqual(decision.parameters.get("not_before"), "2026-09-27T16:00:00Z")

    def test_transient_resource_fails_over_at_same_tier_first(self) -> None:
        rec = validate_work_record({
            **self.base_record,
            "current_problem": {
                "problem_id": "p-transient",
                "failure_class": FailureClass.PROVIDER_UNAVAILABLE.value,
                "normalized_fingerprint": "fp-transient",
            },
            "attempts": [],
        })
        decision = decide(rec, EvidenceSnapshot(), self.policy)
        self.assertEqual(decision.kind, DecisionKind.FAILOVER_RESOURCE)

    def test_strategy_exhaustion_escalates_capability(self) -> None:
        rec = validate_work_record({
            **self.base_record,
            "current_problem": {
                "problem_id": "p-defect",
                "failure_class": FailureClass.IMPLEMENTATION_DEFECT.value,
                "normalized_fingerprint": "fp-defect",
            },
            "attempts": [
                {"attempt_id": "a1", "problem_id": "p-defect", "strategy_id": "strategy_change", "typed_outcome": "fail"},
                {"attempt_id": "a2", "problem_id": "p-defect", "strategy_id": "strategy_change", "typed_outcome": "fail"},
            ],
        })
        decision = decide(rec, EvidenceSnapshot(), self.policy)
        self.assertEqual(decision.kind, DecisionKind.ESCALATE_CAPABILITY)

    def test_total_exhaustion_writes_handoff(self) -> None:
        rec = validate_work_record({
            **self.base_record,
            "status": "NEEDS_HUMAN",
            "current_problem": {
                "problem_id": "p-exhausted",
                "failure_class": FailureClass.IMPLEMENTATION_DEFECT.value,
                "normalized_fingerprint": "fp-exhausted",
            },
            "attempts": [
                {"attempt_id": f"a{i}", "problem_id": "p-exhausted", "strategy_id": "strategy_change", "typed_outcome": "fail"}
                for i in range(5)
            ],
        })
        decision = decide(rec, EvidenceSnapshot(), self.policy)
        self.assertEqual(decision.kind, DecisionKind.WRITE_HANDOFF)

    def test_elapsed_quota_reset_wakes_to_failover_without_manual_continue(self) -> None:
        """Finding 4: When quota reset timestamp elapses, evaluator wakes to FAILOVER_RESOURCE automatically."""
        rec = validate_work_record({
            **self.base_record,
            "current_problem": {
                "problem_id": "p-quota-elapsed",
                "failure_class": FailureClass.RESOURCE_TRANSIENT.value,
                "criterion_or_invariant_id": "rate_limit",
                "normalized_fingerprint": "fp-rate-elapsed",
            },
            "attempts": [],
        })
        policy = build_policy(
            default_budget=ProblemBudget(
                max_resource_attempts=2,
                max_strategy_attempts=2,
                max_capability_escalations=2,
                max_total_attempts=5,
            ),
            quota_reset_rules={
                "rate_limit": {"reset_at": "2026-09-27T12:00:00Z"},
            },
        )

        # 1. Before reset time -> emits WAIT_UNTIL
        dec_before = decide(rec, EvidenceSnapshot(), policy, now="2026-09-27T11:59:00Z")
        self.assertEqual(dec_before.kind, DecisionKind.WAIT_UNTIL)
        self.assertEqual(dec_before.parameters.get("not_before"), "2026-09-27T12:00:00Z")

        # 2. After reset time has elapsed -> automatically wakes to FAILOVER_RESOURCE
        dec_after = decide(rec, EvidenceSnapshot(), policy, now="2026-09-27T12:00:05Z")
        self.assertEqual(dec_after.kind, DecisionKind.FAILOVER_RESOURCE)
        self.assertIn("FAILOVER_RESOURCE", dec_after.kind.value)
