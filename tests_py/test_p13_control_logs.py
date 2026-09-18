import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from dev_orchestrator.control.logs import read_control_logs, redact_secrets
from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.web.server import make_server
from tests_py.test_control_commands import write_config

ROOT = Path(__file__).resolve().parents[1]


class P13ControlLogsUnitTests(unittest.TestCase):
    def test_secret_redaction(self):
        sample = {
            "token": "secret_token_123",
            "api_key": "sk-1234567890abcdef",
            "authorization": "Bearer super-secret",
            "nested": {
                "password": "my_password",
                "normal": "safe_value",
            },
            "array": [
                {"bearer_token": "token_abc"},
                "just a string with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 inside",
            ],
        }
        redacted = redact_secrets(sample)
        self.assertEqual(redacted["token"], "[REDACTED]")
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["authorization"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["password"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["normal"], "safe_value")
        self.assertEqual(redacted["array"][0]["bearer_token"], "[REDACTED]")
        self.assertIn("[REDACTED]", redacted["array"][1])

    def test_diagnostics_code_survives_redaction_and_secrets_redacted(self):
        # Operational diagnostics / watchdog code must NOT be redacted
        diagnostics_record = {
            "code": "agent_stalled",
            "message": "Agent did not report progress within timeout window",
            "detail": "worker-1",
        }
        redacted_diag = redact_secrets(diagnostics_record)
        self.assertEqual(redacted_diag["code"], "agent_stalled")
        self.assertEqual(redacted_diag["message"], "Agent did not report progress within timeout window")

        # Broker subprocess error code must NOT be redacted
        broker_err = {
            "code": "UNAVAILABLE",
            "error": "broker CLI process terminated unexpectedly",
        }
        redacted_broker = redact_secrets(broker_err)
        self.assertEqual(redacted_broker["code"], "UNAVAILABLE")

        # Pairing verification code in pairing context MUST be redacted
        pairing_record = {
            "pairing_id": "pair-test-123",
            "code": "sensitive-pairing-code-456",
            "code_hash": "sha256-hash-of-code",
            "expires_in_seconds": 300,
        }
        redacted_pairing = redact_secrets(pairing_record)
        self.assertEqual(redacted_pairing["pairing_id"], "pair-test-123")
        self.assertEqual(redacted_pairing["code"], "[REDACTED]")
        self.assertEqual(redacted_pairing["code_hash"], "[REDACTED]")
        self.assertEqual(redacted_pairing["expires_in_seconds"], 300)

        # CSRF tokens MUST be redacted
        csrf_record = {
            "csrf_token": "csrf-secret-token-xyz",
            "expires_in_seconds": 1800,
        }
        redacted_csrf = redact_secrets(csrf_record)
        self.assertEqual(redacted_csrf["csrf_token"], "[REDACTED]")
        self.assertEqual(redacted_csrf["expires_in_seconds"], 1800)

        # Explicit pairing_code key MUST be redacted
        explicit_pairing = {
            "pairing_code": "my-secret-pairing-code",
        }
        self.assertEqual(redact_secrets(explicit_pairing)["pairing_code"], "[REDACTED]")

        # Session CSRF in session context MUST be redacted
        session_record = {
            "session_id": "sess-xyz",
            "csrf": "raw-session-csrf",
        }
        redacted_sess = redact_secrets(session_record)
        self.assertEqual(redacted_sess["session_id"], "sess-xyz")
        self.assertEqual(redacted_sess["csrf"], "[REDACTED]")

    def test_read_control_logs_empty_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            res = read_control_logs(td)
            self.assertEqual(res["schema_version"], 1)
            self.assertEqual(res["items"], [])
            self.assertEqual(res["total_matched"], 0)
            self.assertFalse(res["has_more"])
            self.assertIsNone(res["next_cursor"])
            self.assertIn("sources", res)
            self.assertEqual(res["sources"]["events"]["status"], "not_found")

    def test_read_control_logs_filtering_and_pagination(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            events_file = p / "events.jsonl"
            # Write 25 events for p1, 10 events for p2
            with events_file.open("w", encoding="utf-8") as f:
                for i in range(25):
                    rec = {
                        "timestamp": f"2026-09-18T10:{i:02d}:00Z",
                        "project_id": "proj-alpha",
                        "run_id": f"run-{i}",
                        "command_id": f"cmd-{i % 5}",
                        "event": f"alpha_event_{i}",
                        "token": f"sensitive_token_{i}",
                    }
                    f.write(json.dumps(rec) + "\n")
                for j in range(10):
                    rec = {
                        "timestamp": f"2026-09-18T11:{j:02d}:00Z",
                        "project_id": "proj-beta",
                        "event": f"beta_event_{j}",
                    }
                    f.write(json.dumps(rec) + "\n")

            # Filter by proj-alpha, limit 10
            page1 = read_control_logs(td, project_id="proj-alpha", limit=10)
            self.assertEqual(len(page1["items"]), 10)
            self.assertEqual(page1["total_matched"], 25)
            self.assertTrue(page1["has_more"])
            self.assertIsNotNone(page1["next_cursor"])
            # Ensure redaction worked
            self.assertEqual(page1["items"][0]["data"]["token"], "[REDACTED]")

            # Page 2
            page2 = read_control_logs(td, project_id="proj-alpha", limit=10, cursor=page1["next_cursor"])
            self.assertEqual(len(page2["items"]), 10)
            self.assertTrue(page2["has_more"])

            # Page 3
            page3 = read_control_logs(td, project_id="proj-alpha", limit=10, cursor=page2["next_cursor"])
            self.assertEqual(len(page3["items"]), 5)
            self.assertFalse(page3["has_more"])
            self.assertIsNone(page3["next_cursor"])

            # Filter by command_id
            cmd_filter = read_control_logs(td, command_id="cmd-1")
            self.assertEqual(len(cmd_filter["items"]), 5)
            for it in cmd_filter["items"]:
                self.assertEqual(it["command_id"], "cmd-1")

    def test_corrupt_lines_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            events_file = p / "events.jsonl"
            with events_file.open("w", encoding="utf-8") as f:
                f.write(json.dumps({"timestamp": "2026-09-18T10:00:00Z", "project_id": "p1", "msg": "valid1"}) + "\n")
                f.write("{invalid json syntax here\n")
                f.write(json.dumps({"timestamp": "2026-09-18T10:01:00Z", "project_id": "p1", "msg": "valid2"}) + "\n")

            res = read_control_logs(td)
            self.assertEqual(len(res["items"]), 2)
            self.assertEqual(res["items"][0]["data"]["msg"], "valid2")
            self.assertEqual(res["items"][1]["data"]["msg"], "valid1")
            self.assertIn("corrupt_lines_skipped", res["sources"]["events"])
            self.assertEqual(res["sources"]["events"]["corrupt_lines_skipped"], 1)

    def test_control_logs_cursor_stability_with_concurrent_appends(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            events_file = p / "events.jsonl"
            # Write 10 initial records
            with events_file.open("w", encoding="utf-8") as f:
                for i in range(10):
                    f.write(json.dumps({
                        "timestamp": f"2026-09-18T10:{i:02d}:00Z",
                        "project_id": "proj-1",
                        "item_num": i,
                    }) + "\n")

            # Page 1: limit 5. Since logs are ordered descending, gets items 9, 8, 7, 6, 5
            page1 = read_control_logs(td, project_id="proj-1", limit=5)
            self.assertEqual(len(page1["items"]), 5)
            self.assertEqual([it["data"]["item_num"] for it in page1["items"]], [9, 8, 7, 6, 5])
            self.assertTrue(page1["has_more"])
            self.assertIsNotNone(page1["next_cursor"])

            # Concurrently append new records (items 10, 11) to the log
            with events_file.open("a", encoding="utf-8") as f:
                for i in (10, 11):
                    f.write(json.dumps({
                        "timestamp": f"2026-09-18T10:{i:02d}:00Z",
                        "project_id": "proj-1",
                        "item_num": i,
                    }) + "\n")

            # Page 2 using page 1 cursor: should return 4, 3, 2, 1, 0 without seeing 10, 11 or duplicating 5..9
            page2 = read_control_logs(td, project_id="proj-1", limit=5, cursor=page1["next_cursor"])
            self.assertEqual(len(page2["items"]), 5)
            self.assertEqual([it["data"]["item_num"] for it in page2["items"]], [4, 3, 2, 1, 0])
            self.assertFalse(page2["has_more"])
            self.assertIsNone(page2["next_cursor"])

    def test_control_logs_cross_project_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            events_file = p / "events.jsonl"
            with events_file.open("w", encoding="utf-8") as f:
                f.write(json.dumps({"timestamp": "2026-09-18T10:00:00Z", "project_id": "p1", "msg": "p1_msg"}) + "\n")
                f.write(json.dumps({"timestamp": "2026-09-18T10:01:00Z", "project_id": "p2", "msg": "p2_msg"}) + "\n")
                f.write(json.dumps({"timestamp": "2026-09-18T10:02:00Z", "project_id": "p1", "msg": "p1_msg2"}) + "\n")
                f.write(json.dumps({"timestamp": "2026-09-18T10:03:00Z", "msg": "no_project"}) + "\n")

            # Filter for p1: should return only p1 events
            res_p1 = read_control_logs(td, project_id="p1")
            self.assertEqual(len(res_p1["items"]), 2)
            self.assertTrue(all(it["project_id"] == "p1" for it in res_p1["items"]))

            # Filter for p2: should return only p2 events
            res_p2 = read_control_logs(td, project_id="p2")
            self.assertEqual(len(res_p2["items"]), 1)
            self.assertEqual(res_p2["items"][0]["project_id"], "p2")
            self.assertEqual(res_p2["items"][0]["data"]["msg"], "p2_msg")

    def test_control_logs_degraded_and_unknown_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            events_file = p / "events.jsonl"
            with events_file.open("w", encoding="utf-8") as f:
                f.write("corrupted line 1\n")
                f.write(json.dumps({
                    "timestamp": "2026-09-18T10:00:00Z",
                    "project_id": "p1",
                    "provenance": "custom_worker_agent",
                    "unknown_field_123": "preserve_me",
                }) + "\n")
                f.write("corrupted line 2\n")

            res = read_control_logs(td)
            self.assertEqual(len(res["items"]), 1)
            # Check degraded status
            self.assertEqual(res["sources"]["events"]["status"], "degraded")
            self.assertEqual(res["sources"]["events"]["corrupt_lines_skipped"], 2)
            self.assertTrue(len(res["warnings"]) >= 1)
            # Check preservation of unknown provenance and fields
            item = res["items"][0]
            self.assertEqual(item["data"]["provenance"], "custom_worker_agent")
            self.assertEqual(item["data"]["unknown_field_123"], "preserve_me")


class P13ControlLogsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        (self.runtime / "projects").mkdir()
        self.config = self.runtime / "projects.json"
        write_config(self.config, ROOT)

        # Write sample event
        events_file = self.runtime / "events.jsonl"
        with events_file.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": "2026-09-18T12:00:00Z",
                "project_id": "test-p1",
                "action": "pause",
                "secret_key": "supersecret123",
            }) + "\n")

        self.security = ControlSecurity(self.runtime)
        self.auth_token = self.security.token()

        self.server = make_server(
            "127.0.0.1", 0, self.runtime, ROOT / "web",
            enable_control=True, config_path=self.config,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        h = headers or {}
        conn.request("GET", path, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, resp.headers, data

    def test_unauthorized_request_rejected(self):
        status, _, _ = self.request("/api/v1/control/logs")
        self.assertEqual(status, 401)

        status, _, _ = self.request("/api/v1/control/logs", headers={"Authorization": "Bearer invalid-token"})
        self.assertEqual(status, 401)

    def test_authorized_get_logs_success(self):
        status, _, body = self.request(
            "/api/v1/control/logs?project_id=test-p1&limit=10",
            headers={"Authorization": f"Bearer {self.auth_token}"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["project_id"], "test-p1")
        self.assertEqual(item["data"]["secret_key"], "[REDACTED]")
