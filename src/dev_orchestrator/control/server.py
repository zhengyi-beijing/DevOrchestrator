"""Dedicated loopback-only Conversation Control Plane HTTP server."""

from __future__ import annotations

import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlsplit

from dev_orchestrator.control.service import ControlNotFoundError, ControlPlaneService
from dev_orchestrator.control.store import ControlConflictError


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def validate_control_listen(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("control listener must be an explicit loopback IP address") from exc
    if not address.is_loopback:
        raise ValueError("control listener must be loopback-only")


class ControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple, service: ControlPlaneService) -> None:
        self.service = service
        super().__init__(server_address, _ControlHandler)


class _ControlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DevOrchestratorControl/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._route_get()

    def do_POST(self) -> None:
        self._route_post()

    def _send(self, status: int, reason: str, value: Any) -> None:
        body = _json_bytes(value)
        self.send_response(status, reason)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, reason: str, message: str) -> None:
        self._send(status, reason, {"error": reason, "message": message})

    def _path(self) -> str:
        return urlsplit(self.path).path

    def _read_payload(self) -> Optional[dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > 1024 * 1024:
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None
    def _client_is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _route_get(self) -> None:
        path = self._path()
        if path == "/v1/health":
            self._send(200, "OK", {"state": "ok", "surface": "conversation-control-v1"})
            return
        if path == "/v1/sessions":
            self._send(200, "OK", {"sessions": self.server.service.list_sessions()})
            return
        if path == "/v1/bindings":
            self._send(200, "OK", {"bindings": self.server.service.list_bindings()})
            return
        if path == "/v1/projects":
            try:
                projects = self.server.service.list_projects()
            except Exception as exc:
                self._error(409, "Conflict", str(exc)); return
            self._send(200, "OK", {"projects": projects})
            return
        self._error(404, "Not Found", "route not found")
    def _route_post(self) -> None:
        if not self._client_is_loopback():
            self._error(403, "Forbidden", "control mutations require a loopback client")
            return
        path = self._path()
        if path == "/v1/owner-action":
            self._error(501, "Not Implemented", "owner-action is reserved for CCP6")
            return
        if path not in (
            "/v1/session/heartbeat", "/v1/bind", "/v1/unbind", "/v1/rebind"
        ):
            self._error(404, "Not Found", "route not found")
            return
        payload = self._read_payload()
        if payload is None:
            self._error(400, "Bad Request", "request body must be valid JSON")
            return
        try:
            if path == "/v1/session/heartbeat":
                result = self.server.service.heartbeat(payload)
            elif path == "/v1/bind":
                result = {"binding": self.server.service.bind(
                    payload.get("project_id"), payload.get("adapter"), payload.get("binding_id")
                )}
            elif path == "/v1/rebind":
                result = {"binding": self.server.service.rebind(
                    payload.get("project_id"), payload.get("adapter"), payload.get("binding_id")
                )}
            else:
                result = self.server.service.unbind(payload.get("project_id"))
        except ControlNotFoundError as exc:
            self._error(404, "Not Found", str(exc)); return
        except ControlConflictError as exc:
            self._error(409, "Conflict", str(exc)); return
        except (ValueError, TypeError) as exc:
            self._error(400, "Bad Request", str(exc)); return
        except Exception as exc:
            self._error(409, "Conflict", str(exc)); return
        self._send(200, "OK", result)

    def do_HEAD(self) -> None:
        self._error(405, "Method Not Allowed", "control surface supports GET and POST only")

    do_PUT = do_HEAD
    do_DELETE = do_HEAD
    do_PATCH = do_HEAD


def make_control_server(host: str, port: int, service: ControlPlaneService) -> ControlHTTPServer:
    validate_control_listen(host)
    return ControlHTTPServer((host, port), service)
