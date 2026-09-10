import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from dev_orchestrator.web.server import make_server

ROOT = Path(__file__).resolve().parents[1]


class UnboundWebUiTests(unittest.TestCase):
    def test_orchestration_api_surfaces_unbound_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            state = {
                "version": 2,
                "worker_done": {"labdemo": {"occurrences": {"run-1": {
                    "state": "prepared", "delivery_state": "unbound",
                    "request_id": "worker_done:labdemo:run-1",
                    "binding_id": "conv-L", "prepared_at": "2026-09-04T00:00:00+00:00"
                }}}}
            }
            (runtime / "dispatcher-state.json").write_text(json.dumps(state), encoding="utf-8")
            server = make_server("127.0.0.1", 0, runtime, ROOT / "web")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
                conn.request("GET", "/api/orchestration")
                response = conn.getresponse()
                payload = json.loads(response.read())
                conn.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["projects"]["labdemo"]["state"], "UNBOUND")
                self.assertEqual(payload["projects"]["labdemo"]["delivery_state"], "unbound")
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_direct_ai_project_suppresses_stale_browser_unbound(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            state = {"version": 2, "worker_done": {"p1": {"occurrences": {"r1": {
                "state": "prepared", "delivery_state": "unbound",
                "request_id": "worker_done:p1:r1", "binding_id": "old-browser",
                "prepared_at": "2026-09-04T00:00:00+00:00"
            }}}}}
            (runtime / "dispatcher-state.json").write_text(json.dumps(state), encoding="utf-8")
            (runtime / "projects").mkdir()
            (runtime / "projects" / "p1.json").write_text(json.dumps({
                "project_id": "p1", "conversation_binding": None,
                "orchestration_ready": True,
            }), encoding="utf-8")
            server = make_server("127.0.0.1", 0, runtime, ROOT / "web")
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
                conn.request("GET", "/api/orchestration")
                response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["projects"], {})
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_web_app_displays_rebind_instruction_for_unbound_project(self):
        source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("UNBOUND / BLOCKED", source)
        self.assertIn("Rebind the ChatGPT conversation to resume the pending request.", source)
        self.assertIn("/api/orchestration", source)


if __name__ == "__main__":
    unittest.main()
