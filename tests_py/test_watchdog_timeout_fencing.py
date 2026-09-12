import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_orchestrator.core.watchdog import (
    WatchdogCoordinator,
)


class WatchdogTimeoutFencingTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir(parents=True)
        self.state_file = self.runtime_dir / "watchdog.json"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_reap_overdue_attempts(self):
        """Attempts exceeding deadline_at are reaped with state=timed_out and diagnosis=unknown."""
        coordinator = WatchdogCoordinator(self.runtime_dir)
        now = datetime.now(timezone.utc)
        past_deadline = (now - timedelta(seconds=10)).isoformat()

        # Add running attempt to live coordinator
        with coordinator._lock:
            coordinator._cached_state["projects"]["p1"] = {
                "fence_generation": 0,
                "cooldown_minutes": 15,
                "attempts": {
                    "att-timeout": {
                        "attempt_key": "att-timeout",
                        "run_scope_key": "rscope-1",
                        "state": "running",
                        "fence_token": "att-timeout:0",
                        "started_at": (now - timedelta(seconds=130)).isoformat(),
                        "deadline_at": past_deadline,
                    }
                },
            }
            coordinator._save_state(coordinator._cached_state)

        # Trigger reap
        coordinator._reap_overdue_attempts(now)

        st = coordinator.state()
        p1 = st["projects"]["p1"]
        att = p1["attempts"]["att-timeout"]

        self.assertEqual(att["state"], "timed_out")
        self.assertEqual(att["diagnosis"], "unknown")
        self.assertEqual(att["confidence"], 0.0)
        self.assertIn("exceeded deadline", att["reason"])
        self.assertEqual(p1["fence_generation"], 1)
        self.assertEqual(p1["last_diagnosis"], "unknown")
        self.assertIsNotNone(p1.get("cooldown_until"))

    def test_late_worker_result_discarded_by_fence(self):
        """Worker thread completing after reap is fenced and result is discarded."""
        now = datetime.now(timezone.utc)

        # Initial state where attempt is already timed out and generation has advanced
        initial_state = {
            "version": 1,
            "degraded": False,
            "projects": {
                "p1": {
                    "fence_generation": 1,
                    "cooldown_minutes": 15,
                    "attempts": {
                        "att-fenced": {
                            "attempt_key": "att-fenced",
                            "run_scope_key": "rscope-1",
                            "state": "timed_out",
                            "fence_token": "att-fenced:0",  # stale token!
                            "diagnosis": "unknown",
                            "confidence": 0.0,
                        }
                    },
                }
            },
        }
        self.state_file.write_text(json.dumps(initial_state), encoding="utf-8")
        coordinator = WatchdogCoordinator(self.runtime_dir)

        # Simulate worker callback running with stale token "att-fenced:0"
        class DummyAssessment:
            run_scope_key = "rscope-1"
            task_id = "T1"
            lifecycle_state = "EXECUTING"

        coordinator._run_diagnostic_worker(
            project_config={"project_id": "p1", "repo_path": str(self.root)},
            snapshot={"project_id": "p1"},
            assessment=DummyAssessment(),
            attempt_key="att-fenced",
            fence_token="att-fenced:0",
            timeout_seconds=10,
        )

        st = coordinator.state()
        att = st["projects"]["p1"]["attempts"]["att-fenced"]

        # Diagnosis must remain timed_out/unknown, NOT overwritten!
        self.assertEqual(att["state"], "timed_out")
        self.assertEqual(att["diagnosis"], "unknown")

        # Must record late discarded tracking fields
        self.assertIsNotNone(att.get("late_discarded_at"))
        self.assertIsNotNone(att.get("late_discarded_diagnosis"))


if __name__ == "__main__":
    unittest.main()
