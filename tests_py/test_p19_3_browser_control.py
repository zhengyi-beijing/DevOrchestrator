"""Tests for P19.3 ChatGPT Plus Browser Control Bridge."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from dev_orchestrator.bridge.server import (
    _is_allowed_control_origin,
    _is_loopback_address,
    make_bridge_server,
)
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.control.browser_control import (
    DEVORCH_ACTION_RESULT_V1,
    DEVORCH_ACTION_V1,
    BrowserControlConflictError,
    BrowserControlConfirmationRequiredError,
    BrowserControlError,
    BrowserControlRequestStore,
    BrowserControlService,
    format_action_result,
    parse_action_envelope,
    validate_action_envelope,
)
from dev_orchestrator.control.security import (
    create_browser_control_capability,
    create_mcp_capability,
    list_browser_control_capabilities,
    revoke_browser_control_capability,
    validate_browser_control_capability,
)
from dev_orchestrator.storage.json_store import write_json

ROOT = Path(__file__).resolve().parents[1]
USERSCRIPT_PATH = ROOT / "browser" / "chatgpt-web-adapter.user.js"


class ProtocolParserTests(unittest.TestCase):
    """Test DEVORCH_ACTION_V1 envelope parsing, validation, and result formatting."""

    def test_parse_bracket_envelope(self):
        text = """
Here is the requested operation:
[DEVORCH_ACTION_V1]
{
  "protocol": "DEVORCH_ACTION_V1",
  "request_id": "req-101",
  "action": "status",
  "project_id": "devorchestrator",
  "parameters": {}
}
[/DEVORCH_ACTION_V1]
Please confirm when done.
"""
        parsed = parse_action_envelope(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["protocol"], DEVORCH_ACTION_V1)
        self.assertEqual(parsed["request_id"], "req-101")
        self.assertEqual(parsed["action"], "status")
        self.assertEqual(parsed["project_id"], "devorchestrator")
        self.assertFalse(parsed["confirmed"])

    def test_parse_code_fence_envelope(self):
        text = """
