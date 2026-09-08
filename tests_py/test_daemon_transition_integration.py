import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_orchestrator.daemon import _run_orchestration_tick


class FakeExecutor:
    def __init__(self):
        self.advance_seen = None
        self.decision_summary_seen = None
        self.overlay_calls = 0

    def overlay_managed_runs(self, summary):
        self.overlay_calls += 1
        return {"projects": [{**summary["projects"][0], "view": "overlay", "overlay_call": self.overlay_calls}]}

    def advance(self, summary, config, *, decision_summary=None):
        self.advance_seen = summary
        self.decision_summary_seen = decision_summary
        return []


class DaemonTransitionIntegrationTests(unittest.TestCase):
    def test_tick_uses_projected_truth_for_decisions_but_keeps_raw_owner_gates(self):
        raw = {"projects": [{"project_id": "p1", "repo_path": "repo", "view": "raw"}]}
        executor = FakeExecutor()
        phases = []
        dispatched = []
        consumed = []

        def status_writer(summary, runtime, **kwargs):
            phases.append((kwargs["phase"], summary["projects"][0]["view"]))
            return []

        with tempfile.TemporaryDirectory() as td, \
             patch("dev_orchestrator.daemon.run_monitor_once", return_value=raw), \
             patch("dev_orchestrator.daemon.write_project_statuses", side_effect=status_writer), \
             patch("dev_orchestrator.daemon.dispatch_worker_done_events", side_effect=lambda s, *_: dispatched.append(s)), \
             patch("dev_orchestrator.daemon.consume_websol_responses", side_effect=lambda s, *_: consumed.append(s)):
            result = _run_orchestration_tick("config.json", Path(td), object(), executor, pid=123)
        self.assertEqual(dispatched[0]["projects"][0]["view"], "overlay")
        self.assertEqual(consumed[0]["projects"][0]["view"], "overlay")
        self.assertIs(executor.advance_seen, raw)
        self.assertEqual(executor.decision_summary_seen["projects"][0]["view"], "overlay")
        self.assertEqual(executor.decision_summary_seen["projects"][0]["overlay_call"], 1)
        self.assertEqual(result["projects"][0]["view"], "overlay")
        self.assertEqual(phases, [
            ("monitor", "raw"),
            ("dispatch", "overlay"),
            ("decision", "overlay"),
            ("actuation", "overlay"),
        ])


if __name__ == "__main__":
    unittest.main()
