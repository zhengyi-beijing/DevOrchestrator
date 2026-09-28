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
        if not (self.root / "transport-hosts.json").exists():
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

    def test_transport_capabilities_route_success_and_404(self):
        """GET /api/v1/control/transport/capabilities returns capabilities on valid host and 404 on unknown host."""
        import urllib.request
        import urllib.error

        server = self._start_server()
        port = server.server_address[1]
        try:
            headers = {"Authorization": f"Bearer {server.control_security.token()}"}

            # 1. Success for local host
            req_ok = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/capabilities?host_id=local",
                headers=headers,
            )
            with urllib.request.urlopen(req_ok) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode("utf-8"))
                caps = body["data"]
                self.assertEqual(caps["host_id"], "local")
                self.assertIn("jobs_config_valid", caps)
                self.assertIn("supported_operations", caps)
                self.assertIn("approved_policy_pins", caps)
                self.assertIn("response_limits", caps)

            # 2. 404 for unknown host
            req_unknown = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/capabilities?host_id=unknown_host_xyz",
                headers=headers,
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_unknown)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()

    def test_owner_only_capability_mint_list_revoke(self):
        """Owner-only transport capability mint, list, and revoke HTTP endpoints."""
        import urllib.request
        import urllib.error

        server = self._start_server()
        port = server.server_address[1]
        try:
            owner_headers = {
                "Authorization": f"Bearer {server.control_security.token()}",
                "Content-Type": "application/json",
            }
            mint_data = json.dumps({
                "project_id": "p1",
                "host_id": "local",
                "allowed_operations": ["exec", "spawn"],
            }).encode("utf-8")

            # 1. Mint without auth -> 401
            req_no_auth = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport-capabilities",
                data=mint_data,
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_no_auth)
            self.assertEqual(ctx.exception.code, 401)

            # 2. Mint with owner auth -> 201
            req_mint = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport-capabilities",
                data=mint_data,
                headers=owner_headers,
            )
            with urllib.request.urlopen(req_mint) as resp:
                self.assertEqual(resp.status, 201)
                mint_body = json.loads(resp.read().decode("utf-8"))
                tok = mint_body["data"]["token"]
                cap_id = mint_body["data"]["capability_id"]
                self.assertTrue(tok)
                self.assertTrue(cap_id)

            # 3. List with owner auth -> 200
            req_list = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport-capabilities",
                headers={"Authorization": f"Bearer {server.control_security.token()}"},
            )
            with urllib.request.urlopen(req_list) as resp:
                self.assertEqual(resp.status, 200)
                list_body = json.loads(resp.read().decode("utf-8"))
                caps_list = list_body["data"]["capabilities"]
                self.assertTrue(any(c.get("capability_id") == cap_id for c in caps_list))

            # 4. Revoke without owner auth -> 401
            req_rev_no_auth = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport-capabilities/{cap_id}/revoke",
                data=b"{}",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_rev_no_auth)
            self.assertEqual(ctx.exception.code, 401)

            # 5. Revoke with owner auth -> 200
            req_rev = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport-capabilities/{cap_id}/revoke",
                data=b"{}",
                headers=owner_headers,
            )
            with urllib.request.urlopen(req_rev) as resp:
                self.assertEqual(resp.status, 200)
                rev_body = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(rev_body["data"]["revoked"])

            # 6. Verify token is now revoked
            ok_val, reason, _ = validate_transport_capability(
                tok, project_id="p1", host_id="local", operation="exec", runtime_root=self.root
            )
            self.assertFalse(ok_val)
            self.assertIn("revoked", str(reason))
        finally:
            server.shutdown()

    def test_unlisted_transport_post_path_requires_owner_authorization(self):
        """Unlisted POST paths under /api/v1/control/transport require owner authorization (401)."""
        import urllib.request
        import urllib.error

        server = self._start_server()
        port = server.server_address[1]
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/custom-non-self-auth",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 401)
        finally:
            server.shutdown()

    def test_cli_transport_capability_verbs(self):
        """CLI verbs 'transport-capability-create', 'transport-capability-list', and 'transport-capability-revoke' succeed."""
        # 1. Create capability
        buf_create = io.StringIO()
        with patch("sys.stdout", buf_create):
            code_create = main([
                "transport-capability-create",
                "--project-id", "p1",
                "--host-id", "local",
                "--operations", "exec,spawn",
                "--runtime-root", str(self.root),
            ])
        self.assertEqual(code_create, 0)
        res_create = json.loads(buf_create.getvalue())
        cap_id = res_create["capability_id"]
        token = res_create["token"]
        self.assertTrue(cap_id)
        self.assertTrue(token)

        # 2. List capabilities
        buf_list = io.StringIO()
        with patch("sys.stdout", buf_list):
            code_list = main(["transport-capability-list", "--runtime-root", str(self.root)])
        self.assertEqual(code_list, 0)
        res_list = json.loads(buf_list.getvalue())
        self.assertTrue(any(c.get("capability_id") == cap_id for c in res_list.get("capabilities", [])))

        # 3. Revoke capability
        buf_revoke = io.StringIO()
        with patch("sys.stdout", buf_revoke):
            code_rev = main([
                "transport-capability-revoke",
                "--capability-id", cap_id,
                "--runtime-root", str(self.root),
            ])
        self.assertEqual(code_rev, 0)
        res_rev = json.loads(buf_revoke.getvalue())
        self.assertTrue(res_rev.get("revoked"))

        # 4. Verify revoked
        ok, reason, _ = validate_transport_capability(
            token, project_id="p1", host_id="local", operation="exec", runtime_root=self.root
        )
        self.assertFalse(ok)
        self.assertIn("revoked", str(reason))

    def test_transport_policy_pin_enforcement_via_server(self):
        """Server rejects exec request when policy digest mismatches host profile approved_policy_pins."""
        import urllib.request
        import urllib.error
        from dev_orchestrator.storage.json_store import write_json

        server = self._start_server()
        port = server.server_address[1]
        try:
            # Overwrite transport-hosts.json with a policy pin for 'ro' command that won't match
            write_json(
                self.root / "transport-hosts.json",
                {
                    "default_host": "local",
                    "hosts": {
                        "local": {
                            "host_id": "local",
                            "candidate_order": ["local"],
                            "approved_policy_pins": {"ro": "sha256:0000000000000000000000000000000000000000000000000000000000000000"},
                        }
                    },
                },
            )
            cap = create_transport_capability(
                project_id="p1",
                host_id="local",
                allowed_operations=["exec"],
                runtime_root=self.root,
            )
            req_data = json.dumps({
                "project_id": "p1",
                "command_ref": "ro",
                "host_id": "local",
            }).encode("utf-8")
            headers = {
                "Authorization": f"Bearer {cap['token']}",
                "X-DevOrch-Nonce": "nonce-pin-test-1",
                "Content-Type": "application/json",
            }
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/exec",
                data=req_data,
                headers=headers,
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertIn(ctx.exception.code, (400, 403))
        finally:
            server.shutdown()

    def test_transport_stat_logging_includes_metadata(self):
        """GET /api/v1/control/transport/stat logs operation with project_id and transport metadata."""
        import urllib.request

        server = self._start_server()
        port = server.server_address[1]
        try:
            headers = {"Authorization": f"Bearer {server.control_security.token()}"}
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/stat?project_id=p1&path=.",
                headers=headers,
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)

            log_file = self.root / "logs" / "transport-operations.ndjson"
            self.assertTrue(log_file.is_file())
            content = log_file.read_text(encoding="utf-8")
            self.assertIn('"operation": "stat"', content)
            self.assertIn('"project_id": "p1"', content)
            self.assertIn('"selected_transport": "local"', content)
        finally:
            server.shutdown()

    def test_host_capability_cache_remote_host_does_not_recurse(self):
        """HostCapabilityCache.get_capabilities does not infinitely recurse for remote hosts."""
        from unittest.mock import patch
        from dev_orchestrator.transport.hosts import (
            HostCapabilityCache,
            TransportHostProfile,
            TransportHostsConfig,
        )
        from dev_orchestrator.transport.contracts import HostCapabilities

        mock_caps = HostCapabilities(
            host_id="remote1",
            os_family="linux",
            path_style="posix",
            helper_version="1.0.0",
            jobs_config_valid=True,
            approved_policy_pins={},
            response_limits={"max_response_bytes": 1024, "max_file_write_bytes": 1024},
            supported_operations=["exec", "spawn", "capabilities"],
            probed_at="2026-09-28T00:00:00Z",
        )

        cfg = TransportHostsConfig(
            default_host="local",
            hosts={
                "remote1": TransportHostProfile(
                    host_id="remote1",
                    candidate_order=["ssh"],
                    ssh={"peer": "remote1.test"},
                )
            },
        )
        cache = HostCapabilityCache(runtime_root=self.root, hosts_config=cfg)

        with patch("dev_orchestrator.transport.ssh.SSHMachineTransport.capabilities", return_value=mock_caps) as mock_ssh_caps:
            caps = cache.get_capabilities("remote1")
            self.assertEqual(caps.host_id, "remote1")
            self.assertEqual(caps.os_family, "linux")
            mock_ssh_caps.assert_called_once_with("remote1")

        # Second call hits cache, doesn't call SSHMachineTransport again
        with patch("dev_orchestrator.transport.ssh.SSHMachineTransport.capabilities") as mock_ssh_caps2:
            cached_caps = cache.get_capabilities("remote1")
            self.assertEqual(cached_caps.host_id, "remote1")
            mock_ssh_caps2.assert_not_called()

    def test_spawn_route_compares_remote_reported_policy_digest_against_pin(self):
        """The spawn route compares remote reported policy digest against the host profile pin."""
        import urllib.request
        import urllib.error
        from unittest.mock import patch
        from dev_orchestrator.transport.contracts import MachineOperationResult
        from dev_orchestrator.storage.json_store import write_json

        pinned_digest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        different_digest = "sha256:2222222222222222222222222222222222222222222222222222222222222222"

        write_json(
            self.root / "transport-hosts.json",
            {
                "default_host": "local",
                "hosts": {
                    "remote1": {
                        "host_id": "remote1",
                        "candidate_order": ["ssh"],
                        "ssh": {"peer": "remote1.test"},
                        "approved_policy_pins": {"cmd_pin": pinned_digest},
                    }
                },
            },
        )

        cap = create_transport_capability(
            project_id="p1",
            host_id="remote1",
            allowed_operations=["spawn"],
            runtime_root=self.root,
        )

        server = self._start_server()
        port = server.server_address[1]
        try:
            # 1. When remote resolve reports mismatched policy digest, request fails closed with 400
            mismatched_resolve = {
                "status": "ok",
                "project_id": "p1",
                "command_ref": "cmd_pin",
                "effect_class": "effectful",
                "execution_policy_digest": different_digest,
                "resolution_digest": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
                "parameters_digest": None,
            }
            req_data = json.dumps({
                "project_id": "p1",
                "command_ref": "cmd_pin",
                "host_id": "remote1",
            }).encode("utf-8")
            headers = {
                "Authorization": f"Bearer {cap['token']}",
                "X-DevOrch-Nonce": "nonce-pin-test-spawn-1",
                "Content-Type": "application/json",
            }
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/spawn",
                data=req_data,
                headers=headers,
            )

            with patch("dev_orchestrator.transport.ssh.SSHMachineTransport.resolve", return_value=mismatched_resolve):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req)
                self.assertEqual(ctx.exception.code, 400)

            # 2. When remote resolve reports matching policy digest, request succeeds
            matching_resolve = {
                "status": "ok",
                "project_id": "p1",
                "command_ref": "cmd_pin",
                "effect_class": "effectful",
                "execution_policy_digest": pinned_digest,
                "resolution_digest": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
                "parameters_digest": None,
            }
            mock_spawn_res = MachineOperationResult(
                operation_id="remote-op-1",
                command_ref="cmd_pin",
                status="ok",
                job_id="job-remote-1",
                host_identity="remote1.test",
                execution_policy_digest=pinned_digest,
            )
            headers["X-DevOrch-Nonce"] = "nonce-pin-test-spawn-2"
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/control/transport/spawn",
                data=req_data,
                headers=headers,
            )
            with patch("dev_orchestrator.transport.ssh.SSHMachineTransport.resolve", return_value=matching_resolve), \
                 patch("dev_orchestrator.transport.ssh.SSHMachineTransport.spawn", return_value=mock_spawn_res):
                with urllib.request.urlopen(req2) as resp:
                    self.assertEqual(resp.status, 202)
                    body = json.loads(resp.read().decode("utf-8"))
                    data = body.get("data", body)
                    self.assertEqual(data.get("job_id"), "job-remote-1")
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
