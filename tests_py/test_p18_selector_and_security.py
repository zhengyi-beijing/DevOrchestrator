"""Tests for P18 transport selection, capability tokens, nonce replay protection, and MCP adapter tools."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_orchestrator.cli import main
from dev_orchestrator.control.mcp_adapter import MCPAdapter
from dev_orchestrator.control.security import (
    create_transport_capability,
    consume_request_nonce,
    revoke_transport_capability,
    validate_transport_capability,
)
from dev_orchestrator.transport.contracts import HostCapabilities
from dev_orchestrator.transport.hosts import (
    TransportHostProfile,
    TransportHostsConfig,
)
from dev_orchestrator.transport.selector import select_transport


class TestP18SelectorAndSecurity(unittest.TestCase):
    """Verifies deterministic selector rules, capability token lifecycles, and MCP/CLI integration."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

        self.hosts_cfg = TransportHostsConfig(
            hosts={
                "local": TransportHostProfile(
                    host_id="local",
                    candidate_order=["local"],
                    approved_policy_pins={"pinned_cmd": "sha256:correct_digest"},
                ),
                "remote-1": TransportHostProfile(
                    host_id="remote-1",
                    candidate_order=["ssh", "rdc"],
                    ssh={"peer": "192.168.1.50"},
                ),
                "disabled-node": TransportHostProfile(
                    host_id="disabled-node",
                    enabled=False,
                ),
            },
            default_host="local",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_selector_hardware_unconditional_rejection(self):
        """Commands with effect_class='hardware' are unconditionally rejected."""
        decision = select_transport(
            "exec",
            command_ref="hw_flash",
            effect_class="hardware",
            hosts_config=self.hosts_cfg,
        )
        self.assertEqual(decision.selected_transport, "rejected")
        self.assertEqual(decision.reason_code, "hardware_execution_not_supported_in_p18")

    def test_selector_policy_pin_enforcement(self):
        """Host profile approved policy pins reject unapproved policy digests."""
        # Matching pin succeeds
        dec_ok = select_transport(
            "exec",
            command_ref="pinned_cmd",
            policy_digest="sha256:correct_digest",
            hosts_config=self.hosts_cfg,
        )
        self.assertEqual(dec_ok.selected_transport, "local")

        # Mismatched pin fails closed
        dec_bad = select_transport(
            "exec",
            command_ref="pinned_cmd",
            policy_digest="sha256:tampered_digest",
            hosts_config=self.hosts_cfg,
        )
        self.assertEqual(dec_bad.selected_transport, "rejected")
        self.assertEqual(dec_bad.reason_code, "policy_pin_mismatch")

    def test_selector_read_only_vs_effectful_failover(self):
        """Only read_only operations may fail over; effectful commands return 'ambiguous'."""
        pre_fails = {"ssh": "connection_refused"}
        # read_only failover past ssh: candidate_order is ["ssh", "rdc"]
        # rdc is rejected automatically, so no native candidate remains -> rdc_fallback_required
        dec_ro = select_transport(
            "exec",
            target_host_id="remote-1",
            effect_class="read_only",
            hosts_config=self.hosts_cfg,
            pre_dispatch_failures=pre_fails,
        )
        self.assertEqual(dec_ro.selected_transport, "rdc_fallback_required")
        self.assertEqual(dec_ro.reason_code, "no_native_candidate_available")

        # effectful operation fails over -> immediately returns 'ambiguous' to prevent duplicate execution
        dec_mut = select_transport(
            "spawn",
            target_host_id="remote-1",
            effect_class="effectful",
            hosts_config=self.hosts_cfg,
            pre_dispatch_failures=pre_fails,
        )
        self.assertEqual(dec_mut.selected_transport, "ambiguous")
        self.assertEqual(dec_mut.reason_code, "effectful_failover_forbidden")

    def test_selector_unknown_or_disabled_host(self):
        """Unknown or disabled host triggers rdc_fallback_required."""
        dec_unk = select_transport("exec", target_host_id="ghost-host", hosts_config=self.hosts_cfg)
        self.assertEqual(dec_unk.selected_transport, "rdc_fallback_required")
        self.assertEqual(dec_unk.reason_code, "host_unknown_or_disabled")

        dec_dis = select_transport("exec", target_host_id="disabled-node", hosts_config=self.hosts_cfg)
        self.assertEqual(dec_dis.selected_transport, "rdc_fallback_required")
        self.assertEqual(dec_dis.reason_code, "host_unknown_or_disabled")

    def test_transport_capabilities_token_and_nonce_replay(self):
        """Capability token minting, validation, revocation, and nonce replay prevention."""
        cap_record = create_transport_capability(
            project_id="p1",
            host_id="local",
            allowed_operations=["exec"],
            ttl_seconds=60,
            runtime_root=self.root,
        )
        tok = cap_record["token"]
        cap_id = cap_record["capability_id"]

        # Valid capability check
        ok, reason, meta = validate_transport_capability(
            tok, project_id="p1", host_id="local", operation="exec", runtime_root=self.root
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "valid")

        # Operation mismatch fails
        ok_op, reason_op, _ = validate_transport_capability(
            tok, project_id="p1", host_id="local", operation="write_file", runtime_root=self.root
        )
        self.assertFalse(ok_op)
        self.assertIn("not_allowed", str(reason_op))

        # Single-use request nonce
        ok_nonce = consume_request_nonce(cap_id, "nonce-abc-123", runtime_root=self.root)
        self.assertTrue(ok_nonce)

        # Replaying exact same nonce is rejected
        ok_replay = consume_request_nonce(cap_id, "nonce-abc-123", runtime_root=self.root)
        self.assertFalse(ok_replay)

        # Revocation
        revoke_transport_capability(cap_id, runtime_root=self.root)
        ok_rev, reason_rev, _ = validate_transport_capability(
            tok, project_id="p1", host_id="local", operation="exec", runtime_root=self.root
        )
        self.assertFalse(ok_rev)
        self.assertIn("revoked", str(reason_rev))

    def test_mcp_adapter_transport_tools(self):
        """MCP adapter exposes transport tools only when explicitly enabled."""
        class MockClient:
            def transport_hosts(self):
                return {"default_host": "local", "hosts": {"local": {}}}
            def transport_capabilities(self, host_id=None):
                return {"host_id": host_id or "local", "jobs_config_valid": True}
            def transport_operations(self):
                return {"supported_operations": ["exec", "spawn"]}

        client = MockClient()
        adapter_default = MCPAdapter(client, enable_transport_tools=False)
        req_list = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        resp_def = json.loads(adapter_default.handle_message(json.dumps(req_list)))
        tool_names_def = [t["name"] for t in resp_def["result"]["tools"]]
        self.assertNotIn("devorch_transport_hosts", tool_names_def)
        self.assertNotIn("devorch_transport_capabilities", tool_names_def)
        self.assertNotIn("devorch_transport_operations", tool_names_def)

        adapter_enabled = MCPAdapter(client, enable_transport_tools=True)
        resp_en = json.loads(adapter_enabled.handle_message(json.dumps(req_list)))
        tool_names_en = [t["name"] for t in resp_en["result"]["tools"]]
        self.assertIn("devorch_transport_hosts", tool_names_en)
        self.assertIn("devorch_transport_capabilities", tool_names_en)
        self.assertIn("devorch_transport_operations", tool_names_en)

        # Execute devorch_transport_hosts
        req_call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "devorch_transport_hosts", "arguments": {}},
        }
        res_hosts = json.loads(adapter_enabled.handle_message(json.dumps(req_call)))
        self.assertFalse(res_hosts["result"]["isError"])
        self.assertIn("default_host", res_hosts["result"]["content"][0]["text"])

        # Execute devorch_transport_capabilities
        req_caps = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "devorch_transport_capabilities", "arguments": {"host_id": "local"}},
        }
        res_caps = json.loads(adapter_enabled.handle_message(json.dumps(req_caps)))
        self.assertFalse(res_caps["result"]["isError"])
        self.assertIn("jobs_config_valid", res_caps["result"]["content"][0]["text"])

    def test_cli_transport_hosts_and_capabilities(self):
        """CLI verbs 'transport-hosts' and 'transport-capabilities' succeed."""
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["transport-hosts", "--runtime-root", str(self.root)])
        self.assertEqual(code, 0)
        output = json.loads(buf.getvalue())
        self.assertIn("hosts", output)
        self.assertIn("local", output["hosts"])

        buf_caps = io.StringIO()
        with patch("sys.stdout", buf_caps):
            code_caps = main(["transport-capabilities", "--host-id", "local", "--runtime-root", str(self.root)])
        self.assertEqual(code_caps, 0)
        output_caps = json.loads(buf_caps.getvalue())
        self.assertEqual(output_caps["host_id"], "local")

    def _start_server(self):
        (self.root / "daemon.token").write_text("master-test-token\n", encoding="utf-8")
        cfg_file = self.root / "execution-jobs.json"
        from dev_orchestrator.storage.json_store import write_json, utc_now_iso
        write_json(
            cfg_file,
            {
                "runtime_root": str(self.root),
                "projects": {
                    "p1": {
                        "repo_path": str(self.root),
                        "file_roots": [str(self.root)],
                        "commands": {
                            "ro": {"argv": ["python", "-c", "import sys; sys.stdout.write('ok')"], "cwd": ".", "effect_class": "read_only"},
                            "mut": {"argv": ["python", "-c", "import sys; sys.stdout.write('mut_ok')"], "cwd": ".", "effect_class": "effectful"},
                        },
                    }
                },
            },
        )
        write_json(
            self.root / "transport-hosts.json",
            {
                "default_host": "local",
                "hosts": {
                    "local": {
                        "host_id": "local",
                        "candidate_order": ["local"],
                    }
                },
            },
        )
        from dev_orchestrator.web.server import DevOrchestratorHTTPServer
        import threading
        server = DevOrchestratorHTTPServer(
            ("127.0.0.1", 0),
            runtime_root=self.root,
            web_root=self.root,
            listen_label="127.0.0.1",
            started_at=utc_now_iso(),
            enable_control=True,
            config_path=cfg_file,
        )
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()
        return server

    def test_transport_http_routes_enforce_capability_scope_and_nonce_replay(self):
        """HTTP transport routes enforce capability scope and reject missing/replayed nonces with 403."""
        import urllib.request
        import urllib.error

        server = self._start_server()
        port = server.server_address[1]
        try:
            cap = create_transport_capability(
                project_id="p1",
                host_id="local",
                allowed_operations=["exec"],
                runtime_root=self.root,
            )
            tok = cap["token"]

            req_data = json.dumps({
                "project_id": "p1",
                "command_ref": "ro",
                "host_id": "local",
            }).encode("utf-8")

            # 1. Valid request with nonce succeeds
            headers_ok = {
                "Authorization": f"Bearer {tok}",
                "X-DevOrch-Nonce": "nonce-fresh-1",
                "Content-Type": "application/json",
            }
            req_ok = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/exec",
                data=req_data,
                headers=headers_ok,
            )
            with urllib.request.urlopen(req_ok) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(body["data"]["status"], "ok")

            # 2. Replay of same nonce is rejected with 403
            req_replay = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/exec",
                data=req_data,
                headers=headers_ok,
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_replay)
            self.assertEqual(ctx.exception.code, 403)

            # 3. Missing nonce is rejected with 403
            headers_no_nonce = {
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
            }
            req_no_nonce = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/exec",
                data=req_data,
                headers=headers_no_nonce,
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_no_nonce)
            self.assertEqual(ctx.exception.code, 403)

            # 4. Out of scope operation (spawn on exec-only token) is rejected with 403
            headers_spawn = {
                "Authorization": f"Bearer {tok}",
                "X-DevOrch-Nonce": "nonce-fresh-2",
                "Content-Type": "application/json",
            }
            req_spawn = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/spawn",
                data=req_data,
                headers=headers_spawn,
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_spawn)
            self.assertEqual(ctx.exception.code, 403)
        finally:
            server.shutdown()

    def test_transport_spawn_route_logs_operation_and_returns_success(self):
        """POST /api/v1/control/transport/spawn logs operation and returns 202."""
        import urllib.request

        server = self._start_server()
        port = server.server_address[1]
        try:
            req_data = json.dumps({
                "project_id": "p1",
                "command_ref": "mut",
                "host_id": "local",
            }).encode("utf-8")
            headers = {
                "Authorization": f"Bearer {server.control_security.token()}",
                "Content-Type": "application/json",
            }
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/spawn",
                data=req_data,
                headers=headers,
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 202)
                body = json.loads(resp.read().decode("utf-8"))
                self.assertIn(body["data"]["status"], ("ok", "queued", "running"))
                self.assertIsNotNone(body["data"]["job_id"])

            log_file = self.root / "logs" / "transport-operations.ndjson"
            self.assertTrue(log_file.is_file())
            content = log_file.read_text(encoding="utf-8")
            self.assertIn('"operation": "spawn"', content)
            self.assertIn('"command_ref": "mut"', content)
        finally:
            server.shutdown()

    def test_transport_operations_route_returns_logged_rows(self):
        """GET /api/v1/control/transport/operations returns logged rows via read_transport_operations."""
        import urllib.request
        from dev_orchestrator.transport.observability import log_transport_operation

        log_transport_operation(
            self.root,
            operation_id="op-audit-12345",
            operation="exec",
            host_id="local",
            selected_transport="local",
            status="ok",
            command_ref="ro",
        )

        server = self._start_server()
        port = server.server_address[1]
        try:
            headers = {
                "Authorization": f"Bearer {server.control_security.token()}",
            }
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/operations?limit=10",
                headers=headers,
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode("utf-8"))
                ops = body["data"]["operations"]
                self.assertTrue(any(row.get("operation_id") == "op-audit-12345" for row in ops))
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