```devorch_action
{
  "protocol": "DEVORCH_ACTION_V1",
  "request_id": "req-102",
  "action": "start_task",
  "project_id": "devorchestrator",
  "parameters": {"command_ref": "echo_hello"},
  "confirmed": true
}
```
"""
        parsed = parse_action_envelope(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["request_id"], "req-102")
        self.assertEqual(parsed["action"], "start_task")
        self.assertTrue(parsed["confirmed"])
        self.assertEqual(parsed["parameters"], {"command_ref": "echo_hello"})

    def test_parse_raw_json_envelope(self):
        raw = json.dumps({
            "protocol": "DEVORCH_ACTION_V1",
            "request_id": "req-103",
            "action": "job_status",
            "project_id": "test-proj",
            "parameters": {"job_id": "job-999"},
        })
        parsed = parse_action_envelope(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["request_id"], "req-103")
        self.assertEqual(parsed["action"], "job_status")

    def test_parse_invalid_or_missing_envelope(self):
        self.assertIsNone(parse_action_envelope(""))
        self.assertIsNone(parse_action_envelope("just some normal conversation text"))
        self.assertIsNone(parse_action_envelope("[DEVORCH_ACTION_V1] {bad json} [/DEVORCH_ACTION_V1]"))
        # Missing required request_id
        invalid = {"protocol": "DEVORCH_ACTION_V1", "action": "status"}
        with self.assertRaises(BrowserControlError):
            validate_action_envelope(invalid)

    def test_format_action_result(self):
        res = format_action_result("req-101", "success", {"state": "ok"})
        self.assertIn(f"[{DEVORCH_ACTION_RESULT_V1} req-101]", res)
        self.assertIn('"status": "success"', res)
        self.assertIn('"state": "ok"', res)
        self.assertIn(f"[/{DEVORCH_ACTION_RESULT_V1}]", res)

        err_res = format_action_result("req-102", "error", {"error": "Denied", "reason": "Hardware command"})
        self.assertIn('"status": "error"', err_res)
        self.assertIn('"error": "Denied"', err_res)
        self.assertIn('"reason": "Hardware command"', err_res)


class UserscriptAdapterTests(unittest.TestCase):
    """Test pure functions exposed by chatgpt-web-adapter.user.js using Node.js."""

    def test_userscript_pure_browser_control_helpers(self):
        script = USERSCRIPT_PATH.as_posix()
        code = f"""
        globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST_DISABLED__ = true;
        require('{script}');
        const a = globalThis.__DEVORCH_CHATGPT_ADAPTER_TEST__;
        
        // 1. Action classifications
        const ro1 = a.isReadOnlyAction('status');
        const ro2 = a.isReadOnlyAction('commands_catalog');
        const ro3 = a.isReadOnlyAction('start_task');
        const ef1 = a.isEffectfulAction('start_task');
        const ef2 = a.isEffectfulAction('cancel_job');
        const ef3 = a.isEffectfulAction('status');

        // 2. Envelope parsing
        const envText = '[DEVORCH_ACTION_V1]\\n{{"protocol":"DEVORCH_ACTION_V1","request_id":"node-1","action":"status","project_id":"devorchestrator"}}\\n[/DEVORCH_ACTION_V1]';
        const parsed = a.parseActionEnvelope(envText);
        
        // 3. Result formatting
        const formatted = a.formatActionResult('node-1', 'success', {{ alive: true }});
        
        console.log(JSON.stringify({{
            ro: [ro1, ro2, ro3],
            ef: [ef1, ef2, ef3],
            parsed_id: parsed ? parsed.request_id : null,
            parsed_action: parsed ? parsed.action : null,
            has_head: formatted.indexOf('[DEVORCH_ACTION_RESULT_V1 node-1]') !== -1
        }}));
        """
        result = subprocess.run(["node", "-e", code], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip())
        self.assertEqual(data["ro"], [True, True, False])
        self.assertEqual(data["ef"], [True, True, False])
        self.assertEqual(data["parsed_id"], "node-1")
        self.assertEqual(data["parsed_action"], "status")
        self.assertTrue(data["has_head"])

    def test_confirmation_modal_uses_text_nodes_and_delivery_is_confirmed(self):
        source = USERSCRIPT_PATH.read_text(encoding="utf-8")
        modal = source[
            source.index("function showEffectfulConfirmationModal"):
            source.index("function submitActionResult")
        ]
        self.assertNotIn(".innerHTML", modal)
        self.assertIn("content.textContent", modal)
        submit = source[
            source.index("function submitActionResult"):
            source.index("var actionScanActive")
        ]
        self.assertIn("isActionResultInConversation(requestId)", submit)
        self.assertIn("resolve(false)", submit)


class BrowserControlSecurityTests(unittest.TestCase):
    """Test capability minting, validation, scope isolation, and revocation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir)
        (self.runtime_root / "control").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mint_and_validate_capability(self):
        cap = create_browser_control_capability(
            project_id="devorchestrator",
            allowed_actions=["status", "commands_catalog"],
            label="test-token",
            ttl_seconds=3600,
            runtime_root=self.runtime_root,
        )
        self.assertTrue(cap["token"].startswith("bc_"))
        self.assertTrue(cap["capability_id"].startswith("bc-"))
        token = cap["token"]

        # Valid action and project
        ok, reason, row = validate_browser_control_capability(
            f"Bearer {token}",
            project_id="devorchestrator",
            action="status",
            runtime_root=self.runtime_root,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "valid")
        self.assertIsNotNone(row)

        # Disallowed action
        ok, reason, _ = validate_browser_control_capability(
            f"Bearer {token}",
            project_id="devorchestrator",
            action="start_task",
            runtime_root=self.runtime_root,
        )
        self.assertFalse(ok)
        self.assertIn("action_not_allowed", reason)

        # Disallowed project
        ok, reason, _ = validate_browser_control_capability(
            f"Bearer {token}",
            project_id="other_project",
            action="status",
            runtime_root=self.runtime_root,
        )
        self.assertFalse(ok)
        self.assertIn("project_mismatch", reason)

    def test_revocation(self):
        cap = create_browser_control_capability(
            project_id="devorchestrator",
            label="revoke-me",
            runtime_root=self.runtime_root,
        )
        token = cap["token"]
        cap_id = cap["capability_id"]

        ok, _, _ = validate_browser_control_capability(f"Bearer {token}", runtime_root=self.runtime_root)
        self.assertTrue(ok)

        # Revoke
        rev = revoke_browser_control_capability(cap_id, runtime_root=self.runtime_root)
        self.assertTrue(rev.get("revoked"))

        # Now rejected
        ok, reason, _ = validate_browser_control_capability(f"Bearer {token}", runtime_root=self.runtime_root)
        self.assertFalse(ok)
        self.assertEqual(reason, "token_revoked")

    def test_scope_isolation_mcp_and_heartbeat_tokens_cannot_act_as_browser_control(self):
        # Mint MCP token
        mcp_cap = create_mcp_capability(
            label="mcp-only",
            runtime_root=self.runtime_root,
        )
        mcp_token = mcp_cap["token"]

        # MCP token must NOT validate as browser control capability
        ok, reason, _ = validate_browser_control_capability(
            f"Bearer {mcp_token}",
            project_id="devorchestrator",
            action="status",
            runtime_root=self.runtime_root,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_or_revoked_token")


class BrowserControlServiceTests(unittest.TestCase):
    """Test execution service, confirmation gating, idempotency, and isolation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir)
        (self.runtime_root / "control").mkdir(parents=True, exist_ok=True)
        (self.runtime_root / "projects").mkdir(parents=True, exist_ok=True)
        (self.runtime_root / "bridge").mkdir(parents=True, exist_ok=True)

        write_json(self.runtime_root / "daemon.json", {
            "state": "running",
            "pid": 12345,
            "listen_address": "127.0.0.1",
            "port": 8770,
            "last_tick_at": "2026-09-30T00:00:00+00:00",
            "last_error": None,
        })
        write_json(self.runtime_root / "projects" / "devorchestrator.json", {
            "project_id": "devorchestrator",
            "name": "DevOrchestrator",
            "repo_path": str(self.runtime_root),
            "state": "READY_TO_RUN",
            "lifecycle_state": "COMPLETE",
        })

        import sys
        py = sys.executable
        # Setup test execution-jobs.json
        jobs_cfg = {
            "version": 1,
            "enabled": True,
            "runtime_root": str(self.runtime_root),
            "projects": {
                "devorchestrator": {
                    "project_id": "devorchestrator",
                    "repo_path": str(self.runtime_root),
                    "file_roots": [str(self.runtime_root)],
                    "commands": {
                        "echo_test": {
                            "command_ref": "echo_test",
                            "effect_class": "read_only",
                            "description": "Echo test command",
                            "argv": [py, "-c", "print('hello')"],
                            "timeout_seconds": 30,
                            "parameters": {"count": {"type": "integer"}},
                        },
                        "hardware_flash": {
                            "command_ref": "hardware_flash",
                            "effect_class": "hardware",
                            "description": "Flash hardware device",
                            "argv": ["echo", "flashing"],
                            "timeout_seconds": 60,
                        },
                    },
                }
            },
        }
        write_json(self.runtime_root / "execution-jobs.json", jobs_cfg)

        self.bridge_store = BrowserBridgeStore(self.runtime_root / "bridge", require_live_binding=False)
        self.service = BrowserControlService(
            runtime_root=self.runtime_root,
            bridge_store=self.bridge_store,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_commands_catalog_marks_hardware_non_selectable(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-cat-1",
            "action": "commands_catalog",
            "project_id": "devorchestrator",
        }
        res = self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertEqual(res["status"], "success")
        cmds = res["data"]["commands"]
        by_ref = {c["command_ref"]: c for c in cmds}
        self.assertIn("echo_test", by_ref)
        self.assertIn("hardware_flash", by_ref)

        self.assertTrue(by_ref["echo_test"]["selectable"])
        self.assertFalse(by_ref["echo_test"]["is_hardware"])

        self.assertFalse(by_ref["hardware_flash"]["selectable"])
        self.assertTrue(by_ref["hardware_flash"]["is_hardware"])

    def test_read_file_path_traversal_fails_closed(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-rf-1",
            "action": "read_file",
            "project_id": "devorchestrator",
            "parameters": {"path": "../../../../../etc/passwd"},
        }
        with self.assertRaises(BrowserControlError) as ctx:
            self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertIn(ctx.exception.status_code, (400, 403, 404))

    def test_effectful_action_requires_confirmation(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-spawn-1",
            "action": "start_task",
            "project_id": "devorchestrator",
            "parameters": {"command_ref": "echo_test"},
            "confirmed": False,
        }
        with self.assertRaises(BrowserControlConfirmationRequiredError) as ctx:
            self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.action, "start_task")

    def test_confirmed_effectful_action_and_idempotency(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-spawn-2",
            "action": "start_task",
            "project_id": "devorchestrator",
            "parameters": {"command_ref": "echo_test", "parameters": {"count": 3}},
            "confirmed": True,
        }
        res1 = self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertEqual(res1["status"], "success")
        job_id = res1["data"].get("job_id") or res1["data"].get("operation_id")
        self.assertIsNotNone(job_id)

        # Exact replay returns cached response
        res2 = self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertEqual(res2["status"], "success")
        job_id_2 = res2["data"].get("job_id") or res2["data"].get("operation_id")
        self.assertEqual(job_id, job_id_2)

        # Mismatched payload with same request_id raises Conflict 409
        conflicting_req = dict(req)
        conflicting_req["parameters"] = {"command_ref": "echo_test", "parameters": {"count": 5}}
        with self.assertRaises(BrowserControlConflictError):
            self.service.execute_action(conflicting_req, host_id="test", capability_id="test-cap")

    def test_concurrent_replay_dispatches_only_once(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-concurrent-1",
            "action": "start_task",
            "project_id": "devorchestrator",
            "parameters": {"command_ref": "echo_test"},
            "confirmed": True,
        }
        calls = 0
        calls_lock = threading.Lock()

        def fake_dispatch(*_args):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.15)
            return {"job_id": "job-single", "selected_transport": "local"}

        self.service._dispatch = fake_dispatch
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self.service.execute_action(req, host_id="test", capability_id="test-cap"),
                range(2),
            ))

        self.assertEqual(calls, 1)
        self.assertEqual([row["data"]["job_id"] for row in results], ["job-single", "job-single"])

    def test_failed_or_corrupt_claim_fails_closed_on_replay(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-failed-1",
            "action": "start_task",
            "project_id": "devorchestrator",
            "parameters": {"command_ref": "echo_test"},
            "confirmed": True,
        }
        calls = 0

        def failing_dispatch(*_args):
            nonlocal calls
            calls += 1
            raise RuntimeError("ambiguous transport failure")

        self.service._dispatch = failing_dispatch
        with self.assertRaises(BrowserControlError):
            self.service.execute_action(req, host_id="test", capability_id="test-cap")
        with self.assertRaises(BrowserControlConflictError):
            self.service.execute_action(req, host_id="test", capability_id="test-cap")
        self.assertEqual(calls, 1)

        corrupt_id = "req-corrupt-1"
        self.service.store._file_path(corrupt_id).write_text("{not-json", encoding="utf-8")
        corrupt = dict(req, request_id=corrupt_id)
        with self.assertRaises(BrowserControlConflictError):
            self.service.execute_action(corrupt, host_id="test", capability_id="test-cap")

    def test_web_sol_channel_isolation(self):
        # Executing browser control action must NOT enqueue anything in BrowserBridgeStore
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-iso-1",
            "action": "status",
            "project_id": "devorchestrator",
        }
        self.service.execute_action(req, host_id="test", capability_id="test-cap")
        # Bridge queue must be completely empty
        claim = self.bridge_store.claim("chatgpt_web", "test-binding")
        self.assertIsNone(claim)


class BridgeHTTPServerIntegrationTests(unittest.TestCase):
    """Test Bridge HTTP server on loopback port 8765 handling control action routes."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir)
        (self.runtime_root / "control").mkdir(parents=True, exist_ok=True)
        (self.runtime_root / "bridge").mkdir(parents=True, exist_ok=True)

        self.bridge_store = BrowserBridgeStore(self.runtime_root / "bridge", require_live_binding=False)
        self.server = make_bridge_server(
            "127.0.0.1",
            0,
            self.bridge_store,
            runtime_root=self.runtime_root,
        )
        import threading
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.port = self.server.server_address[1]

        # Mint capability token
        self.cap = create_browser_control_capability(
            project_id="devorchestrator",
            label="http-test",
            runtime_root=self.runtime_root,
        )
        self.token = self.cap["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _request(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = headers or {}
        data = None
        if body is not None:
            if isinstance(body, dict):
                data = json.dumps(body).encode("utf-8")
                h.setdefault("Content-Type", "application/json")
            elif isinstance(body, (bytes, str)):
                data = body.encode("utf-8") if isinstance(body, str) else body
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        resp_body = resp.read().decode("utf-8")
        conn.close()
        return resp.status, resp_headers, resp_body

    def test_options_preflight_cors(self):
        status, headers, _ = self._request(
            "OPTIONS",
            "/v1/control/action",
            headers={"Origin": "https://chatgpt.com"},
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("access-control-allow-origin"), "https://chatgpt.com")
        self.assertIn("POST", headers.get("access-control-allow-methods", ""))
        self.assertIn("Authorization", headers.get("access-control-allow-headers", ""))

        # Disallowed origin
        status, _, _ = self._request(
            "OPTIONS",
            "/v1/control/action",
            headers={"Origin": "https://attacker.site"},
        )
        self.assertEqual(status, 403)

        # Prefix/suffix spoofing must not be treated as the ChatGPT origin.
        status, headers, _ = self._request(
            "OPTIONS",
            "/v1/control/action",
            headers={"Origin": "https://chatgpt.com.attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", headers)

    def test_control_origin_and_peer_helpers_are_exact(self):
        self.assertTrue(_is_allowed_control_origin("https://chatgpt.com"))
        self.assertTrue(_is_allowed_control_origin("https://chatgpt.com:443"))
        self.assertTrue(_is_allowed_control_origin("http://127.0.0.1:8765"))
        self.assertTrue(_is_allowed_control_origin("http://[::1]:8765"))
        self.assertFalse(_is_allowed_control_origin("https://chatgpt.com.attacker.example"))
        self.assertFalse(_is_allowed_control_origin("http://localhost.attacker.example"))
        self.assertFalse(_is_allowed_control_origin("https://chatgpt.com/path"))
        self.assertTrue(_is_loopback_address("127.0.0.1"))
        self.assertTrue(_is_loopback_address("::1"))
        self.assertTrue(_is_loopback_address("::ffff:127.0.0.1"))
        self.assertFalse(_is_loopback_address("192.0.2.10"))

    def test_health_check(self):
        status, headers, body = self._request("GET", "/v1/control/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("service"), "browser_control_bridge")

    def test_post_action_unauthorized(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-http-1",
            "action": "status",
            "project_id": "devorchestrator",
        }
        status, _, body = self._request("POST", "/v1/control/action", body=req)
        self.assertEqual(status, 401)
        self.assertIn("unauthorized", body.lower())

    def test_post_action_read_only(self):
        req = {
            "protocol": DEVORCH_ACTION_V1,
            "request_id": "req-http-2",
            "action": "commands_catalog",
            "project_id": "devorchestrator",
        }
        status, headers, body = self._request(
            "POST",
            "/v1/control/action",
            body=req,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Origin": "https://chatgpt.com",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("access-control-allow-origin"), "https://chatgpt.com")
        data = json.loads(body)
        self.assertEqual(data.get("protocol"), DEVORCH_ACTION_RESULT_V1)
        self.assertEqual(data.get("status"), "success")
        self.assertEqual(data.get("request_id"), "req-http-2")


if __name__ == "__main__":
    unittest.main()
