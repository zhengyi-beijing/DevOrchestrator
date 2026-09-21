import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.control.adapter import ControlAdapterClient
from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.mobile.gateway import make_mobile_gateway
from dev_orchestrator.mobile.projection import MobileProjectionService
from dev_orchestrator.storage.json_store import write_json
from dev_orchestrator.web.server import make_server


class TestP15MobileControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.web_root = self.base / "web"
        self.web_root.mkdir(parents=True, exist_ok=True)
        (self.web_root / "index.html").write_text("ok", encoding="utf-8")

        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "commit.gpgsign", "false"], check=True)
        (self.repo / "seed.txt").write_text("seed", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "seed"], check=True)

        truth = read_repository_truth(self.repo)
        self.config_path = self.base / "projects.json"
        self.project_config = {
            "project_id": "proj-controls",
            "repo_path": str(self.repo),
            "execution": {"enabled": True, "owner_authorized": True, "allowed_next_actions": ["next_task"]},
        }
        write_json(self.config_path, {"projects": [self.project_config]})

        # Seed snapshot in runtime
        (self.runtime / "projects").mkdir(parents=True, exist_ok=True)
        self.snapshot = {
            "project_id": "proj-controls",
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
        write_json(self.runtime / "projects" / "proj-controls.json", self.snapshot)
        write_json(self.runtime / "summary.json", {"projects": [self.snapshot]})

        # Start master web server on loopback
        self.web_server = make_server(
            "127.0.0.1", 0, self.runtime, self.web_root,
            enable_control=True, config_path=self.config_path
        )
        self.web_port = self.web_server.server_address[1]
        self.web_thread = threading.Thread(target=self.web_server.serve_forever, daemon=True)
        self.web_thread.start()

        self.security = self.web_server.control_security
        pairing = self.security.create_mobile_pairing()
        self.device = self.security.redeem_mobile_pairing(pairing["pairing_id"], pairing["code"], "Pixel Test")
        self.device_token = self.device["token"]
        self.device_id = self.device["device_id"]

        # Control adapter client connecting to loopback master server
        self.control_client = ControlAdapterClient(
            base_url=f"http://127.0.0.1:{self.web_port}",
            token=self.security.token(),
            runtime_root=self.runtime,
        )

        self.projection_service = MobileProjectionService(
            runtime_root=self.runtime,
            config_provider={"projects": [self.project_config]},
            mobile_device_authorizer=self.security,
        )

        # Start mobile gateway on loopback for testing
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

        self.principal = self.security.lookup_mobile_device(self.device_id)[2]
        view = self.projection_service.project_view(self.snapshot, self.project_config, self.principal)
        self.current_rev = view["revision"]

    def tearDown(self):
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

    def _post(self, path: str, payload: dict, token: str = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.gateway_port}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token or self.device_token}",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            return exc.code, body

    def test_authenticated_control_submission_and_namespacing(self):
        req = {
            "action": "pause",
            "expected_revision": self.current_rev,
            "device_request_id": "req-001",
        }
        status, body = self._post("/api/v1/mobile/v1/projects/proj-controls/controls", req)
        self.assertEqual(status, 202)
        cmd_id = body.get("command_id")
        self.assertTrue(cmd_id)

        # Stored command record must have exact source mobile_gateway:<device_id>
        stored = self.web_server.command_store.get(cmd_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["source"], f"mobile_gateway:{self.device_id}")
        self.assertEqual(stored["action"], "pause")
        # Identity is server derived: contains branch, head, etc.
        self.assertIn("branch", stored["expected"])
        self.assertIn("head", stored["expected"])

    def test_stale_revision_fails_closed_with_409(self):
        req = {
            "action": "pause",
            "expected_revision": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
            "device_request_id": "req-002",
        }
        status, body = self._post("/api/v1/mobile/v1/projects/proj-controls/controls", req)
        self.assertEqual(status, 409)

    def test_same_device_replay_is_idempotent(self):
        req = {
            "action": "pause",
            "expected_revision": self.current_rev,
            "device_request_id": "req-replay",
        }
        status1, body1 = self._post("/api/v1/mobile/v1/projects/proj-controls/controls", req)
        self.assertEqual(status1, 202)

        # Replay with same body
        status2, body2 = self._post("/api/v1/mobile/v1/projects/proj-controls/controls", req)
        self.assertEqual(status2, 202)
        self.assertEqual(body1["command_id"], body2["command_id"])

    def test_same_device_changed_body_conflicts(self):
        req1 = {
            "action": "pause",
            "expected_revision": self.current_rev,
            "device_request_id": "req-conflict",
        }
        status1, _ = self._post("/api/v1/mobile/v1/projects/proj-controls/controls", req1)
        self.assertEqual(status1, 202)

        # Changed action with same device_request_id
        req2 = {
            "action": "resume",
            "expected_revision": self.current_rev,
            "device_request_id": "req-conflict",
        }
        status2, body2 = self._post("/api/v1/mobile/v1/projects/proj-controls/controls", req2)
        self.assertEqual(status2, 409)

    def test_unsupported_controls_never_reach_store(self):
        inbox = self.web_server.command_store.inbox
        count_before = len(list(inbox.glob("*.json"))) if inbox.exists() else 0
        req = {
            "action": "bind_conversation",
            "expected_revision": self.current_rev,
            "device_request_id": "req-unsupported",
        }
        status, body = self._post("/api/v1/mobile/v1/projects/proj-controls/controls", req)
        self.assertEqual(status, 400)
        self.assertIn("unsupported mobile control action", body.get("message", ""))
        count_after = len(list(inbox.glob("*.json"))) if inbox.exists() else 0
        self.assertEqual(count_before, count_after)


if __name__ == "__main__":
    unittest.main()
