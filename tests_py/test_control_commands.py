import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from dev_orchestrator.core.control_commands import (
    ControlCommandCoordinator,
    latest_control_result,
    submit_control_command,
)
from dev_orchestrator.core.transition_executor import TransitionExecutor
from tests_py.test_transition_executor import FakeBackend, make_repo


class FakeExecutor:
    def __init__(self, launch=True):
        self.calls = []
        self.launch = launch
        self.records = {}

    def start_control(self, project, snapshot, command_id):
        self.calls.append((project["project_id"], snapshot["state"], command_id))
        if self.launch:
            return SimpleNamespace(task_id="P1", backend_id="fake")
        self.records[command_id] = {"state": "blocked", "reason": "not ready"}
        return None

    def state(self):
        return {"executions": self.records}


def write_config(path: Path, repo: Path) -> None:
    path.write_text(json.dumps({"projects": [{
        "project_id": "p1", "repo_path": str(repo), "adapter": "agent_files",
        "execution": {
            "enabled": True, "owner_authorized": True,
            "allowed_next_actions": ["next_task"],
            "preferred_backends": ["agy"], "backends": {"agy": {"project": "agy-p1"}},
        },
    }]}), encoding="utf-8")


class FakePlanner:
    def __init__(self):
        self.calls = []; self.ready = []; self.launched = []
    def start(self, project, snapshot, command_id):
        self.calls.append((project["project_id"], command_id))
        plan_id = "ai_plan:" + command_id
        self.ready.append({"plan_id":plan_id,"command_id":command_id,"project_id":project["project_id"],"state":"ready"})
        return plan_id, "planning started"
    def ready_records(self): return list(self.ready)
    def terminal_records(self): return []
    def mark_control_synced(self, plan_id): pass
    def mark_worker_launched(self, plan_id, source_request_id):
        self.launched.append((plan_id, source_request_id)); self.ready = [r for r in self.ready if r["plan_id"] != plan_id]
    def mark_worker_blocked(self, plan_id, reason):
        self.ready = [r for r in self.ready if r["plan_id"] != plan_id]


