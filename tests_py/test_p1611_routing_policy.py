"""Unit tests for P16.11 AGYFirstRoutingPolicy reviewer independence, same-failure escalation, and fallback."""
import unittest
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleRequest, ResourceContext
from dev_orchestrator.pool.agy_pool import AGYResourcePool
from dev_orchestrator.pool.models import (
    FailureSignature,
    PolicyDisposition,
    QuotaState,
    RoutingTier,
)
from dev_orchestrator.pool.routing_policy import AGYFirstRoutingPolicy


class TestAGYFirstRoutingPolicy(unittest.TestCase):
    def setUp(self):
        self.pool = AGYResourcePool()
        self.policy = AGYFirstRoutingPolicy(self.pool)
        self.work_dir = Path(".").resolve()

    def test_agy_first_normal_allocation(self):
        """Standard worker, planner, debugger, and evidence_packaging tasks route to AGY pool."""
        for role in ("worker", "planner", "debugger", "evidence_packaging"):
            req = AIRoleRequest(
                project_id="test_proj",
                task_run_id=f"t_{role}",
                role=role,
                prompt="test prompt",
                working_directory=self.work_dir,
            )
            rec = self.policy.route(req)
            self.assertEqual(rec.routing_tier, RoutingTier.AGY_POOL)
            self.assertEqual(rec.provider, "agy")
            self.assertFalse(rec.is_escalated)
            self.assertEqual(rec.policy_disposition, PolicyDisposition.SUPPORTED)
            self.assertFalse(rec.same_failure_prevented)
            # Release allocated resource
            self.pool.release(rec.selected_resource_id, success=True)

    def test_reviewer_provider_independence_enforcement(self):
        """When worker used AGY and review requires provider independence, AGY is rejected and escalated."""
        req = AIRoleRequest(
            project_id="test_proj",
            task_run_id="t_review_strict",
            role="reviewer",
            prompt="verify critical API patch",
            working_directory=self.work_dir,
            independence="provider",
            previous_resource_context=ResourceContext(
                provider="agy",
                account="agy-1",
                resource_id="agy/agy-1/gemini-3.8-flash-high",
            ),
        )

        rec = self.policy.route(req)
        self.assertEqual(rec.routing_tier, RoutingTier.INDEPENDENT_REVIEWER)
        self.assertNotEqual(rec.provider, "agy")
        self.assertTrue(rec.is_escalated)
        self.assertTrue(rec.independence_enforced)
        self.assertEqual(rec.policy_disposition, PolicyDisposition.UNSUPPORTED)
        self.assertIn("reviewer_independence_enforced", rec.escalation_reason)

    def test_reviewer_account_independence_allows_other_agy_account(self):
        """When worker used agy-1 and review requires account independence, another AGY account can be used."""
        req = AIRoleRequest(
            project_id="test_proj",
            task_run_id="t_review_acct",
            role="reviewer",
            prompt="verify low-risk patch",
            working_directory=self.work_dir,
            independence="account",
            previous_resource_context=ResourceContext(
                provider="agy",
                account="agy-1",
                resource_id="agy/agy-1/gemini-3.8-flash-high",
            ),
        )

        rec = self.policy.route(req)
        self.assertEqual(rec.routing_tier, RoutingTier.AGY_POOL)
        self.assertEqual(rec.provider, "agy")
        self.assertNotEqual(rec.account_id, "agy-1")  # agy-1 excluded
        self.assertIn(rec.account_id, ("agy-2", "agy-3"))
        self.assertTrue(rec.independence_enforced)
        self.assertEqual(rec.policy_disposition, PolicyDisposition.SUPPORTED)
        self.pool.release(rec.selected_resource_id, success=True)

    def test_same_failure_retry_blocks_blind_rotation_and_escalates(self):
        """Retrying an identical failure on AGY without strategy change blocks AGY rotation and triggers heterogeneous escalation."""
        sig = FailureSignature.from_error("AssertionError: test failed at line 42", category="unit_test")
        history = [
            {
                "attempt": 1,
                "provider": "agy",
                "resource_id": "agy/agy-1/gemini-3.8-flash-high",
                "failure_signature": sig.signature_hash,
            }
        ]

        req = AIRoleRequest(
            project_id="test_proj",
            task_run_id="t_worker_retry",
            role="worker",
            prompt="implement cache",
            working_directory=self.work_dir,
        )

        # Retry with SAME failure signature and NO strategy change
        rec = self.policy.route(
            req,
            failure_history=history,
            strategy_changed=False,
            current_failure_signature=sig.signature_hash,
        )

        self.assertEqual(rec.routing_tier, RoutingTier.HETEROGENEOUS_ESCALATION)
        self.assertNotEqual(rec.provider, "agy")
        self.assertTrue(rec.is_escalated)
        self.assertTrue(rec.same_failure_prevented)
        self.assertEqual(rec.policy_disposition, PolicyDisposition.UNSUPPORTED)
        self.assertIn("blind AGY account rotation prevented", rec.escalation_reason)

    def test_strategy_change_permits_second_agy_attempt(self):
        """When strategy has changed (e.g. failure memory injected), a second AGY attempt is permitted."""
        sig = FailureSignature.from_error("AssertionError: test failed at line 42", category="unit_test")
        history = [
            {
                "attempt": 1,
                "provider": "agy",
                "resource_id": "agy/agy-1/gemini-3.8-flash-high",
                "failure_signature": sig.signature_hash,
            }
        ]

        req = AIRoleRequest(
            project_id="test_proj",
            task_run_id="t_worker_retry_adapted",
            role="worker",
            prompt="implement cache with failure memory hint",
            working_directory=self.work_dir,
        )

        # Retry with strategy_changed=True!
        rec = self.policy.route(
            req,
            failure_history=history,
            strategy_changed=True,
            current_failure_signature=sig.signature_hash,
        )

        self.assertEqual(rec.routing_tier, RoutingTier.AGY_POOL)
        self.assertEqual(rec.provider, "agy")
        self.assertFalse(rec.is_escalated)
        self.assertFalse(rec.same_failure_prevented)
        self.assertEqual(rec.policy_disposition, PolicyDisposition.SUPPORTED)
        self.pool.release(rec.selected_resource_id, success=True)

    def test_pool_exhaustion_escalates_to_fallback(self):
        """When all AGY accounts are exhausted or in cooldown, requests escalate to heterogeneous paid fallback."""
        for acc_id in self.pool.account_ids:
            self.pool.set_quota_state(acc_id, QuotaState.EXHAUSTED)

        req = AIRoleRequest(
            project_id="test_proj",
            task_run_id="t_worker_fallback",
            role="worker",
            prompt="implement cache",
            working_directory=self.work_dir,
        )

        rec = self.policy.route(req)
        self.assertEqual(rec.routing_tier, RoutingTier.HETEROGENEOUS_ESCALATION)
        self.assertNotEqual(rec.provider, "agy")
        self.assertTrue(rec.is_escalated)
        self.assertEqual(rec.policy_disposition, PolicyDisposition.UNCERTAIN)
        self.assertIn("agy_pool_capacity_exhausted", rec.escalation_reason)


if __name__ == "__main__":
    unittest.main()
