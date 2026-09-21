import json
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
from dev_orchestrator.mobile.gateway import make_mobile_gateway
from dev_orchestrator.mobile.projection import MobileProjectionService
from dev_orchestrator.storage.json_store import write_json
from dev_orchestrator.web.server import make_server


class TestP15MobileGateway(unittest.TestCase):
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
        pairing = self.security.create_mobile_pairing()
        self.device = self.security.redeem_mobile_pairing(pairing["pairing_id"], pairing["code"], "Pixel Test")
        self.device_token = self.device["token"]
        self.device_id = self.device["device_id"]

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

    def _request(self, method: str, path: str, data: bytes = None, headers: dict = None) -> tuple[int, dict, dict]:
        url = f"http://127.0.0.1:{self.gateway_port}{path}"
        req_headers = {}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                resp_headers = dict(resp.headers)
                raw = resp.read()
                body = json.loads(raw.decode("utf-8")) if raw else {}
                return resp.status, body, resp_headers
        except urllib.error.HTTPError as exc:
            resp_headers = dict(exc.headers)
            raw = exc.read()
            body = json.loads(raw.decode("utf-8")) if raw else {}
            return exc.code, body, resp_headers

    def test_unauthenticated_requests_return_401(self):
        # Missing Authorization header
        status, body, headers = self._request("GET", "/api/v1/mobile/v1/projects")
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

        # Invalid bearer token
        status2, _, _ = self._request(
            "GET", "/api/v1/mobile/v1/projects",
            headers={"Authorization": "Bearer invalid-token"}
        )
        self.assertEqual(status2, 401)

    def test_authenticated_read_only_projection_access(self):
        status, body, headers = self._request(
            "GET", "/api/v1/mobile/v1/projects",
            headers={"Authorization": f"Bearer {self.device_token}"}
        )
        self.assertEqual(status, 200)
        self.assertIn("projects", body)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_path_traversal_rejection(self):
        status, _, _ = self._request("GET", "/api/v1/mobile/v1/../secret")
        self.assertEqual(status, 400)

    def test_no_options_cors_surface(self):
        # OPTIONS request must fail closed (405 Method Not Allowed)
        status, _, _ = self._request("OPTIONS", "/api/v1/mobile/v1/projects")
        self.assertEqual(status, 405)

    def test_payload_body_limit(self):
        # Exceed 65536 bytes
        huge_body = b"x" * 70000
        status, body, _ = self._request(
            "POST", "/api/v1/mobile/v1/pair",
            data=huge_body,
            headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 413)

    def test_canonical_store_failure_returns_503(self):
        # Corrupt capability store
        cap_path = self.runtime / "control" / "adapter-capabilities.json"
        cap_path.write_text("{ unreadable json ...", encoding="utf-8")

        # Requests fail closed with 503 Service Unavailable
        status, body, _ = self._request(
            "GET", "/api/v1/mobile/v1/projects",
            headers={"Authorization": f"Bearer {self.device_token}"}
        )
        self.assertEqual(status, 503)
        self.assertIn("unavailable", body.get("error", "").lower() + body.get("message", "").lower())


if __name__ == "__main__":
    unittest.main()
