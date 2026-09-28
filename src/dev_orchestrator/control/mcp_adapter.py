"""MCPAdapter MVP: Local stdio JSON-RPC 2.0 Model Context Protocol adapter.

Exposes only four semantic tools: devorch_status, devorch_logs, devorch_control,
and devorch_command_status. Tool schemas are closed and bounded, with no arbitrary
shell, prompt, filesystem, patch, provider-selection, or raw HTTP facility.
All lifecycle actions are translated into authenticated loopback Control API calls.
"""

from __future__ import annotations

import json
import sys
from typing import Any, IO, Mapping

from dev_orchestrator.control.adapter import (
    ControlAdapterClient,
    ControlAdapterError,
    ControlAdapterRevisionMismatchError,
)
from dev_orchestrator.control.command_store import CONTROL_ACTIONS

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "devorchestrator-control"
SERVER_VERSION = "1.0.0"

_CORE_TOOLS = [
    {
        "name": "devorch_status",
        "description": "Inspect authoritative DevOrchestrator state (overview or specific project).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Optional project identifier to inspect specific project details.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "devorch_logs",
        "description": "Query bounded paginated authoritative logs across events, runs, commands, and audit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Optional project identifier to filter log entries.",
                },
                "cursor": {
                    "type": "string",
                    "description": "Optional opaque pagination cursor from a previous logs query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of log records to return (1-100, default 50).",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "devorch_control",
        "description": "Submit a guarded lifecycle control command (continue, pause, resume, stop, retry, rereview, reconcile, approve_owner_gate, bind_conversation, unbind_conversation, rebind_conversation).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Target project identifier.",
                },
                "command_id": {
                    "type": "string",
                    "description": "Unique, deterministic command or request identifier.",
                },
                "expected_revision": {
                    "type": "string",
                    "description": "Exact expected revision hash from project status for CAS guard.",
                },
                "action": {
                    "type": "string",
                    "enum": sorted(CONTROL_ACTIONS),
                    "description": "Lifecycle control action to execute.",
                },
                "target": {
                    "type": "object",
                    "properties": {
                        "gate_id": {"type": "string"},
                        "target_id": {"type": "string"},
                        "adapter": {"type": "string"},
                        "binding_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                    "description": "Optional action-specific target parameters.",
                },
            },
            "required": ["project_id", "command_id", "expected_revision", "action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "devorch_command_status",
        "description": "Check status and outcome of an enqueued or settled lifecycle control command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command_id": {
                    "type": "string",
                    "description": "Command identifier to check.",
                },
            },
            "required": ["command_id"],
            "additionalProperties": False,
        },
    },
]

_TRANSPORT_TOOLS = [
    {
        "name": "devorch_transport_hosts",
        "description": "Inspect configured execution transport hosts and their profiles.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "devorch_transport_capabilities",
        "description": "Inspect discovered capabilities, approved policy pins, and limits for an execution host.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host_id": {
                    "type": "string",
                    "description": "Optional host identifier (defaults to local host).",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "devorch_transport_operations",
        "description": "Inspect recent sanitized machine execution operations and status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of recent operations to return (1-100, default 50).",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "additionalProperties": False,
        },
    },
]

_TOOLS = _CORE_TOOLS
_TOOL_NAMES = {t["name"] for t in _CORE_TOOLS}


