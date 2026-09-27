import json
import os
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


class CapturingWatchdog:
    def __init__(self):
        self.summary = None
        self.executor = None

    def advance(self, _config, summary, *, executor=None):
        self.summary = summary
        self.executor = executor
        return []

    def record_tick_error(self, _exc):
        raise AssertionError("watchdog should not fail")


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

    def test_watchdog_uses_raw_ready_state_not_terminal_execution_projection(self):
        """A prior failed Worker cannot hide a current READY_TO_RUN launch gap."""
        raw = {"projects": [{
            "project_id": "p1", "repo_path": "repo", "state": "READY_TO_RUN",
            "lifecycle_state": "READY_TO_RUN", "view": "raw",
        }]}
        executor = FakeExecutor()
        executor.overlay_managed_runs = lambda summary: {"projects": [{
            **summary["projects"][0], "state": "WORKER_FAILED",
            "lifecycle_state": "WORKER_FAILED", "view": "terminal-overlay",
        }]}
        watchdog = CapturingWatchdog()

        with tempfile.TemporaryDirectory() as td, \
             patch("dev_orchestrator.daemon.run_monitor_once", return_value=raw), \
             patch("dev_orchestrator.daemon.write_project_statuses", return_value=[]), \
             patch("dev_orchestrator.daemon.dispatch_worker_done_events", return_value=None), \
             patch("dev_orchestrator.daemon.consume_websol_responses", return_value=None):
            result = _run_orchestration_tick(
                "config.json", Path(td), object(), executor, watchdog=watchdog, pid=123
            )

        self.assertIs(watchdog.summary, raw)
        self.assertIs(watchdog.executor, executor)
        self.assertEqual(watchdog.summary["projects"][0]["lifecycle_state"], "READY_TO_RUN")
        self.assertEqual(result["projects"][0]["lifecycle_state"], "WORKER_FAILED")


class DaemonReadinessTests(unittest.TestCase):
    def test_readiness_heartbeat_is_published_before_the_first_tick(self):
        """start-daemon kills a child whose heartbeat misses its deadline.

        After a restart the first tick is where pending lifecycle work runs
        (successor repair, handoff publication, Planner launch), so it can
        legitimately outlast that deadline.  Readiness must therefore be
        published before the first tick, or the supported restart path kills a
        healthy daemon mid-transaction.
        """
        from dev_orchestrator.daemon import run_daemon

        seen = {}

        def first_tick(_config, runtime, *_args, **_kwargs):
            path = Path(runtime) / "daemon.json"
            seen["heartbeat"] = (
                json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            )
            raise KeyboardInterrupt  # end the loop after observing readiness

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            web = root / "web"; web.mkdir()
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": []}), encoding="utf-8")
            with patch("dev_orchestrator.daemon._run_orchestration_tick", side_effect=first_tick):
                rc = run_daemon(config, runtime, web, 60, "127.0.0.1", 0,
                                bridge_listen="127.0.0.1", bridge_port=0)

        self.assertEqual(rc, 0)
        heartbeat = seen.get("heartbeat")
        self.assertIsNotNone(heartbeat, "no initialization heartbeat existed when the first tick began")
        self.assertEqual(heartbeat["pid"], os.getpid())
        # Initialized, but deliberately not a ready state: readiness still means
        # the first tick completed.
        self.assertEqual(heartbeat["state"], "starting")
        self.assertIsNone(heartbeat["last_tick_at"], "claimed a tick that has not happened")


class StartDaemonFirstTickTests(unittest.TestCase):
    """start-daemon must not kill an initialized daemon during a slow first tick."""

    def _runtime(self, td, *, pid, state):
        runtime = Path(td)
        (runtime / "daemon.pid").write_text(str(pid), encoding="utf-8")
        if state is not None:
            (runtime / "daemon.json").write_text(json.dumps({
                "state": state, "pid": pid, "last_tick_at": None,
            }), encoding="utf-8")
        return runtime

    def test_initialized_child_is_awaited_through_its_first_tick(self):
        import threading
        from dev_orchestrator.cli import _await_first_tick

        with tempfile.TemporaryDirectory() as td:
            runtime = self._runtime(td, pid=os.getpid(), state="starting")

            def finish_first_tick():
                (runtime / "daemon.json").write_text(json.dumps({
                    "state": "running", "pid": os.getpid(),
                    "last_tick_at": "2026-09-27T00:00:00+00:00",
                }), encoding="utf-8")

            timer = threading.Timer(0.4, finish_first_tick)
            timer.start()
            try:
                heartbeat = _await_first_tick(runtime, runtime / "daemon.pid", grace_seconds=10)
            finally:
                timer.cancel()
        self.assertIsNotNone(heartbeat)
        self.assertEqual(heartbeat["state"], "running")

    def test_initialized_child_past_grace_is_reported_not_killed(self):
        from dev_orchestrator.cli import _await_first_tick

        with tempfile.TemporaryDirectory() as td:
            runtime = self._runtime(td, pid=os.getpid(), state="starting")
            heartbeat = _await_first_tick(runtime, runtime / "daemon.pid", grace_seconds=0.3)
        self.assertIsNotNone(heartbeat, "a live initialized daemon would have been terminated")
        self.assertEqual(heartbeat["state"], "starting")

    def test_child_that_never_initialized_still_takes_the_termination_path(self):
        from dev_orchestrator.cli import _await_first_tick

        with tempfile.TemporaryDirectory() as td:
            runtime = self._runtime(td, pid=os.getpid(), state=None)
            self.assertIsNone(_await_first_tick(runtime, runtime / "daemon.pid", grace_seconds=5))

    def test_dead_child_still_takes_the_termination_path(self):
        import subprocess
        import sys
        from dev_orchestrator.cli import _await_first_tick

        finished = subprocess.run([sys.executable, "-c", "pass"], check=True)
        del finished
        probe = subprocess.Popen([sys.executable, "-c", "pass"])
        probe.wait()
        with tempfile.TemporaryDirectory() as td:
            runtime = self._runtime(td, pid=probe.pid, state="starting")
            self.assertIsNone(_await_first_tick(runtime, runtime / "daemon.pid", grace_seconds=5))


if __name__ == "__main__":
    unittest.main()
