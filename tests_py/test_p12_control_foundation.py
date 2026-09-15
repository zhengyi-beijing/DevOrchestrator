import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from dev_orchestrator.control.binding_resolver import resolve_effective_project
from dev_orchestrator.control.command_store import (
    ControlCommandConflictError,
    ControlCommandStore,
)
from dev_orchestrator.storage.json_store import write_json
from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.control.store import ConversationConflictError, ConversationControlStore
from dev_orchestrator.core.control_commands import ControlCommandCoordinator
from dev_orchestrator.web.server import broker_proxy_payload, make_server
from tests_py.test_control_commands import FakeExecutor, write_config


ROOT = Path(__file__).resolve().parents[1]


def command(command_id: str = "same-id", action: str = "continue") -> dict:
    return {
        "schema_version": 1,
        "command_id": command_id,
        "project_id": "p1",
        "action": action,
        "expected": {
            "revision": "r1", "project_id": "p1", "repo_path": "repo",
            "branch": "main", "head": "head", "dirty": False,
            "status_hash": None, "task_id": "P1", "lifecycle_state": "READY_TO_RUN",
            "gate_id": None, "paused": False, "binding_state": None,
            "binding_id": None, "binding_adapter": None,
        },
        "target": {},
    }


class P12StoreTests(unittest.TestCase):
    def test_direct_submission_requires_complete_expected_identity(self):
        with tempfile.TemporaryDirectory() as td:
            store = ControlCommandStore(td)
            incomplete = command("incomplete")
            incomplete["expected"] = {"revision": "r1"}
            with self.assertRaisesRegex(ValueError, "expected identity missing fields"):
                store.submit(incomplete, source="local_client")

    def test_atomic_thread_replay_and_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            store = ControlCommandStore(td)
            with ThreadPoolExecutor(max_workers=8) as pool:
                rows = list(pool.map(lambda _: store.submit(command(), source="test"), range(24)))
            self.assertEqual({row["request_hash"] for row in rows}, {rows[0]["request_hash"]})
            self.assertEqual(len(list((Path(td) / "control" / "inbox").glob("*.json"))), 1)
            with self.assertRaises(ControlCommandConflictError):
                store.submit(command(action="pause"), source="test")
            audit = (Path(td) / "control" / "audit.jsonl").read_text(encoding="utf-8")
            self.assertEqual(len(audit.splitlines()), 1)
            self.assertNotIn("Bearer", audit)
            with self.assertRaisesRegex(ValueError, "schema_version"):
                store.submit({**command("bad-schema"), "schema_version": 2}, source="test")
            with self.assertRaisesRegex(ValueError, "unknown control request fields"):
                store.submit({**command("unknown"), "surprise": True}, source="test")
            with self.assertRaisesRegex(ValueError, "unknown target fields"):
                store.submit({**command("bad-target"), "target": {"shell": "no"}}, source="test")
            with self.assertRaisesRegex(ValueError, "url must match"):
                ConversationControlStore(td).heartbeat(
                    "chatgpt_web", "one", title="wrong",
                    url="https://chatgpt.com/c/one-other", tab_instance_id="tab",
                )

    def test_atomic_subprocess_replay(self):
        with tempfile.TemporaryDirectory() as td:
            script = (
                "import json,sys; from dev_orchestrator.control.command_store import ControlCommandStore; "
                "v=json.loads(sys.argv[2]); print(ControlCommandStore(sys.argv[1]).submit(v,source='subprocess')['request_hash'])"
            )
            value = json.dumps(command("process-id"), separators=(",", ":"))
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script, td, value], cwd=ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                for _ in range(4)
            ]
            results = [process.communicate(timeout=15) for process in processes]
            self.assertEqual([process.returncode for process in processes], [0, 0, 0, 0])
            self.assertEqual(len({stdout.strip() for stdout, _ in results}), 1)
            self.assertEqual(len(list((Path(td) / "control" / "inbox").glob("*.json"))), 1)

    def test_corrupt_queue_and_torn_audit_are_preserved_and_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td); store = ControlCommandStore(runtime)
            store.submit(command("good"), source="test")
            store.audit_path.write_text('{"torn":', encoding="utf-8")
            store.audit("recovery", {"command_id": "good", "state": "pending"})
            self.assertTrue(any(store.quarantine_dir.glob("audit.jsonl.corrupt-*")))
            bad = store.inbox / "bad.json"; bad.write_text("{", encoding="utf-8")
            store.history.mkdir(parents=True, exist_ok=True)
            bad_history = store.history / "bad-result.json"; bad_history.write_text("[]", encoding="utf-8")
            outcomes = store.repair_corruption()
            self.assertEqual([outcome["state"] for outcome in outcomes], ["failed", "failed"])
            health = store.health()
            self.assertTrue(health["degraded"])
            self.assertGreaterEqual(len(health["quarantined"]), 3)
            self.assertFalse(bad.exists())
            self.assertFalse(bad_history.exists())

    def test_settlement_crash_boundaries_recover_terminal_audit_before_unlink(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)

            # Before history: an ordinary retry completes all three durable steps.
            before = ControlCommandStore(runtime)
            pending = before.submit(command("before-history"), source="test")
            inbox = before.inbox / "before-history.json"
            outcome = {**pending, "state": "accepted", "processed_at": "now"}
            before.settle(inbox, outcome)
            self.assertFalse(inbox.exists())

            # After history, before audit: a recreated store repairs the audit,
            # then removes the still-durable inbox record.
            missing_audit = ControlCommandStore(runtime)
            pending = missing_audit.submit(command("missing-audit"), source="test")
            inbox = missing_audit.inbox / "missing-audit.json"
            outcome = {**pending, "state": "blocked", "processed_at": "now"}
            write_json(missing_audit.history / "missing-audit.json", outcome, indent=2)
            recovered = ControlCommandStore(runtime).settle(inbox, outcome)
            self.assertEqual(recovered, outcome)
            self.assertFalse(inbox.exists())

            # After audit, before unlink: restart/replay does not duplicate the
            # terminal audit and does remove the leftover inbox record.
            after_audit = ControlCommandStore(runtime)
            pending = after_audit.submit(command("after-audit"), source="test")
            inbox = after_audit.inbox / "after-audit.json"
            outcome = {**pending, "state": "accepted", "processed_at": "now"}
            write_json(after_audit.history / "after-audit.json", outcome, indent=2)
            after_audit.audit("command_settled", outcome)
            replayed = ControlCommandStore(runtime).settle(inbox, outcome)
            self.assertEqual(replayed, outcome)
            self.assertFalse(inbox.exists())

            rows = [
                json.loads(line)
                for line in (runtime / "control" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            terminal_ids = [row["command_id"] for row in rows if row["event"] == "command_settled"]
            self.assertEqual(
                terminal_ids,
                ["before-history", "missing-audit", "after-audit"],
            )

            # The daemon restart path delegates the same history/inbox state to
            # settle instead of deleting inbox directly.
            restarted_runtime = runtime / "restart-runtime"
            restarted = ControlCommandStore(restarted_runtime)
            pending = restarted.submit(command("restart-replay"), source="test")
            outcome = {**pending, "state": "accepted", "processed_at": "now"}
            write_json(restarted.history / "restart-replay.json", outcome, indent=2)
            repo = runtime / "restart-repo"; repo.mkdir()
            config = runtime / "restart-projects.json"; write_config(config, repo)
            replay = ControlCommandCoordinator(restarted_runtime).advance(
                config, {"projects": []}, FakeExecutor()
            )
            self.assertEqual(replay, [outcome])
            self.assertFalse((restarted.inbox / "restart-replay.json").exists())
            restart_rows = [
                json.loads(line)
                for line in restarted.audit_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [row["event"] for row in restart_rows],
                ["request_accepted", "command_settled"],
            )

    def test_conversation_liveness_uniqueness_tombstone_and_direct_ai(self):
        with tempfile.TemporaryDirectory() as td:
            store = ConversationControlStore(td, session_presence_seconds=60)
            now = datetime.now(timezone.utc)
            store.heartbeat(
                "chatgpt", "one", title="One", url="https://chatgpt.com/c/one",
                tab_instance_id="tab-1", now=now,
            )
            self.assertEqual(store.session_status("chatgpt", "one", now=now)["state"], "live")
            self.assertEqual(
                store.session_status("chatgpt", "one", now=now + timedelta(seconds=61))["state"],
                "stale",
            )
            store.bind("p1", "chatgpt", "one")
            with self.assertRaises(ConversationConflictError):
                store.bind("p2", "chatgpt", "one")
            tombstone = store.unbind("p1")
            self.assertEqual(tombstone["state"], "unbound")
            self.assertIsNone(store.binding_for_project("p1"))

            direct = resolve_effective_project(
                {"project_id": "direct", "orchestration_ready": True, "conversation_binding": None},
                store,
            )
            self.assertTrue(direct["orchestration_ready"])
            self.assertEqual(direct["conversation_binding_source"], "none")

    def test_security_origin_and_pairing_hash_only(self):
        with tempfile.TemporaryDirectory() as td:
            security = ControlSecurity(td)
            self.assertTrue(security.valid_origin("http://127.0.0.1:8770", "127.0.0.1:8770", 8770))
            self.assertFalse(security.valid_origin("http://127.0.0.1:8770", "127.0.0.1:9999", 8770))
            self.assertFalse(security.valid_origin("http://localhost:8770", "127.0.0.1:8770", 8770))
            pairing = security.create_pairing()
            persisted = security.pairings_path.read_text(encoding="utf-8")
            self.assertNotIn(pairing["code"], persisted)
            redeemed = security.redeem_pairing(pairing["pairing_id"], pairing["code"])
            self.assertNotIn(redeemed["capability"], security.pairings_path.read_text(encoding="utf-8"))
            self.assertTrue(security.adapter_authorized("Bearer " + redeemed["capability"]))
            with self.assertRaises(ValueError):
                security.redeem_pairing(pairing["pairing_id"], pairing["code"])
            security.revoke_pairing(pairing["pairing_id"])
            self.assertFalse(security.adapter_authorized("Bearer " + redeemed["capability"]))
            unused = security.create_pairing()
            self.assertTrue(security.revoke_pairing(unused["pairing_id"])["revoked"])
            with self.assertRaisesRegex(ValueError, "used"):
                security.redeem_pairing(unused["pairing_id"], unused["code"])
            expired = security.create_pairing()
            data = json.loads(security.pairings_path.read_text(encoding="utf-8"))
            data["pairings"][expired["pairing_id"]]["expires_at_epoch"] = 0
            security.pairings_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expired"):
                security.redeem_pairing(expired["pairing_id"], expired["code"])

    def test_broker_failure_is_isolated_and_unknown_fields_are_preserved(self):
        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return self.body

        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            (runtime / "aibroker.json").write_text(json.dumps({"base_url": "http://127.0.0.1:8875"}), encoding="utf-8")
            with patch("dev_orchestrator.web.server.urlopen", side_effect=URLError("timed out")):
                unavailable = broker_proxy_payload(runtime, "/api/resources")
                self.assertFalse(unavailable["available"])
                self.assertEqual(unavailable["availability"], "unavailable")
            with patch("dev_orchestrator.web.server.urlopen", return_value=Response(b"[]")):
                self.assertEqual(broker_proxy_payload(runtime, "/api/resources")["error"], "invalid AIBroker response")
            with patch("dev_orchestrator.web.server.urlopen", return_value=Response(b'{"resources":[],"future_field":"preserved"}')):
                result = broker_proxy_payload(runtime, "/api/resources")
                self.assertTrue(result["available"])
                self.assertEqual(result["availability"], "available")
                self.assertEqual(result["future_field"], "preserved")
                self.assertEqual(result["unknown_fields"], [])
            with patch("dev_orchestrator.web.server.urlopen", return_value=Response(b'{"resources":[],"availability":"stale","observed_at":"2026-01-01T00:00:00Z","unknown_fields":["quota"]}')):
                stale = broker_proxy_payload(runtime, "/api/resources")
                self.assertTrue(stale["available"])
                self.assertEqual(stale["availability"], "stale")
                self.assertEqual(stale["unknown_fields"], ["quota"])


class P12HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        (self.runtime / "projects").mkdir()
        project = {
            "project_id": "p1", "id": "p1", "name": "P1", "repo_path": str(ROOT),
            "state": "READY_TO_RUN", "lifecycle_state": "READY_TO_RUN",
            "git": {"branch": "feature/test", "head": "abc123", "dirty": False},
            "telemetry": {"task_id": "P12"},
        }
        (self.runtime / "summary.json").write_text(
            json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(), "project_count": 1, "projects": [project]}),
            encoding="utf-8",
        )
        (self.runtime / "projects" / "p1.json").write_text(json.dumps(project), encoding="utf-8")
        self.config = self.runtime / "projects.json"
        write_config(self.config, ROOT)
        self.server = make_server(
            "127.0.0.1", 0, self.runtime, ROOT / "web",
            enable_control=True, config_path=self.config,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, target, *, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request(method, target, body=body, headers=headers or {})
        response = conn.getresponse(); raw = response.read(); result_headers = dict(response.getheaders())
        conn.close()
        return response.status, result_headers, raw

    def browser_session(self):
        status, headers, body = self.request(
            "POST", "/api/v1/control/browser-sessions",
            headers={"Origin": self.origin, "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 201)
        return headers["Set-Cookie"].split(";", 1)[0], json.loads(body)["data"]["csrf_token"]

    def test_read_envelopes_and_browser_command_guards(self):
        status, _, body = self.request("GET", "/api/v1/control/overview")
        self.assertEqual(status, 200)
        overview = json.loads(body)
        self.assertEqual(overview["schema_version"], 1)
        self.assertTrue(overview["data"]["control_enabled"])
        self.assertTrue(any("broker_resources unavailable" in item for item in overview["warnings"]))
        continue_capability = next(
            item for item in overview["data"]["projects"][0]["controls"]
            if item["action"] == "continue"
        )
        self.assertTrue(continue_capability["available"])
        identity = overview["data"]["projects"][0]["control_identity"]

        payload = command("http-command")
        payload["expected"] = identity
        encoded = json.dumps(payload)
        self.assertEqual(self.request("POST", "/api/v1/control/commands", body=encoded, headers={"Content-Type": "application/json"})[0], 401)
        cookie, csrf = self.browser_session()
        headers = {
            "Content-Type": "application/json", "Origin": self.origin,
            "Sec-Fetch-Site": "same-origin", "Cookie": cookie, "X-DevOrch-CSRF": csrf,
        }
        first = self.request("POST", "/api/v1/control/commands", body=encoded, headers=headers)
        self.assertEqual(first[0], 202)
        self.assertEqual(json.loads(first[2])["data"]["state"], "pending")
        self.assertEqual(self.request("POST", "/api/v1/control/commands", body=encoded, headers=headers)[0], 200)
        conflict = dict(payload); conflict["action"] = "pause"
        self.assertEqual(self.request("POST", "/api/v1/control/commands", body=json.dumps(conflict), headers=headers)[0], 409)
        self.assertEqual(self.request("GET", "/api/v1/control/commands/http-command")[0], 200)

        bearer_value = dict(payload); bearer_value["command_id"] = "http-bearer"
        token = (self.runtime / "control" / "api-token").read_text(encoding="utf-8").strip()
        bearer = self.request(
            "POST", "/api/v1/control/commands", body=json.dumps(bearer_value),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        self.assertEqual(bearer[0], 202)

        bad_headers = dict(headers); bad_headers["Origin"] = "http://evil.invalid"
        self.assertEqual(self.request("POST", "/api/v1/control/commands", body=encoded, headers=bad_headers)[0], 401)
        no_csrf = dict(headers); no_csrf.pop("X-DevOrch-CSRF")
        self.assertEqual(self.request("POST", "/api/v1/control/commands", body=encoded, headers=no_csrf)[0], 401)
        self.assertEqual(self.request("POST", "/api/v1/control/commands", body=encoded, headers={**headers, "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/v1/control/commands", body="{", headers=headers)[0], 400)
        incomplete = dict(payload); incomplete["command_id"] = "incomplete-identity"
        incomplete["expected"] = {"revision": identity["revision"]}
        self.assertEqual(
            self.request(
                "POST", "/api/v1/control/commands",
                body=json.dumps(incomplete), headers=headers,
            )[0],
            400,
        )
        oversized = {**headers, "Content-Length": str(64 * 1024 + 1)}
        self.assertEqual(self.request("POST", "/api/v1/control/commands", body="{}", headers=oversized)[0], 413)
        self.assertEqual(self.request("PUT", "/api/v1/control/commands", body=encoded, headers=headers)[0], 405)
        self.assertEqual(self.request(
            "POST", "/api/v1/control/browser-sessions",
            headers={"Origin": self.origin, "Host": "127.0.0.1:1", "Sec-Fetch-Site": "same-origin"},
        )[0], 403)
        self.assertEqual(self.request(
            "POST", "/api/v1/control/browser-sessions",
            headers={"Origin": self.origin, "Sec-Fetch-Site": "cross-site"},
        )[0], 403)

    def test_pairing_cors_heartbeat_and_revoke(self):
        cookie, csrf = self.browser_session()
        owner = {"Origin": self.origin, "Sec-Fetch-Site": "same-origin", "Cookie": cookie, "X-DevOrch-CSRF": csrf}
        status, _, body = self.request("POST", "/api/v1/control/adapter-pairings", headers=owner)
        self.assertEqual(status, 201); pairing = json.loads(body)["data"]
        cors = self.request("OPTIONS", "/api/v1/control/session-heartbeats", headers={"Origin": "https://chatgpt.com"})
        self.assertEqual(cors[0], 204); self.assertEqual(cors[1]["Access-Control-Allow-Origin"], "https://chatgpt.com")
        redeem_body = json.dumps({"pairing_id": pairing["pairing_id"], "code": pairing["code"]})
        status, _, body = self.request(
            "POST", "/api/v1/control/adapter-pairings/redeem", body=redeem_body,
            headers={"Origin": "https://chatgpt.com", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 200); capability = json.loads(body)["data"]["capability"]
        heartbeat = json.dumps({
            "adapter": "chatgpt", "binding_id": "conv", "title": "Conversation",
            "url": "https://chatgpt.com/c/conv", "tab_instance_id": "tab-1",
        })
        cap_headers = {"Origin": "https://chatgpt.com", "Content-Type": "application/json", "Authorization": "Bearer " + capability}
        self.assertEqual(self.request("POST", "/api/v1/control/session-heartbeats", body=heartbeat, headers=cap_headers)[0], 200)
        self.assertEqual(self.request("POST", f"/api/v1/control/adapter-pairings/{pairing['pairing_id']}/revoke", headers=owner)[0], 200)
        self.assertEqual(self.request("POST", "/api/v1/control/session-heartbeats", body=heartbeat, headers=cap_headers)[0], 401)

    def test_representative_8770_command_is_consumed_and_audited(self):
        overview = json.loads(self.request("GET", "/api/v1/control/overview")[2])
        identity = overview["data"]["projects"][0]["control_identity"]
        cookie, csrf = self.browser_session()
        headers = {
            "Content-Type": "application/json", "Origin": self.origin,
            "Sec-Fetch-Site": "same-origin", "Cookie": cookie, "X-DevOrch-CSRF": csrf,
        }
        value = command("representative-continue"); value["expected"] = identity
        self.assertEqual(self.request("POST", "/api/v1/control/commands", body=json.dumps(value), headers=headers)[0], 202)
        summary = json.loads((self.runtime / "summary.json").read_text(encoding="utf-8"))
        executor = FakeExecutor()
        outcomes = ControlCommandCoordinator(self.runtime).advance(self.config, summary, executor)
        self.assertEqual(outcomes[0]["state"], "accepted")
        status, _, body = self.request("GET", "/api/v1/control/commands/representative-continue")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"]["state"], "accepted")
        audit_events = [json.loads(line)["event"] for line in (self.runtime / "control" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(audit_events, ["request_accepted", "command_settled"])

    def test_non_loopback_listener_never_enables_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            server = make_server("0.0.0.0", 0, td, ROOT / "web", enable_control=True)
            try:
                self.assertFalse(server.control_enabled)
                self.assertIsNone(server.control_security)
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
