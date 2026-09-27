"""Per-project containment of decision and handoff actuation faults.

Regression for the 2026-09-27 incident: a single project's successor repair
raised ``FileExistsError`` (WinError 183) inside the decision actuation path.
The exception escaped ``executor.advance``, so every tick aborted before the
watchdog, supervisor and harvesting ran -- for every project -- and the daemon
heartbeat went ``degraded`` every 60 seconds with no work being done.

Containment is fail-closed: the failing project's decision or handoff is never
consumed or settled, the fault stays visible on that project and on the
heartbeat, and every other project plus every later tick phase still runs.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.transition_executor import ActuationLaunch, TransitionExecutor
from dev_orchestrator.daemon import _run_orchestration_tick
from tests_py.test_staged_handoff import (
    FakeHandoffPort,
    make_git_repo,
    setup_roadmap,
    setup_staged_spec,
)
from tests_py.test_transition_executor import FakeBackend


PREDECESSOR = "P12.7"
SUCCESSOR = "P13"


class RecordingWatchdog:
    """Stands in for the real watchdog, which runs after decision actuation."""

    def __init__(self) -> None:
        self.advance_calls = 0
        self.tick_errors: list[str] = []

    def advance(self, _config, _summary, *, executor=None):
        self.advance_calls += 1
        return []

    def record_tick_error(self, exc) -> None:
        self.tick_errors.append(str(exc))


class RecordingSupervisor:
    def __init__(self) -> None:
        self.advance_calls = 0

    def advance(self, _config, _summary, *, executor=None, watchdog=None):
        self.advance_calls += 1
        return []


class ActuationFaultContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        # Managed runs and planner threads can still hold runtime files open
        # when the case ends; cleanup noise must not fail the assertion.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.runtime = base / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.repos: dict[str, Path] = {}
        self.heads: dict[str, str] = {}
        decisions: dict[str, dict] = {}
        projects: list[dict] = []
        for project_id in ("alpha", "beta"):
            repo = base / project_id
            make_git_repo(repo, PREDECESSOR)
            spec_path, _ = setup_staged_spec(repo, SUCCESSOR)
            setup_roadmap(repo, PREDECESSOR, SUCCESSOR, spec_path)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "setup staged"],
                check=True, capture_output=True,
            )
            head = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
            ).strip()
            self.repos[project_id] = repo
            self.heads[project_id] = head
            projects.append({
                "project_id": project_id,
                "repo_path": str(repo),
                "adapter": "agent_files",
                "execution": {
                    "enabled": True,
                    "engine": "aibroker",
                    "owner_authorized": True,
                    "allowed_next_actions": ["next_task"],
                },
                "ai_roles": {"planner": {"enabled": True}},
            })
            request_id = "worker_done:{0}:r1".format(project_id)
            decisions[request_id] = {
                "project_id": project_id, "request_id": request_id,
                "disposition": "apply", "decision": "next", "next_action": "next_task",
                "task_id": PREDECESSOR, "stage_id": None,
                "branch": read_repository_truth(repo).branch, "head": head,
                "role": "reviewer", "event": "worker_done",
                "consumed_at": "2026-09-27T00:00:00+00:00",
            }
        self.config_path = base / "projects.json"
        self.config_path.write_text(json.dumps({"projects": projects}), encoding="utf-8")
        (self.runtime / "websol-decisions.json").write_text(
            json.dumps({"version": 1, "decisions": decisions}), encoding="utf-8",
        )
        self.port = FakeHandoffPort(SUCCESSOR)
        self.planner = AIPlannerCoordinator(self.runtime, self.port)
        self.executor = TransitionExecutor(
            self.runtime, backend_overrides={"agy": FakeBackend("agy")},
        )
        self.controls = ControlCommandCoordinator(self.runtime, self.planner)

    def _summary(self) -> dict:
        return {"projects": [
            {
                "project_id": project_id,
                "repo_path": str(self.repos[project_id]),
                "state": "IDLE",
                "next_status": "**COMPLETE**",
                "telemetry": {"task_id": PREDECESSOR},
                "git": {"head": self.heads[project_id]},
            }
            for project_id in ("alpha", "beta")
        ]}

    def _run_tick(self, watchdog, supervisor):
        """Run one orchestration tick, capturing the per-phase status writes."""
        status_writes: list[tuple[str, dict]] = []

        def status_writer(summary, _runtime, **kwargs):
            status_writes.append((kwargs["phase"], summary))
            return []

        with patch("dev_orchestrator.daemon.run_monitor_once", return_value=self._summary()), \
             patch("dev_orchestrator.daemon.write_project_statuses", side_effect=status_writer), \
             patch("dev_orchestrator.daemon.dispatch_worker_done_events", return_value=None), \
             patch("dev_orchestrator.daemon.consume_websol_responses", return_value=None):
            projected = _run_orchestration_tick(
                self.config_path, self.runtime, object(), self.executor,
                controls=self.controls, watchdog=watchdog, supervisor=supervisor,
                pid=4242,
            )
        return projected, status_writes

    @staticmethod
    def _row(summary, project_id) -> dict:
        for row in summary.get("projects") or []:
            if isinstance(row, dict) and row.get("project_id") == project_id:
                return row
        raise AssertionError("no row for project " + project_id)

    def test_one_project_decision_fault_does_not_stop_the_tick(self):
        """A successor-resolution fault mirrors the incident's repair failure."""
        from dev_orchestrator.core import transition_executor as te

        real_resolve = te.resolve_successor
        alpha_repo = str(self.repos["alpha"])

        def exploding_resolve(repo_path, task_id):
            if str(repo_path) == alpha_repo:
                raise FileExistsError(183, "Cannot create a file when it already exists")
            return real_resolve(repo_path, task_id)

        watchdog = RecordingWatchdog()
        supervisor = RecordingSupervisor()
        with patch.object(te, "resolve_successor", side_effect=exploding_resolve):
            projected, status_writes = self._run_tick(watchdog, supervisor)

        executions = self.executor.state()["executions"]

        # The healthy project still actuated its decision in the same tick.
        self.assertEqual(executions["worker_done:beta:r1"]["state"], "handoff")
        self.assertEqual(executions["worker_done:beta:r1"]["next_task_id"], SUCCESSOR)

        # Fail-closed: the failed decision is neither consumed nor settled, so
        # the next tick retries it.
        self.assertNotIn("worker_done:alpha:r1", executions)

        # The later tick phases still ran.
        self.assertEqual(watchdog.advance_calls, 1)
        self.assertEqual(watchdog.tick_errors, [])
        self.assertEqual(supervisor.advance_calls, 1)
        self.assertEqual(
            [phase for phase, _ in status_writes],
            ["monitor", "dispatch", "decision", "actuation"],
        )

        # Visible, not swallowed: degraded heartbeat plus a per-project error.
        self.assertIn("FileExistsError", projected["_actuation_tick_error"])
        alpha_error = self._row(projected, "alpha")["actuation_error"]
        self.assertEqual(alpha_error["project_id"], "alpha")
        self.assertEqual(alpha_error["request_id"], "worker_done:alpha:r1")
        self.assertEqual(alpha_error["phase"], "decision_actuation")
        self.assertIn("FileExistsError", alpha_error["error"])
        self.assertNotIn("actuation_error", self._row(projected, "beta"))

        actuation_phase = [s for phase, s in status_writes if phase == "actuation"][0]
        self.assertIn("actuation_error", self._row(actuation_phase, "alpha"))

        persisted = json.loads((self.runtime / "summary.json").read_text(encoding="utf-8"))
        self.assertIn("FileExistsError", persisted["_actuation_tick_error"])
        self.assertIn("actuation_error", self._row(persisted, "alpha"))

        # The contained fault is drained per tick, not repeated forever.
        self.assertEqual(self.executor.drain_actuation_errors(), {})

    def test_one_project_handoff_fault_does_not_stop_the_other_handoff(self):
        """A planner fault during handoff resume is contained per project."""
        self.executor.advance(self._summary(), self.config_path)
        executions = self.executor.state()["executions"]
        self.assertEqual(executions["worker_done:alpha:r1"]["state"], "handoff")
        self.assertEqual(executions["worker_done:beta:r1"]["state"], "handoff")

        real_start_deferred = self.planner.start_deferred

        def exploding_start_deferred(project, snapshot, continuation_id, row):
            if str(project.get("project_id")) == "alpha":
                raise FileExistsError(183, "Cannot create a file when it already exists")
            return real_start_deferred(project, snapshot, continuation_id, row)

        watchdog = RecordingWatchdog()
        supervisor = RecordingSupervisor()
        with patch.object(self.planner, "start_deferred", side_effect=exploding_start_deferred):
            projected, _ = self._run_tick(watchdog, supervisor)

        executions = self.executor.state()["executions"]

        # The healthy project's handoff still reached the Planner this tick.
        self.assertTrue(executions["worker_done:beta:r1"]["handoff_consumed"])

        # Fail-closed: the failed handoff stays pending and unblocked, so the
        # next tick retries it.
        alpha = executions["worker_done:alpha:r1"]
        self.assertEqual(alpha["state"], "handoff")
        self.assertNotEqual(alpha.get("handoff_consumed"), True)

        self.assertEqual(watchdog.advance_calls, 1)
        self.assertEqual(supervisor.advance_calls, 1)
        self.assertIn("FileExistsError", projected["_actuation_tick_error"])
        alpha_error = self._row(projected, "alpha")["actuation_error"]
        self.assertEqual(alpha_error["phase"], "handoff_resume")
        self.assertEqual(alpha_error["request_id"], "worker_done:alpha:r1")

    def test_authority_failure_fences_every_mutating_and_launch_phase(self):
        """A tick with unknown authority may report, but must not actuate."""
        watchdog = RecordingWatchdog()
        supervisor = RecordingSupervisor()
        status_writes: list[tuple[str, dict]] = []

        def status_writer(summary, _runtime, **kwargs):
            status_writes.append((kwargs["phase"], summary))
            return []

        with patch.object(
            self.executor, "reconcile_lifecycle_authority",
            side_effect=RuntimeError("authority store unavailable"),
        ), patch.object(self.controls, "advance") as control_advance, patch(
            "dev_orchestrator.daemon.run_monitor_once", return_value=self._summary(),
        ), patch(
            "dev_orchestrator.daemon.write_project_statuses", side_effect=status_writer,
        ), patch(
            "dev_orchestrator.daemon.dispatch_worker_done_events",
        ) as dispatch, patch(
            "dev_orchestrator.daemon.consume_websol_responses",
        ) as consume:
            projected = _run_orchestration_tick(
                self.config_path, self.runtime, object(), self.executor,
                controls=self.controls, watchdog=watchdog, supervisor=supervisor,
                pid=4242,
            )

        control_advance.assert_not_called()
        dispatch.assert_not_called()
        consume.assert_not_called()
        self.assertEqual(watchdog.advance_calls, 0)
        self.assertEqual(supervisor.advance_calls, 0)
        self.assertEqual(self.executor.state()["executions"], {})
        self.assertEqual([phase for phase, _ in status_writes], ["monitor", "actuation"])
        self.assertIn("authority store unavailable", projected["_actuation_tick_error"])
        persisted = json.loads((self.runtime / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["_actuation_tick_error"], projected["_actuation_tick_error"])
        self.assertTrue(persisted["_unattributed_actuation_errors"])

    def test_project_phase_failure_does_not_skip_later_project_or_drop_launch(self):
        """Isolation applies inside a phase, not only between whole phases."""
        expected = ActuationLaunch("beta", "auto-ready:P13:abc", "P13", "agy", "launching")
        calls: list[str] = []

        def one_project(projects, _snapshots):
            project_id = next(iter(projects))
            calls.append(project_id)
            if project_id == "alpha":
                raise RuntimeError("alpha launch evidence write failed")
            return [expected]

        with patch.object(self.executor, "_advance_decisions", return_value=[]), patch.object(
            self.executor, "_advance_completed_predecessor_handoffs", return_value=None,
        ), patch.object(
            self.executor, "_advance_unlaunched_ready", side_effect=one_project,
        ), patch.object(
            self.executor, "_advance_owner_start", return_value=[],
        ), patch.object(
            self.executor, "_advance_bootstrap", return_value=[],
        ):
            launches = self.executor.advance(self._summary(), self.config_path)

        self.assertEqual(calls, ["alpha", "beta"])
        self.assertEqual(launches, [expected])
        errors = self.executor.drain_actuation_errors()
        self.assertEqual(errors["alpha"]["phase"], "unlaunched_ready")
        self.assertNotIn("beta", errors)


if __name__ == "__main__":
    unittest.main()
