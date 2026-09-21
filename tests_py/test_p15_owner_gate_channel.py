import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.control.surface import project_control_view
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.control_commands import (
    ControlCommandCoordinator,
    submit_control_command,
)
from dev_orchestrator.core.repository import read_repository_truth


from tests_py.test_control_commands import FakeExecutor


class FakeBridgeStore:
    def __init__(self, active_claim=False):
        self._active_claim = active_claim

    def has_active_claim(self, adapter, binding_id):
        return self._active_claim


class TestP15OwnerGateChannel(unittest.TestCase):
    def owner_gate_fixture(self, base: Path):
        repo = base / "owner-repo"
        (repo / "agent").mkdir(parents=True)
        next_text = "# P15 Example\n\nStatus: **PENDING DESIGN**\n\nGoal: bounded work.\n"
        (repo / "agent" / "next.md").write_text(next_text, encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
        truth = read_repository_truth(repo)
        runtime = base / "owner-runtime"
        config = base / "owner-projects.json"
        project = {
            "project_id": "p1", "repo_path": str(repo), "adapter": "agent_files",
            "conversation_binding": {
                "transport": "browser_bridge", "adapter": "chatgpt", "binding_id": "conv-one",
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
            "telemetry": {"task_id": "P15"},
            "conversation_binding": project["conversation_binding"],
        }
        gate_id = "ai_plan:cmd-source"
        plan = {
            "task_id": "P15", "summary": "Owner accepts the bounded plan.",
            "implementation_steps": ["Implement the bounded change"],
            "interfaces": ["Keep the current interface"],
            "validation": ["Run tests"],
            "risks": ["Repository state may move"],
            "out_of_scope": ["No unrelated refactor"],
        }
        runtime.mkdir(parents=True)
        (runtime / "ai-planner.json").write_text(json.dumps({
            "version": 1,
            "plans": {gate_id: {
                "plan_id": gate_id, "command_id": "cmd-source",
                "project_id": "p1", "task_id": "P15", "repo_path": str(repo),
                "branch": truth.branch, "head": truth.head, "status_hash": truth.status_hash,
                "state": "owner_gate", "started_at": "2026-09-15T00:00:00+00:00",
                "completed_at": "2026-09-15T00:01:00+00:00", "reason": "bounded review exhausted",
                "next_text": next_text, "plan": plan, "rejection_chain": [],
                "conversation_binding": project["conversation_binding"],
            }},
        }), encoding="utf-8")
        conversations = ConversationControlStore(runtime)
        planner = AIPlannerCoordinator(runtime, None)
        security = ControlSecurity(runtime)
        return repo, config, runtime, project, snapshot, gate_id, planner, conversations, security

    def test_mobile_gate_approval_succeeds_without_live_session_and_persists_audit(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo, config, runtime, project, snapshot, gate_id, planner, conversations, security = self.owner_gate_fixture(base)
            bridge = FakeBridgeStore(False)
            pairing = security.create_mobile_pairing()
            redeemed = security.redeem_mobile_pairing(pairing["pairing_id"], pairing["code"], "Pixel Phone")
            device_id = redeemed["device_id"]

            view = project_control_view(snapshot, runtime, project, bridge)
            expected = view["control_identity"]
            command = submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="approve-mob-1",
                expected=expected, target={"gate_id": gate_id},
                source=f"mobile_gateway:{device_id}",
            )
            executor = FakeExecutor()
            coordinator = ControlCommandCoordinator(
                runtime, planner, conversation_store=conversations, bridge_store=bridge,
                mobile_device_authorizer=security,
            )
            outcomes = coordinator.advance(config, {"projects": [snapshot]}, executor)
            approved = next(row for row in outcomes if row.get("command_id") == "approve-mob-1")
            self.assertEqual(approved["state"], "accepted")
            self.assertEqual(approved["effect"], "approve_exact_owner_gate_no_worker_started")
            self.assertEqual(executor.calls, [])

            # Plan is owner_approved with channel and device_id audit fields
            plan_rec = planner.state()["plans"][gate_id]
            self.assertEqual(plan_rec["state"], "owner_approved")
            self.assertEqual(plan_rec["approved_via"], "mobile_device")
            self.assertEqual(plan_rec["approving_device_id"], device_id)

    def test_missing_authorizer_blocks_mobile_approval(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _, config, runtime, project, snapshot, gate_id, planner, conversations, _ = self.owner_gate_fixture(base)
            bridge = FakeBridgeStore(False)
            view = project_control_view(snapshot, runtime, project, bridge)
            expected = view["control_identity"]
            submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="approve-mob-no-auth",
                expected=expected, target={"gate_id": gate_id},
                source="mobile_gateway:dev-1234",
            )
            executor = FakeExecutor()
            coordinator = ControlCommandCoordinator(
                runtime, planner, conversation_store=conversations, bridge_store=bridge,
                mobile_device_authorizer=None,
            )
            outcomes = coordinator.advance(config, {"projects": [snapshot]}, executor)
            res = next(row for row in outcomes if row.get("command_id") == "approve-mob-no-auth")
            self.assertEqual(res["state"], "blocked")
            self.assertIn("mobile authorizer unavailable", res["reason"])

    def test_revoked_device_blocks_mobile_approval(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _, config, runtime, project, snapshot, gate_id, planner, conversations, security = self.owner_gate_fixture(base)
            bridge = FakeBridgeStore(False)
            pairing = security.create_mobile_pairing()
            redeemed = security.redeem_mobile_pairing(pairing["pairing_id"], pairing["code"], "Pixel Phone")
            device_id = redeemed["device_id"]
            # Revoke device before execution
            security.revoke_mobile_device(device_id)

            view = project_control_view(snapshot, runtime, project, bridge)
            expected = view["control_identity"]
            submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="approve-mob-revoked",
                expected=expected, target={"gate_id": gate_id},
                source=f"mobile_gateway:{device_id}",
            )
            executor = FakeExecutor()
            coordinator = ControlCommandCoordinator(
                runtime, planner, conversation_store=conversations, bridge_store=bridge,
                mobile_device_authorizer=security,
            )
            outcomes = coordinator.advance(config, {"projects": [snapshot]}, executor)
            res = next(row for row in outcomes if row.get("command_id") == "approve-mob-revoked")
            self.assertEqual(res["state"], "blocked")
            self.assertIn("mobile device unauthorized", res["reason"])

    def test_active_bridge_claim_blocks_mobile_approval(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _, config, runtime, project, snapshot, gate_id, planner, conversations, security = self.owner_gate_fixture(base)
            bridge = FakeBridgeStore(active_claim=True)
            pairing = security.create_mobile_pairing()
            redeemed = security.redeem_mobile_pairing(pairing["pairing_id"], pairing["code"], "Pixel Phone")
            device_id = redeemed["device_id"]

            view = project_control_view(snapshot, runtime, project, bridge)
            expected = view["control_identity"]
            submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="approve-mob-claim",
                expected=expected, target={"gate_id": gate_id},
                source=f"mobile_gateway:{device_id}",
            )
            executor = FakeExecutor()
            coordinator = ControlCommandCoordinator(
                runtime, planner, conversation_store=conversations, bridge_store=bridge,
                mobile_device_authorizer=security,
            )
            outcomes = coordinator.advance(config, {"projects": [snapshot]}, executor)
            res = next(row for row in outcomes if row.get("command_id") == "approve-mob-claim")
            self.assertEqual(res["state"], "blocked")
            self.assertIn("active claimed Web Sol request blocks owner-gate approval", res["reason"])

    def test_dirty_repo_blocks_approval(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo, config, runtime, project, snapshot, gate_id, planner, conversations, security = self.owner_gate_fixture(base)
            bridge = FakeBridgeStore(False)
            pairing = security.create_mobile_pairing()
            redeemed = security.redeem_mobile_pairing(pairing["pairing_id"], pairing["code"], "Pixel Phone")
            device_id = redeemed["device_id"]

            # Dirties repository
            (repo / "dirty.txt").write_text("dirt", encoding="utf-8")

            view = project_control_view(snapshot, runtime, project, bridge)
            expected = view["control_identity"]
            submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="approve-mob-dirty",
                expected=expected, target={"gate_id": gate_id},
                source=f"mobile_gateway:{device_id}",
            )
            executor = FakeExecutor()
            coordinator = ControlCommandCoordinator(
                runtime, planner, conversation_store=conversations, bridge_store=bridge,
                mobile_device_authorizer=security,
            )
            outcomes = coordinator.advance(config, {"projects": [snapshot]}, executor)
            res = next(row for row in outcomes if row.get("command_id") == "approve-mob-dirty")
            self.assertEqual(res["state"], "blocked")
            self.assertIn("clean repository", res["reason"])

    def test_control_api_source_preserves_live_conversation_requirement(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _, config, runtime, project, snapshot, gate_id, planner, conversations, security = self.owner_gate_fixture(base)
            bridge = FakeBridgeStore(False)
            view = project_control_view(snapshot, runtime, project, bridge)
            expected = view["control_identity"]
            # No live heartbeat in conversation store
            submit_control_command(
                runtime, "p1", "approve_owner_gate", command_id="approve-control-api",
                expected=expected, target={"gate_id": gate_id},
                source="control_api",
            )
            executor = FakeExecutor()
            coordinator = ControlCommandCoordinator(
                runtime, planner, conversation_store=conversations, bridge_store=bridge,
                mobile_device_authorizer=security,
            )
            outcomes = coordinator.advance(config, {"projects": [snapshot]}, executor)
            res = next(row for row in outcomes if row.get("command_id") == "approve-control-api")
            self.assertEqual(res["state"], "blocked")
            self.assertIn("bound conversation is not currently live", res["reason"])


if __name__ == "__main__":
    unittest.main()