class MCPAdapter:
    """Local stdio MCP JSON-RPC 2.0 server."""

    def __init__(
        self,
        client: ControlAdapterClient,
        stdin: IO[str] | None = None,
        stdout: IO[str] | None = None,
        *,
        enable_transport_tools: bool = False,
    ) -> None:
        self.client = client
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.enable_transport_tools = enable_transport_tools
        self.tools = list(_CORE_TOOLS + _TRANSPORT_TOOLS if enable_transport_tools else _CORE_TOOLS)
        self.tool_names = {t["name"] for t in self.tools}

    def handle_message(self, raw_line: str) -> str | None:
        raw_line = raw_line.strip()
        if not raw_line:
            return None

        try:
            req = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError) as exc:
            return json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            })

        if not isinstance(req, dict):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: expected JSON object"},
            })

        req_id = req.get("id")
        method = req.get("method")

        # Notifications (no id)
        if req_id is None:
            if method in ("notifications/initialized", "initialized"):
                return None
            return None

        if not isinstance(method, str):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid Request: missing method"},
            })

        params = req.get("params") or {}
        if not isinstance(params, dict):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Invalid params: expected object"},
            })

        try:
            if method == "initialize":
                return self._handle_initialize(req_id, params)
            if method == "ping":
                return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}})
            if method == "tools/list":
                return json.dumps({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": self.tools},
                })
            if method == "tools/call":
                return self._handle_tools_call(req_id, params)
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method!r}"},
            })
        except Exception as exc:  # noqa: BLE001
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            })

    def _handle_initialize(self, req_id: Any, params: dict[str, Any]) -> str:
        return json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        })

    def _handle_tools_call(self, req_id: Any, params: dict[str, Any]) -> str:
        name = params.get("name")
        args = params.get("arguments") or {}

        if not isinstance(name, str) or name not in self.tool_names:
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Unknown tool: {name!r}"}],
                    "isError": True,
                },
            })

        if not isinstance(args, dict):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "Tool arguments must be an object"}],
                    "isError": True,
                },
            })

        try:
            if name == "devorch_status":
                return self._call_status(req_id, args)
            if name == "devorch_logs":
                return self._call_logs(req_id, args)
            if name == "devorch_control":
                return self._call_control(req_id, args)
            if name == "devorch_command_status":
                return self._call_command_status(req_id, args)
            if name == "devorch_transport_hosts":
                return self._call_transport_hosts(req_id, args)
            if name == "devorch_transport_capabilities":
                return self._call_transport_capabilities(req_id, args)
            if name == "devorch_transport_operations":
                return self._call_transport_operations(req_id, args)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                },
            })

        return json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": "Unhandled tool"}],
                "isError": True,
            },
        })

    def _call_status(self, req_id: Any, args: dict[str, Any]) -> str:
        allowed = {"project_id"}
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ValueError(f"Unknown arguments for devorch_status: {', '.join(unknown)}")

        project_id = args.get("project_id")
        result = self.client.status(project_id)
        return self._format_tool_result(req_id, result)

    def _call_logs(self, req_id: Any, args: dict[str, Any]) -> str:
        allowed = {"project_id", "cursor", "limit"}
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ValueError(f"Unknown arguments for devorch_logs: {', '.join(unknown)}")

        project_id = args.get("project_id")
        cursor = args.get("cursor")
        limit = args.get("limit", 50)
        result = self.client.logs(project_id, cursor=cursor, limit=limit)
        return self._format_tool_result(req_id, result)

    def _call_control(self, req_id: Any, args: dict[str, Any]) -> str:
        allowed = {"project_id", "command_id", "expected_revision", "action", "target"}
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ValueError(f"Unknown arguments for devorch_control: {', '.join(unknown)}")

        for required in ("project_id", "command_id", "expected_revision", "action"):
            if not args.get(required):
                raise ValueError(f"devorch_control requires nonblank '{required}'")

        project_id = str(args["project_id"]).strip()
        command_id = str(args["command_id"]).strip()
        expected_revision = str(args["expected_revision"]).strip()
        action = str(args["action"]).strip()
        target = args.get("target") or {}
        if not isinstance(target, dict):
            raise ValueError("target must be an object")

        result = self.client.submit_control(
            adapter_request_id=command_id,
            project_id=project_id,
            action=action,
            expected_revision=expected_revision,
            target=target,
        )
        return self._format_tool_result(req_id, result)

    def _call_command_status(self, req_id: Any, args: dict[str, Any]) -> str:
        allowed = {"command_id"}
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ValueError(f"Unknown arguments for devorch_command_status: {', '.join(unknown)}")

        command_id = args.get("command_id")
        if not command_id or not isinstance(command_id, str):
            raise ValueError("devorch_command_status requires nonblank 'command_id'")

        result = self.client.command_status(command_id.strip())
        return self._format_tool_result(req_id, result)

    def _call_transport_hosts(self, req_id: Any, args: dict[str, Any]) -> str:
        if args:
            raise ValueError(f"Unknown arguments for devorch_transport_hosts: {', '.join(sorted(args))}")
        result = self.client.transport_hosts()
        return self._format_tool_result(req_id, result)

    def _call_transport_capabilities(self, req_id: Any, args: dict[str, Any]) -> str:
        allowed = {"host_id"}
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ValueError(f"Unknown arguments for devorch_transport_capabilities: {', '.join(unknown)}")
        host_id = args.get("host_id")
        result = self.client.transport_capabilities(host_id=host_id)
        return self._format_tool_result(req_id, result)

    def _call_transport_operations(self, req_id: Any, args: dict[str, Any]) -> str:
        allowed = {"limit"}
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise ValueError(f"Unknown arguments for devorch_transport_operations: {', '.join(unknown)}")
        limit = int(args.get("limit", 50))
        result = self.client.transport_operations(limit=limit)
        return self._format_tool_result(req_id, result)

    @staticmethod
    def _format_tool_result(req_id: Any, result: Any, is_error: bool = False) -> str:
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        # Bound output to max 64KB per call
        if len(rendered) > 65536:
            rendered = rendered[:65500] + "\n...[OUTPUT_TRUNCATED]"
        return json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": rendered}],
                "isError": is_error,
            },
        })

    def run(self) -> None:
        """Process line-delimited JSON-RPC requests on stdio until EOF."""
        for line in self.stdin:
            response = self.handle_message(line)
            if response:
                self.stdout.write(response + "\n")
                self.stdout.flush()


def run_mcp_adapter(
    base_url: str | None = None,
    token: str | None = None,
    runtime_root: str | None = None,
) -> int:
    client = ControlAdapterClient(base_url=base_url, token=token, runtime_root=runtime_root)
    adapter = MCPAdapter(client)
    adapter.run()
    return 0


if __name__ == "__main__":
    sys.exit(run_mcp_adapter())
