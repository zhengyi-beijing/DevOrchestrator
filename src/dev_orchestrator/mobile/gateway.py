"""MobileGateway daemon HTTP server and route handlers.

Binds exclusively to verified local Tailscale interface addresses.
Applies single-chokepoint mobile device authentication, command namespacing,
read-only projection composition, and reconnectable SSE / long-polling streams.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, List, Optional, Set
from urllib.parse import parse_qs, unquote, urlsplit

from dev_orchestrator.control.adapter import (
    ControlAdapterClient,
    ControlAdapterConflictError,
    ControlAdapterRevisionMismatchError,
    ControlAdapterError,
)
from dev_orchestrator.mobile.alerts import load_alert_policy, save_alert_policy
from dev_orchestrator.mobile.authorizer import MobileDeviceAuthorizer, MobileDevicePrincipal
from dev_orchestrator.mobile.bind_policy import verify_tailscale_bind_address
from dev_orchestrator.mobile.projection import MOBILE_CONTROL_ACTIONS, MobileProjectionService
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

_MAX_BODY = 64 * 1024
_CMD_PATH_RE = re.compile(r"^/api/v1/mobile/v1/commands/([A-Za-z0-9_-]+)$")
_PROJECT_PATH_RE = re.compile(r"^/api/v1/mobile/v1/projects/([A-Za-z0-9_-]+)$")
_PROJECT_CONTROLS_PATH_RE = re.compile(r"^/api/v1/mobile/v1/projects/([A-Za-z0-9_-]+)/controls$")
_ALERT_ACK_PATH_RE = re.compile(r"^/api/v1/mobile/v1/alerts/([A-Za-z0-9_:-]+)/ack$")
_ALERT_SNOOZE_PATH_RE = re.compile(r"^/api/v1/mobile/v1/alerts/([A-Za-z0-9_:-]+)/snooze$")


class MobileGatewayHTTPServer(ThreadingHTTPServer):
    """Threading HTTPServer for mobile observation and guarded control."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        runtime_root: Path | str,
        authorizer: MobileDeviceAuthorizer,
        projection_service: MobileProjectionService,
        control_client: ControlAdapterClient,
        *,
        config_path: Path | str | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.authorizer = authorizer
        self.projection_service = projection_service
        self.control_client = control_client
        self.config_path = Path(config_path) if config_path else None
        self.started_at = utc_now_iso()

        self._streams_lock = threading.Lock()
        self._open_streams: Set[Any] = set()
        self._state_lock = threading.Lock()
        self._active_alert_dedup: Set[str] = set()

        self._events_lock = threading.Lock()
        self._events_cv = threading.Condition(self._events_lock)
        self._events_buffer: List[dict[str, Any]] = []
        self._next_cursor = 1
        self._max_retention = 100
        self._shutdown_flag = threading.Event()

        super().__init__(server_address, MobileGatewayHandler)
        self.touch_health("running")

    def touch_health(self, state: str, error: Optional[str] = None) -> None:
        """Publish runtime/mobile-gateway.json health."""
        try:
            port = int(self.server_address[1])
        except (TypeError, ValueError, IndexError):
            port = 0
        with self._streams_lock:
            active_streams = len(self._open_streams)
        payload = {
            "schema_version": 1,
            "state": state,
            "pid": os.getpid(),
            "listen_address": self.server_address[0],
            "port": port,
            "started_at": self.started_at,
            "last_tick_at": utc_now_iso(),
            "active_streams": active_streams,
            "error": error,
        }
        health_path = self.runtime_root / "mobile-gateway.json"
        health_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(health_path, payload)

    def broadcast_event(self, event_type: str, data: dict[str, Any]) -> str:
        """Broadcast an event to all active streams and store in ring buffer."""
        with self._events_lock:
            cursor = str(self._next_cursor)
            self._next_cursor += 1
            entry = {
                "cursor": cursor,
                "event": event_type,
                "data": data,
                "timestamp": utc_now_iso(),
            }
            self._events_buffer.append(entry)
            if len(self._events_buffer) > self._max_retention:
                self._events_buffer.pop(0)
            self._events_cv.notify_all()
            return cursor

    def evaluate_and_broadcast_alerts(self, now: Optional[datetime] = None) -> List[Any]:
        """Evaluate alerts and broadcast newly active items to open streams."""
        if self.projection_service is None:
            return []
        try:
            from dev_orchestrator.mobile.alerts import evaluate_all_mobile_alerts
            from dataclasses import asdict
            cfg = read_json(self.config_path, {}) if self.config_path else {}
            health_path = self.runtime_root / "mobile-gateway.json"
            health = read_json(health_path, {}) if health_path.is_file() else {}
            is_degraded = bool(health.get("error"))
            notifications = evaluate_all_mobile_alerts(
                self.runtime_root,
                self.projection_service,
                config=cfg,
                transport_connected=True,
                transport_degraded=is_degraded,
                now=now,
            )
            current_keys = set()
            for item in notifications:
                current_keys.add(item.dedup_key)
                with self._events_lock:
                    if item.dedup_key not in self._active_alert_dedup:
                        self.broadcast_event("alert", asdict(item))
            with self._events_lock:
                self._active_alert_dedup = current_keys
            return notifications
        except Exception:
            return []

    def get_events_since(self, cursor: Optional[str]) -> tuple[bool, List[dict[str, Any]], str]:
        """Fetch events since cursor from buffer.

        Returns:
            (resync_required: bool, events: list, current_cursor: str)
        """
        with self._events_lock:
            current_cursor = str(self._next_cursor - 1)
            if not cursor:
                return False, [], current_cursor
            try:
                c_int = int(cursor)
            except ValueError:
                return True, [], current_cursor

            if not self._events_buffer:
                return False, [], current_cursor

            oldest_cursor = int(self._events_buffer[0]["cursor"])
            if c_int < oldest_cursor - 1:
                # Outside retention: resync required
                return True, [], current_cursor

            events = [ev for ev in self._events_buffer if int(ev["cursor"]) > c_int]
            return False, events, current_cursor

    def close_all_streams(self) -> None:
        """Close open streams on gateway shutdown."""
        self._shutdown_flag.set()
        with self._events_lock:
            self._events_cv.notify_all()
        with self._streams_lock:
            streams = list(self._open_streams)
        for s in streams:
            try:
                s.close_connection = True
            except Exception:
                pass


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class MobileGatewayHandler(BaseHTTPRequestHandler):
    """Handler enforcing device authentication, routes, and security headers."""

    protocol_version = "HTTP/1.1"

    # -- Security headers -------------------------------------------------
    def _send(
        self,
        status: int,
        reason: str,
        content_type: str,
        body: bytes,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.send_response(status, reason)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: int, reason: str, message: str) -> None:
        payload = {"error": reason, "message": message, "schema_version": 1}
        self._send(status, reason, "application/json; charset=utf-8", _json_bytes(payload))

    # Reject unsupported methods
    def do_OPTIONS(self) -> None:
        self._error(405, "Method Not Allowed", "CORS and OPTIONS are not supported on MobileGateway")

    def do_TRACE(self) -> None:
        self._error(405, "Method Not Allowed", "TRACE is not allowed")

    def do_CONNECT(self) -> None:
        self._error(405, "Method Not Allowed", "CONNECT is not allowed")

    def _read_json(self) -> Optional[dict[str, Any]]:
        ctype = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._error(415, "Unsupported Media Type", "Content-Type must be application/json")
            return None
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY:
            self._error(413 if length > _MAX_BODY else 400, "Bad Request", "invalid request payload size")
            return None
        try:
            val = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._error(400, "Bad Request", f"malformed JSON: {exc}")
            return None
        if not isinstance(val, dict):
            self._error(400, "Bad Request", "request body must be a JSON object")
            return None
        return val

    def _update_telemetry(self, device_id: str) -> None:
        """Store non-authoritative telemetry separately."""
        try:
            telemetry_path = self.server.runtime_root / "mobile" / "devices-telemetry.json"
            telemetry_path.parent.mkdir(parents=True, exist_ok=True)
            with self.server._state_lock:
                data = read_json(telemetry_path, {})
                if not isinstance(data, dict):
                    data = {}
                data[device_id] = {
                    "last_seen": utc_now_iso(),
                    "user_agent": self.headers.get("User-Agent"),
                    "app_version": self.headers.get("X-DevO-App-Version"),
                }
                write_json(telemetry_path, data, indent=2)
        except Exception:
            pass

    def _authenticate(self) -> Optional[MobileDevicePrincipal]:
        auth_header = self.headers.get("Authorization")
        try:
            ok, reason, principal = self.server.authorizer.validate_mobile_bearer(auth_header)
        except Exception as exc:
            self._error(503, "Service Unavailable", f"security store unavailable: {exc}")
            return None
        if not ok or principal is None:
            if any(term in reason.lower() for term in ("unreadable", "corrupt", "malformed", "unavailable")):
                self._error(503, "Service Unavailable", reason)
                return None
            self._error(401, "Unauthorized", reason)
            return None
        self._update_telemetry(principal.device_id)
        return principal

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if ".." in path or "\\" in path:
            self._error(400, "Bad Request", "path traversal is not allowed")
            return

        principal = self._authenticate()
        if principal is None:
            return

        if path == "/api/v1/mobile/v1/projects":
            summary = read_json(self.server.runtime_root / "summary.json", {})
            cfg = read_json(self.server.config_path, {}) if self.server.config_path else {}
            overview = self.server.projection_service.overview(summary, cfg, principal=principal)
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(overview))
            return

        match_proj = _PROJECT_PATH_RE.fullmatch(path)
        if match_proj:
            project_id = match_proj.group(1)
            snapshot_path = self.server.runtime_root / "projects" / f"{project_id}.json"
            snapshot = read_json(snapshot_path, None)
            if not isinstance(snapshot, dict):
                self._error(404, "Not Found", f"project {project_id!r} not found")
                return
            cfg = read_json(self.server.config_path, {}) if self.server.config_path else {}
            proj_view = self.server.projection_service.project_view(snapshot, cfg, principal=principal)
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(proj_view))
            return

        match_cmd = _CMD_PATH_RE.fullmatch(path)
        if match_cmd:
            cmd_id = match_cmd.group(1)
            try:
                res = self.server.control_client.command_status(cmd_id)
            except ControlAdapterError as exc:
                self._error(404, "Not Found", str(exc))
                return
            resp_body = res.get("data") if isinstance(res.get("data"), dict) else res
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(resp_body))
            return

        if path == "/api/v1/mobile/v1/alert-policy":
            policy = load_alert_policy(self.server.runtime_root)
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(policy))
            return

        if path == "/api/v1/mobile/v1/alerts":
            from dataclasses import asdict
            alerts = self.server.evaluate_and_broadcast_alerts()
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes({
                "schema_version": 1,
                "alerts": [asdict(a) for a in alerts],
            }))
            return

        if path == "/api/v1/mobile/v1/events":
            self._handle_sse_stream(parsed, principal)
            return

        if path == "/api/v1/mobile/v1/events/poll":
            self._handle_long_poll(parsed, principal)
            return

        self._error(404, "Not Found", "route not found")

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if ".." in path or "\\" in path:
            self._error(400, "Bad Request", "path traversal is not allowed")
            return

        # 1. Unauthenticated pairing route
        if path == "/api/v1/mobile/v1/pair":
            body = self._read_json()
            if body is None:
                return
            pairing_id = body.get("pairing_id")
            code = body.get("code")
            device_label = body.get("device_label", "android_device")
            try:
                result = self.server.authorizer.redeem_mobile_pairing(
                    pairing_id, code, device_label=device_label
                )
            except ValueError:
                # Indistinguishable failure
                self._error(400, "Bad Request", "pairing is missing, expired, or already used")
                return
            except Exception as exc:
                self._error(503, "Service Unavailable", f"security store unavailable: {exc}")
                return
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(result))
            return

        # All other POST routes require authentication
        principal = self._authenticate()
        if principal is None:
            return

        match_ctrl = _PROJECT_CONTROLS_PATH_RE.fullmatch(path)
        if match_ctrl:
            project_id = match_ctrl.group(1)
            body = self._read_json()
            if body is None:
                return
            action = body.get("action")
            if action not in MOBILE_CONTROL_ACTIONS:
                self._error(400, "Bad Request", f"unsupported mobile control action: {action!r}")
                return
            expected_revision = body.get("expected_revision")
            device_req_id = body.get("device_request_id")
            if not expected_revision or not device_req_id:
                self._error(400, "Bad Request", "expected_revision and device_request_id are required")
                return

            namespaced_req_id = f"{principal.device_id}-{str(device_req_id).strip()}"
            source_provenance = f"mobile_gateway:{principal.device_id}"

            try:
                result = self.server.control_client.submit_control(
                    adapter_request_id=namespaced_req_id,
                    project_id=project_id,
                    action=action,
                    expected_revision=str(expected_revision).strip(),
                    target=body.get("target"),
                    source=source_provenance,
                )
            except ControlAdapterConflictError as exc:
                self._error(409, "Conflict", str(exc))
                return
            except ControlAdapterRevisionMismatchError as exc:
                self._error(409, "Conflict", str(exc))
                return
            except ControlAdapterError as exc:
                self._error(400, "Bad Request", str(exc))
                return

            resp_payload = result.get("data") if isinstance(result.get("data"), dict) else result
            self._send(202, "Accepted", "application/json; charset=utf-8", _json_bytes(resp_payload))
            return

        match_ack = _ALERT_ACK_PATH_RE.fullmatch(path)
        if match_ack:
            alert_key = match_ack.group(1)
            ack_path = self.server.runtime_root / "mobile" / "alerts-ack.json"
            ack_path.parent.mkdir(parents=True, exist_ok=True)
            with self.server._state_lock:
                data = read_json(ack_path, {})
                if not isinstance(data, dict):
                    data = {}
                data[alert_key] = {"acknowledged": True, "acknowledged_at": utc_now_iso(), "by_device": principal.device_id}
                write_json(ack_path, data, indent=2)
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes({"alert_key": alert_key, "acknowledged": True}))
            return

        match_snooze = _ALERT_SNOOZE_PATH_RE.fullmatch(path)
        if match_snooze:
            alert_key = match_snooze.group(1)
            body = self._read_json()
            if body is None:
                return
            duration = max(60, min(86400, int(body.get("duration_seconds", 3600))))
            snoozed_until = (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat()
            ack_path = self.server.runtime_root / "mobile" / "alerts-ack.json"
            ack_path.parent.mkdir(parents=True, exist_ok=True)
            with self.server._state_lock:
                data = read_json(ack_path, {})
                if not isinstance(data, dict):
                    data = {}
                data[alert_key] = {"snoozed_until": snoozed_until, "snoozed_at": utc_now_iso(), "by_device": principal.device_id}
                write_json(ack_path, data, indent=2)
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes({"alert_key": alert_key, "snoozed_until": snoozed_until}))
            return

        self._error(404, "Not Found", "route not found")

    def do_PUT(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path != "/api/v1/mobile/v1/alert-policy":
            self._error(404, "Not Found", "route not found")
            return

        principal = self._authenticate()
        if principal is None:
            return

        body = self._read_json()
        if body is None:
            return

        try:
            save_alert_policy(self.server.runtime_root, body)
        except ValueError as exc:
            self._error(400, "Bad Request", str(exc))
            return
        self._send(200, "OK", "application/json; charset=utf-8", _json_bytes({"status": "saved"}))

    def _handle_sse_stream(self, parsed: Any, principal: MobileDevicePrincipal) -> None:
        qs = parse_qs(parsed.query)
        cursor = qs.get("cursor", [None])[0] or self.headers.get("Last-Event-ID")

        # Register open stream
        with self.server._streams_lock:
            self.server._open_streams.add(self)

        self.send_response(200, "OK")
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        resync, backlog, current_c = self.server.get_events_since(cursor)
        last_cursor = cursor if cursor else current_c

        try:
            if resync:
                summary = read_json(self.server.runtime_root / "summary.json", {})
                cfg = read_json(self.server.config_path, {}) if self.server.config_path else {}
                overview = self.server.projection_service.overview(summary, cfg, principal=principal)
                frame = f"event: resync_required\nid: {current_c}\ndata: {json.dumps(overview, ensure_ascii=False)}\n\n"
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
                last_cursor = current_c
            else:
                for ev in backlog:
                    ev_c = ev["cursor"]
                    ev_name = ev["event"]
                    ev_data = json.dumps(ev["data"], ensure_ascii=False)
                    frame = f"event: {ev_name}\nid: {ev_c}\ndata: {ev_data}\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                    last_cursor = ev_c

            while not self.server._shutdown_flag.is_set():
                with self.server._events_lock:
                    self.server._events_cv.wait(timeout=14.0)

                if self.server._shutdown_flag.is_set():
                    break

                # Revalidate device authorization before every frame or heartbeat
                ok, reason, _ = self.server.authorizer.lookup_mobile_device(principal.device_id)
                if not ok:
                    # Revoked or expired mid-stream: terminal closure
                    break

                # Send new events
                _, new_events, cur_c = self.server.get_events_since(last_cursor)
                if new_events:
                    for ev in new_events:
                        ev_c = ev["cursor"]
                        ev_name = ev["event"]
                        ev_data = json.dumps(ev["data"], ensure_ascii=False)
                        frame = f"event: {ev_name}\nid: {ev_c}\ndata: {ev_data}\n\n"
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                        last_cursor = ev_c
                else:
                    # Heartbeat ping frame
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True
            with self.server._streams_lock:
                self.server._open_streams.discard(self)

    def _handle_long_poll(self, parsed: Any, principal: MobileDevicePrincipal) -> None:
        qs = parse_qs(parsed.query)
        cursor = qs.get("cursor", [None])[0] or self.headers.get("Last-Event-ID")

        resync, events, cur_c = self.server.get_events_since(cursor)
        if resync:
            summary = read_json(self.server.runtime_root / "summary.json", {})
            cfg = read_json(self.server.config_path, {}) if self.server.config_path else {}
            overview = self.server.projection_service.overview(summary, cfg, principal=principal)
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes({
                "resync_required": True,
                "cursor": cur_c,
                "overview": overview,
                "events": [],
            }))
            return

        if events:
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes({
                "resync_required": False,
                "cursor": events[-1]["cursor"],
                "events": events,
            }))
            return

        # Wait up to 15s for new events
        with self.server._events_lock:
            self.server._events_cv.wait(timeout=14.0)

        # Revalidate device after wait
        ok, reason, _ = self.server.authorizer.lookup_mobile_device(principal.device_id)
        if not ok:
            self._error(401, "Unauthorized", f"device token revoked or expired: {reason}")
            return

        _, events_after, cur_after = self.server.get_events_since(cursor)
        self._send(200, "OK", "application/json; charset=utf-8", _json_bytes({
            "resync_required": False,
            "cursor": events_after[-1]["cursor"] if events_after else cur_after,
            "events": events_after,
        }))


def make_mobile_gateway(
    listen_address: str,
    port: int,
    runtime_root: Path | str,
    authorizer: MobileDeviceAuthorizer,
    projection_service: MobileProjectionService,
    control_client: ControlAdapterClient,
    *,
    config_path: Path | str | None = None,
    verify_bind: bool = True,
) -> MobileGatewayHTTPServer:
    """Create and bind the MobileGateway server on a verified Tailscale address."""
    if verify_bind:
        ok, reason = verify_tailscale_bind_address(listen_address)
        if not ok:
            raise ValueError(f"refusing MobileGateway bind: {reason}")

    return MobileGatewayHTTPServer(
        (listen_address, port),
        runtime_root=runtime_root,
        authorizer=authorizer,
        projection_service=projection_service,
        control_client=control_client,
        config_path=config_path,
    )
