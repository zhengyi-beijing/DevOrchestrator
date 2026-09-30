"""Streamable HTTP MCP protocol adapter for DevOrchestrator control."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from uuid import uuid4

from dev_orchestrator.control.logs import read_control_logs
from dev_orchestrator.control.mcp_shared import (
    checked_tool_args,
    format_tool_result_dict,
)
from dev_orchestrator.control.operations import (
    ControlOperationError,
    cancel_job,
    poll_job,
    read_file,
    spawn_job,
)

logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "devorchestrator-remote-mcp"
SERVER_VERSION = "1.0.0"

_ALLOWED_LOG_SOURCES = frozenset({"events", "runs", "control_audit", "accounting"})

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "devo_status",
        "description": "Return consolidated authoritative daemon, project, lifecycle, owner_gate, and git state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Optional project identifier to inspect specific project details.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "devo_start_task",
        "description": "Start one durable host-allowlisted background task and return its stable job ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Target project identifier."},
                "command_ref": {"type": "string", "description": "Command reference registered in execution-jobs.json."},
                "idempotency_key": {"type": "string", "description": "Caller-supplied stable idempotency key."},
                "parameters": {"type": "object", "description": "Optional parameters dictionary for the command."},
                "host_id": {"type": "string", "description": "Optional execution host (defaults to 'local')."},
                "expected_working_directory": {"type": "string", "description": "Optional expected working directory."},
            },
            "required": ["project_id", "command_ref", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "devo_job_status",
        "description": "Return durable status, exit code, and verification summary for one background job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier the job belongs to."},
                "job_id": {"type": "string", "description": "Durable job or operation identifier."},
            },
            "required": ["project_id", "job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "devo_cancel_job",
        "description": "Idempotently cancel only the referenced background job without cross-job impact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier the job belongs to."},
                "job_id": {"type": "string", "description": "Durable job or operation identifier to cancel."},
                "reason": {"type": "string", "description": "Optional cancellation reason message."},
            },
            "required": ["project_id", "job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "devo_read_log",
        "description": "Query bounded, redacted authoritative logs across events, runs, control_audit, or accounting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": sorted(_ALLOWED_LOG_SOURCES),
                    "description": "Authoritative log source to query.",
                },
                "project_id": {"type": "string", "description": "Optional project identifier to filter entries."},
                "job_id": {"type": "string", "description": "Optional job/run/command identifier to filter entries."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum number of records to return (1-100, default 50).",
                },
                "cursor": {"type": "string", "description": "Opaque pagination cursor from previous query."},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    },
    {
        "name": "devo_read_file",
        "description": "Read bounded content from a configured project root with explicit text/binary reporting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier defining allowed roots."},
                "path": {"type": "string", "description": "Relative file path within project file roots."},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10485760,
                    "description": "Maximum bytes to read (default 1MB, hard cap 10MB).",
                },
                "offset_bytes": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Byte offset to begin reading from (default 0).",
                },
            },
            "required": ["project_id", "path"],
            "additionalProperties": False,
        },
    },
]


class MCPSession:
    """In-memory tracking for an active remote MCP session."""

    def __init__(self, session_id: str, protocol_version: str) -> None:
        self.session_id = session_id
        self.protocol_version = protocol_version
        now = time.time()
        self.created_at = now
        self.last_accessed_at = now
        self.in_flight_requests = 0

    def touch(self) -> None:
        self.last_accessed_at = time.time()


class MCPSessionManager:
    """Thread-safe bounded in-memory session manager for remote MCP clients."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        max_concurrent_sessions: int = 100,
        max_in_flight_per_session: int = 4,
        max_global_in_flight: int = 32,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_concurrent_sessions = max_concurrent_sessions
        self.max_in_flight_per_session = max_in_flight_per_session
        self.max_global_in_flight = max_global_in_flight
        self._sessions: dict[str, MCPSession] = {}
        self._global_in_flight = 0
        self._lock = threading.Lock()

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            sid
            for sid, s in self._sessions.items()
            if (now - s.last_accessed_at) > self.ttl_seconds and s.in_flight_requests == 0
        ]
        for sid in expired:
            self._sessions.pop(sid, None)

    def create_session(self, protocol_version: str) -> str:
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            if len(self._sessions) >= self.max_concurrent_sessions:
                # Evict oldest inactive session
                oldest_sid = min(self._sessions.keys(), key=lambda sid: self._sessions[sid].last_accessed_at)
                self._sessions.pop(oldest_sid, None)
            session_id = f"mcp-sess-{uuid4().hex}"
            self._sessions[session_id] = MCPSession(session_id, protocol_version)
            return session_id

    def acquire_slot(self, session_id: str) -> tuple[bool, str, Optional[MCPSession]]:
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                return False, "session_not_found", None
            if (now - session.last_accessed_at) > self.ttl_seconds:
                self._sessions.pop(session_id, None)
                return False, "session_expired", None
            if self._global_in_flight >= self.max_global_in_flight:
                return False, "global_concurrency_limit_exceeded", None
            if session.in_flight_requests >= self.max_in_flight_per_session:
                return False, "session_concurrency_limit_exceeded", None
            session.in_flight_requests += 1
            self._global_in_flight += 1
            session.touch()
            return True, "ok", session

    def release_slot(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.in_flight_requests = max(0, session.in_flight_requests - 1)
                session.touch()
            self._global_in_flight = max(0, self._global_in_flight - 1)

    def get_session(self, session_id: str) -> Optional[MCPSession]:
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            session = self._sessions.get(session_id)
            if session and (now - session.last_accessed_at) <= self.ttl_seconds:
                session.touch()
                return session
            return None

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._sessions.pop(session_id, None))


