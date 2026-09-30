from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.control.mcp_http import MCP_TOOLS
from dev_orchestrator.control.security import (
    ControlSecurity,
    create_mcp_capability,
    list_mcp_capabilities,
    revoke_mcp_capability,
)
from dev_orchestrator.storage.json_store import write_json
from dev_orchestrator.web.server import make_server


ROOT = Path(__file__).resolve().parents[1]


class P19_2RemoteMCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.runtime = self.base / "runtime"
        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True)
        self.runtime.mkdir(parents=True)
        (self.runtime / "projects").mkdir()
        (self.runtime / "history").mkdir()

        (self.repo / "inside.bin").write_bytes(b"inside\x00bytes")
        (self.repo / "hello.txt").write_bytes(b"Hello World\n")
        self.outside = self.base / "outside.bin"
        self.outside.write_bytes(b"outside")

        self.project = {
            "project_id": "p19_test",
            "id": "p19_test",
            "name": "P19 Remote MCP Test",
            "repo_path": str(self.repo),
            "root": str(self.repo),
            "state": "READY_TO_RUN",
            "lifecycle_state": "COMPLETE",
            "git": {"branch": "main", "head": "abc123", "dirty": False, "changed_entries": 0},
            "telemetry": {"task_id": "P19.2"},
            "active_execution": None,
            "active_roles": [],
            "gate": None,
            "last_activity_at": "2026-09-29T00:00:00+00:00",
            "activity": {"active_role": None, "disposition": "terminal_success"},
            "authoritative_lifecycle": {
                "current_task_id": "P19.2",
                "lifecycle_state": "COMPLETE",
                "active_owner": None,
                "owner_gate": None,
                "invariants": [{"code": "REMOTE_MCP_INTERFACE", "holds": True}],
            },
        }
        write_json(self.runtime / "summary.json", {"projects": [self.project], "project_count": 1})
        write_json(self.runtime / "projects" / "p19_test.json", self.project)
        write_json(self.runtime / "daemon.json", {
            "state": "running",
            "pid": os.getpid(),
            "listen_address": "127.0.0.1",
            "port": 0,
            "last_tick_at": "2026-09-29T00:00:00+00:00",
            "last_error": None,
        })
        self.config = self.base / "projects.json"
        write_json(self.config, {"projects": [{"project_id": "p19_test", "repo_path": str(self.repo)}]})

        py = sys.executable
        write_json(self.runtime / "execution-jobs.json", {
            "enabled": True,
            "runtime_root": str(self.runtime),
            "max_concurrent_jobs": 2,
            "log_caps": {"max_line_bytes": 128, "max_job_bytes": 256, "head_lines": 10, "tail_lines": 10},
            "projects": {
                "p19_test": {
                    "repo_path": str(self.repo),
                    "file_roots": [str(self.repo)],
                    "commands": {
                        "short_job": {
                            "argv": [py, "-c", "print('job-output-success')"],
                            "cwd": ".",
                            "effect_class": "idempotent",
                            "max_runtime_seconds": 10,
                        },
                        "long_job": {
                            "argv": [py, "-c", "import time; time.sleep(10)"],
                            "cwd": ".",
                            "effect_class": "idempotent",
                            "max_runtime_seconds": 20,
                        },
                    },
                }
            },
        })

        self._start_server()

    def _start_server(self) -> None:
        self.server = make_server(
            "127.0.0.1", 0, self.runtime, ROOT / "web",
            enable_control=True, config_path=self.config,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.mcp_url = f"http://127.0.0.1:{self.port}/mcp"
        self.master_token = self.server.control_security.token()

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def tearDown(self) -> None:
        self._stop_server()
        self.temp.cleanup()

    def _rpc_request(
        self,
        payload: Optional[dict[str, Any]],
        *,
        token: Optional[str] = None,
        session_id: Optional[str] = None,
        accept: str = "application/json",
        origin: Optional[str] = None,
        method: str = "POST",
        raw_body: Optional[bytes] = None,
    ) -> tuple[int, dict[str, str], Any]:
        req_token = self.master_token if token is None else token
        body = raw_body if raw_body is not None else (json.dumps(payload).encode("utf-8") if payload is not None else b"")
        req = urllib.request.Request(self.mcp_url, data=body if method == "POST" else None, method=method)
        if req_token:
            req.add_header("Authorization", f"Bearer {req_token}")
        if session_id:
            req.add_header("Mcp-Session-Id", session_id)
        if accept:
            req.add_header("Accept", accept)
        if origin is not None:
            req.add_header("Origin", origin)

        headers: dict[str, str] = {}
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                for k, v in resp.headers.items():
                    headers[k] = v
                raw_resp = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            for k, v in exc.headers.items():
                headers[k] = v
            raw_resp = exc.read()

        parsed: Any = None
        if raw_resp:
            ctype = headers.get("Content-Type", "")
            if "text/event-stream" in ctype:
                parsed = raw_resp.decode("utf-8")
            else:
                try:
                    parsed = json.loads(raw_resp.decode("utf-8"))
                except Exception:
                    parsed = raw_resp.decode("utf-8", errors="replace")
        return status, headers, parsed

    def _init_session(self, token: Optional[str] = None, proto_ver: str = "2025-06-18") -> tuple[str, str]:
        status, headers, resp = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": proto_ver},
            },
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertIn("Mcp-Session-Id", headers)
        session_id = headers["Mcp-Session-Id"]
        negotiated_proto = headers.get("MCP-Protocol-Version") or resp["result"]["protocolVersion"]

        # Send notifications/initialized
        n_status, _, _ = self._rpc_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            token=token,
            session_id=session_id,
        )
        self.assertEqual(n_status, 202)
        return session_id, negotiated_proto

    def test_discovery_and_schema_conformance(self) -> None:
        session_id, _ = self._init_session()
        status, _, resp = self._rpc_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_id=session_id,
        )
        self.assertEqual(status, 200)
        tools = resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        expected = ["devo_status", "devo_start_task", "devo_job_status", "devo_cancel_job", "devo_read_log", "devo_read_file"]
        self.assertEqual(sorted(tool_names), sorted(expected))

        # Schema match with MCP_TOOLS
        mcp_tools_by_name = {t["name"]: t for t in MCP_TOOLS}
        for tool in tools:
            name = tool["name"]
            self.assertIn(name, mcp_tools_by_name)
            self.assertEqual(tool["inputSchema"], mcp_tools_by_name[name]["inputSchema"])

        # Strictly absent tools: raw execution, shell, deletion, writing
        forbidden_substrings = ["shell", "exec", "write", "delete", "stage_write", "raw_exec"]
        for f in forbidden_substrings:
            self.assertNotIn(f, tool_names)

    def test_protocol_version_negotiation(self) -> None:
        # Latest supported version
        _, proto1 = self._init_session(proto_ver="2025-06-18")
        self.assertEqual(proto1, "2025-06-18")

        # Prior supported version
        _, proto2 = self._init_session(proto_ver="2025-03-26")
        self.assertEqual(proto2, "2025-03-26")

        # Baseline supported version
        _, proto3 = self._init_session(proto_ver="2024-11-05")
        self.assertEqual(proto3, "2024-11-05")

        # Unsupported version falls back safely to latest
        _, proto_fallback = self._init_session(proto_ver="1999-01-01")
        self.assertEqual(proto_fallback, "2025-06-18")

    def test_session_lifecycle_and_termination(self) -> None:
        session_id, _ = self._init_session()

        # Non-initialize call without Mcp-Session-Id must fail with 404
        status, _, err = self._rpc_request(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
            session_id=None,
        )
        self.assertEqual(status, 404)
        self.assertEqual(err.get("error"), "Session Required")

        # DELETE /mcp terminates session
        del_status, _, del_resp = self._rpc_request(None, session_id=session_id, method="DELETE")
        self.assertEqual(del_status, 200)
        self.assertTrue(del_resp.get("deleted"))

        # Subsequent call with deleted session ID fails with session error (404)
        status2, _, err2 = self._rpc_request(
            {"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}},
            session_id=session_id,
        )
        self.assertEqual(status2, 404)
        self.assertIn("Session", err2.get("error", ""))

    def test_auth_matrix_and_token_secrecy(self) -> None:
        # Missing auth header -> 401
        status, _, _ = self._rpc_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            token="",
        )
        self.assertEqual(status, 401)

        # Invalid token -> 401
        status, _, _ = self._rpc_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            token="invalid-token",
        )
        self.assertEqual(status, 401)

        # Valid capability token -> 200
        cap = create_mcp_capability(label="test-scoped", ttl_seconds=3600, runtime_root=self.runtime)
        cap_token = cap["token"]
        session_id, _ = self._init_session(token=cap_token)
        self.assertTrue(session_id)

        # Revoked capability token -> 401
        revoke_mcp_capability(cap["capability_id"], runtime_root=self.runtime)
        status, _, _ = self._rpc_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            token=cap_token,
            session_id=session_id,
        )
        self.assertEqual(status, 401)

        # Disallowed cross-site Origin header -> 403
        status, _, _ = self._rpc_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            token=self.master_token,
            origin="https://malicious-website.example.com",
        )
        self.assertEqual(status, 403)

        # GET /mcp -> 405 Method Not Allowed with Allow: POST, DELETE
        req = urllib.request.Request(self.mcp_url, method="GET")
        req.add_header("Authorization", f"Bearer {self.master_token}")
        try:
            with urllib.request.urlopen(req) as resp:
                get_code = resp.status
                allow = resp.headers.get("Allow")
        except urllib.error.HTTPError as exc:
            get_code = exc.code
            allow = exc.headers.get("Allow")
        self.assertEqual(get_code, 405)
        self.assertIn("POST", allow)
        self.assertIn("DELETE", allow)

        # Token secrecy in audit logs and output
        audit_file = self.runtime / "control" / "audit.jsonl"
        if audit_file.exists():
            content = audit_file.read_text(encoding="utf-8")
            self.assertNotIn(self.master_token, content)
            self.assertNotIn(cap_token, content)

    def test_devo_status_authoritative(self) -> None:
        session_id, _ = self._init_session()

        # 1. Global status without project_id
        status, _, resp = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {"name": "devo_status", "arguments": {}},
            },
            session_id=session_id,
        )
        self.assertEqual(status, 200)
        tool_res = resp["result"]["content"][0]["text"]
        data = json.loads(tool_res)
        self.assertTrue(data["daemon"]["process_alive"])
        proj = data.get("project") or (data["projects"][0] if data.get("projects") else None)
        self.assertIsNotNone(proj)
        self.assertEqual(proj["project_id"], "p19_test")

        # 2. Scoped status with project_id
        status, _, resp = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {"name": "devo_status", "arguments": {"project_id": "p19_test"}},
            },
            session_id=session_id,
        )
        self.assertEqual(status, 200)
        data = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(data["project"]["project_id"], "p19_test")
        self.assertEqual(data["authoritative_lifecycle"]["lifecycle_state"], "COMPLETE")
        self.assertEqual(data["git"]["branch"], "main")

        # 3. Unknown project returns clean failure without crash
        status, _, resp = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {"name": "devo_status", "arguments": {"project_id": "nonexistent"}},
            },
            session_id=session_id,
        )
        self.assertEqual(status, 200)
        self.assertTrue(resp["result"].get("isError"))
        err_data = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(err_data.get("status"), "failed")

    def test_devo_start_task_and_job_status_and_server_recovery(self) -> None:
        session_id, _ = self._init_session()

        # Start task
        status, _, resp = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 30,
                "method": "tools/call",
                "params": {
                    "name": "devo_start_task",
                    "arguments": {
                        "project_id": "p19_test",
                        "command_ref": "short_job",
                        "idempotency_key": "task-spawn-1",
                    },
                },
            },
            session_id=session_id,
        )
        self.assertEqual(status, 200)
        res_data = json.loads(resp["result"]["content"][0]["text"])
        job_id = res_data.get("job_id") or res_data.get("operation_id")
        self.assertTrue(job_id)
        self.assertEqual(res_data.get("selected_transport"), "local")

        # Poll until complete
        completed = False
        final_data = None
        for _ in range(50):
            p_status, _, p_resp = self._rpc_request(
                {
                    "jsonrpc": "2.0",
                    "id": 31,
                    "method": "tools/call",
                    "params": {
                        "name": "devo_job_status",
                        "arguments": {"project_id": "p19_test", "job_id": job_id},
                    },
                },
                session_id=session_id,
            )
            self.assertEqual(p_status, 200)
            final_data = json.loads(p_resp["result"]["content"][0]["text"])
            if final_data.get("status") in ("completed", "failed"):
                completed = True
                break
            time.sleep(0.05)

        self.assertTrue(completed)
        self.assertEqual(final_data.get("status"), "completed")
        self.assertEqual(final_data.get("exit_code"), 0)

        # Server restart durable job recovery test:
        # Recreate server against the same runtime root
        self._stop_server()
        self._start_server()

        # Initialize fresh session on restarted server
        new_session_id, _ = self._init_session()
        r_status, _, r_resp = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 32,
                "method": "tools/call",
                "params": {
                    "name": "devo_job_status",
                    "arguments": {"project_id": "p19_test", "job_id": job_id},
                },
            },
            session_id=new_session_id,
        )
        self.assertEqual(r_status, 200)
        recovered_data = json.loads(r_resp["result"]["content"][0]["text"])
        self.assertEqual(recovered_data.get("status"), "completed")
        self.assertEqual(recovered_data.get("exit_code"), 0)

    def test_idempotency_key_replay(self) -> None:
        session_id, _ = self._init_session()

        args = {
            "project_id": "p19_test",
            "command_ref": "short_job",
            "idempotency_key": "fixed-idempotency-key",
        }
        # First spawn
        status1, _, resp1 = self._rpc_request(
            {"jsonrpc": "2.0", "id": 40, "method": "tools/call", "params": {"name": "devo_start_task", "arguments": args}},
            session_id=session_id,
        )
        self.assertEqual(status1, 200)
        res1 = json.loads(resp1["result"]["content"][0]["text"])
        job_id_1 = res1.get("job_id") or res1.get("operation_id")

        # Second spawn with same idempotency key
        status2, _, resp2 = self._rpc_request(
            {"jsonrpc": "2.0", "id": 41, "method": "tools/call", "params": {"name": "devo_start_task", "arguments": args}},
            session_id=session_id,
        )
        self.assertEqual(status2, 200)
        res2 = json.loads(resp2["result"]["content"][0]["text"])
        job_id_2 = res2.get("job_id") or res2.get("operation_id")

        self.assertEqual(job_id_1, job_id_2)

    def test_scoped_devo_cancel_job(self) -> None:
        session_id, _ = self._init_session()

        status, _, resp = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 50,
                "method": "tools/call",
                "params": {
                    "name": "devo_start_task",
                    "arguments": {
                        "project_id": "p19_test",
                        "command_ref": "long_job",
                        "idempotency_key": "cancel-task-1",
                    },
                },
            },
            session_id=session_id,
        )
        self.assertEqual(status, 200)
        res = json.loads(resp["result"]["content"][0]["text"])
        job_id = res.get("job_id") or res.get("operation_id")

        # Cancel targeted job
        c_status, _, c_resp = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 51,
                "method": "tools/call",
                "params": {
                    "name": "devo_cancel_job",
                    "arguments": {"project_id": "p19_test", "job_id": job_id, "reason": "testing cancellation"},
                },
            },
            session_id=session_id,
        )
        self.assertEqual(c_status, 200)
        c_res = json.loads(c_resp["result"]["content"][0]["text"])
        self.assertEqual(c_res.get("status"), "cancelled")

    def test_devo_read_log_filtering_and_clamping(self) -> None:
        session_id, _ = self._init_session()

        # Seed events.jsonl
        events_file = self.runtime / "history" / "events.jsonl"
        for i in range(15):
            with events_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"event_id": f"evt-{i}", "event": "test_event", "project_id": "p19_test"}) + "\n")

        # Read allowed source "events"
        status, _, resp = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 60,
                "method": "tools/call",
                "params": {
                    "name": "devo_read_log",
                    "arguments": {"source": "events", "limit": 10},
                },
            },
            session_id=session_id,
        )
        self.assertEqual(status, 200)
        data = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("items", data)
        self.assertEqual(len(data["items"]), 10)

        # Disallowed source rejected
        status2, _, resp2 = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 61,
                "method": "tools/call",
                "params": {
                    "name": "devo_read_log",
                    "arguments": {"source": "forbidden_file"},
                },
            },
            session_id=session_id,
        )
        self.assertEqual(status2, 200)
        self.assertTrue(resp2["result"].get("isError"))
        self.assertIn("not in allowed sources", resp2["result"]["content"][0]["text"])

    def test_devo_read_file_text_and_binary_and_containment(self) -> None:
        session_id, _ = self._init_session()

        # 1. Text file: is_text=True, content has utf-8 text
        status, _, resp = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 70,
                "method": "tools/call",
                "params": {
                    "name": "devo_read_file",
                    "arguments": {"project_id": "p19_test", "path": "hello.txt"},
                },
            },
            session_id=session_id,
        )
        self.assertEqual(status, 200)
        res = json.loads(resp["result"]["content"][0]["text"])
        self.assertTrue(res["is_text"])
        self.assertEqual(res["content"], "Hello World\n")
        self.assertEqual(res["selected_transport"], "local")

        # 2. Binary file with null bytes: is_text=False, encoding=base64
        status2, _, resp2 = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 71,
                "method": "tools/call",
                "params": {
                    "name": "devo_read_file",
                    "arguments": {"project_id": "p19_test", "path": "inside.bin"},
                },
            },
            session_id=session_id,
        )
        self.assertEqual(status2, 200)
        res2 = json.loads(resp2["result"]["content"][0]["text"])
        self.assertFalse(res2["is_text"])
        self.assertEqual(res2["encoding"], "base64")
        self.assertEqual(base64.b64decode(res2["content_base64"]), b"inside\x00bytes")

        # 3. Path traversal outside repo root rejected
        status3, _, resp3 = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": 72,
                "method": "tools/call",
                "params": {
                    "name": "devo_read_file",
                    "arguments": {"project_id": "p19_test", "path": "../outside.bin"},
                },
            },
            session_id=session_id,
        )
        self.assertEqual(status3, 200)
        self.assertTrue(resp3["result"].get("isError"))

    def test_content_negotiation_sse_and_json(self) -> None:
        session_id, _ = self._init_session()

        # SSE negotiation: Accept: text/event-stream
        status, headers, body = self._rpc_request(
            {"jsonrpc": "2.0", "id": 80, "method": "tools/list", "params": {}},
            session_id=session_id,
            accept="text/event-stream",
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        self.assertTrue(isinstance(body, str))
        self.assertTrue(body.startswith("event: message\ndata: {"))

        # JSON negotiation: Accept: application/json
        status2, headers2, body2 = self._rpc_request(
            {"jsonrpc": "2.0", "id": 81, "method": "tools/list", "params": {}},
            session_id=session_id,
            accept="application/json",
        )
        self.assertEqual(status2, 200)
        self.assertIn("application/json", headers2.get("Content-Type", ""))
        self.assertIsInstance(body2, dict)
        self.assertEqual(body2.get("jsonrpc"), "2.0")

    def test_cli_mcp_token_management(self) -> None:
        # Mint capability
        cap = create_mcp_capability(label="cli-test-token", ttl_seconds=600, runtime_root=self.runtime)
        cap_id = cap["capability_id"]
        token = cap["token"]
        self.assertTrue(cap_id.startswith("mcp-"))
        self.assertTrue(token.startswith("mcp_"))

        # List capabilities
        caps = list_mcp_capabilities(runtime_root=self.runtime)
        found = next((c for c in caps if c.get("capability_id") == cap_id), None)
        self.assertIsNotNone(found)
        self.assertEqual(found["label"], "cli-test-token")
        self.assertFalse(found["revoked"])

        # Revoke capability
        rev_res = revoke_mcp_capability(cap_id, runtime_root=self.runtime)
        self.assertTrue(rev_res["revoked"])

        # Check list shows revoked
        caps2 = list_mcp_capabilities(runtime_root=self.runtime)
        found2 = next((c for c in caps2 if c.get("capability_id") == cap_id), None)
        self.assertIsNotNone(found2)
        self.assertTrue(found2["revoked"])


if __name__ == "__main__":
    unittest.main()
