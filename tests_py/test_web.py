import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_orchestrator.web.server import make_server


ROOT = Path(__file__).resolve().parents[1]


class WebContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        (self.runtime / "projects").mkdir()
        (self.runtime / "history").mkdir()
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        (self.runtime / "monitor.json").write_text(json.dumps({
            "state": "running", "pid": 999999, "interval_seconds": 60,
            "started_at": old, "last_tick_at": old, "last_error": None,
        }), encoding="utf-8")
        project = {"id": "labdemo", "name": "LabDemo", "state": "WAITING_PHASE_GATE"}
        (self.runtime / "summary.json").write_text(json.dumps({"observed_at": old, "project_count": 1, "projects": [project]}), encoding="utf-8")
        (self.runtime / "projects" / "labdemo.json").write_text(json.dumps(project), encoding="utf-8")
        (self.runtime / "history" / "events.jsonl").write_text('{"project_id":"labdemo","to_state":"WAITING_PHASE_GATE"}\n', encoding="utf-8")
        (self.runtime / "history" / "runs.jsonl").write_text('{"project_id":"labdemo","result":"completed"}\n', encoding="utf-8")
        self.server = make_server("127.0.0.1", 0, self.runtime, ROOT / "web")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, target: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        conn.request(method, target)
        response = conn.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        conn.close()
        return response.status, headers, body

    def test_static_and_read_only_api_contract(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Dev Orchestrator", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, _, body = self.request("GET", "/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["project_count"], 1)
        status, _, body = self.request("GET", "/api/monitor")
        monitor = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(monitor["stale"])
        self.assertFalse(monitor["process_alive"])
        self.assertEqual(self.request("HEAD", "/app.js")[0], 200)
        self.assertEqual(self.request("GET", "/api/events?limit=999")[0], 200)
        self.assertEqual(self.request("GET", "/api/projects/labdemo")[0], 200)
        self.assertEqual(self.request("GET", "/api/projects/missing")[0], 404)
        self.assertEqual(self.request("GET", "/api/broker/resources")[0], 200)
        self.assertFalse(json.loads(self.request("GET", "/api/broker/resources")[2])["available"])
        self.assertEqual(self.request("GET", "/api/broker/executions")[0], 200)
        self.assertEqual(self.request("GET", "/api/broker/usage")[0], 200)
        self.assertEqual(self.request("GET", "/missing")[0], 404)
        post_status, post_headers, _ = self.request("POST", "/api/summary")
        self.assertEqual(post_status, 405)
        self.assertEqual(post_headers["Allow"], "GET, HEAD")
        self.assertEqual(self.request("GET", "/%2e%2e/README.md")[0], 400)
        self.assertTrue((self.runtime / "web.json").is_file())


if __name__ == "__main__":
    unittest.main()
