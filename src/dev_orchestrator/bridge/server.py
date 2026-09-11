"""Dedicated Browser Bridge HTTP server (adapter-facing v1 surface).

The accepted read-only dashboard stays GET/HEAD-only on its own listener, so
the bridge gets a dedicated local listener (default ``127.0.0.1:8765``) with
exactly four adapter-facing endpoints:

- ``GET /v1/health``
- ``POST /v1/claim`` with ``{adapter, binding_id}`` -> 200 envelope or 204
  when the binding queue is empty
- ``POST /v1/renew`` with
  ``{adapter, binding_id, request_id, nonce, claim_token}``
  -> 200 claimed-state envelope (``state``, ``request_id``,
  ``lease_expires_at``) while the exact active claim is still valid
- ``POST /v1/response`` with
  ``{adapter, binding_id, request_id, nonce, claim_token, response_text}``
  -> accepted/rejected acknowledgement

Claim and renew envelopes expose ``lease_expires_at`` (UTC ISO-8601) so an
adapter can reason only about transport authority (when to renew, when a stale
claim must be abandoned).

No generic prompt endpoint, arbitrary shell/Worker endpoint, or
workflow-transition endpoint is exposed. The server is transport only: it
hands requests to the ``BrowserBridgeStore`` and never interprets response
content.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlsplit

from dev_orchestrator.bridge.store import BridgeConflictError, BrowserBridgeStore

_ALLOW = "GET, POST"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class BridgeHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server owning a :class:`BrowserBridgeStore`."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple, store: BrowserBridgeStore) -> None:
        self.store = store
        super().__init__(server_address, _BridgeHandler)


class _BridgeHandler(BaseHTTPRequestHandler):
    """Minimal bridge handler: exactly the four v1 endpoints above."""

    protocol_version = "HTTP/1.1"
    server_version = "DevOrchestratorBridge/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence access log
        return

    # -- entry points -----------------------------------------------------
    def do_GET(self) -> None:
        self._route_get()

    def do_POST(self) -> None:
        self._route_post()

    def _unsupported(self) -> None:
        self._error(405, "Method Not Allowed", "bridge supports GET and POST only", {"Allow": _ALLOW})

    do_HEAD = _unsupported
    do_PUT = _unsupported
    do_DELETE = _unsupported
    do_PATCH = _unsupported
    do_OPTIONS = _unsupported
    do_TRACE = _unsupported
    do_CONNECT = _unsupported

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
        if path == "/v1/progress":
            query = urlsplit(self.path).query
            params = dict(item.split("=", 1) for item in query.split("&") if "=" in item)
            adapter = params.get("adapter", "")
            binding_id = params.get("binding_id", "")
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

    def _read_payload(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > 1024 * 1024:
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


def make_bridge_server(host: str, port: int, store: BrowserBridgeStore) -> BridgeHTTPServer:
    """Create (and bind) the bridge server owning ``store``."""
    return BridgeHTTPServer((host, port), store)
