import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.control.surface import project_control_view, project_identity
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.core.transition_executor import TransitionExecutor
from tests_py.test_control_commands import FakeExecutor, write_config
from tests_py.test_transition_executor import FakeBackend, make_repo


class FakeInterruptPort:
    def __init__(self):
        self.calls = []

    def interrupt(self, request_id, reason):
        self.calls.append((request_id, reason))
        return {"request_id": request_id, "status": "interrupted"}


class FakeBridgeStore:
    def __init__(self, claimed=False):
        self.claimed = claimed

    def has_active_claim(self, adapter, binding_id):
        return self.claimed


class P12ActionTests(unittest.TestCase):
    def fixture(self, base: Path):
        repo = base / "repo"; repo.mkdir()
        config = base / "projects.json"; write_config(config, repo)
        runtime = base / "runtime"
        snapshot = {
            "project_id": "p1", "state": "READY_TO_RUN", "lifecycle_state": "READY_TO_RUN",
            "next_status": "**READY_TO_RUN**", "git": {"branch": "main", "head": "h1"},
            "telemetry": {"task_id": "P1"},
        }
        return repo, config, runtime, snapshot

    def consume(self, runtime, config, snapshot, action, executor=None, **coordinator_options):
        record = submit_control_command(runtime, "p1", action)
        result = ControlCommandCoordinator(runtime, **coordinator_options).advance(
            config, {"projects": [snapshot]}, executor or FakeExecutor()
        )
        return record, result[0]

    def test_pause_resume_and_final_launch_barrier(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); _, config, runtime, snapshot = self.fixture(base)
            _, paused = self.consume(runtime, config, snapshot, "pause")
            self.assertEqual(paused["state"], "accepted")
            self.assertTrue(OwnerControlStore(runtime).is_paused("p1"))
            blocked_executor = FakeExecutor()
            command = submit_control_command(runtime, "p1", "continue")
            blocked = ControlCommandCoordinator(runtime).advance(
                config, {"projects": [snapshot]}, blocked_executor
            )[0]
            self.assertEqual(blocked["command_id"], command["command_id"])
            self.assertEqual(blocked["state"], "blocked")
            self.assertEqual(blocked_executor.calls, [])
            _, resumed = self.consume(runtime, config, snapshot, "resume")
            self.assertEqual(resumed["state"], "accepted")
            self.assertFalse(OwnerControlStore(runtime).is_paused("p1"))

    def test_transition_executor_enforces_pause_at_launch_gate(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo, "P1")
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"enabled": True, "owner_authorized": True,
                              "allowed_next_actions": ["next_task"],
                              "preferred_backends": ["agy"], "backends": {"agy": {"project": "p1"}}},
            }
            snapshot = {"project_id": "p1", "state": "READY_TO_RUN", "telemetry": {"task_id": "P1"}}
            OwnerControlStore(runtime).set_paused("p1", True, command_id="pause-1", action="pause")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": FakeBackend("agy")})
            self.assertIsNone(executor.start_control(project, snapshot, "launch-1"))
            self.assertIn("pause", executor.state()["executions"]["launch-1"]["reason"])

    def test_stop_interrupts_only_exact_aibroker_execution(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); _, config, runtime, snapshot = self.fixture(base)
            port = FakeInterruptPort(); executor = FakeExecutor()
            executor._ai_execution_port = port
            executor.records["run-1"] = {
                "source_request_id": "run-1", "project_id": "p1", "state": "running",
                "engine": "aibroker", "broker_request_id": "broker-1", "started_at": "2026-01-01T00:00:00Z",
            }
            _, outcome = self.consume(runtime, config, snapshot, "stop", executor=executor)
            self.assertEqual(outcome["state"], "accepted")
            self.assertEqual(outcome["effect"], "pause_and_interrupt")
            self.assertEqual(port.calls[0][0], "broker-1")
            self.assertTrue(OwnerControlStore(runtime).is_paused("p1"))

            # Legacy activity is never guessed or killed.
            OwnerControlStore(runtime).set_paused("p1", False, command_id="resume-legacy", action="resume")
            executor.records = {"legacy": {"project_id": "p1", "state": "running", "engine": "legacy"}}
            _, blocked = self.consume(runtime, config, snapshot, "stop", executor=executor)
            self.assertEqual(blocked["state"], "blocked")
            self.assertFalse(OwnerControlStore(runtime).is_paused("p1"))

    def test_binding_actions_preserve_uniqueness_and_claim_guard(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); _, config, runtime, snapshot = self.fixture(base)
            conversations = ConversationControlStore(runtime)
            for binding_id in ("one", "two"):
                conversations.heartbeat(
                    "chatgpt", binding_id, title=binding_id,
                    url=f"https://chatgpt.com/c/{binding_id}", tab_instance_id="tab-" + binding_id,
                )
            target = {"adapter": "chatgpt", "binding_id": "one"}
            submit_control_command(runtime, "p1", "bind_conversation", target=target)
            coordinator = ControlCommandCoordinator(runtime, conversation_store=conversations)
            bound = coordinator.advance(config, {"projects": [snapshot]}, FakeExecutor())[0]
            self.assertEqual(bound["binding"]["binding_id"], "one")

            bound_snapshot = dict(snapshot)
            bound_snapshot["conversation_binding"] = {"transport": "browser_bridge", **target}
            submit_control_command(runtime, "p1", "unbind_conversation")
            guarded = ControlCommandCoordinator(
                runtime, conversation_store=conversations, bridge_store=FakeBridgeStore(True)
            ).advance(config, {"projects": [bound_snapshot]}, FakeExecutor())[0]
            self.assertEqual(guarded["state"], "blocked")
            self.assertEqual(conversations.binding_for_project("p1")["binding_id"], "one")

            submit_control_command(runtime, "p1", "rebind_conversation", target={"adapter": "chatgpt", "binding_id": "two"})
            rebound = coordinator.advance(config, {"projects": [bound_snapshot]}, FakeExecutor())[0]
            self.assertEqual(rebound["binding"]["binding_id"], "two")

    def test_stale_revision_and_unimplemented_actions_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); _, config, runtime, snapshot = self.fixture(base)
            expected = project_identity(snapshot, runtime)
            expected["revision"] = "sha256:stale"
            submit_control_command(runtime, "p1", "pause", expected=expected, source="control_api")
            stale = ControlCommandCoordinator(runtime).advance(config, {"projects": [snapshot]}, FakeExecutor())[0]
            self.assertEqual(stale["state"], "blocked")
            self.assertIn("stale project identity", stale["reason"])
            for action in ("retry", "reconcile", "approve_owner_gate"):
                view = project_control_view(snapshot, runtime)
                capability = next(item for item in view["controls"] if item["action"] == action)
                self.assertFalse(capability["available"])
                submit_control_command(runtime, "p1", action)
                outcome = ControlCommandCoordinator(runtime).advance(config, {"projects": [snapshot]}, FakeExecutor())[0]
                self.assertEqual(outcome["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
