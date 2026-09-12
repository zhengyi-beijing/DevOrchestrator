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


class ExplodingWatchdog:
    def __init__(self):
        self.advance_calls = 0
        self.recorded = []

    def advance(self, *_args, **_kwargs):
        self.advance_calls += 1
        raise RuntimeError("watchdog boom")

    def record_tick_error(self, exc):
        self.recorded.append(str(exc))


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


    def test_direct_reviewer_project_is_excluded_from_browser_dispatch_and_consume(self):
        raw = {"projects": [
            {"project_id": "direct", "repo_path": "repo1", "view": "raw"},
            {"project_id": "legacy", "repo_path": "repo2", "view": "raw"},
        ]}
        executor = FakeExecutor()
        executor.overlay_managed_runs = lambda summary: {"projects": [
            {**item, "view": "overlay"} for item in summary["projects"]
        ]}
        class Reviewer:
            def enabled_project_ids(self, _config): return frozenset({"direct"})
            def advance(self, _config): return []
        dispatched = []
        consumed = []
        with tempfile.TemporaryDirectory() as td, \
             patch("dev_orchestrator.daemon.run_monitor_once", return_value=raw), \
             patch("dev_orchestrator.daemon.write_project_statuses", return_value=[]), \
             patch("dev_orchestrator.daemon.dispatch_worker_done_events", side_effect=lambda s, *_: dispatched.append(s)), \
             patch("dev_orchestrator.daemon.consume_websol_responses", side_effect=lambda s, *_: consumed.append(s)):
            _run_orchestration_tick("config.json", Path(td), object(), executor, Reviewer(), pid=123)
        self.assertEqual([p["project_id"] for p in dispatched[0]["projects"]], ["legacy"])
        self.assertEqual([p["project_id"] for p in consumed[0]["projects"]], ["legacy"])

    def test_watchdog_tick_errors_are_recorded_without_stopping_tick(self):
        raw = {"projects": [{"project_id": "p1", "repo_path": "repo", "view": "raw"}]}
        executor = FakeExecutor()
        watchdog = ExplodingWatchdog()

        with tempfile.TemporaryDirectory() as td, \
             patch("dev_orchestrator.daemon.run_monitor_once", return_value=raw), \
             patch("dev_orchestrator.daemon.write_project_statuses", return_value=[]), \
             patch("dev_orchestrator.daemon.dispatch_worker_done_events", return_value=None), \
             patch("dev_orchestrator.daemon.consume_websol_responses", return_value=None):
            result = _run_orchestration_tick(
                "config.json", Path(td), object(), executor, watchdog=watchdog, pid=123
            )

        self.assertEqual(watchdog.advance_calls, 1)
        self.assertEqual(watchdog.recorded, ["watchdog boom"])
        self.assertEqual(result["projects"][0]["view"], "overlay")
        self.assertEqual(result["_watchdog_tick_error"], "watchdog boom")



if __name__ == "__main__":
    unittest.main()
