"""Unit tests for P16.11 AGYResourcePool telemetry, concurrency, sliding window, and cooldown."""
import unittest
from datetime import datetime, timedelta, timezone

from dev_orchestrator.pool.agy_pool import AGYResourcePool
from dev_orchestrator.pool.models import PoolAccount, PoolResource, QuotaState


class TestAGYResourcePool(unittest.TestCase):
    def setUp(self):
        self.pool = AGYResourcePool()

    def test_pool_initialization_defaults(self):
        """All 3 AGY accounts are registered with expected properties."""
        self.assertEqual(self.pool.account_ids, ("agy-1", "agy-2", "agy-3"))
        telemetry = self.pool.telemetry()
        self.assertEqual(telemetry.total_accounts, 3)
        self.assertEqual(telemetry.available_accounts, 3)
        self.assertEqual(telemetry.in_cooldown_accounts, 0)
        self.assertEqual(telemetry.exhausted_accounts, 0)
        self.assertEqual(telemetry.active_requests, 0)

        for acc_id in ("agy-1", "agy-2", "agy-3"):
            acc_t = telemetry.accounts[acc_id]
            self.assertEqual(acc_t.provider, "agy")
            self.assertTrue(acc_t.available)
            self.assertFalse(acc_t.in_cooldown)
            self.assertEqual(acc_t.quota_state, QuotaState.HEALTHY)
            self.assertEqual(acc_t.max_concurrency, 1)
            self.assertEqual(acc_t.window_limit, 60)

    def test_parallel_load_balancing_dispatch(self):
        """Acquiring resources on independent tasks dispatches across available accounts in parallel."""
        # 1st acquire -> agy-1
        res1, msg1 = self.pool.acquire(role="worker")
        self.assertIsNotNone(res1)
        self.assertEqual(res1.account_id, "agy-1")

        # 2nd acquire while res1 is active -> agy-2 (parallel load balancing!)
        res2, msg2 = self.pool.acquire(role="worker")
        self.assertIsNotNone(res2)
        self.assertEqual(res2.account_id, "agy-2")

        # 3rd acquire while res1, res2 active -> agy-3
        res3, msg3 = self.pool.acquire(role="worker")
        self.assertIsNotNone(res3)
        self.assertEqual(res3.account_id, "agy-3")

        # Check telemetry shows all 3 busy
        tel = self.pool.telemetry()
        self.assertEqual(tel.active_requests, 3)

        # 4th acquire -> all accounts at max concurrency (1), acquire fails gracefully
        res4, msg4 = self.pool.acquire(role="worker")
        self.assertIsNone(res4)
        self.assertIn("max concurrency reached", msg4)

        # Release res1
        self.pool.release(res1.resource_id, success=True)
        tel_after = self.pool.telemetry()
        self.assertEqual(tel_after.active_requests, 2)
        self.assertEqual(tel_after.accounts["agy-1"].active_requests, 0)
        self.assertEqual(tel_after.accounts["agy-1"].successful_dispatches, 1)

        # Now acquire succeeds on agy-1
        res5, _ = self.pool.acquire(role="worker")
        self.assertIsNotNone(res5)
        self.assertEqual(res5.account_id, "agy-1")

        # Cleanup
        self.pool.release(res2.resource_id, success=True)
        self.pool.release(res3.resource_id, success=True)
        self.pool.release(res5.resource_id, success=True)
        self.assertEqual(self.pool.telemetry().active_requests, 0)

    def test_account_exclusion(self):
        """Excluded accounts are bypassed during acquisition."""
        res, msg = self.pool.acquire(role="worker", excluded_accounts={"agy-1", "agy-2"})
        self.assertIsNotNone(res)
        self.assertEqual(res.account_id, "agy-3")
        self.pool.release(res.resource_id, success=True)

        res_all_ex, msg_ex = self.pool.acquire(role="worker", excluded_accounts={"agy-1", "agy-2", "agy-3"})
        self.assertIsNone(res_all_ex)
        self.assertIn("excluded by request", msg_ex)

    def test_cooldown_arming_and_expiration(self):
        """Cooldown arms on quota/rate-limit failure and expires after duration."""
        t0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
        res, _ = self.pool.acquire(role="worker", now=t0)
        self.assertIsNotNone(res)
        acc_id = res.account_id

        # Release with rate_limited classification -> arms cooldown
        self.pool.release(
            res.resource_id,
            success=False,
            failure_classification="rate_limited",
            cooldown_seconds=300.0,
            now=t0,
        )

        tel = self.pool.telemetry(now=t0)
        self.assertTrue(tel.accounts[acc_id].in_cooldown)
        self.assertEqual(tel.accounts[acc_id].cooldown_reason, "rate_limited")
        self.assertEqual(tel.in_cooldown_accounts, 1)

        # Attempting to acquire bypasses the cooled account
        res2, _ = self.pool.acquire(role="worker", now=t0)
        self.assertIsNotNone(res2)
        self.assertNotEqual(res2.account_id, acc_id)
        self.pool.release(res2.resource_id, success=True, now=t0)

        # Fast forward time past cooldown expiration (+301s)
        t_after = t0 + timedelta(seconds=301)
        tel_after = self.pool.telemetry(now=t_after)
        self.assertFalse(tel_after.accounts[acc_id].in_cooldown)

        # Can now acquire acc_id again
        self.pool.trigger_cooldown(acc_id, 60.0, "manual_test", now=t_after)
        self.assertTrue(self.pool.telemetry(now=t_after).accounts[acc_id].in_cooldown)
        self.pool.clear_cooldown(acc_id)
        self.assertFalse(self.pool.telemetry(now=t_after).accounts[acc_id].in_cooldown)

    def test_sliding_window_quota_degradation(self):
        """Window usage transitions quota state through CONSERVE, LOW, EXHAUSTED."""
        acc = PoolAccount(account_id="tiny-acc", provider="agy", max_concurrency=10, window_limit=10)
        res = PoolResource(
            resource_id="agy/tiny-acc/test-model",
            account_id="tiny-acc",
            provider="agy",
            model="test-model",
            roles=frozenset({"worker"}),
        )
        custom_pool = AGYResourcePool(accounts=[acc], resources=[res])

        # 6 requests -> HEALTHY (60% < 65%)
        for _ in range(6):
            r, _ = custom_pool.acquire("worker")
            custom_pool.release(r.resource_id, success=True)
        self.assertEqual(custom_pool.telemetry().accounts["tiny-acc"].quota_state, QuotaState.HEALTHY)

        # 7th request -> CONSERVE (70% >= 65%)
        r, _ = custom_pool.acquire("worker")
        custom_pool.release(r.resource_id, success=True)
        self.assertEqual(custom_pool.telemetry().accounts["tiny-acc"].quota_state, QuotaState.CONSERVE)

        # 9th request -> LOW (90% >= 85%)
        for _ in range(2):
            r, _ = custom_pool.acquire("worker")
            custom_pool.release(r.resource_id, success=True)
        self.assertEqual(custom_pool.telemetry().accounts["tiny-acc"].quota_state, QuotaState.LOW)

        # 10th request -> EXHAUSTED (100% >= 100%)
        r, _ = custom_pool.acquire("worker")
        custom_pool.release(r.resource_id, success=True)
        self.assertEqual(custom_pool.telemetry().accounts["tiny-acc"].quota_state, QuotaState.EXHAUSTED)

        # 11th acquire fails due to EXHAUSTED quota
        r_fail, msg = custom_pool.acquire("worker")
        self.assertIsNone(r_fail)
        self.assertIn("quota EXHAUSTED", msg)


if __name__ == "__main__":
    unittest.main()
