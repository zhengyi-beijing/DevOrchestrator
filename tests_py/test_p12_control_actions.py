import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.command_store import ControlCommandConflictError
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.control.surface import project_control_view, project_identity
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.repository import read_repository_truth
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
        record = submit_control_command(
            runtime, "p1", action, expected=project_identity(snapshot, runtime)
        )
        result = ControlCommandCoordinator(runtime, **coordinator_options).advance(
            config, {"projects": [snapshot]}, executor or FakeExecutor()
        )
        return record, result[0]

    def owner_gate_fixture(self, base: Path):
        repo = base / "owner-repo"
        (repo / "agent").mkdir(parents=True)
        next_text = "# P14 Example\n\nStatus: **PENDING DESIGN**\n\nGoal: bounded work.\n"
        (repo / "agent" / "next.md").write_text(next_text, encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
        truth = read_repository_truth(repo)
        runtime = base / "owner-runtime"
        config = base / "owner-projects.json"
        project = {
            "project_id": "p1", "repo_path": str(repo), "adapter": "agent_files",
            "conversation_binding": {
                "transport": "browser_bridge", "adapter": "chatgpt", "binding_id": "conversation-one",
            },
            "execution": {
                "enabled": True, "owner_authorized": True, "engine": "aibroker",
                "allowed_next_actions": ["next_task"],
            },
            "ai_roles": {"planner": {"enabled": True}},
        }
        config.write_text(json.dumps({"projects": [project]}), encoding="utf-8")
        snapshot = {
            "project_id": "p1", "repo_path": str(repo), "state": "IDLE",
            "lifecycle_state": "IDLE", "next_status": "**PENDING DESIGN**",
            "git": {
                "branch": truth.branch, "head": truth.head, "dirty": False,
                "status_hash": truth.status_hash,
            },
            "telemetry": {"task_id": "P14"},
            "conversation_binding": project["conversation_binding"],
        }
        gate_id = "ai_plan:source-command"
        plan = {
            "task_id": "P14", "summary": "Owner accepts the bounded plan.",
            "implementation_steps": ["Implement the bounded change"],
            "interfaces": ["Keep the current interface"],
            "validation": ["Run focused and full tests"],
            "risks": ["Repository state may move"],
            "out_of_scope": ["No unrelated refactor"],
        }
        runtime.mkdir(parents=True)
        (runtime / "ai-planner.json").write_text(json.dumps({
            "version": 1,
            "plans": {gate_id: {
                "plan_id": gate_id, "command_id": "source-command",
                "project_id": "p1", "task_id": "P14", "repo_path": str(repo),
                "branch": truth.branch, "head": truth.head, "status_hash": truth.status_hash,
                "state": "owner_gate", "started_at": "2026-09-15T00:00:00+00:00",
                "completed_at": "2026-09-15T00:01:00+00:00", "reason": "bounded review exhausted",
                "next_text": next_text, "plan": plan, "rejection_chain": [],
                "conversation_binding": project["conversation_binding"],
            }},
        }), encoding="utf-8")
        conversations = ConversationControlStore(runtime)
        conversations.heartbeat(
            "chatgpt", "conversation-one", title="Owner conversation",
            url="https://chatgpt.com/c/conversation-one", tab_instance_id="tab-owner",
        )
        conversations.bind("p1", "chatgpt", "conversation-one")
        planner = AIPlannerCoordinator(runtime, None)
        return repo, config, runtime, project, snapshot, gate_id, planner, conversations

    @staticmethod
    def outcome_for(outcomes, command_id):
        return next(row for row in outcomes if row.get("command_id") == command_id)

    def test_pause_resume_and_final_launch_barrier(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); _, config, runtime, snapshot = self.fixture(base)
            _, paused = self.consume(runtime, config, snapshot, "pause")
            self.assertEqual(paused["state"], "accepted")
            self.assertTrue(OwnerControlStore(runtime).is_paused("p1"))
            blocked_executor = FakeExecutor()
            command = submit_control_command(
                runtime, "p1", "continue", expected=project_identity(snapshot, runtime)
            )
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

    def test_stop_is_isolated_from_other_project_execution_and_owner_state(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo, config, runtime, snapshot = self.fixture(base)
            config.write_text(json.dumps({"projects": [
                {
                    "project_id": project_id, "repo_path": str(repo), "adapter": "agent_files",
                    "execution": {
                        "enabled": True, "owner_authorized": True,
                        "allowed_next_actions": ["next_task"],
                        "preferred_backends": ["agy"], "backends": {"agy": {"project": project_id}},
                    },
                }
                for project_id in ("p1", "p2")
            ]}), encoding="utf-8")
            other = {**snapshot, "project_id": "p2"}
            port = FakeInterruptPort(); executor = FakeExecutor(); executor._ai_execution_port = port
            executor.records = {
                "run-p1": {
                    "source_request_id": "run-p1", "project_id": "p1", "state": "running",
                    "engine": "aibroker", "broker_request_id": "broker-p1", "started_at": "2026-01-01T00:00:00Z",
                },
                "run-p2": {
                    "source_request_id": "run-p2", "project_id": "p2", "state": "running",
                    "engine": "aibroker", "broker_request_id": "broker-p2", "started_at": "2026-01-02T00:00:00Z",
                },
            }
            submit_control_command(
                runtime, "p1", "stop", expected=project_identity(snapshot, runtime)
            )
            outcome = ControlCommandCoordinator(runtime).advance(
                config, {"projects": [snapshot, other]}, executor
            )[0]
            self.assertEqual(outcome["execution_id"], "run-p1")
            self.assertEqual(port.calls, [("broker-p1", "explicit P12 owner stop")])
            self.assertTrue(OwnerControlStore(runtime).is_paused("p1"))
            self.assertFalse(OwnerControlStore(runtime).is_paused("p2"))

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
            submit_control_command(
                runtime, "p1", "bind_conversation", target=target,
                expected=project_identity(snapshot, runtime),
            )
            coordinator = ControlCommandCoordinator(runtime, conversation_store=conversations)
            bound = coordinator.advance(config, {"projects": [snapshot]}, FakeExecutor())[0]
            self.assertEqual(bound["binding"]["binding_id"], "one")

            bound_snapshot = dict(snapshot)
            bound_snapshot["conversation_binding"] = {"transport": "browser_bridge", **target}
            submit_control_command(
                runtime, "p1", "unbind_conversation",
                expected=project_identity(bound_snapshot, runtime),
            )
            guarded = ControlCommandCoordinator(
                runtime, conversation_store=conversations, bridge_store=FakeBridgeStore(True)
            ).advance(config, {"projects": [bound_snapshot]}, FakeExecutor())[0]
            self.assertEqual(guarded["state"], "blocked")
            self.assertEqual(conversations.binding_for_project("p1")["binding_id"], "one")
            guarded_view = project_control_view(
                bound_snapshot, runtime, bridge_store=FakeBridgeStore(True)
            )
            guarded_capabilities = {item["action"]: item for item in guarded_view["controls"]}
            self.assertFalse(guarded_capabilities["unbind_conversation"]["available"])
            self.assertFalse(guarded_capabilities["rebind_conversation"]["available"])
            self.assertTrue(guarded_view["conversation"]["active_claim"])

            submit_control_command(
                runtime, "p1", "rebind_conversation",
                target={"adapter": "chatgpt", "binding_id": "two"},
                expected=project_identity(bound_snapshot, runtime),
            )
            rebound = coordinator.advance(config, {"projects": [bound_snapshot]}, FakeExecutor())[0]
            self.assertEqual(rebound["binding"]["binding_id"], "two")

    def test_exact_owner_gate_approval_is_idempotent_and_requires_continue(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo, config, runtime, project, snapshot, gate_id, planner, conversations = self.owner_gate_fixture(base)
            bridge = FakeBridgeStore(False)
            view = project_control_view(snapshot, runtime, project, bridge)
            approve_capability = next(
                item for item in view["controls"] if item["action"] == "approve_owner_gate"
            )
            self.assertTrue(approve_capability["available"], approve_capability)
            expected = view["control_identity"]
            command = submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="approve-one",
                expected=expected, target={"gate_id": gate_id},
            )
            executor = FakeExecutor()
            coordinator = ControlCommandCoordinator(
                runtime, planner, conversation_store=conversations, bridge_store=bridge,
            )
            approved = self.outcome_for(
                coordinator.advance(config, {"projects": [snapshot]}, executor),
                command["command_id"],
            )
            self.assertEqual(approved["state"], "accepted")
            self.assertEqual(approved["effect"], "approve_exact_owner_gate_no_worker_started")
            self.assertEqual(executor.calls, [])
            self.assertEqual(planner.state()["plans"][gate_id]["state"], "owner_approved")
            audit = [
                json.loads(line)
                for line in (runtime / "control" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            settled = [
                row for row in audit
                if row.get("event") == "command_settled" and row.get("command_id") == "approve-one"
            ]
            self.assertEqual(len(settled), 1)
            self.assertEqual(settled[0]["effect"], "approve_exact_owner_gate_no_worker_started")
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip(),
                snapshot["git"]["head"],
            )

            # Exact replay returns the terminal result and never repeats mutation.
            replay = submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="approve-one",
                expected=expected, target={"gate_id": gate_id},
            )
            self.assertEqual(replay, approved)
            self.assertEqual(coordinator.advance(config, {"projects": [snapshot]}, executor), [])
            with self.assertRaises(ControlCommandConflictError):
                submit_control_command(
                    runtime, "p1", "approve_owner_gate", command_id="approve-one",
                    expected=expected, target={"gate_id": "different-gate"},
                )

            # A different command cannot approve a gate that is no longer pending.
            refreshed = project_identity(snapshot, runtime)
            duplicate = submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="approve-two",
                expected=refreshed, target={"gate_id": gate_id},
            )
            duplicate_outcome = self.outcome_for(
                coordinator.advance(config, {"projects": [snapshot]}, executor), "approve-two"
            )
            self.assertEqual(duplicate_outcome["state"], "blocked")
            self.assertIn("currently projected owner gate", duplicate_outcome["reason"])

            # Normal continue applies the accepted plan but still does not launch
            # a Worker in the approval or continue command transaction.
            continue_command = submit_control_command(
                runtime, "p1", "continue", command_id="continue-after-approval",
                expected=project_identity(snapshot, runtime),
            )
            continued = self.outcome_for(
                coordinator.advance(config, {"projects": [snapshot]}, executor),
                continue_command["command_id"],
            )
            self.assertEqual(continued["state"], "accepted")
            self.assertEqual(planner.state()["plans"][gate_id]["state"], "ready")
            self.assertEqual(executor.calls, [])

            truth = read_repository_truth(repo)
            ready_snapshot = {
                **snapshot, "state": "IDLE", "lifecycle_state": "IDLE",
                "next_status": "**READY_TO_RUN**",
                "git": {
                    "branch": truth.branch, "head": truth.head, "dirty": False,
                    "status_hash": truth.status_hash,
                },
            }
            launched = coordinator.advance(config, {"projects": [ready_snapshot]}, executor)
            self.assertTrue(any(row.get("lifecycle_action") == "execute" for row in launched))
            self.assertEqual(len(executor.calls), 1)

    def test_owner_gate_rejects_stale_wrong_and_cross_project_identity(self):
        mutations = {
            "revision": "sha256:stale",
            "branch": "wrong-branch",
            "head": "wrong-head",
            "task_id": "wrong-task",
        }
        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                _, config, runtime, _, snapshot, gate_id, planner, conversations = self.owner_gate_fixture(base)
                expected = project_identity(snapshot, runtime)
                expected[field] = value
                submit_control_command(
                    runtime, "p1", "approve_owner_gate", command_id="bad-" + field,
                    expected=expected, target={"gate_id": gate_id},
                )
                outcome = self.outcome_for(
                    ControlCommandCoordinator(
                        runtime, planner, conversation_store=conversations,
                        bridge_store=FakeBridgeStore(False),
                    ).advance(config, {"projects": [snapshot]}, FakeExecutor()),
                    "bad-" + field,
                )
                self.assertEqual(outcome["state"], "blocked")
                self.assertIn("stale project identity", outcome["reason"])
                self.assertEqual(planner.state()["plans"][gate_id]["state"], "owner_gate")

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo, config, runtime, project, snapshot, gate_id, planner, conversations = self.owner_gate_fixture(base)
            submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="wrong-gate",
                expected=project_identity(snapshot, runtime), target={"gate_id": "ai_plan:other"},
            )
            coordinator = ControlCommandCoordinator(
                runtime, planner, conversation_store=conversations,
                bridge_store=FakeBridgeStore(False),
            )
            outcome = self.outcome_for(
                coordinator.advance(config, {"projects": [snapshot]}, FakeExecutor()), "wrong-gate"
            )
            self.assertEqual(outcome["state"], "blocked")
            self.assertIn("currently projected owner gate", outcome["reason"])

            project_two = {
                **project, "project_id": "p2",
                "conversation_binding": {
                    "transport": "browser_bridge", "adapter": "chatgpt",
                    "binding_id": "conversation-two",
                },
            }
            config.write_text(json.dumps({"projects": [project, project_two]}), encoding="utf-8")
            snapshot_two = {
                **snapshot, "project_id": "p2",
                "conversation_binding": project_two["conversation_binding"],
            }
            conversations.heartbeat(
                "chatgpt", "conversation-two", title="Other",
                url="https://chatgpt.com/c/conversation-two", tab_instance_id="tab-two",
            )
            conversations.bind("p2", "chatgpt", "conversation-two")
            cross = submit_control_command(
                runtime, "p2", "approve_owner_gate", command_id="cross-project",
                expected=project_identity(snapshot_two, runtime), target={"gate_id": gate_id},
            )
            cross_outcome = self.outcome_for(
                coordinator.advance(config, {"projects": [snapshot, snapshot_two]}, FakeExecutor()),
                cross["command_id"],
            )
            self.assertEqual(cross_outcome["state"], "blocked")
            self.assertIn("currently projected owner gate", cross_outcome["reason"])

    def test_owner_gate_rejects_dirty_repo_and_active_browser_claim(self):
        for condition in ("dirty", "claimed"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                repo, config, runtime, project, snapshot, gate_id, planner, conversations = self.owner_gate_fixture(base)
                bridge = FakeBridgeStore(condition == "claimed")
                if condition == "dirty":
                    (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
                    snapshot = {**snapshot, "git": {**snapshot["git"], "dirty": True}}
                view = project_control_view(snapshot, runtime, project, bridge)
                capability = next(
                    item for item in view["controls"] if item["action"] == "approve_owner_gate"
                )
                self.assertFalse(capability["available"])
                command = submit_control_command(
                    runtime, "p1", "approve_owner_gate", command_id="blocked-" + condition,
                    expected=view["control_identity"], target={"gate_id": gate_id},
                )
                outcome = self.outcome_for(
                    ControlCommandCoordinator(
                        runtime, planner, conversation_store=conversations, bridge_store=bridge,
                    ).advance(config, {"projects": [snapshot]}, FakeExecutor()),
                    command["command_id"],
                )
                self.assertEqual(outcome["state"], "blocked")
                self.assertIn("clean" if condition == "dirty" else "active claimed", outcome["reason"])
                self.assertEqual(planner.state()["plans"][gate_id]["state"], "owner_gate")

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo, config, runtime, _, snapshot, gate_id, planner, conversations = self.owner_gate_fixture(base)
            (repo / "moved.txt").write_text("new head", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "moved.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "move head"], check=True)
            moved = read_repository_truth(repo)
            snapshot = {
                **snapshot,
                "git": {
                    "branch": moved.branch, "head": moved.head, "dirty": False,
                    "status_hash": moved.status_hash,
                },
            }
            command = submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="moved-repository",
                expected=project_identity(snapshot, runtime), target={"gate_id": gate_id},
            )
            outcome = self.outcome_for(
                ControlCommandCoordinator(
                    runtime, planner, conversation_store=conversations,
                    bridge_store=FakeBridgeStore(False),
                ).advance(config, {"projects": [snapshot]}, FakeExecutor()),
                command["command_id"],
            )
            self.assertEqual(outcome["state"], "blocked")
            self.assertIn("repository changed", outcome["reason"])

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _, config, runtime, _, snapshot, gate_id, planner, conversations = self.owner_gate_fixture(base)
            conversations.heartbeat(
                "chatgpt", "replacement", title="Replacement",
                url="https://chatgpt.com/c/replacement", tab_instance_id="tab-replacement",
            )
            conversations.rebind("p1", "chatgpt", "replacement")
            capability = next(
                item for item in project_control_view(
                    snapshot, runtime, bridge_store=FakeBridgeStore(False)
                )["controls"]
                if item["action"] == "approve_owner_gate"
            )
            self.assertFalse(capability["available"])
            command = submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="wrong-conversation",
                expected=project_identity(snapshot, runtime), target={"gate_id": gate_id},
            )
            outcome = self.outcome_for(
                ControlCommandCoordinator(
                    runtime, planner, conversation_store=conversations,
                    bridge_store=FakeBridgeStore(False),
                ).advance(config, {"projects": [snapshot]}, FakeExecutor()),
                command["command_id"],
            )
            self.assertEqual(outcome["state"], "blocked")
            self.assertIn("exact bound conversation", outcome["reason"])

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
                target = {"approve_owner_gate": {"gate_id": "gate"}}.get(action, {})
                submit_control_command(
                    runtime, "p1", action, target=target,
                    expected=project_identity(snapshot, runtime),
                )
                outcome = ControlCommandCoordinator(runtime).advance(config, {"projects": [snapshot]}, FakeExecutor())[0]
                self.assertEqual(outcome["state"], "blocked")

    def test_project_identity_is_project_scoped_and_every_action_revalidates_revision(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); _, config, runtime, snapshot = self.fixture(base)
            other = {**snapshot, "project_id": "p2"}
            self.assertNotEqual(project_identity(snapshot, runtime)["revision"], project_identity(other, runtime)["revision"])
            stale = project_identity(snapshot, runtime); stale["revision"] = "sha256:stale"
            targets = {
                "bind_conversation": {"adapter": "chatgpt_web", "binding_id": "one"},
                "rebind_conversation": {"adapter": "chatgpt_web", "binding_id": "two"},
                "approve_owner_gate": {"gate_id": "gate"},
                "retry": {"target_id": "retry"}, "reconcile": {"target_id": "reconcile"},
            }
            executor = FakeExecutor()
            for action in (
                "continue", "pause", "resume", "stop", "retry", "reconcile",
                "approve_owner_gate", "bind_conversation", "unbind_conversation", "rebind_conversation",
            ):
                submit_control_command(
                    runtime, "p1", action, expected=stale, target=targets.get(action, {}),
                    source="control_api",
                )
                outcome = ControlCommandCoordinator(runtime).advance(
                    config, {"projects": [snapshot]}, executor
                )[0]
                self.assertEqual(outcome["state"], "blocked", action)
                self.assertIn("stale project identity", outcome["reason"], action)
            self.assertEqual(executor.calls, [])
            self.assertFalse(OwnerControlStore(runtime).is_paused("p1"))


if __name__ == "__main__":
    unittest.main()
