import io
import json
import unittest
from unittest.mock import MagicMock

from dev_orchestrator.control.adapter import (
    ControlAdapterClient,
    ControlAdapterError,
    ControlAdapterRevisionMismatchError,
)
from dev_orchestrator.control.mcp_adapter import MCPAdapter, _TOOLS, SERVER_NAME, SERVER_VERSION


class FakeControlAdapterClient:
    def __init__(self):
        self.status_calls = []
        self.logs_calls = []
        self.submit_calls = []
        self.command_status_calls = []
        self.mock_status_result = {"project_id": "p1", "revision": "rev123", "lifecycle_state": "READY_TO_RUN"}
        self.mock_logs_result = {"version": 1, "items": [{"id": 1}], "has_more": False}
        self.mock_submit_result = {"command_id": "cmd-1", "state": "enqueued", "action": "pause"}
        self.mock_cmd_status_result = {"command_id": "cmd-1", "state": "applied"}
        self.conflict_on_submit = False

    def status(self, project_id=None):
        self.status_calls.append(project_id)
        return self.mock_status_result

    def logs(self, project_id=None, limit=50, cursor=None):
        self.logs_calls.append((project_id, limit, cursor))
        return self.mock_logs_result

    def submit_control(self, adapter_request_id, project_id, action, expected_revision, target=None):
        self.submit_calls.append((adapter_request_id, project_id, action, expected_revision, target))
        if self.conflict_on_submit:
            raise ControlAdapterRevisionMismatchError("revision mismatch: expected revOld but got revNew")
        return self.mock_submit_result

    def command_status(self, command_id):
        self.command_status_calls.append(command_id)
        return self.mock_cmd_status_result


class P13MCPAdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeControlAdapterClient()
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.adapter = MCPAdapter(self.client, stdin=self.stdin, stdout=self.stdout)

    def test_jsonrpc_parse_error(self):
        resp_str = self.adapter.handle_message("{not valid json")
        resp = json.loads(resp_str)
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["error"]["code"], -32700)
        self.assertIn("Parse error", resp["error"]["message"])

    def test_jsonrpc_invalid_request(self):
        resp_str = self.adapter.handle_message("12345")
        resp = json.loads(resp_str)
        self.assertEqual(resp["error"]["code"], -32600)

        resp_str = self.adapter.handle_message(json.dumps({"id": 1}))
        resp = json.loads(resp_str)
        self.assertEqual(resp["error"]["code"], -32600)

    def test_jsonrpc_method_not_found(self):
        req = {"jsonrpc": "2.0", "id": 1, "method": "unsupported/method", "params": {}}
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        self.assertEqual(resp["error"]["code"], -32601)

    def test_jsonrpc_notification_ignored(self):
        req = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = self.adapter.handle_message(json.dumps(req))
        self.assertIsNone(resp)

    def test_ping(self):
        req = {"jsonrpc": "2.0", "id": "p1", "method": "ping"}
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        self.assertEqual(resp["id"], "p1")
        self.assertEqual(resp["result"], {})

    def test_initialize(self):
        req = {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        self.assertEqual(resp["id"], 2)
        res = resp["result"]
        self.assertEqual(res["serverInfo"]["name"], SERVER_NAME)
        self.assertEqual(res["serverInfo"]["version"], SERVER_VERSION)
        self.assertIn("tools", res["capabilities"])

    def test_tools_list_exposes_only_four_closed_tools(self):
        req = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        tools = resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertEqual(sorted(tool_names), sorted([
            "devorch_status", "devorch_logs", "devorch_control", "devorch_command_status"
        ]))

    def test_tools_call_unknown_tool(self):
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "arbitrary_eval", "arguments": {}},
        }
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("Unknown tool", resp["result"]["content"][0]["text"])

    def test_tool_call_status(self):
        req = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "devorch_status", "arguments": {"project_id": "my-proj"}},
        }
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("rev123", resp["result"]["content"][0]["text"])
        self.assertEqual(self.client.status_calls, ["my-proj"])

    def test_tool_call_status_rejects_unknown_argument(self):
        req = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "devorch_status", "arguments": {"invalid_arg": True}},
        }
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("Unknown arguments", resp["result"]["content"][0]["text"])

    def test_tool_call_logs(self):
        req = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "devorch_logs",
                "arguments": {"project_id": "p1", "limit": 10, "cursor": "cur1"},
            },
        }
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(self.client.logs_calls, [("p1", 10, "cur1")])

    def test_tool_call_control_success_and_validation(self):
        # Missing required parameter
        req_missing = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "devorch_control",
                "arguments": {
                    "project_id": "p1",
                    "command_id": "cmd-1",
                    "action": "pause",
                    # missing expected_revision
                },
            },
        }
        resp = json.loads(self.adapter.handle_message(json.dumps(req_missing)))
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("expected_revision", resp["result"]["content"][0]["text"])

        # Valid call
        req_valid = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "devorch_control",
                "arguments": {
                    "project_id": "p1",
                    "command_id": "cmd-1",
                    "expected_revision": "rev1",
                    "action": "pause",
                    "target": {"gate_id": "gate-1"},
                },
            },
        }
        resp = json.loads(self.adapter.handle_message(json.dumps(req_valid)))
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(
            self.client.submit_calls,
            [("cmd-1", "p1", "pause", "rev1", {"gate_id": "gate-1"})],
        )

    def test_tool_call_control_revision_mismatch_conflict(self):
        self.client.conflict_on_submit = True
        req = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "devorch_control",
                "arguments": {
                    "project_id": "p1",
                    "command_id": "cmd-1",
                    "expected_revision": "revOld",
                    "action": "continue",
                },
            },
        }
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("revision mismatch", resp["result"]["content"][0]["text"])

    def test_tool_call_command_status(self):
        req = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "devorch_command_status",
                "arguments": {"command_id": "cmd-1"},
            },
        }
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(self.client.command_status_calls, ["cmd-1"])

    def test_output_truncation_bound(self):
        large_dict = {"data": "x" * 70000}
        self.client.mock_status_result = large_dict
        req = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "devorch_status", "arguments": {}},
        }
        resp = json.loads(self.adapter.handle_message(json.dumps(req)))
        text = resp["result"]["content"][0]["text"]
        self.assertTrue(len(text) <= 66000)
        self.assertIn("...[OUTPUT_TRUNCATED]", text)

    def test_stdio_run_loop(self):
        req1 = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        req2 = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        in_stream = io.StringIO(f"{req1}\n{req2}\n")
        out_stream = io.StringIO()

        adapter = MCPAdapter(self.client, stdin=in_stream, stdout=out_stream)
        adapter.run()

        output_lines = [line.strip() for line in out_stream.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(output_lines), 2)
        r1 = json.loads(output_lines[0])
        r2 = json.loads(output_lines[1])
        self.assertEqual(r1["id"], 1)
        self.assertEqual(r2["id"], 2)
