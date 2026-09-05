import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.core.transition_executor import TransitionExecutor


class TransitionOverlayTests(unittest.TestCase):
    def test_completed_managed_run_preserves_captured_task_id_without_mutating_raw(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td) / "runtime"; runtime.mkdir()
            (runtime / "transition-executor.json").write_text(json.dumps({
                "version": 1,
                "executions": {"source-p1": {
                    "project_id": "p1", "source_request_id": "source-p1",
                    "source_kind": "bootstrap", "task_id": "P1",
                    "backend_id": "agy", "backend_run_id": "agy-run-1",
                    "state": "completed", "exit_code": 0,
                    "started_at": "2026-09-05T01:00:00+00:00",
                    "completed_at": "2026-09-05T01:05:00+00:00",
                }},
            }), encoding="utf-8")
            raw = {"projects": [{
                "project_id": "p1", "state": "READY_TO_RUN",
                "worker": {"kind": "none", "state": "not_started", "process_alive": False},
                "telemetry": {"task_id": "P2", "run_id": None},
            }]}
            executor = TransitionExecutor(runtime)
            projected = executor.overlay_managed_runs(raw)
            self.assertEqual(raw["projects"][0]["telemetry"]["task_id"], "P2")
            snap = projected["projects"][0]
            self.assertEqual(snap["telemetry"]["task_id"], "P1")
            self.assertEqual(snap["telemetry"]["run_id"], "agy-run-1")
            self.assertEqual(snap["worker"]["state"], "completed")
            self.assertEqual(snap["worker"]["command"], "managed agy")
            self.assertEqual(snap["state"], "WAITING_REVIEW")


if __name__ == "__main__":
    unittest.main()
