import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dev_orchestrator.web.server import make_server

ROOT = Path(__file__).resolve().parents[1]


class ConversationDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        now = datetime.now(timezone.utc).isoformat()
        summary = {"observed_at": now, "project_count": 3, "projects": [
            {"id": "labdemo", "name": "LabDemo", "orchestration_ready": True,
             "conversation_binding_source": "static", "conversation_binding": {
                 "transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "conv-old"}},
            {"id": "xray", "name": "XRay", "orchestration_ready": True,
             "conversation_binding_source": "static", "conversation_binding": {
                 "transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "conv-x"}},
            {"id": "viewer", "name": "Viewer", "orchestration_ready": False,
             "conversation_binding_source": "none", "conversation_binding": None},
        ]}
        (self.runtime / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        sessions = {
            "chatgpt_web\nconv-runtime": {
                "adapter": "chatgpt_web", "binding_id": "conv-runtime",
                "title": "LabDemo integration", "url": "https://chatgpt.com/c/conv-runtime",
                "first_seen_at": now, "last_seen_at": now,
                "tabs": {"tab-1": {"last_seen_at": now}},
            }
        }
        bindings = {
            "labdemo": {"project_id": "labdemo", "state": "bound",
                        "adapter": "chatgpt_web", "binding_id": "conv-runtime",
                        "title": "LabDemo integration", "url": "https://chatgpt.com/c/conv-runtime"},
            "viewer": {"project_id": "viewer", "state": "unbound",
                       "provenance": "owner_control", "updated_at": now},
        }
        (self.runtime / "conversation-sessions.json").write_text(json.dumps(sessions), encoding="utf-8")
        (self.runtime / "conversation-bindings.json").write_text(json.dumps(bindings), encoding="utf-8")
        decisions = {"version": 1, "decisions": {"gate-lab": {
            "project_id": "labdemo", "request_id": "gate-lab",
            "disposition": "owner_gate", "decision": "owner_gate", "next_action": "stop",
            "task_id": "P6", "branch": "integration", "head": "a" * 40,
            "consumed_at": now,
        }}}
        owner = {"version": 1, "projects": {"labdemo": {
            "project_id": "labdemo", "paused": True, "suppress_static_starts": True,
        }}, "actions": {"approve-lab": {
            "action_id": "approve-lab", "project_id": "labdemo",
            "action": "approve_next_stage", "state": "accepted", "gate_request_id": "gate-lab",
            "updated_at": now,
        }}}
        (self.runtime / "websol-decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
        (self.runtime / "owner-control.json").write_text(json.dumps(owner), encoding="utf-8")
        self.server = make_server("127.0.0.1", 0, self.runtime, ROOT / "web")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.tmp.cleanup()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        conn.request("GET", path)
        response = conn.getresponse(); body = response.read(); conn.close()
        return response.status, json.loads(body)

    def test_api_projects_runtime_binding_and_live_state(self):
        status, payload = self.get("/api/conversations")
        self.assertEqual(status, 200)
        by_id = {item["project_id"]: item for item in payload["projects"]}
        lab = by_id["labdemo"]
        self.assertEqual(lab["binding_state"], "BOUND")
        self.assertEqual(lab["binding_source"], "runtime")
        self.assertEqual(lab["binding_id"], "conv-runtime")
        self.assertEqual(lab["conversation_title"], "LabDemo integration")
        self.assertEqual(lab["conversation_url"], "https://chatgpt.com/c/conv-runtime")
        self.assertEqual(lab["active_tab_count"], 1)
        self.assertIsNotNone(lab["last_seen_at"])
        self.assertTrue(lab["owner_paused"])
        self.assertEqual(lab["owner_gate"]["request_id"], "gate-lab")
        self.assertEqual(lab["owner_gate"]["owner_state"], "approved")

    def test_static_unseen_is_stale_and_runtime_tombstone_is_unbound(self):
        _, payload = self.get("/api/conversations")
        by_id = {item["project_id"]: item for item in payload["projects"]}
        self.assertEqual(by_id["xray"]["binding_state"], "STALE")
        self.assertEqual(by_id["xray"]["binding_source"], "static")
        self.assertEqual(by_id["xray"]["conversation_url"], "https://chatgpt.com/c/conv-x")
        self.assertEqual(by_id["viewer"]["binding_state"], "UNBOUND")
        self.assertEqual(by_id["viewer"]["binding_source"], "runtime_unbound")
        self.assertIsNone(by_id["viewer"]["binding_id"])

    def test_web_app_renders_conversation_binding_section(self):
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/api/conversations", app)
        self.assertIn("renderConversationBindings", app)
        self.assertIn("conversationBindings", html)
        self.assertIn("Open conversation", app)
        self.assertIn("Owner gate", app)
        self.assertIn("Owner control", app)


if __name__ == "__main__":
    unittest.main()
