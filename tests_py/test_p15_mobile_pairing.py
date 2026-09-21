import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dev_orchestrator.control.adapter import ControlAdapterClient
from dev_orchestrator.control.logs import redact_secrets
from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.mobile.gateway import make_mobile_gateway
from dev_orchestrator.mobile.projection import MobileProjectionService
from dev_orchestrator.web.server import make_server


class TestP15MobilePairing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.web_root = self.base / "web"
        self.web_root.mkdir(parents=True, exist_ok=True)
        (self.web_root / "index.html").write_text("ok", encoding="utf-8")

        self.web_server = make_server(
            "127.0.0.1", 0, self.runtime, self.web_root,
            enable_control=True
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
        self.projection_service = MobileProjectionService(
            runtime_root=self.runtime,
            config_provider={"projects": []},
            mobile_device_authorizer=self.security,
        )
        self.gateway = make_mobile_gateway(
            "127.0.0.1", 0, self.runtime,
            authorizer=self.security,
            projection_service=self.projection_service,
            control_client=self.control_client,
            verify_bind=False,
        )
        self.gateway_port = self.gateway.server_address[1]
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()

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

    def _pair_request(self, payload: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.gateway_port}/api/v1/mobile/v1/pair"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            return exc.code, body

    def test_successful_single_use_redemption(self):
        pairing = self.security.create_mobile_pairing(expires_in_seconds=300)
        req = {
            "pairing_id": pairing["pairing_id"],
            "code": pairing["code"],
            "device_label": "Android 14 Pixel 8",
        }
        status, body = self._pair_request(req)
        self.assertEqual(status, 200)
        self.assertTrue(body["device_id"].startswith("dev-"))
        self.assertTrue(len(body["token"]) > 20)
        self.assertEqual(body["scope"], "mobile_device")

        # Second redemption attempt (replay) must fail
        status2, body2 = self._pair_request(req)
        self.assertEqual(status2, 400)
        self.assertIn("pairing is missing, expired, or already used", body2.get("message", ""))

    def test_indistinguishable_failures_for_unknown_expired_wrong_code(self):
        # 1. Unknown pairing_id
        status_unk, body_unk = self._pair_request({
            "pairing_id": "pair-unknown-123",
            "code": "nonexistentcode",
        })
        self.assertEqual(status_unk, 400)
        msg_unk = body_unk.get("message", "")

        # 2. Expired pairing
        expired_pairing = self.security.create_mobile_pairing(expires_in_seconds=1)
        time.sleep(1.2)
        status_exp, body_exp = self._pair_request({
            "pairing_id": expired_pairing["pairing_id"],
            "code": expired_pairing["code"],
        })
        self.assertEqual(status_exp, 400)
        msg_exp = body_exp.get("message", "")

        # 3. Wrong code
        valid_pairing = self.security.create_mobile_pairing(expires_in_seconds=300)
        status_wrong, body_wrong = self._pair_request({
            "pairing_id": valid_pairing["pairing_id"],
            "code": "wrongcode9999",
        })
        self.assertEqual(status_wrong, 400)
        msg_wrong = body_wrong.get("message", "")

        # Error messages must be completely indistinguishable
        self.assertEqual(msg_unk, msg_exp)
        self.assertEqual(msg_unk, msg_wrong)
        self.assertEqual(msg_unk, "pairing is missing, expired, or already used")

    def test_attempt_lockout(self):
        pairing = self.security.create_mobile_pairing(expires_in_seconds=300)
        p_id = pairing["pairing_id"]
        correct_code = pairing["code"]

        # Submit 3 wrong attempts
        for _ in range(3):
            status, _ = self._pair_request({"pairing_id": p_id, "code": "badcode"})
            self.assertEqual(status, 400)

        # 4th attempt with the correct code must now fail due to lockout
        status, body = self._pair_request({"pairing_id": p_id, "code": correct_code})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("message"), "pairing is missing, expired, or already used")

    def test_pairing_code_invalid_on_authenticated_routes(self):
        pairing = self.security.create_mobile_pairing(expires_in_seconds=300)
        # Attempt to access authenticated route /projects using pairing code or pairing_id as bearer token
        url = f"http://127.0.0.1:{self.gateway_port}/api/v1/mobile/v1/projects"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {pairing['code']}"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

    def test_log_sanitization_redacts_pairing_codes_and_tokens(self):
        sensitive_entry = {
            "event": "mobile_pairing_created",
            "pairing_id": "pair-123",
            "code": "secret_pairing_code_123",
            "token": "secret_token_abc",
            "device_token": "secret_device_token_xyz",
            "mobile_token": "secret_mobile_token_456",
            "safe_field": "visible_value",
        }
        sanitized = redact_secrets(sensitive_entry)
        self.assertEqual(sanitized["code"], "[REDACTED]")
        self.assertEqual(sanitized["token"], "[REDACTED]")
        self.assertEqual(sanitized["device_token"], "[REDACTED]")
        self.assertEqual(sanitized["mobile_token"], "[REDACTED]")
        self.assertEqual(sanitized["safe_field"], "visible_value")


if __name__ == "__main__":
    unittest.main()
