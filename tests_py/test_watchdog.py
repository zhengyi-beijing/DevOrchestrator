import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_orchestrator.core.watchdog import (
    ACTIVE_LIFECYCLE_STATES,
    WatchdogCoordinator,
    evaluate_stall,
    resolve_threshold_seconds,
    resolve_watchdog_policy,
)


class WatchdogPolicyAndLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir(parents=True)
        self.config_file = self.root / "projects.json"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_policy_defaults_and_family_resolution(self):
        """Policy defaults and family fallback for lifecycle overrides."""
        empty_cfg = {}
        pol = resolve_watchdog_policy(empty_cfg)
        self.assertFalse(pol["enabled"])
        self.assertEqual(pol["no_progress_threshold_minutes"], 15)
        self.assertEqual(pol["cooldown_minutes"], 30)
        self.assertFalse(pol["auto_recovery"])
        self.assertEqual(pol["diagnostic_timeout_seconds"], 120)

        # Lifecycle override with family fallback
        custom_cfg = {
            "watchdog": {
                "enabled": True,
                "no_progress_threshold_minutes": 25,
                "lifecycle_overrides": {
                    "PLANNING": 10,
                    "EXECUTING": 40,
                },
            }
        }
        pol2 = resolve_watchdog_policy(custom_cfg)
        # Exact override
        self.assertEqual(resolve_threshold_seconds(pol2, "EXECUTING"), 2400.0)
        # Family fallback: REVIEWING_PLAN -> PLANNING (10 min = 600s)
        self.assertEqual(resolve_threshold_seconds(pol2, "REVIEWING_PLAN"), 600.0)
        # Family fallback: APPLYING_PLAN -> PLANNING (10 min = 600s)
        self.assertEqual(resolve_threshold_seconds(pol2, "APPLYING_PLAN"), 600.0)
        # Unspecified state falls back to project default (25 min = 1500s)
        self.assertEqual(resolve_threshold_seconds(pol2, "REVIEWING"), 1500.0)

    def test_active_lifecycle_states_monitored(self):
        """Only ACTIVE_LIFECYCLE_STATES are monitored; other states are ignored."""
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(hours=2)).isoformat()
        policy = resolve_watchdog_policy({"watchdog": {"enabled": True}})

        # Monitored states
        for state in ACTIVE_LIFECYCLE_STATES:
            signals = (old_time, "fp1", {"activity_evidence": "available"})
            assessment = evaluate_stall(
                snapshot={"state": state, "task_id": "T1"},
                policy=policy,
                signals=signals,
                now=now,
            )
            self.assertTrue(assessment.monitored, msg=f"{state} should be monitored")
            self.assertTrue(assessment.breached, msg=f"{state} should be breached")

        # Inactive states
        inactive_states = ["IDLE", "COMPLETED", "WAITING_FOR_USER", "UNKNOWN", "UNAVAILABLE"]
        for state in inactive_states:
            signals = (old_time, "fp1", {"activity_evidence": "available"})
            assessment = evaluate_stall(
                snapshot={"state": state, "task_id": "T1"},
                policy=policy,
                signals=signals,
                now=now,
            )
            self.assertFalse(assessment.monitored, msg=f"{state} should not be monitored")
            self.assertFalse(assessment.breached, msg=f"{state} should not be breached")

    def test_stall_evaluation_evidence_unavailable(self):
        """When activity evidence is not available, breach is never declared."""
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(hours=2)).isoformat()
        policy = resolve_watchdog_policy({"watchdog": {"enabled": True}})
        signals = (old_time, "fp1", {"activity_evidence": "unavailable"})
        assessment = evaluate_stall(
            snapshot={"state": "EXECUTING", "task_id": "T1"},
            policy=policy,
            signals=signals,
            now=now,
        )
        self.assertTrue(assessment.monitored)
        self.assertFalse(assessment.breached)

    def test_cooldown_and_deduplication(self):
        """Coordinator deduplicates attempts and enforces cooldown."""
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(minutes=45)).isoformat()
        repo_dir = self.root / "repo"
        repo_dir.mkdir(parents=True)
        import hashlib
        from dev_orchestrator.core.watchdog import canonical_path
        c_repo = canonical_path(repo_dir)
        repo_fp = hashlib.sha256(c_repo.encode("utf-8")).hexdigest()[:16]

        config_data = {
            "projects": [
                {
                    "project_id": "p1",
                    "repo_path": str(repo_dir),
                    "watchdog": {
                        "enabled": True,
                        "no_progress_threshold_minutes": 15,
                        "cooldown_minutes": 30,
                    },
                }
            ]
        }
        self.config_file.write_text(json.dumps(config_data), encoding="utf-8")

        coordinator = WatchdogCoordinator(self.runtime_dir)

        # Snapshot with valid evidence and stalled timestamp
        snapshot = {
            "project_id": "p1",
            "repo_path": str(repo_dir),
            "state": "EXECUTING",
            "task_id": "T1",
            "activity": {
                "watchdog_safe": {
                    "last_activity_at": old_time,
                    "newest_kind": "agent_file",
                    "newest_path": "agent/task.md",
                    "sources": {},
                    "changed_entries_considered": 0,
                    "repo_root_fingerprint": repo_fp,
                    "repo_scope": "canonical",
                }
            },
        }

        # Advance 1: initiates attempt
        results1 = coordinator.advance(self.config_file, {"projects": [snapshot]}, now=now)
        self.assertEqual(len(results1), 1)
        self.assertEqual(results1[0].get("status"), "attempt_started")
        att_key = results1[0].get("attempt_key")

        # Advance 2: thread is running or deduplicated
        results2 = coordinator.advance(self.config_file, {"projects": [snapshot]}, now=now)
        self.assertEqual(len(results2), 1)
        self.assertIn(results2[0].get("status"), ("single_flight_active", "deduplicated"))

        # Wait for thread to finish (it does almost nothing because it's a stub or finishes quickly)
        thread = coordinator._threads.get("p1")
        if thread is not None:
            thread.join(timeout=2.0)

        # Mark attempt completed and set cooldown
        with coordinator._lock:
            prow = coordinator._cached_state["projects"]["p1"]
            prow["attempts"][att_key]["state"] = "completed"
            prow["attempts"][att_key]["diagnosis"] = "agent_stalled"
            prow["cooldown_until"] = (now + timedelta(minutes=30)).isoformat()
            coordinator._save_state(coordinator._cached_state)

        # Advance 3: same attempt key is deduplicated
        results3 = coordinator.advance(self.config_file, {"projects": [snapshot]}, now=now + timedelta(minutes=10))
        self.assertEqual(len(results3), 1)
        self.assertEqual(results3[0].get("status"), "deduplicated")

        # Advance 4: different task (different attempt key) while in cooldown => suppressed by cooldown
        snapshot_diff_task = dict(snapshot)
        snapshot_diff_task["task_id"] = "T2"
        results4 = coordinator.advance(self.config_file, {"projects": [snapshot_diff_task]}, now=now + timedelta(minutes=10))
        self.assertEqual(len(results4), 1)
        self.assertEqual(results4[0].get("status"), "cooldown")


if __name__ == "__main__":
    unittest.main()
