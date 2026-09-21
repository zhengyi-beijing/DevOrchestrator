import sys
import unittest
from datetime import datetime, time as dtime, timezone

SRC = Path = __import__("pathlib").Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.mobile.alerts import (
    DEFAULT_ALERT_POLICY,
    evaluate_notification,
    _in_quiet_hours,
)


class TestP15AlertPolicy(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 21, 14, 0, 0, tzinfo=timezone.utc)
        self.policy = dict(DEFAULT_ALERT_POLICY)
        self.transport_ok = {"connected": True, "degraded": False}
        self.ack_empty = {}

    def test_disjoint_alert_families(self):
        # Transport alert with progress=None
        transport_down = {"connected": False, "reason": "Tailscale disconnect"}
        dec = evaluate_notification(None, transport_down, self.policy, self.now, self.ack_empty)
        self.assertTrue(dec.should_notify)
        self.assertEqual(len(dec.notifications), 1)
        item = dec.notifications[0]
        self.assertEqual(item.family, "transport")
        self.assertTrue(item.dedup_key.startswith("transport:"))

        # Progress alert with transport=ok
        progress_stalled = {
            "project_id": "proj-x",
            "progress_observation_state": "authoritative",
            "watchdog_state": "agent_stalled",
            "watchdog_reason": "no heartbeat for 600s",
        }
        dec2 = evaluate_notification(progress_stalled, self.transport_ok, self.policy, self.now, self.ack_empty)
        self.assertTrue(dec2.should_notify)
        self.assertEqual(len(dec2.notifications), 1)
        item2 = dec2.notifications[0]
        self.assertEqual(item2.family, "progress")
        self.assertTrue(item2.dedup_key.startswith("progress:"))

        # Verify families and keys never overlap
        self.assertNotEqual(item.family, item2.family)
        self.assertFalse(item.dedup_key.startswith("progress:"))
        self.assertFalse(item2.dedup_key.startswith("transport:"))

    def test_stall_alert_only_from_authoritative_watchdog_stalled(self):
        # 1. Authoritative + agent_stalled -> Fires alert
        obs1 = {
            "project_id": "p1",
            "progress_observation_state": "authoritative",
            "watchdog_state": "agent_stalled",
        }
        dec1 = evaluate_notification(obs1, self.transport_ok, self.policy, self.now, self.ack_empty)
        self.assertTrue(dec1.should_notify)

        # 2. Authoritative + running -> No alert
        obs2 = {
            "project_id": "p1",
            "progress_observation_state": "authoritative",
            "watchdog_state": "running",
        }
        dec2 = evaluate_notification(obs2, self.transport_ok, self.policy, self.now, self.ack_empty)
        self.assertFalse(dec2.should_notify)

        # 3. Stale + agent_stalled -> Stale progress produces NO progress alerts
        obs3 = {
            "project_id": "p1",
            "progress_observation_state": "stale",
            "watchdog_state": "agent_stalled",
        }
        dec3 = evaluate_notification(obs3, self.transport_ok, self.policy, self.now, self.ack_empty)
        self.assertFalse(dec3.should_notify)

        # 4. Unavailable progress -> NO progress alerts
        obs4 = {
            "project_id": "p1",
            "progress_observation_state": "unavailable",
            "watchdog_state": "agent_stalled",
        }
        dec4 = evaluate_notification(obs4, self.transport_ok, self.policy, self.now, self.ack_empty)
        self.assertFalse(dec4.should_notify)

        # 5. None progress -> NO progress alerts
        dec5 = evaluate_notification(None, self.transport_ok, self.policy, self.now, self.ack_empty)
        self.assertFalse(dec5.should_notify)

    def test_quiet_hours_filtering(self):
        policy_quiet = dict(self.policy)
        policy_quiet["quiet_hours"] = {
            "enabled": True,
            "start": "22:00",
            "end": "08:00",
        }
        # Time during quiet hours (23:30)
        quiet_time = datetime(2026, 9, 21, 23, 30, 0, tzinfo=timezone.utc)

        # Non-critical alert (owner_gate warn) is suppressed in quiet hours
        obs_gate = {
            "project_id": "p1",
            "progress_observation_state": "authoritative",
            "owner_gate": {"state": "owner_gate", "reason": "review"},
        }
        dec1 = evaluate_notification(obs_gate, self.transport_ok, policy_quiet, quiet_time, self.ack_empty)
        self.assertFalse(dec1.should_notify)

        # Critical alert (stall) bypasses quiet hours
        obs_stall = {
            "project_id": "p1",
            "progress_observation_state": "authoritative",
            "watchdog_state": "agent_stalled",
        }
        dec2 = evaluate_notification(obs_stall, self.transport_ok, policy_quiet, quiet_time, self.ack_empty)
        self.assertTrue(dec2.should_notify)

    def test_acknowledgement_and_snooze_suppression(self):
        obs_stall = {
            "project_id": "p1",
            "progress_observation_state": "authoritative",
            "watchdog_state": "agent_stalled",
        }
        dedup_key = "progress:stall:p1"

        # Acknowledged -> suppressed
        ack_state = {dedup_key: {"acknowledged": True}}
        dec = evaluate_notification(obs_stall, self.transport_ok, self.policy, self.now, self.ack_state_to_dict(ack_state))
        self.assertFalse(dec.should_notify)

        # Snoozed in future -> suppressed
        snooze_future = {dedup_key: {"snoozed_until": "2026-09-21T15:00:00+00:00"}}
        dec_snoozed = evaluate_notification(obs_stall, self.transport_ok, self.policy, self.now, snooze_future)
        self.assertFalse(dec_snoozed.should_notify)

        # Snooze expired -> alert fires again
        snooze_past = {dedup_key: {"snoozed_until": "2026-09-21T13:00:00+00:00"}}
        dec_expired = evaluate_notification(obs_stall, self.transport_ok, self.policy, self.now, snooze_past)
        self.assertTrue(dec_expired.should_notify)

    def ack_state_to_dict(self, ack):
        return ack


if __name__ == "__main__":
    unittest.main()
