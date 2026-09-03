import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_orchestrator.monitor.project import resolve_monitor_state
from dev_orchestrator.monitor.telemetry import (
    eta_policy,
    extract_task_id,
    new_state_event,
    worker_telemetry,
)


PROJECT = {
    "id": "labdemo",
    "eta": {
        "default_worker_minutes": {"min": 30, "max": 90},
        "historical_min_samples": 3,
        "stall_warning_minutes": 15,
        "hard_timeout_minutes": 180,
        "task_overrides": [{"task_id": "P4.2.3b", "min": 45, "max": 90}],
    },
}


class TelemetryParityTests(unittest.TestCase):
    def test_task_id_and_state_parity(self):
        self.assertEqual(extract_task_id("P4.2.3b DESIGN READY"), "P4.2.3b")
        self.assertEqual(resolve_monitor_state({"kind": "none", "state": "not_started", "process_alive": False}, "STATUS: DESIGN READY", None), "READY_TO_RUN")
        self.assertEqual(resolve_monitor_state({"kind": "task", "state": "failed", "process_alive": False}, "", None), "WORKER_FAILED")
        self.assertEqual(resolve_monitor_state({"kind": "task", "state": "running", "process_alive": False}, "", None), "WORKER_LOST")

    def test_worker_telemetry_matches_p1_policy(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs.jsonl"
            utility = {"kind": "utility", "state": "completed", "pid": 1, "started_at": now.isoformat(), "updated_at": now.isoformat()}
            info = worker_telemetry(PROJECT, utility, "P4.2.3b", now.isoformat(), runs, now=now)
            self.assertIsNone(info["run_id"])
            self.assertIsNone(info["elapsed_seconds"])
            self.assertEqual(info["eta"]["total_min_seconds"], 2700)
            self.assertEqual(info["eta"]["total_max_seconds"], 5400)

            started = now - timedelta(minutes=10)
            running = {"kind": "task", "state": "running", "pid": 12345, "started_at": started.isoformat(), "updated_at": started.isoformat(), "command": "dsh --profile headless next"}
            live = worker_telemetry(PROJECT, running, "P4.2.3b", now.isoformat(), runs, now=now)
            self.assertTrue(live["run_id"].startswith("labdemo-"))
            self.assertGreaterEqual(live["elapsed_seconds"], 599)
            self.assertLessEqual(live["elapsed_seconds"], 601)
            self.assertEqual(live["health"], "OK")

            stale = worker_telemetry(PROJECT, running, "P4.2.3b", (now - timedelta(minutes=20)).isoformat(), runs, now=now)
            self.assertEqual(stale["health"], "STALLED_WARNING")

    def test_history_median_and_event_noise(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs.jsonl"
            rows = [
                {"project_id": "other", "result": "completed", "duration_seconds": 2400},
                {"project_id": "other", "result": "completed", "duration_seconds": 3600},
                {"project_id": "other", "result": "completed", "duration_seconds": 3000},
            ]
            runs.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            project = {**PROJECT, "id": "other", "eta": {**PROJECT["eta"], "task_overrides": []}}
            policy = eta_policy(project, "P9.1", runs)
            self.assertEqual(policy["source"], "project_history_median")
            self.assertEqual(policy["min_minutes"], 35.0)
            self.assertEqual(policy["max_minutes"], 67.5)

        previous = {"id": "labdemo", "state": "READY_TO_RUN", "telemetry": {"task_id": "P4.2.3b", "run_id": "old-utility"}}
        current = {"id": "labdemo", "state": "READY_TO_RUN", "telemetry": {"task_id": "P4.2.3b", "run_id": None}}
        self.assertIsNone(new_state_event(previous, current))


if __name__ == "__main__":
    unittest.main()
