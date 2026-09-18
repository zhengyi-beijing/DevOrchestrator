import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from dev_orchestrator.control.adapter import ControlAdapterClient
from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.control.web_bridge import (
    DEFAULT_FRESHNESS_WINDOW_SECONDS,
    WEB_BRIDGE_SCHEMA_VERSION,
    WebBridgeConflictError,
    WebBridgeCorruptionError,
    WebBridgeFreshnessError,
    WebBridgeRequestStore,
    canonical_web_bridge_request,
    web_bridge_request_hash,
)
from dev_orchestrator.web.server import make_server
from tests_py.test_control_commands import write_config

ROOT = Path(__file__).resolve().parents[1]


def make_valid_wb_request(
    adapter_request_id: str = "req-1",
    project_id: str = "p1",
    binding_id: str = "conv-1",
    operation: str = "status",
    issued_at: str | None = None,
    payload: dict | None = None,
    expected_revision: str | None = None,
) -> dict:
    if issued_at is None:
        issued_at = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "adapter_request_id": adapter_request_id,
        "operation": operation,
        "issued_at": issued_at,
        "binding": {
            "adapter": "chatgpt_web",
            "binding_id": binding_id,
        },
        "project_id": project_id,
        "expected_revision": expected_revision,
        "payload": payload or {},
    }


class WebBridgeRequestStoreUnitTests(unittest.TestCase):
    def test_canonical_validation_and_hashing(self):
        req = make_valid_wb_request()
        canonical = canonical_web_bridge_request(req)
        self.assertEqual(canonical["adapter_request_id"], "req-1")
        h1 = web_bridge_request_hash(canonical)
        h2 = web_bridge_request_hash(canonical)
        self.assertEqual(h1, h2)
        self.assertTrue(h1.startswith("sha256:"))

        # Unknown field rejected
        with self.assertRaisesRegex(ValueError, "unknown WebBridge request fields"):
            invalid = dict(req)
            invalid["arbitrary_field"] = "bad"
            canonical_web_bridge_request(invalid)

    def test_freshness_window(self):
        with tempfile.TemporaryDirectory() as td:
            store = WebBridgeRequestStore(td)
            client = MagicMock(spec=ControlAdapterClient)

            # Expired timestamp (10 minutes old)
            expired_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            expired_req = make_valid_wb_request(issued_at=expired_time)
            with self.assertRaises(WebBridgeFreshnessError):
                store.handle_request(expired_req, client)

            # Future timestamp (1 minute ahead)
            future_time = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
            future_req = make_valid_wb_request(issued_at=future_time)
            with self.assertRaises(WebBridgeFreshnessError):
                store.handle_request(future_req, client)

    def test_idempotent_replay_and_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            store = WebBridgeRequestStore(td)
            client = MagicMock(spec=ControlAdapterClient)
            client.status.return_value = {"project_id": "p1", "revision": "rev1"}

            req = make_valid_wb_request(adapter_request_id="req-same", operation="status")

            # First execution
            res1 = store.handle_request(req, client)
            self.assertEqual(res1["revision"], "rev1")
            self.assertEqual(client.status.call_count, 1)

            # Exact replay: must return cached response without calling client.status again
            res2 = store.handle_request(req, client)
            self.assertEqual(res2["revision"], "rev1")
            self.assertEqual(client.status.call_count, 1)

            # Replay conflict: same request_id with altered operation
            conflict_req = dict(req)
            conflict_req["operation"] = "logs"
            with self.assertRaises(WebBridgeConflictError):
                store.handle_request(conflict_req, client)

    def test_corruption_quarantine_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            store = WebBridgeRequestStore(td)
            client = MagicMock(spec=ControlAdapterClient)

            # Write corrupt JSON to the requests directory
            corrupt_file = store.requests_dir / "req-corrupt.json"
            corrupt_file.write_text("{broken json", encoding="utf-8")

            req = make_valid_wb_request(adapter_request_id="req-corrupt")
            with self.assertRaises(WebBridgeCorruptionError):
                store.handle_request(req, client)

            # Check that file was quarantined
            quarantined = list(store.quarantine_dir.glob("wb_req-corrupt.json.corrupt-*"))
            self.assertTrue(len(quarantined) >= 1)

            # Check health.json marks degraded
            health_file = store.health_path
            self.assertTrue(health_file.is_file())
            health = json.loads(health_file.read_text(encoding="utf-8"))
            self.assertTrue(health.get("degraded"))


class WebBridgeHTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        (self.runtime / "projects").mkdir()
        self.config = self.runtime / "projects.json"
        write_config(self.config, ROOT)

        project = {
            "project_id": "p1", "id": "p1", "name": "P1", "repo_path": str(ROOT),
            "state": "READY_TO_RUN", "lifecycle_state": "READY_TO_RUN",
            "git": {"branch": "main", "head": "head1", "dirty": False},
            "telemetry": {"task_id": "T1"},
        }
        (self.runtime / "projects" / "p1.json").write_text(json.dumps(project), encoding="utf-8")
        (self.runtime / "summary.json").write_text(
            json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(), "projects": [project]}),
            encoding="utf-8",
        )

        self.server = make_server(
            "127.0.0.1", 0, self.runtime, ROOT / "web",
            enable_control=True, config_path=self.config,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

        self.security = ControlSecurity(self.runtime)
        self.master_token = self.security.token()

        # Bind session in conversation store
        self.conv_store = ConversationControlStore(self.runtime)
        self.conv_store.heartbeat(
            "chatgpt_web",
            "conv-1",
            title="P1 Conversation",
            url="https://chatgpt.com/c/conv-1",
            tab_instance_id="tab-1",
        )
        self.conv_store.bind("p1", "chatgpt_web", "conv-1")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def post(self, path, body, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        data = json.dumps(body).encode("utf-8")
        conn.request("POST", path, body=data, headers=h)
        resp = conn.getresponse()
        resp_data = resp.read()
        conn.close()
        return resp.status, resp.headers, resp_data

    def test_capability_creation_and_bridge_dispatch(self):
        # 1. Issue capability token via master token
        cap_req = {
            "project_id": "p1",
            "binding_id": "conv-1",
            "adapter": "chatgpt_web",
            "expires_in_seconds": 3600,
        }
        status, _, body = self.post(
            "/api/v1/control/web-bridge-capabilities",
            cap_req,
            headers={"Authorization": f"Bearer {self.master_token}"},
        )
        self.assertEqual(status, 201)
        cap_resp = json.loads(body.decode("utf-8"))
        cap_data = cap_resp["data"]
        token = cap_data["token"]
        cap_id = cap_data["capability_id"]
        self.assertTrue(token)

        # 2. Dispatch status operation with capability token
        req = make_valid_wb_request(
            adapter_request_id="req-test-1",
            project_id="p1",
            binding_id="conv-1",
            operation="status",
        )
        status, _, body = self.post(
            "/api/v1/control/web-bridge/requests",
            req,
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://chatgpt.com",
            },
        )
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertEqual(resp["data"]["project_id"], "p1")

        # 3. Idempotent replay: send exact same request again
        status2, _, body2 = self.post(
            "/api/v1/control/web-bridge/requests",
            req,
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://chatgpt.com",
            },
        )
        self.assertEqual(status2, 200)
        resp2 = json.loads(body2.decode("utf-8"))
        self.assertEqual(resp2["data"]["project_id"], "p1")

        # 4. Conflict detection: send same adapter_request_id with altered operation
        conflict_req = dict(req)
        conflict_req["operation"] = "logs"
        status_conf, _, _ = self.post(
            "/api/v1/control/web-bridge/requests",
            conflict_req,
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://chatgpt.com",
            },
        )
        self.assertEqual(status_conf, 409)

        # 5. Revoke capability
        status_rev, _, _ = self.post(
            f"/api/v1/control/web-bridge-capabilities/{cap_id}/revoke",
            {},
            headers={"Authorization": f"Bearer {self.master_token}"},
        )
        self.assertEqual(status_rev, 200)

        # 6. Verify subsequent request with revoked token is rejected
        new_req = make_valid_wb_request(
            adapter_request_id="req-test-2",
            project_id="p1",
            binding_id="conv-1",
            operation="status",
        )
        status_blocked, _, _ = self.post(
            "/api/v1/control/web-bridge/requests",
            new_req,
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": "https://chatgpt.com",
            },
        )
        self.assertEqual(status_blocked, 401)

    def test_unauthorized_and_mismatched_capabilities_rejected(self):
        # Create capability for p1
        cap = self.security.create_web_bridge_capability("p1", "conv-1", adapter="chatgpt_web")
        token = cap["token"]

        # 1. Attempt to access different project p2 with p1 capability
        req_p2 = make_valid_wb_request(
            adapter_request_id="req-p2",
            project_id="p2",
            binding_id="conv-1",
        )
        status, _, _ = self.post(
            "/api/v1/control/web-bridge/requests",
            req_p2,
            headers={"Authorization": f"Bearer {token}", "Origin": "https://chatgpt.com"},
        )
        self.assertEqual(status, 403)

        # 2. Attempt to access different binding_id
        req_b2 = make_valid_wb_request(
            adapter_request_id="req-b2",
            project_id="p1",
            binding_id="conv-other",
        )
        status, _, _ = self.post(
            "/api/v1/control/web-bridge/requests",
            req_b2,
            headers={"Authorization": f"Bearer {token}", "Origin": "https://chatgpt.com"},
        )
        self.assertEqual(status, 403)

        # 3. Disallowed origin (cross-site attempt from untrusted origin)
        req_origin = make_valid_wb_request(adapter_request_id="req-origin")
        status, _, _ = self.post(
            "/api/v1/control/web-bridge/requests",
            req_origin,
            headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.com"},
        )
        self.assertEqual(status, 403)
