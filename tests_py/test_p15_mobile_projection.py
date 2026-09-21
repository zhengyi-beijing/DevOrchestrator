import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.control.surface import _latest_owner_gate, project_control_view
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.mobile.authorizer import MobileDevicePrincipal
from dev_orchestrator.mobile.eligibility import mobile_owner_gate_eligibility
from dev_orchestrator.mobile.projection import MOBILE_CONTROL_ACTIONS, MobileProjectionService
from dev_orchestrator.storage.json_store import utc_now_iso


class TestP15MobileProjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.runtime_root = self.base / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "commit.gpgsign", "false"], check=True)
        (self.repo / "test.txt").write_text("hello", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)

        self.security = ControlSecurity(self.runtime_root)
        pairing = self.security.create_mobile_pairing()
        self.device = self.security.redeem_mobile_pairing(pairing["pairing_id"], pairing["code"], "Pixel Test")
        self.principal = MobileDevicePrincipal(
            device_id=self.device["device_id"],
            scope="mobile_device",
            device_label="Pixel Test",
            created_at="2026-09-21T00:00:00+00:00",
            expires_at=None,
            revoked=False,
        )

        truth = read_repository_truth(self.repo)
        self.project_config = {
            "project_id": "proj-1",
            "repo_path": str(self.repo),
            "execution": {"enabled": True, "owner_authorized": True, "allowed_next_actions": ["next_task"]},
        }
        self.snapshot = {
            "project_id": "proj-1",
            "repo_path": str(self.repo),
            "state": "IDLE",
            "lifecycle_state": "IDLE",
            "git": {
                "branch": truth.branch,
                "head": truth.head,
                "dirty": False,
                "status_hash": truth.status_hash,
            },
            "telemetry": {"task_id": "T1"},
        }
        self.projection = MobileProjectionService(
            runtime_root=self.runtime_root,
            config_provider={"projects": [self.project_config]},
            mobile_device_authorizer=self.security,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_mobile_control_actions_exposed(self):
        view = self.projection.project_view(self.snapshot, self.project_config, self.principal)
        controls = view["controls"]
        action_names = {c["action"] for c in controls}
        # Every exposed action must belong to MOBILE_CONTROL_ACTIONS
        self.assertTrue(action_names.issubset(MOBILE_CONTROL_ACTIONS))
        # None of the conversation-binding or rereview actions can be present
        self.assertNotIn("bind_conversation", action_names)
        self.assertNotIn("unbind_conversation", action_names)
        self.assertNotIn("rebind_conversation", action_names)
        self.assertNotIn("rereview", action_names)

    def test_verbatim_watchdog_and_recovery_epoch_copying(self):
        # 1. No watchdog file: progress_observation_state is unavailable
        view1 = self.projection.project_view(self.snapshot, self.project_config, self.principal)
        self.assertEqual(view1["progress_observation_state"], "unavailable")
        self.assertIsNone(view1["watchdog_state"])
        self.assertIsNone(view1["recovery_epoch"])

        # 2. Write authoritative watchdog.json
        watchdog_data = {
            "schema_version": 1,
            "projects": {
                "proj-1": {
                    "classification": "running",
                    "reason": "active turn in progress",
                    "recovery_epoch": {"epoch_id": "epoch-99", "timestamp": "2026-09-21T00:00:00+00:00"},
                    "last_check_epoch": time.time(),
                }
            }
        }
        (self.runtime_root / "watchdog.json").write_text(json.dumps(watchdog_data), encoding="utf-8")

        view2 = self.projection.project_view(self.snapshot, self.project_config, self.principal)
        self.assertEqual(view2["progress_observation_state"], "authoritative")
        self.assertEqual(view2["watchdog_state"], "running")
        self.assertEqual(view2["watchdog_reason"], "active turn in progress")
        self.assertEqual(view2["recovery_epoch"]["epoch_id"], "epoch-99")

        # 3. Old last_check_epoch -> stale
        watchdog_data["projects"]["proj-1"]["last_check_epoch"] = time.time() - 300
        (self.runtime_root / "watchdog.json").write_text(json.dumps(watchdog_data), encoding="utf-8")

        view3 = self.projection.project_view(self.snapshot, self.project_config, self.principal)
        self.assertEqual(view3["progress_observation_state"], "stale")

    def test_read_only_composition_mutates_no_runtime_state(self):
        # Record file states in runtime_root before projection
        before = {}
        for p in self.runtime_root.rglob("*"):
            if p.is_file():
                before[p] = (p.stat().st_mtime_ns, p.stat().st_size)

        _ = self.projection.project_view(self.snapshot, self.project_config, self.principal)
        _ = self.projection.overview({"projects": [self.snapshot]}, self.project_config, self.principal)

        after = {}
        for p in self.runtime_root.rglob("*"):
            if p.is_file():
                after[p] = (p.stat().st_mtime_ns, p.stat().st_size)

        # No files created, deleted, or modified
        self.assertEqual(set(before.keys()), set(after.keys()))
        for p in before:
            self.assertEqual(before[p], after[p], f"File modified by projection: {p}")

    def test_approve_owner_gate_eligibility_and_revocation_behavior(self):
        gate_id = "ai_plan:gate-proj1"
        truth = read_repository_truth(self.repo)
        (self.runtime_root / "ai-planner.json").write_text(json.dumps({
            "version": 1,
            "plans": {gate_id: {
                "plan_id": gate_id,
                "project_id": "proj-1",
                "task_id": "T1",
                "repo_path": str(self.repo),
                "branch": truth.branch,
                "head": truth.head,
                "status_hash": truth.status_hash,
                "state": "owner_gate",
                "plan": {"summary": "plan"},
            }}
        }), encoding="utf-8")

        # With active device: approve_owner_gate is available
        view = self.projection.project_view(self.snapshot, self.project_config, self.principal)
        approve_action = next(c for c in view["controls"] if c["action"] == "approve_owner_gate")
        self.assertTrue(approve_action["available"])

        # Revoke device: approve_owner_gate becomes unavailable
        self.security.revoke_mobile_device(self.principal.device_id)
        view_revoked = self.projection.project_view(self.snapshot, self.project_config, self.principal)
        approve_revoked = next(c for c in view_revoked["controls"] if c["action"] == "approve_owner_gate")
        self.assertFalse(approve_revoked["available"])
        self.assertIn("unauthorized", approve_revoked["reason"].lower())

    def test_real_watchdog_last_checked_at_iso_and_diagnosis(self):
        now_iso = utc_now_iso()
        watchdog_data = {
            "schema_version": 1,
            "projects": {
                "proj-1": {
                    "last_diagnosis": "agent_stalled",
                    "reason": "heartbeat interval exceeded",
                    "recovery_epoch": {"epoch_id": "epoch-101", "timestamp": now_iso},
                    "last_checked_at": now_iso,
                }
            }
        }
        (self.runtime_root / "watchdog.json").write_text(json.dumps(watchdog_data), encoding="utf-8")

        view = self.projection.project_view(self.snapshot, self.project_config, self.principal)
        self.assertEqual(view["progress_observation_state"], "authoritative")
        self.assertEqual(view["watchdog_state"], "agent_stalled")
        self.assertIn("heartbeat interval exceeded", view["watchdog_reason"])
        self.assertEqual(view["recovery_epoch"]["epoch_id"], "epoch-101")

        # Stale when last_checked_at is older than 120s
        watchdog_data["projects"]["proj-1"]["last_checked_at"] = "2026-01-01T00:00:00+00:00"
        (self.runtime_root / "watchdog.json").write_text(json.dumps(watchdog_data), encoding="utf-8")
        view_stale = self.projection.project_view(self.snapshot, self.project_config, self.principal)
        self.assertEqual(view_stale["progress_observation_state"], "stale")

    def test_owner_gate_presence_from_watchdog_and_surface(self):
        watchdog_data = {
            "schema_version": 1,
            "projects": {
                "proj-1": {
                    "last_diagnosis": "agent_stalled",
                    "owner_gate": {
                        "project_id": "proj-1",
                        "gate_source": "watchdog",
                        "reason": "max attempts exceeded",
                    },
                }
            }
        }
        (self.runtime_root / "watchdog.json").write_text(json.dumps(watchdog_data), encoding="utf-8")
        gate = _latest_owner_gate(self.runtime_root, "proj-1")
        self.assertIsNotNone(gate)
        self.assertEqual(gate.get("state"), "owner_gate")
        self.assertEqual(gate.get("gate_source"), "watchdog")

        ctrl_view = project_control_view(self.snapshot, self.runtime_root, self.project_config)
        self.assertIn("gate", ctrl_view)
        self.assertEqual(ctrl_view["gate"].get("gate_source"), "watchdog")

    def test_mobile_eligibility_fails_closed_when_bridge_claim_unavailable(self):
        gate_id = "ai_plan:gate-proj1"
        truth = read_repository_truth(self.repo)
        (self.runtime_root / "ai-planner.json").write_text(json.dumps({
            "version": 1,
            "plans": {gate_id: {
                "plan_id": gate_id,
                "project_id": "proj-1",
                "task_id": "T1",
                "repo_path": str(self.repo),
                "branch": truth.branch,
                "head": truth.head,
                "status_hash": truth.status_hash,
                "state": "owner_gate",
                "plan": {"summary": "plan"},
            }}
        }), encoding="utf-8")

        snapshot_bound = dict(self.snapshot)
        snapshot_bound["conversation_binding"] = {
            "transport": "web_bridge",
            "adapter": "chatgpt_web",
            "binding_id": "conv-42",
        }

        # Case A: bridge_store is None -> fails closed with "bridge claim state is unavailable"
        avail, reason, g_id = mobile_owner_gate_eligibility(
            self.project_config,
            snapshot_bound,
            self.runtime_root,
            self.principal.device_id,
            self.security,
            conversation_store=None,
            bridge_store=None,
        )
        self.assertFalse(avail)
        self.assertIn("bridge claim state is unavailable", reason)
        self.assertEqual(g_id, gate_id)

        # Case B: bridge_store lacks callable has_active_claim -> fails closed
        class DummyBridgeStoreNoClaim:
            pass

        avail2, reason2, g_id2 = mobile_owner_gate_eligibility(
            self.project_config,
            snapshot_bound,
            self.runtime_root,
            self.principal.device_id,
            self.security,
            conversation_store=None,
            bridge_store=DummyBridgeStoreNoClaim(),
        )
        self.assertFalse(avail2)
        self.assertIn("bridge claim state is unavailable", reason2)

        # Case C: bridge_store with has_active_claim=True -> fails with active claimed request blocks
        class DummyBridgeStoreClaimed:
            def has_active_claim(self, adapter, binding_id):
                return True

        avail3, reason3, _ = mobile_owner_gate_eligibility(
            self.project_config,
            snapshot_bound,
            self.runtime_root,
            self.principal.device_id,
            self.security,
            conversation_store=None,
            bridge_store=DummyBridgeStoreClaimed(),
        )
        self.assertFalse(avail3)
        self.assertIn("active claimed request blocks", reason3)

        # Case D: bridge_store with has_active_claim=False -> eligible
        class DummyBridgeStoreUnclaimed:
            def has_active_claim(self, adapter, binding_id):
                return False

        avail4, reason4, _ = mobile_owner_gate_eligibility(
            self.project_config,
            snapshot_bound,
            self.runtime_root,
            self.principal.device_id,
            self.security,
            conversation_store=None,
            bridge_store=DummyBridgeStoreUnclaimed(),
        )
        self.assertTrue(avail4)
        self.assertIn("exact pending planner gate can be approved", reason4)


if __name__ == "__main__":
    unittest.main()
