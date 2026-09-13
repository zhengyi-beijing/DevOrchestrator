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

    def test_r8b2_single_flight_released_after_timeout_allows_new_diagnostic(self):
        """R8-B2 regression: after _reap_overdue_attempts marks an attempt timed_out,
        the single-flight slot must be released so the next advance() tick can start a
        fresh diagnostic.  A timed-out (possibly blocking) old thread must not permanently
        hold the project's diagnostic single-flight slot.
        Also proves late results from the old run are discarded (fenced by generation)."""
        import threading as _threading

        coordinator = WatchdogCoordinator(self.runtime_dir)
        now = datetime.now(timezone.utc)
        past_deadline = (now - timedelta(seconds=5)).isoformat()
        old_started = (now - timedelta(seconds=130)).isoformat()
        fence_token_v0 = "att-block:0"

        # Set up state with a running attempt whose deadline has passed
        with coordinator._lock:
            coordinator._cached_state["projects"]["p1"] = {
                "fence_generation": 0,
                "cooldown_minutes": 1,
                "attempts": {
                    "att-block": {
                        "attempt_key": "att-block",
                        "run_scope_key": "rscope-block",
                        "state": "running",
                        "fence_token": fence_token_v0,
                        "started_at": old_started,
                        "deadline_at": past_deadline,
                    }
                },
            }
            coordinator._save_state(coordinator._cached_state)

        # Manufacture a "blocked" old thread held alive by an event to simulate
        # a diagnostic worker blocked inside ai_execution_port.status()
        _alive_flag = _threading.Event()

        def _blocking_worker():
            _alive_flag.wait(timeout=10)  # simulates a blocked port.status() call

        old_thread = _threading.Thread(target=_blocking_worker, daemon=True)
        old_thread.start()
        # Ensure thread is running before we inject it
        _alive_flag.clear()  # keep it blocked (wait() will not return until set or 10s)
        # Give a moment for thread to enter wait()
        import time as _time
        _time.sleep(0.05)

        with coordinator._lock:
            coordinator._threads["p1"] = old_thread

        self.assertTrue(old_thread.is_alive(), "old thread must be alive before reap")

        # Reap: must mark timed_out AND release single-flight slot
        coordinator._reap_overdue_attempts(now)

        st = coordinator.state()
        att = st["projects"]["p1"]["attempts"]["att-block"]
        self.assertEqual(att["state"], "timed_out")
        # Single-flight slot must be cleared even though old_thread is still alive
        self.assertNotIn("p1", coordinator._threads,
                         "single-flight thread ref must be cleared after reap so new diagnostic can start")

        # Simulate the blocked old thread eventually completing with its stale fence token —
        # it must be fenced (late result discarded) and must not overwrite timed_out.
        class _OldAssessment:
            run_scope_key = "rscope-block"
            task_id = "T-old"
            lifecycle_state = "EXECUTING"

        coordinator._run_diagnostic_worker(
            project_config={"project_id": "p1", "repo_path": str(self.root)},
            snapshot={"project_id": "p1"},
            assessment=_OldAssessment(),
            attempt_key="att-block",
            fence_token=fence_token_v0,  # stale token; generation advanced after reap
            timeout_seconds=1,
        )
        att2 = coordinator.state()["projects"]["p1"]["attempts"]["att-block"]
        self.assertEqual(att2["state"], "timed_out",
                         "timed_out state must survive late-result from old blocking worker")
        # Cleanup: release the simulated blocking thread
        _alive_flag.set()


if __name__ == "__main__":
    unittest.main()
