import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.control.server import make_control_server, validate_control_listen
from dev_orchestrator.control.service import ControlPlaneService
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole


def request(port: int, method: str, path: str, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, (json.loads(raw) if raw else None)


class ControlHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "projects.json"
        self.config.write_text(json.dumps({"projects": [
            {"project_id": "labdemo", "repo_path": str(self.root),
             "conversation_binding": {"transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "conv-old"}},
            {"project_id": "xray", "repo_path": str(self.root),
             "conversation_binding": {"transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "conv-xray"}},
        ]}), encoding="utf-8")
        self.control_store = ConversationControlStore(self.root / "runtime")
        self.bridge_store = BrowserBridgeStore(self.root / "runtime" / "bridge")
        self.service = ControlPlaneService(
            self.config, self.root / "runtime", self.control_store, self.bridge_store
        )
        self.server = make_control_server("127.0.0.1", 0, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.tmp.cleanup()

    def heartbeat(self, binding_id="conv-new", title="Current LabDemo"):
        return request(self.port, "POST", "/v1/session/heartbeat", {
            "adapter": "chatgpt_web", "binding_id": binding_id,
            "title": title, "url": "https://chatgpt.com/c/" + binding_id,
            "tab_instance_id": "tab-1",
        })
    def test_health_sessions_and_projects(self):
        status, health = request(self.port, "GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["surface"], "conversation-control-v1")
        status, hb = self.heartbeat()
        self.assertEqual(status, 200)
        self.assertEqual(hb["session"]["binding_id"], "conv-new")
        status, sessions = request(self.port, "GET", "/v1/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(sessions["sessions"][0]["state"], "live")
        status, projects = request(self.port, "GET", "/v1/projects")
        self.assertEqual(status, 200)
        by_id = {item["project_id"]: item for item in projects["projects"]}
        self.assertEqual(by_id["labdemo"]["conversation_binding_source"], "static")

    def test_rebind_then_unbind_suppresses_static_fallback(self):
        self.heartbeat()
        status, rebound = request(self.port, "POST", "/v1/rebind", {
            "project_id": "labdemo", "adapter": "chatgpt_web", "binding_id": "conv-new"
        })
        self.assertEqual(status, 200)
        self.assertEqual(rebound["binding"]["binding_id"], "conv-new")
        status, projects = request(self.port, "GET", "/v1/projects")
        by_id = {item["project_id"]: item for item in projects["projects"]}
        self.assertEqual(by_id["labdemo"]["conversation_binding_source"], "runtime")
        status, unbound = request(self.port, "POST", "/v1/unbind", {"project_id": "labdemo"})
        self.assertEqual(status, 200)
        self.assertEqual(unbound["state"], "unbound")
        status, projects = request(self.port, "GET", "/v1/projects")
        by_id = {item["project_id"]: item for item in projects["projects"]}
        self.assertFalse(by_id["labdemo"]["orchestration_ready"])
        self.assertEqual(by_id["labdemo"]["conversation_binding_source"], "runtime_unbound")

    def test_unknown_project_and_duplicate_effective_route_fail_closed(self):
        self.heartbeat("conv-new")
        status, body = request(self.port, "POST", "/v1/rebind", {
            "project_id": "missing", "adapter": "chatgpt_web", "binding_id": "conv-new"
        })
        self.assertEqual(status, 404)
        self.heartbeat("conv-xray", title="Xray")
        status, body = request(self.port, "POST", "/v1/rebind", {
            "project_id": "labdemo", "adapter": "chatgpt_web", "binding_id": "conv-xray"
        })
        self.assertEqual(status, 409)

    def test_active_claim_blocks_rebind_and_unbind(self):
        self.heartbeat("conv-new")
        req = WebSolRequest(
            project_id="labdemo", request_id="req-claimed", task_id="P6", stage_id=None,
            branch="main", head="a" * 40, role=WebSolRole.REVIEWER,
            event=WebSolEvent.REVIEW_REQUIRED, nonce="nonce-claimed",
        )
        self.bridge_store.submit("chatgpt_web", "conv-old", req, "prompt")
        self.assertIsNotNone(self.bridge_store.claim("chatgpt_web", "conv-old"))
        status, _ = request(self.port, "POST", "/v1/rebind", {
            "project_id": "labdemo", "adapter": "chatgpt_web", "binding_id": "conv-new"
        })
        self.assertEqual(status, 409)
        status, _ = request(self.port, "POST", "/v1/unbind", {"project_id": "labdemo"})
        self.assertEqual(status, 409)

    def test_owner_action_is_ccp6_surface_and_validates_payload(self):
        status, body = request(self.port, "POST", "/v1/owner-action", {
            "project_id": "labdemo", "action": "approve_next_stage"
        })
        self.assertEqual(status, 400)
        self.assertIn("requires", body["message"])

    def test_control_listener_is_loopback_only(self):
        with self.assertRaises(ValueError):
            validate_control_listen("0.0.0.0")

    def test_unknown_control_route_is_not_shell_surface(self):
        status, _ = request(self.port, "POST", "/v1/shell", {"command": "whoami"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