class ControlCommandTests(unittest.TestCase):
    def test_atomic_inbox_command_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; repo.mkdir()
            config = base / "projects.json"; write_config(config, repo)
            runtime = base / "runtime"
            command = submit_control_command(runtime, "p1", "continue")
            executor = FakeExecutor(launch=True)
            summary = {"projects": [{"project_id": "p1", "state": "READY_TO_RUN"}]}
            coordinator = ControlCommandCoordinator(runtime)
            outcomes = coordinator.advance(config, summary, executor)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["state"], "accepted")
            self.assertEqual(outcomes[0]["command_id"], command["command_id"])
            self.assertEqual(coordinator.advance(config, summary, executor), [])
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(latest_control_result(runtime, "p1")["state"], "accepted")

    def test_missing_project_and_executor_refusal_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; repo.mkdir()
            config = base / "projects.json"; write_config(config, repo)
            runtime = base / "runtime"
            missing = submit_control_command(runtime, "missing", "continue")
            coordinator = ControlCommandCoordinator(runtime)
            outcomes = coordinator.advance(config, {"projects": []}, FakeExecutor())
            self.assertEqual(outcomes[0]["command_id"], missing["command_id"])
            self.assertEqual(outcomes[0]["state"], "blocked")
            self.assertIn("not configured", outcomes[0]["reason"])

            command = submit_control_command(runtime, "p1", "continue")
            executor = FakeExecutor(launch=False)
            summary = {"projects": [{"project_id": "p1", "state": "IDLE"}]}
            outcomes = coordinator.advance(config, summary, executor)
            self.assertEqual(outcomes[0]["command_id"], command["command_id"])
            self.assertEqual(outcomes[0]["state"], "blocked")
            self.assertEqual(outcomes[0]["reason"], "not ready")

    def test_pending_design_routes_to_planner_then_resumes_worker(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td); repo=base/"repo"; repo.mkdir(); runtime=base/"runtime"
            config=base/"projects.json"; write_config(config, repo)
            command=submit_control_command(runtime,"p1","continue")
            planner=FakePlanner(); executor=FakeExecutor(launch=True)
            coordinator=ControlCommandCoordinator(runtime, planner)
            pending={"projects":[{"project_id":"p1","state":"IDLE","next_status":"**PENDING DESIGN**","telemetry":{"task_id":"P1"}}]}
            first=coordinator.advance(config,pending,executor)
            self.assertEqual(first[0]["lifecycle_action"],"plan")
            self.assertEqual(executor.calls,[])
            ready={"projects":[{"project_id":"p1","state":"READY_TO_RUN","next_status":"**READY_TO_RUN**","telemetry":{"task_id":"P1"}}]}
            second=coordinator.advance(config,ready,executor)
            self.assertEqual(second[0]["lifecycle_action"],"execute")
            self.assertEqual(len(executor.calls),1)
            self.assertEqual(planner.launched[0][0],"ai_plan:"+command["command_id"])
            latest=latest_control_result(runtime,"p1")
            self.assertEqual(latest["lifecycle_action"],"execute")

    def test_transition_control_requires_ready_to_run(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P1")
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {
                    "enabled": True, "owner_authorized": True,
                    "allowed_next_actions": ["next_task"],
                    "preferred_backends": ["agy"], "backends": {"agy": {"project": "agy-p1"}},
                },
            }
            executor = TransitionExecutor(runtime, backend_overrides={"agy": FakeBackend("agy")})
            snapshot = {
                "project_id": "p1", "repo_path": str(repo), "state": "IDLE",
                "worker": {"kind": "none", "state": "not_started"},
                "telemetry": {"task_id": "P1"}, "next_title": "P1 task",
            }
            self.assertIsNone(executor.start_control(project, snapshot, "control-1"))
            record = executor.state()["executions"]["control-1"]
            self.assertEqual(record["state"], "blocked")
            self.assertIn("READY_TO_RUN", record["reason"])

    def test_transition_control_ready_launches_once(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P1")
            backend = FakeBackend("agy")
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {
                    "enabled": True, "owner_authorized": True,
                    "allowed_next_actions": ["next_task"],
                    "preferred_backends": ["agy"], "backends": {"agy": {"project": "agy-p1"}},
                },
            }
            snapshot = {
                "project_id": "p1", "repo_path": str(repo), "state": "READY_TO_RUN",
                "worker": {"kind": "none", "state": "not_started"},
                "telemetry": {"task_id": "P1"}, "next_title": "P1 task",
            }
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launch = executor.start_control(project, snapshot, "control-ready")
            self.assertIsNotNone(launch)
            thread = executor._threads["control-ready"]; thread.join(timeout=2)
            self.assertEqual(executor.state()["executions"]["control-ready"]["state"], "completed")
            self.assertEqual(len(backend.started), 1)
            self.assertIsNone(executor.start_control(project, snapshot, "control-ready"))
            self.assertEqual(len(backend.started), 1)

    def test_malicious_command_id_is_rejected_without_path_escape(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; repo.mkdir()
            config = base / "projects.json"; write_config(config, repo)
            runtime = base / "runtime"; inbox = runtime / "control" / "inbox"; inbox.mkdir(parents=True)
            (inbox / "bad.json").write_text(json.dumps({
                "version": 1, "command_id": "../escape", "project_id": "p1",
                "action": "continue", "state": "pending", "requested_at": "x",
            }), encoding="utf-8")
            outcomes = ControlCommandCoordinator(runtime).advance(
                config, {"projects": [{"project_id": "p1", "state": "READY_TO_RUN"}]}, FakeExecutor()
            )
            self.assertEqual(outcomes[0]["state"], "blocked")
            self.assertEqual(outcomes[0]["reason"], "invalid command_id")
            self.assertNotEqual(outcomes[0]["command_id"], "../escape")
            self.assertFalse((runtime / "control" / "escape.json").exists())

    def test_control_cannot_bypass_unresolved_aibroker_review(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P2")
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {
                    "enabled": True, "owner_authorized": True, "engine": "aibroker",
                    "allowed_next_actions": ["next_task"],
                },
            }
            snapshot = {
                "project_id": "p1", "repo_path": str(repo), "state": "READY_TO_RUN",
                "worker": {"kind": "none", "state": "not_started"},
                "telemetry": {"task_id": "P2"}, "next_title": "P2 task",
            }
            runtime.mkdir(parents=True)
            ledger = {"version": 1, "executions": {"worker-1": {
                "project_id": "p1", "source_request_id": "worker-1", "engine": "aibroker",
                "state": "completed", "completed_at": "2026-09-10T01:00:00+00:00",
            }}}
            (runtime / "transition-executor.json").write_text(json.dumps(ledger), encoding="utf-8")
            executor = TransitionExecutor(runtime)
            self.assertIsNone(executor.start_control(project, snapshot, "control-review-1"))
            reason = executor.state()["executions"]["control-review-1"]["reason"]
            self.assertIn("review is unresolved", reason)

            (runtime / "ai-reviewer.json").write_text(json.dumps({"version": 1, "reviews": {
                "ai_review:worker-1": {"state": "completed"}
            }}), encoding="utf-8")
            self.assertIsNone(executor.start_control(project, snapshot, "control-review-2"))
            reason = executor.state()["executions"]["control-review-2"]["reason"]
            self.assertIn("transition is not yet applied", reason)

    def test_replayed_command_id_is_idempotent_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; repo.mkdir()
            config = base / "projects.json"; write_config(config, repo)
            runtime = base / "runtime"; coordinator = ControlCommandCoordinator(runtime)
            command = submit_control_command(runtime, "p1", "continue")
            executor = FakeExecutor(launch=True)
            summary = {"projects": [{"project_id": "p1", "state": "READY_TO_RUN"}]}
            first = coordinator.advance(config, summary, executor)[0]
            self.assertEqual(first["state"], "accepted")
            inbox = runtime / "control" / "inbox"; inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "replay.json").write_text(json.dumps(command), encoding="utf-8")
            second = coordinator.advance(config, summary, executor)[0]
            self.assertEqual(second, first)
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(latest_control_result(runtime, "p1")["state"], "accepted")

    def test_invalid_version_or_state_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; repo.mkdir()
            config = base / "projects.json"; write_config(config, repo)
            runtime = base / "runtime"; inbox = runtime / "control" / "inbox"; inbox.mkdir(parents=True)
            (inbox / "bad-version.json").write_text(json.dumps({
                "version": 99, "command_id": "bad-version", "project_id": "p1",
                "action": "continue", "state": "pending", "requested_at": "x",
            }), encoding="utf-8")
            outcome = ControlCommandCoordinator(runtime).advance(
                config, {"projects": [{"project_id": "p1", "state": "READY_TO_RUN"}]}, FakeExecutor()
            )[0]
            self.assertEqual(outcome["state"], "blocked")
            self.assertIn("version or state", outcome["reason"])


if __name__ == "__main__":
    unittest.main()
