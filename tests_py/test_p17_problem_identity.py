"""P17 Problem identity stability and volatile key rejection tests."""
from __future__ import annotations

import unittest

from dev_orchestrator.convergence.policy import ProblemBudget
from dev_orchestrator.convergence.problems import (
    FailureClass,
    ProblemTracker,
    normalized_problem_fingerprint,
    select_next_problem,
)


class TestP17ProblemIdentity(unittest.TestCase):
    def test_fingerprint_stable_across_non_semantic_variations(self) -> None:
        fp1 = normalized_problem_fingerprint(
            goal_id="P17",
            criterion_or_invariant_id="INVARIANT_FOO",
            failure_class=FailureClass.IMPLEMENTATION_DEFECT,
            semantic_error_family="type_error",
            capability_selector="default",
            stable_scope="project",
        )
        fp2 = normalized_problem_fingerprint(
            goal_id="P17",
            criterion_or_invariant_id="INVARIANT_FOO",
            failure_class=FailureClass.IMPLEMENTATION_DEFECT,
            semantic_error_family="type_error",
            capability_selector="default",
            stable_scope="project",
        )
        self.assertEqual(fp1, fp2)

    def test_volatile_keys_rejected(self) -> None:
        # Rejects timestamp in extra semantic
        with self.assertRaises(ValueError):
            normalized_problem_fingerprint(
                goal_id="P17",
                criterion_or_invariant_id="INV-1",
                failure_class=FailureClass.IMPLEMENTATION_DEFECT,
                semantic_error_family="err",
                extra_semantic={"timestamp": "2026-09-27T00:00:00Z"},
            )

        # Rejects head / commit
        with self.assertRaises(ValueError):
            normalized_problem_fingerprint(
                goal_id="P17",
                criterion_or_invariant_id="INV-1",
                failure_class=FailureClass.IMPLEMENTATION_DEFECT,
                semantic_error_family="err",
                extra_semantic={"head": "da699cd7238ed39caf62fb229ba82a2e4f392734"},
            )

        # Rejects pid
        with self.assertRaises(ValueError):
            normalized_problem_fingerprint(
                goal_id="P17",
                criterion_or_invariant_id="INV-1",
                failure_class=FailureClass.IMPLEMENTATION_DEFECT,
                semantic_error_family="err",
                extra_semantic={"pid": 12345},
            )

    def test_problem_tracker_exhaustion(self) -> None:
        tracker = ProblemTracker(
            problem_id="p-1",
            failure_class=FailureClass.IMPLEMENTATION_DEFECT,
            normalized_fingerprint="fp-1",
            first_seen_at="2026-09-27T00:00:00Z",
            strategy_attempts=2,
            capability_escalations=2,
            total_attempts=4,
        )
        budget = ProblemBudget(max_strategy_attempts=2, max_capability_escalations=2, max_total_attempts=5)
        self.assertTrue(tracker.is_exhausted(budget))

    def test_deterministic_next_problem_selection(self) -> None:
        p1 = ProblemTracker("prob-b", FailureClass.IMPLEMENTATION_DEFECT, "fp-b", "2026-09-27T00:01:00Z")
        p2 = ProblemTracker("prob-a", FailureClass.RESOURCE_TRANSIENT, "fp-a", "2026-09-27T00:00:00Z")

        # Stable hash ordering
        selected = select_next_problem([p1, p2], order="stable_hash")
        self.assertIsNotNone(selected)
        # Should be deterministic
        selected_again = select_next_problem([p1, p2], order="stable_hash")
        self.assertEqual(selected, selected_again)
