"""Dedicated Browser Bridge HTTP server (adapter-facing v1 surface).

The accepted read-only dashboard stays GET/HEAD-only on its own listener, so
the bridge gets a dedicated local listener (default ``127.0.0.1:8765``) with
adapter-facing endpoints:

- ``GET /v1/health``
- ``GET /v1/progress`` with ``adapter`` and ``binding_id``
- ``POST /v1/claim`` with ``{adapter, binding_id}`` -> 200 envelope or 204
  when the binding queue is empty
- ``POST /v1/renew`` with
  ``{adapter, binding_id, request_id, nonce, claim_token}``
  -> 200 claimed-state envelope (``state``, ``request_id``,
  ``lease_expires_at``) while the exact active claim is still valid
- ``POST /v1/response`` with
  ``{adapter, binding_id, request_id, nonce, claim_token, response_text}``
  -> accepted/rejected acknowledgement
- ``POST /v1/progress`` with ``{adapter, binding_id}``

And P19.3 ChatGPT Plus Browser Control Bridge endpoints:
- ``GET /v1/control/health`` -> 200 browser control health check
- ``POST /v1/control/action`` -> execute structured DEVORCH_ACTION_V1 action
- ``OPTIONS /v1/control/action`` -> CORS preflight for https://chatgpt.com

Claim and renew envelopes expose ``lease_expires_at`` (UTC ISO-8601) so an
adapter can reason only about transport authority (when to renew, when a stale
claim must be abandoned).

No generic prompt endpoint or arbitrary shell/Worker execution is exposed.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from dev_orchestrator.bridge.store import BridgeConflictError, BrowserBridgeStore
from dev_orchestrator.control.browser_control import (
    BrowserControlConflictError,
    BrowserControlConfirmationRequiredError,
    BrowserControlError,
    BrowserControlService,
    MAX_REQUEST_BYTES,
)
from dev_orchestrator.control.security import validate_browser_control_capability

_ALLOW = "GET, POST, OPTIONS"

ALLOWED_CONTROL_ORIGIN_PREFIXES = (
    "https://chatgpt.com",
    "http://localhost:",
    "http://127.0.0.1:",
    "http://localhost",
    "http://127.0.0.1",
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class BridgeHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server owning a :class:`BrowserBridgeStore`."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple,
        store: BrowserBridgeStore,
        runtime_root: Optional[Path | str] = None,
        config_path: Optional[Path | str] = None,
    ) -> None:
        self.store = store
        self.runtime_root = Path(runtime_root) if runtime_root else Path(store.root).parent
        self.config_path = Path(config_path) if config_path else None
        self._browser_control_service: Optional[BrowserControlService] = None
        super().__init__(server_address, _BridgeHandler)

    @property
    def browser_control_service(self) -> BrowserControlService:
        if self._browser_control_service is None:
            self._browser_control_service = BrowserControlService(
                runtime_root=self.runtime_root,
                config_path=self.config_path,
                bridge_store=self.store,
            )
        return self._browser_control_service


class _BridgeHandler(BaseHTTPRequestHandler):
    """Minimal bridge handler: v1 Web Sol endpoints + v1 control action endpoints."""

    protocol_version = "HTTP/1.1"
    server_version = "DevOrchestratorBridge/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence access log
        return

    # -- entry points -----------------------------------------------------
    def do_GET(self) -> None:
        self._route_get()

    def do_POST(self) -> None:
        self._route_post()

    def do_OPTIONS(self) -> None:
        path = self._path()
        if path == "/v1/control/action":
            allowed, origin = self._is_origin_allowed()
            if not allowed:
                self._error(403, "Forbidden", f"Origin '{origin}' not allowed")
                return
            headers = self._cors_headers(origin)
            self._send(204, "No Content", b"", extra_headers=headers)
            return
        self._unsupported()

    def _unsupported(self) -> None:
        self._error(405, "Method Not Allowed", "bridge supports GET, POST, and OPTIONS only", {"Allow": _ALLOW})

    do_HEAD = _unsupported
    do_PUT = _unsupported
    do_DELETE = _unsupported
    do_PATCH = _unsupported
    do_TRACE = _unsupported
    do_CONNECT = _unsupported

    # -- CORS helpers -----------------------------------------------------
    def _is_origin_allowed(self) -> tuple[bool, Optional[str]]:
        origin = self.headers.get("Origin")
        if not origin:
            return True, None
        for prefix in ALLOWED_CONTROL_ORIGIN_PREFIXES:
            if origin == prefix or origin.startswith(prefix):
                return True, origin
        return False, origin

    def _cors_headers(self, origin: Optional[str]) -> dict[str, str]:
        if not origin:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With, X-DevOrch-Client, X-DevOrch-Request-ID",
            "Access-Control-Max-Age": "86400",
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }

    # -- plumbing ---------------------------------------------------------
    def _send(
        self,
        status: int,
        reason: str,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
        extra_headers: Optional[dict] = None,
    ) -> None:
        self.send_response(status, reason)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: int, reason: str, message: str, extra_headers: Optional[dict] = None) -> None:
        self._send(
            status,
            reason,
            _json_bytes({"error": reason, "message": message}),
            extra_headers=extra_headers,
        )

    def _path(self) -> str:
        return urlsplit(self.path).path

    # -- routing ----------------------------------------------------------
    def _route_get(self) -> None:
        path = self._path()
        if path == "/v1/health":
            self._send(
                200,
                "OK",
                _json_bytes({"state": "ok", "surface": "v1"}),
            )
            return
        if path == "/v1/control/health":
            allowed, origin = self._is_origin_allowed()
            if not allowed:
                self._error(403, "Forbidden", f"Origin '{origin}' not allowed")
                return
            headers = self._cors_headers(origin)
            self._send(
                200,
                "OK",
                _json_bytes({
                    "status": "ok",
                    "service": "browser_control_bridge",
                    "version": "DEVORCH_ACTION_V1",
                }),
                extra_headers=headers,
            )
            return
        if path == "/v1/progress":
            query = urlsplit(self.path).query
            params = parse_qs(query, keep_blank_values=True)
            adapter = (params.get("adapter") or [""])[0]
            binding_id = (params.get("binding_id") or [""])[0]
            if not adapter or not binding_id:
                self._error(400, "Bad Request", "adapter and binding_id query parameters required")
                return
            notifs = self.server.store.claim_progress(adapter, binding_id)
            if not notifs:
                self._send(204, "No Content", b"")
                return
            self._send(
                200,
                "OK",
                _json_bytes({"notifications": notifs, "claimed": notifs, "count": len(notifs)}),
            )
            return
        self._error(404, "Not Found", "route not found")

    def _route_post(self) -> None:
        path = self._path()
        if path == "/v1/control/action":
            self._handle_control_action()
            return
        if path not in ("/v1/claim", "/v1/renew", "/v1/response", "/v1/progress"):
            self._error(404, "Not Found", "route not found")
            return
        payload = self._read_payload()
        if payload is None:
            self._error(400, "Bad Request", "request body must be valid JSON")
            return
        try:
            if path == "/v1/claim":
                self._handle_claim(payload)
            elif path == "/v1/renew":
                self._handle_renew(payload)
            elif path == "/v1/progress":
                self._handle_progress(payload)
            else:
                self._handle_response(payload)
        except BridgeConflictError as exc:
            self._error(409, "Conflict", str(exc))
        except (ValueError, TypeError) as exc:
            self._error(400, "Bad Request", str(exc))

    def _read_payload(self, max_bytes: int = 1024 * 1024) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > max_bytes:
            return None
        try:
            raw = self.rfile.read(length)
        except OSError:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _require_str(payload: dict, key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("{0} must be a non-blank string".format(key))
        return value

    def _handle_claim(self, payload: dict) -> None:
        adapter = self._require_str(payload, "adapter")
        binding_id = self._require_str(payload, "binding_id")
        claim = self.server.store.claim(adapter, binding_id)
        if claim is None:
            self.send_response(204, "No Content")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return
        self._send(200, "OK", _json_bytes(asdict(claim)))

    def _handle_renew(self, payload: dict) -> None:
        adapter = self._require_str(payload, "adapter")
        binding_id = self._require_str(payload, "binding_id")
        request_id = self._require_str(payload, "request_id")
        nonce = self._require_str(payload, "nonce")
        claim_token = self._require_str(payload, "claim_token")
        renewed = self.server.store.renew(
            adapter, binding_id, request_id, nonce, claim_token
        )
        self._send(
            200,
            "OK",
            _json_bytes({
                "state": renewed.state,
                "request_id": renewed.request_id,
                "lease_expires_at": renewed.lease_expires_at,
            }),
        )

    def _handle_response(self, payload: dict) -> None:
        adapter = self._require_str(payload, "adapter")
        binding_id = self._require_str(payload, "binding_id")
        request_id = self._require_str(payload, "request_id")
        nonce = self._require_str(payload, "nonce")
        claim_token = self._require_str(payload, "claim_token")
        response_text = self._require_str(payload, "response_text")
        stored = self.server.store.respond(
            adapter, binding_id, request_id, nonce, claim_token, response_text
        )
        self._send(
            200,
            "OK",
            _json_bytes({"state": stored.state, "request_id": stored.request_id}),
        )

    def _handle_progress(self, payload: dict) -> None:
        adapter = self._require_str(payload, "adapter")
        binding_id = self._require_str(payload, "binding_id")
        notifs = self.server.store.claim_progress(adapter, binding_id)
        if not notifs:
            self._send(204, "No Content", b"")
            return
        self._send(
            200,
            "OK",
            _json_bytes({"notifications": notifs, "claimed": notifs, "count": len(notifs)}),
        )

    def _handle_control_action(self) -> None:
        allowed, origin = self._is_origin_allowed()
        if not allowed:
            self._error(403, "Forbidden", f"Origin '{origin}' not allowed")
            return
        cors = self._cors_headers(origin)

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._error(400, "Bad Request", "invalid Content-Length header", extra_headers=cors)
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._error(400, "Bad Request", f"payload size {length} out of bounds (max {MAX_REQUEST_BYTES} bytes)", extra_headers=cors)
            return

        payload = self._read_payload(max_bytes=MAX_REQUEST_BYTES)
        if payload is None or not isinstance(payload, dict):
            self._error(400, "Bad Request", "request body must be valid JSON object", extra_headers=cors)
            return

        auth_header = self.headers.get("Authorization")
        project_id = payload.get("project_id") or "devorchestrator"
        action = payload.get("action")
        auth_ok, auth_reason, cap_row = validate_browser_control_capability(
            auth_header,
            project_id=str(project_id) if project_id else None,
            action=str(action) if action else None,
            runtime_root=self.server.runtime_root,
        )
        if not auth_ok:
            self._error(401, "Unauthorized", f"browser control unauthorized: {auth_reason}", extra_headers=cors)
            return

        cap_id = cap_row.get("capability_id", "unknown") if cap_row else "unknown"
        service = self.server.browser_control_service
        try:
            res = service.execute_action(
                payload,
                host_id="bridge-local",
                capability_id=cap_id,
            )
            self._send(200, "OK", _json_bytes(res), extra_headers=cors)
        except BrowserControlConfirmationRequiredError as exc:
            self._send(400, "Confirmation Required", _json_bytes(exc.to_dict()), extra_headers=cors)
        except BrowserControlConflictError as exc:
            self._send(409, "Conflict", _json_bytes(exc.to_dict()), extra_headers=cors)
        except BrowserControlError as exc:
            self._send(exc.status_code, exc.reason, _json_bytes(exc.to_dict()), extra_headers=cors)
        except Exception as exc:
            self._send(500, "Internal Server Error", _json_bytes({"error": "internal_error", "message": str(exc)}), extra_headers=cors)


def make_bridge_server(
    host: str,
    port: int,
    store: BrowserBridgeStore,
    runtime_root: Optional[Path | str] = None,
    config_path: Optional[Path | str] = None,
) -> BridgeHTTPServer:
    """Create (and bind) the bridge server owning ``store``."""
    return BridgeHTTPServer((host, port), store, runtime_root=runtime_root, config_path=config_path)