class MCPHttpEndpoint:
    """Processes JSON-RPC 2.0 requests over Streamable HTTP."""

    def __init__(
        self,
        runtime_root: Path | str,
        *,
        config_path: Path | str | None = None,
        bridge_store: Any | None = None,
        audit_fn: Optional[Callable[[str, dict[str, Any]], None]] = None,
        session_manager: Optional[MCPSessionManager] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.config_path = Path(config_path) if config_path else None
        self.bridge_store = bridge_store
        self.audit_fn = audit_fn
        self.session_manager = session_manager or MCPSessionManager()

    def delete_session(self, session_id: str) -> bool:
        return self.session_manager.delete_session(session_id)

    def _negotiate_protocol(self, client_proto: Optional[str]) -> str:
        if client_proto and client_proto in SUPPORTED_PROTOCOL_VERSIONS:
            return client_proto
        return LATEST_PROTOCOL_VERSION

    def handle_request(
        self,
        raw_body: bytes,
        *,
        headers: Mapping[str, str],
        request_id: Optional[str] = None,
        auth_context: Optional[dict[str, Any]] = None,
    ) -> tuple[int, dict[str, str], bytes]:
        req_id_str = request_id or f"mcp-req-{uuid4().hex}"
        accept = headers.get("Accept", "") or headers.get("accept", "")
        wants_sse = ("text/event-stream" in accept) and ("application/json" not in accept)

        # 1. Parse JSON body
        try:
            req = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            return 400, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(err_resp).encode("utf-8")

        if not isinstance(req, dict):
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: root must be JSON object"},
            }
            return 400, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(err_resp).encode("utf-8")

        rpc_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        # 2. Check notifications (rpc_id is None)
        if rpc_id is None:
            # Client notification (e.g. notifications/initialized) -> 202 Accepted, empty body
            sess_hdr = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
            resp_headers: dict[str, str] = {"X-DevOrch-Request-ID": req_id_str}
            if sess_hdr:
                resp_headers["Mcp-Session-Id"] = sess_hdr
            return 202, resp_headers, b""

        if not isinstance(method, str):
            err_resp = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32600, "message": "Invalid Request: method must be string"},
            }
            return 400, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(err_resp).encode("utf-8")

        if not isinstance(params, dict):
            err_resp = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32602, "message": "Invalid params: must be object"},
            }
            return 400, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(err_resp).encode("utf-8")

        # 3. Handle 'initialize' (does not require session ID yet)
        if method == "initialize":
            client_version = params.get("protocolVersion")
            proto_version = self._negotiate_protocol(str(client_version) if client_version else None)
            new_session_id = self.session_manager.create_session(proto_version)

            result_data = {
                "protocolVersion": proto_version,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            }
            rpc_reply = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": result_data,
            }
            return self._format_response(
                rpc_reply,
                session_id=new_session_id,
                protocol_version=proto_version,
                request_id=req_id_str,
                wants_sse=wants_sse,
            )

        # 4. Require valid Mcp-Session-Id for all non-initialize methods
        session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if not session_id:
            err_body = json.dumps({
                "error": "Session Required",
                "message": "Mcp-Session-Id header is required after initialize",
            }).encode("utf-8")
            return 404, {"Content-Type": "application/json; charset=utf-8"}, err_body

        slot_ok, slot_err, session = self.session_manager.acquire_slot(session_id)
        if not slot_ok:
            if slot_err in ("session_not_found", "session_expired"):
                err_body = json.dumps({
                    "error": "Session Invalid",
                    "message": f"Mcp-Session-Id is invalid or expired ({slot_err}); please re-initialize",
                }).encode("utf-8")
                return 404, {"Content-Type": "application/json; charset=utf-8"}, err_body
            # Concurrency limit hit
            err_resp = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32000, "message": f"Server busy: {slot_err}"},
            }
            return 429, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(err_resp).encode("utf-8")

        try:
            proto_version = session.protocol_version if session else LATEST_PROTOCOL_VERSION
            status_code, rpc_reply = self._dispatch_method(
                rpc_id,
                method,
                params,
                session_id=session_id,
                request_id=req_id_str,
                auth_context=auth_context,
            )
            return self._format_response(
                rpc_reply,
                session_id=session_id,
                protocol_version=proto_version,
                request_id=req_id_str,
                wants_sse=wants_sse,
                status_code=status_code,
            )
        finally:
            self.session_manager.release_slot(session_id)

    def _dispatch_method(
        self,
        rpc_id: Any,
        method: str,
        params: dict[str, Any],
        *,
        session_id: str,
        request_id: str,
        auth_context: Optional[dict[str, Any]] = None,
    ) -> tuple[int, dict[str, Any]]:
        if method == "ping":
            return 200, {"jsonrpc": "2.0", "id": rpc_id, "result": {}}

        if method == "tools/list":
            return 200, {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": MCP_TOOLS}}

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return 200, {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": format_tool_result_dict(
                        {"error": "Invalid arguments: expected object"}, is_error=True
                    ),
                }

            known_tools = {t["name"]: t for t in MCP_TOOLS}
            if tool_name not in known_tools:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
                }

            # Check capability tool scope if token is scoped
            allowed_tools = (auth_context or {}).get("allowed_tools", ["*"])
            if allowed_tools and "*" not in allowed_tools and tool_name not in allowed_tools:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": format_tool_result_dict(
                        {"error": f"Tool {tool_name!r} not permitted by MCP token scope"}, is_error=True
                    ),
                }

            # Invoke tool
            res_dict, is_error, job_id = self._call_tool(
                tool_name, arguments, request_id=request_id
            )

            # Audit emission per tool call
            if self.audit_fn:
                try:
                    self.audit_fn(
                        "mcp_tool_call",
                        {
                            "request_id": request_id,
                            "session_id": session_id,
                            "tool": tool_name,
                            "job_id": job_id,
                            "outcome": "error" if is_error else "ok",
                        },
                    )
                except Exception:
                    pass

            return 200, {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": format_tool_result_dict(res_dict, is_error=is_error),
            }

        return 200, {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def _call_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        request_id: str,
    ) -> tuple[Any, bool, Optional[str]]:
        try:
            if name == "devo_status":
                checked_tool_args(args, {"project_id"}, ())
                from dev_orchestrator.web.server import external_status_payload
                proj_id = args.get("project_id")
                try:
                    b_store = self.bridge_store() if callable(self.bridge_store) else self.bridge_store
                    status_envelope = external_status_payload(
                        self.runtime_root,
                        config_path=self.config_path,
                        bridge_store=b_store,
                        project_id=proj_id,
                    )
                    return status_envelope.get("data", status_envelope), False, None
                except KeyError:
                    return {"status": "failed", "error": f"project {proj_id!r} not found"}, True, None

            if name == "devo_start_task":
                allowed = {
                    "project_id",
                    "command_ref",
                    "idempotency_key",
                    "parameters",
                    "host_id",
                    "expected_working_directory",
                }
                checked_tool_args(args, allowed, ("project_id", "command_ref", "idempotency_key"))
                res = spawn_job(
                    self.runtime_root,
                    project_id=args["project_id"],
                    command_ref=args["command_ref"],
                    idempotency_key=args["idempotency_key"],
                    parameters=args.get("parameters"),
                    host_id=args.get("host_id", "local"),
                    expected_working_directory=args.get("expected_working_directory"),
                    request_id=request_id,
                )
                job_id = res.get("job_id") or res.get("operation_id")
                is_err = res.get("status") not in ("ok", "queued", "running")
                return res, is_err, job_id

            if name == "devo_job_status":
                checked_tool_args(args, {"project_id", "job_id"}, ("project_id", "job_id"))
                res = poll_job(
                    self.runtime_root,
                    project_id=args["project_id"],
                    job_id=args["job_id"],
                    request_id=request_id,
                )
                job_id = res.get("job_id") or res.get("operation_id") or args["job_id"]
                is_err = res.get("status") == "failed"
                return res, is_err, job_id

            if name == "devo_cancel_job":
                checked_tool_args(args, {"project_id", "job_id", "reason"}, ("project_id", "job_id"))
                res = cancel_job(
                    self.runtime_root,
                    project_id=args["project_id"],
                    job_id=args["job_id"],
                    reason=args.get("reason", "cancelled"),
                    request_id=request_id,
                )
                job_id = res.get("job_id") or res.get("operation_id") or args["job_id"]
                return res, False, job_id

            if name == "devo_read_log":
                checked_tool_args(args, {"source", "project_id", "job_id", "limit", "cursor"}, ("source",))
                source = args["source"]
                if source not in _ALLOWED_LOG_SOURCES:
                    return {
                        "status": "failed",
                        "error": f"source {source!r} not in allowed sources: {sorted(_ALLOWED_LOG_SOURCES)}",
                    }, True, None
                limit = max(1, min(100, int(args.get("limit", 50))))
                logs_res = read_control_logs(
                    self.runtime_root,
                    project_id=args.get("project_id"),
                    run_id=args.get("job_id"),
                    command_id=args.get("job_id"),
                    source=source,
                    limit=limit,
                    cursor=args.get("cursor"),
                )
                return logs_res, False, args.get("job_id")

            if name == "devo_read_file":
                checked_tool_args(args, {"project_id", "path", "max_bytes", "offset_bytes"}, ("project_id", "path"))
                max_bytes = max(1, min(10 * 1024 * 1024, int(args.get("max_bytes", 1024 * 1024))))
                offset_bytes = max(0, int(args.get("offset_bytes", 0)))
                res = read_file(
                    self.runtime_root,
                    project_id=args["project_id"],
                    path=args["path"],
                    max_bytes=max_bytes,
                    offset_bytes=offset_bytes,
                    request_id=request_id,
                )
                if res.get("status") != "ok":
                    return res, True, None
                # Explicit text/binary output reporting
                if res.get("content_text") is not None:
                    out = {
                        "status": res["status"],
                        "path": res["path"],
                        "is_text": True,
                        "content": res["content_text"],
                        "size_bytes": res["size_bytes"],
                        "content_sha256": res["content_sha256"],
                        "selected_transport": res["selected_transport"],
                    }
                else:
                    out = {
                        "status": res["status"],
                        "path": res["path"],
                        "is_text": False,
                        "encoding": "base64",
                        "content_base64": res.get("content_base64"),
                        "size_bytes": res["size_bytes"],
                        "content_sha256": res["content_sha256"],
                        "selected_transport": res["selected_transport"],
                    }
                return out, False, None

            return {"error": f"unhandled tool {name}"}, True, None

        except ControlOperationError as exc:
            return {"status": "error", "error": exc.message, "reason": exc.reason}, True, None
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}, True, None
        except Exception as exc:
            return {"status": "error", "error": f"internal error: {exc}"}, True, None

    def _format_response(
        self,
        rpc_reply: dict[str, Any],
        *,
        session_id: str,
        protocol_version: str,
        request_id: str,
        wants_sse: bool = False,
        status_code: int = 200,
    ) -> tuple[int, dict[str, str], bytes]:
        json_str = json.dumps(rpc_reply, ensure_ascii=False)
        headers = {
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": protocol_version,
            "X-DevOrch-Request-ID": request_id,
        }
        if wants_sse:
            headers["Content-Type"] = "text/event-stream; charset=utf-8"
            body = f"event: message\ndata: {json_str}\n\n".encode("utf-8")
        else:
            headers["Content-Type"] = "application/json; charset=utf-8"
            body = json_str.encode("utf-8")
        return status_code, headers, body
