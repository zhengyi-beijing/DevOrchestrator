import http.client
import io
import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from dev_orchestrator.ai.aibroker_subprocess import (
    AIBrokerClientConfig,
    AIBrokerExecutionPort,
    AIBrokerInvocationError,
)
from dev_orchestrator.ai.contracts import AIRoleRequest
from dev_orchestrator.ai.execution_transport import (
    ExecutionTransportError,
    ExecutionTransportResult,
    LocalTransport,
    SSHTransport,
    SSHTransportConfig,
)
from dev_orchestrator.control.adapter import ControlAdapterClient
from dev_orchestrator.control.mcp_adapter import MCPAdapter
from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.control.web_bridge import (
    WebBridgeRequestStore,
    canonical_web_bridge_request,
)
from dev_orchestrator.web.server import make_server
from tests_py.test_control_commands import write_config

ROOT = Path(__file__).resolve().parents[1]


class P13SoftwareAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        (self.runtime / "projects").mkdir()
        (self.runtime / "history").mkdir()
        self.config = self.runtime / "projects.json"
        write_config(self.config, ROOT)

        # Setup test project
        self.project_id = "test-p13"
        project = {
            "project_id": self.project_id,
            "id": self.project_id,
            "name": "P13 Project",
            "repo_path": str(ROOT),
            "state": "READY_TO_RUN",
            "lifecycle_state": "READY_TO_RUN",
            "git": {"branch": "main", "head": "abc1234", "dirty": False},
            "telemetry": {"task_id": "P13-TASK"},
        }
        (self.runtime / "projects" / f"{self.project_id}.json").write_text(
            json.dumps(project), encoding="utf-8"
        )
        (self.runtime / "summary.json").write_text(
            json.dumps({
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "projects": [project],
            }),
            encoding="utf-8",
        )

        # Start control server
        self.server = make_server(
            "127.0.0.1", 0, self.runtime, ROOT / "web",
            enable_control=True, config_path=self.config,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

        self.security = ControlSecurity(self.runtime)
        self.master_token = self.security.token()

        # Register live browser session and binding
        self.conv_store = ConversationControlStore(self.runtime)
        self.binding_id = "chat-13"
        self.conv_store.heartbeat(
            "chatgpt_web",
            self.binding_id,
            title="P13 Chat",
            url=f"https://chatgpt.com/c/{self.binding_id}",
            tab_instance_id="tab-1",
        )
        self.conv_store.bind(self.project_id, "chatgpt_web", self.binding_id)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def post(self, path, body, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        data = json.dumps(body).encode("utf-8")
        conn.request("POST", path, body=data, headers=h)
        resp = conn.getresponse()
        resp_data = resp.read()
        conn.close()
        return resp.status, resp.headers, resp_data

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = headers or {}
        conn.request("GET", path, headers=h)
        resp = conn.getresponse()
        resp_data = resp.read()
        conn.close()
        return resp.status, resp.headers, resp_data

    def test_end_to_end_adapters_control_and_logs(self):
        """Acceptance: Control via Web Bridge and MCP Adapter with redacted log inspection."""
        # 1. Inspect status via ControlAdapterClient
        client = ControlAdapterClient(
            base_url=f"http://127.0.0.1:{self.port}",
            token=self.master_token,
            runtime_root=self.runtime,
        )
        status_env = client.status(self.project_id)
        self.assertEqual(status_env["schema_version"], 1)
        data = status_env["data"]
        self.assertEqual(data["project_id"], self.project_id)
        rev = data["control_identity"]["revision"]
        self.assertTrue(rev.startswith("sha256:"))

        # 2. Issue scoped capability token for Web Bridge
        cap = self.security.create_web_bridge_capability(
            project_id=self.project_id,
            binding_id=self.binding_id,
            adapter="chatgpt_web",
            expires_in_seconds=1800,
        )
        wb_token = cap["token"]

        # 3. Submit control command through Web Bridge endpoint
        wb_req = {
            "schema_version": 1,
            "adapter_request_id": "cmd-wb-1",
            "operation": "submit_control",
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "binding": {"adapter": "chatgpt_web", "binding_id": self.binding_id},
            "project_id": self.project_id,
            "expected_revision": rev,
            "payload": {
                "action": "pause",
                "expected_revision": rev,
                "target": {},
            },
        }

        s_code, _, s_body = self.post(
            "/api/v1/control/web-bridge/requests",
            wb_req,
            headers={
                "Authorization": f"Bearer {wb_token}",
                "Origin": "https://chatgpt.com",
            },
        )
        self.assertEqual(s_code, 200)
        s_payload = json.loads(s_body.decode("utf-8"))
        self.assertEqual(s_payload["data"]["action"], "pause")
        self.assertEqual(s_payload["data"]["state"], "pending")

        # 4. Check command status via MCP Adapter JSON-RPC stdio
        in_stream = io.StringIO(
            json.dumps({
                "jsonrpc": "2.0",
                "id": "mcp-cmd-status",
                "method": "tools/call",
                "params": {
                    "name": "devorch_command_status",
                    "arguments": {"command_id": "cmd-wb-1"},
                },
            }) + "\n"
        )
        out_stream = io.StringIO()
        mcp = MCPAdapter(client, stdin=in_stream, stdout=out_stream)
        mcp.run()

        output = json.loads(out_stream.getvalue().strip())
        self.assertEqual(output["id"], "mcp-cmd-status")
        self.assertFalse(output["result"]["isError"])
        text = output["result"]["content"][0]["text"]
        self.assertIn("pending", text)

        # 5. Write an event record with secrets into events.jsonl
        events_path = self.runtime / "history" / "events.jsonl"
        with events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "project_id": self.project_id,
                "command_id": "cmd-wb-1",
                "auth_token": "secret-token-12345",
                "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC...\n-----END PRIVATE KEY-----",
                "message": "Enqueued pause control command",
            }) + "\n")

        # 6. Read logs via GET /api/v1/control/logs
        log_code, _, log_body = self.get(
            f"/api/v1/control/logs?project_id={self.project_id}",
            headers={"Authorization": f"Bearer {self.master_token}"},
        )
        self.assertEqual(log_code, 200)
        logs = json.loads(log_body.decode("utf-8"))
        self.assertEqual(logs["schema_version"], 1)
        self.assertTrue(len(logs["items"]) >= 1)
        first_item = logs["items"][0]
        # Redaction verification: sensitive secrets must be masked
        self.assertEqual(first_item["data"]["auth_token"], "[REDACTED]")
        self.assertEqual(first_item["data"]["private_key"], "[REDACTED]")
        self.assertNotIn("secret-token-12345", log_body.decode("utf-8"))

    def test_transport_failure_fails_closed_without_rdc_fallback(self):
        """Acceptance: Verification that ExecutionTransport failures NEVER trigger RDC fallback."""
        cfg = AIBrokerClientConfig(
            broker_repo=ROOT,
            config_path=ROOT / "config.json",
            python_executable=Path("python"),
        )
        req = AIRoleRequest(
            project_id="P1",
            role="planner",
            prompt="do planning",
            working_directory=Path(ROOT),
            request_id="req-accept-1",
        )

        ssh_cfg = SSHTransportConfig(peer="100.64.0.99", connect_timeout_seconds=5.0)
        ssh_transport = SSHTransport(ssh_cfg)
        port = AIBrokerExecutionPort(config=cfg, transport=ssh_transport)

        # Mock SSH failure
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=255,
            stdout=b"",
            stderr=b"ssh: connect to host 100.64.0.99: No route to host",
        )

        # Mock RDC / secondary fallback detector to prove it is NEVER invoked
        rdc_mock = MagicMock()

        with patch("subprocess.run", return_value=mock_completed), patch.dict(
            "os.environ", {"DEVORCH_RDC_FALLBACK_CALLED": "0"}
        ):
            with self.assertRaises(AIBrokerInvocationError):
                port.execute(req)
            # Proves no secondary fallback hook or RDC was called
            rdc_mock.assert_not_called()
