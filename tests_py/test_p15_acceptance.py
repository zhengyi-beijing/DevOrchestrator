import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.control.adapter import ControlAdapterClient
from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.mobile.alerts import DEFAULT_ALERT_POLICY, evaluate_notification
from dev_orchestrator.mobile.client import MobileContractClient, MobileContractClientError
from dev_orchestrator.mobile.gateway import make_mobile_gateway
from dev_orchestrator.mobile.projection import MobileProjectionService
from dev_orchestrator.storage.json_store import write_json
from dev_orchestrator.web.server import make_server
from tests_py.test_control_commands import FakeExecutor


class TestP15Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.web_root = self.base / "web"
        self.web_root.mkdir(parents=True, exist_ok=True)
        (self.web_root / "index.html").write_text("ok", encoding="utf-8")

        # Initialize git repo
        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "commit.gpgsign", "false"], check=True)
        (self.repo / "test.txt").write_text("initial", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)

        truth = read_repository_truth(self.repo)
        self.project_id = "proj-acceptance"
        self.config_path = self.base / "projects.json"
        self.project_config = {
            "project_id": self.project_id,
            "repo_path": str(self.repo),
            "execution": {
                "enabled": True,
                "owner_authorized": True,
                "allowed_next_actions": ["next_task"],
            },
            "ai_roles": {"planner": {"enabled": True}},
        }
        write_json(self.config_path, {"projects": [self.project_config]})

        # Seed snapshot
        self.snapshot = {
            "project_id": self.project_id,
            "repo_path": str(self.repo),
            "state": "IDLE",
            "lifecycle_state": "IDLE",
            "git": {
                "branch": truth.branch,
                "head": truth.head,
                "dirty": False,
                "status_hash": truth.status_hash,
            },
            "telemetry": {"task_id": "P15-Task"},
        }
        (self.runtime / "projects").mkdir(parents=True, exist_ok=True)
        write_json(self.runtime / "projects" / f"{self.project_id}.json", self.snapshot)
        write_json(self.runtime / "summary.json", {"projects": [self.snapshot]})

        # Master server
        self.web_server = make_server(
            "127.0.0.1", 0, self.runtime, self.web_root,
            enable_control=True, config_path=self.config_path
        )
        self.web_port = self.web_server.server_address[1]
        self.web_thread = threading.Thread(target=self.web_server.serve_forever, daemon=True)
        self.web_thread.start()

        self.security = self.web_server.control_security
        self.control_client = ControlAdapterClient(
            base_url=f"http://127.0.0.1:{self.web_port}",
            token=self.security.token(),
            runtime_root=self.runtime,
        )
        self.conversations = ConversationControlStore(self.runtime)
        self.planner = AIPlannerCoordinator(self.runtime, None)
        self.coordinator = ControlCommandCoordinator(
            self.runtime, self.planner,
            conversation_store=self.conversations,
            mobile_device_authorizer=self.security,
        )
        self.projection_service = MobileProjectionService(
            runtime_root=self.runtime,
            config_provider={"projects": [self.project_config]},
            mobile_device_authorizer=self.security,
            conversation_store=self.conversations,
        )

        # Mobile gateway
        self.gateway = make_mobile_gateway(
            "127.0.0.1", 0, self.runtime,
            authorizer=self.security,
            projection_service=self.projection_service,
            control_client=self.control_client,
            config_path=self.config_path,
            verify_bind=False,
        )
        self.gateway_port = self.gateway.server_address[1]
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()

        # Headless Python mobile client
        self.client = MobileContractClient(f"http://127.0.0.1:{self.gateway_port}")

    def tearDown(self):
        self.gateway.close_all_streams()
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(timeout=2)
        self.web_server.shutdown()
        self.web_server.server_close()
        self.web_thread.join(timeout=2)
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_end_to_end_mobile_lifecycle_and_guarded_control(self):
        # 1. Unauthenticated Pair
        pairing = self.security.create_mobile_pairing(expires_in_seconds=300)
        pair_res = self.client.pair(pairing["pairing_id"], pairing["code"], "Pixel Acceptance")
        self.assertTrue(pair_res["device_id"].startswith("dev-"))
        self.assertEqual(self.client.device_id, pair_res["device_id"])
        self.assertIsNotNone(self.client.token)

        # 2. Observe Running & Watchdog Authoritative Stalled States
        # A) Running
        watchdog_data = {
            "schema_version": 1,
            "projects": {
                self.project_id: {
                    "classification": "running",
                    "reason": "active turn in progress",
                    "recovery_epoch": {"epoch_id": "ep-1", "timestamp": "2026-09-21T00:00:00+00:00"},
                    "last_check_epoch": time.time(),
                }
            }
        }
        write_json(self.runtime / "watchdog.json", watchdog_data)
        proj_view = self.client.project(self.project_id)
        self.assertEqual(proj_view["progress_observation_state"], "authoritative")
        self.assertEqual(proj_view["watchdog_state"], "running")
        self.assertEqual(proj_view["recovery_epoch"]["epoch_id"], "ep-1")

        # B) Stalled
        watchdog_data["projects"][self.project_id]["classification"] = "agent_stalled"
        watchdog_data["projects"][self.project_id]["reason"] = "no progress heartbeat in 600s"
        write_json(self.runtime / "watchdog.json", watchdog_data)
        proj_view2 = self.client.project(self.project_id)
        self.assertEqual(proj_view2["watchdog_state"], "agent_stalled")

        # 3. Disjoint Alert Families Evaluation
        now = datetime.now(timezone.utc)
        # Transport alert
        transport_down = {"connected": False, "reason": "Tailscale tunnel closed"}
        dec_t = evaluate_notification(None, transport_down, DEFAULT_ALERT_POLICY, now, {})
        self.assertTrue(dec_t.should_notify)
        self.assertEqual(dec_t.notifications[0].family, "transport")
        # Progress alert
        transport_ok = {"connected": True, "degraded": False}
        dec_p = evaluate_notification(proj_view2, transport_ok, DEFAULT_ALERT_POLICY, now, {})
        self.assertTrue(dec_p.should_notify)
        self.assertEqual(dec_p.notifications[0].family, "progress")
        self.assertEqual(dec_p.notifications[0].alert_type, "stall")

        # 4. Approve an OWNER_GATE via mobile channel
        gate_id = "ai_plan:gate-acceptance"
        truth = read_repository_truth(self.repo)
        plan = {
            "task_id": "P15-Task",
            "summary": "Owner approval acceptance test plan.",
            "implementation_steps": ["Step 1"],
            "interfaces": ["Interface 1"],
            "validation": ["Validation 1"],
        }
        write_json(self.runtime / "ai-planner.json", {
            "version": 1,
            "plans": {
                gate_id: {
                    "plan_id": gate_id,
                    "command_id": "cmd-src",
                    "project_id": self.project_id,
                    "task_id": "P15-Task",
                    "repo_path": str(self.repo),
                    "branch": truth.branch,
                    "head": truth.head,
                    "status_hash": truth.status_hash,
                    "state": "owner_gate",
                    "plan": plan,
                    "started_at": "2026-09-21T00:00:00+00:00",
                    "completed_at": "2026-09-21T00:01:00+00:00",
                }
            }
        })

        proj_gate_view = self.client.project(self.project_id)
        approve_ctrl = next((c for c in proj_gate_view["controls"] if c["action"] == "approve_owner_gate"), None)
        self.assertIsNotNone(approve_ctrl)
        self.assertTrue(approve_ctrl["available"])

        sub_res = self.client.submit_control(
            project_id=self.project_id,
            action="approve_owner_gate",
            expected_revision=proj_gate_view["revision"],
            device_request_id="dev-req-approve-1",
            target={"gate_id": gate_id},
        )
        self.assertTrue(sub_res.get("command_id"))
        cmd_id = sub_res["command_id"]

        # Run coordinator advance to process approval
        executor = FakeExecutor()
        outcomes = self.coordinator.advance(self.config_path, {"projects": [self.snapshot]}, executor)
        outcome = next(o for o in outcomes if o.get("command_id") == cmd_id)
        self.assertEqual(outcome["state"], "accepted")
        self.assertEqual(outcome["effect"], "approve_exact_owner_gate_no_worker_started")
        # Assert NO worker launched before explicit continue
        self.assertEqual(len(executor.calls), 0)

        # Assert plan state updated with mobile channel audit
        saved_plan = self.planner.state()["plans"][gate_id]
        self.assertEqual(saved_plan["state"], "owner_approved")
        self.assertEqual(saved_plan["approved_via"], "mobile_device")
        self.assertEqual(saved_plan["approving_device_id"], self.client.device_id)

        # 5. Execute another supported guarded control (pause)
        proj_view3 = self.client.project(self.project_id)
        pause_res = self.client.submit_control(
            project_id=self.project_id,
            action="pause",
            expected_revision=proj_view3["revision"],
            device_request_id="dev-req-pause-1",
        )
        self.assertTrue(pause_res.get("command_id"))
        pause_stored = self.web_server.command_store.get(pause_res["command_id"])
        self.assertEqual(pause_stored["source"], f"mobile_gateway:{self.client.device_id}")

        # 6. Mid-Stream Revocation
        # Owner revokes this mobile device
        self.security.revoke_mobile_device(self.client.device_id)

        # Client immediately receives 401 on next call
        with self.assertRaises(MobileContractClientError) as ctx:
            self.client.project(self.project_id)
        self.assertEqual(ctx.exception.status, 401)


if __name__ == "__main__":
    unittest.main()
