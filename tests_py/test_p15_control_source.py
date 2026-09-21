import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.control.command_store import request_hash
from dev_orchestrator.web.server import make_server
import urllib.request
import urllib.error


def make_command_payload(command_id: str, action: str = "pause"):
    return {
        "schema_version": 1,
        "command_id": command_id,
        "project_id": "test_proj",
        "action": action,
        "expected": {
            "revision": 1,
            "project_id": "test_proj",
            "repo_path": "C:/work/test",
            "branch": "main",
            "head": "abc",
            "dirty": False,
            "status_hash": "sha256:111",
            "task_id": "task-1",
            "lifecycle_state": "idle",
            "gate_id": None,
            "paused": False,
            "binding_state": "none",
            "binding_id": None,
            "binding_adapter": None,
        },
        "target": {},
    }


class TestP15ControlSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.runtime_root = Path(self.tmp.name)
        self.web_root = self.runtime_root / "web"
        self.web_root.mkdir(parents=True, exist_ok=True)
        (self.web_root / "index.html").write_text("ok", encoding="utf-8")

        # Create control security and pair a device
        self.security = ControlSecurity(self.runtime_root)
        pairing = self.security.create_mobile_pairing(expires_in_seconds=600)
        redeemed = self.security.redeem_mobile_pairing(pairing["pairing_id"], pairing["code"], "Pixel 8")
        self.device_id = redeemed["device_id"]
        self.master_token = self.security.token()

        # Start server with control_enabled=True on loopback port 0
        self.server = make_server(
            "127.0.0.1", 0, self.runtime_root, self.web_root,
            enable_control=True
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def _post(self, path: str, payload: dict, headers: dict = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        all_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.master_token}",
        }
        if headers:
            all_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=all_headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                body = json.loads(resp.read().decode("utf-8"))
                return status, body
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            return exc.code, body

    def test_absent_source_header_defaults_to_control_api(self):
        cmd = make_command_payload("cmd-src-01")
        status, body = self._post("/api/v1/control/commands", cmd)
        self.assertEqual(status, 202)
        stored = self.server.command_store.get("cmd-src-01")
        self.assertEqual(stored["source"], "control_api")

    def test_valid_mobile_source_header_accepted(self):
        cmd = make_command_payload("cmd-src-02")
        headers = {"X-DevO-Control-Source": f"mobile_gateway:{self.device_id}"}
        status, body = self._post("/api/v1/control/commands", cmd, headers=headers)
        self.assertEqual(status, 202)
        stored = self.server.command_store.get("cmd-src-02")
        self.assertEqual(stored["source"], f"mobile_gateway:{self.device_id}")

    def test_malformed_source_header_rejected(self):
        cmd = make_command_payload("cmd-src-03")
        # Empty device ID
        headers = {"X-DevO-Control-Source": "mobile_gateway:"}
        status, body = self._post("/api/v1/control/commands", cmd, headers=headers)
        self.assertEqual(status, 400)

        # Illegal characters in device ID
        headers = {"X-DevO-Control-Source": "mobile_gateway:dev;drop table"}
        status, body = self._post("/api/v1/control/commands", cmd, headers=headers)
        self.assertEqual(status, 400)

        # Unsupported prefix
        headers = {"X-DevO-Control-Source": "custom_agent:123"}
        status, body = self._post("/api/v1/control/commands", cmd, headers=headers)
        self.assertEqual(status, 400)

    def test_unknown_or_revoked_device_id_rejected(self):
        cmd = make_command_payload("cmd-src-04")
        headers = {"X-DevO-Control-Source": "mobile_gateway:dev-unknown-999"}
        status, body = self._post("/api/v1/control/commands", cmd, headers=headers)
        self.assertEqual(status, 403)
        self.assertIn("mobile device unauthorized", body.get("message", ""))

        # Revoke existing device and verify refusal
        self.security.revoke_mobile_device(self.device_id)
        cmd2 = make_command_payload("cmd-src-05")
        headers = {"X-DevO-Control-Source": f"mobile_gateway:{self.device_id}"}
        status, body = self._post("/api/v1/control/commands", cmd2, headers=headers)
        self.assertEqual(status, 403)

    def test_no_bearer_tokens_in_stored_records_or_hashes(self):
        cmd = make_command_payload("cmd-src-06")
        headers = {"X-DevO-Control-Source": f"mobile_gateway:{self.device_id}"}
        status, _ = self._post("/api/v1/control/commands", cmd, headers=headers)
        self.assertEqual(status, 202)
        stored = self.server.command_store.get("cmd-src-06")

        # Serialized record must not contain tokens
        stored_str = json.dumps(stored)
        self.assertNotIn("token", stored_str.lower().replace("csrf_token", ""))
        self.assertNotIn(self.master_token, stored_str)

        # Body hash is body-only and identical regardless of source
        expected_hash = request_hash(cmd)
        self.assertEqual(stored["request_hash"], expected_hash)


if __name__ == "__main__":
    unittest.main()
