"""Stdlib dashboard server with legacy reads and opt-in P12 Control API.

The standalone web process remains GET/HEAD-only. The unified daemon opts into
the bounded authenticated ``/api/v1/control/*`` POST surface; all other write
routes remain disabled. Static paths are allowlisted, history limits clamp to
1..100, and unknown/traversal/malformed routes fail closed.
``Cache-Control: no-store`` and ``X-Content-Type-Options: nosniff`` are always
present.

``/api/orchestration`` is a read-only projection of the dispatcher ledger: it
surfaces projects whose prepared Web Sol request cannot be delivered yet
(``delivery_state=unbound``) so the dashboard can ask the owner to bind/rebind
the ChatGPT conversation. The server never mutates the ledger.

Standalone ``run_web`` remains read-only. Only the unified daemon opts into
authenticated command enqueueing; HTTP never executes lifecycle actions and
never shells into observed projects.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import urlopen

from dev_orchestrator.core.dispatcher import DISPATCHER_STATE_FILE
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.accounting import ROLES, build_p11_report, reporting_event_store
from dev_orchestrator.accounting.events import EventWriteError
from dev_orchestrator.control.command_store import (
    ControlCommandConflictError,
    ControlCommandStore,
)
from dev_orchestrator.control.security import ControlSecurity, is_loopback
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.control.surface import project_control_view
from dev_orchestrator.core.control_commands import submit_control_command
from dev_orchestrator.platform.process import is_pid_alive
from dev_orchestrator.storage.json_store import (
    parse_utc,
    read_json,
    read_last_jsonl,
    utc_now,
    utc_now_iso,
    write_json,
)

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
_PROJECT_PATH_RE = re.compile(r"^/api/projects/([A-Za-z0-9_-]+)$")
_CONTROL_PROJECT_PATH_RE = re.compile(r"^/api/v1/control/projects/([A-Za-z0-9_-]+)$")
_CONTROL_COMMAND_PATH_RE = re.compile(r"^/api/v1/control/commands/([A-Za-z0-9_-]+)$")
_PAIRING_REVOKE_PATH_RE = re.compile(r"^/api/v1/control/adapter-pairings/([A-Za-z0-9_-]+)/revoke$")
_ALLOW_HEADER = "GET, HEAD"
_CONTROL_ALLOW_HEADER = "GET, HEAD, POST, OPTIONS"
_MAX_CONTROL_BODY = 64 * 1024


def monitor_payload(runtime_root: Path | str) -> dict[str, Any]:
    """Derive the /api/monitor payload from the monitor heartbeat file."""
    runtime = Path(runtime_root)
    raw = read_json(runtime / "monitor.json", {"state": "not_started"})
    if not isinstance(raw, dict):
        raw = {"state": "not_started"}
    pid_value = raw.get("pid")
    try:
        pid = int(pid_value) if pid_value is not None else 0
    except (TypeError, ValueError):
        pid = 0
    alive = pid > 0 and is_pid_alive(pid)
    last_tick = parse_utc(raw.get("last_tick_at"))
    age: Optional[float] = None
    if last_tick is not None:
        age = round(max(0.0, (utc_now() - last_tick).total_seconds()), 1)
    interval_value = raw.get("interval_seconds")
    try:
        interval = int(interval_value) if interval_value is not None else 60
    except (TypeError, ValueError):
        interval = 60
    stale = (not alive) or (age is None) or (age > interval * 2.5)
    state = raw.get("state")
    return {
        "state": state if isinstance(state, str) and state else "not_started",
        "pid": pid,
        "process_alive": alive,
        "interval_seconds": interval,
        "started_at": raw.get("started_at"),
        "last_tick_at": raw.get("last_tick_at"),
        "heartbeat_age_seconds": age,
        "stale": stale,
        "last_error": raw.get("last_error"),
    }


def _default_summary() -> dict[str, Any]:
    return {"observed_at": None, "project_count": 0, "projects": []}


def broker_proxy_payload(runtime_root: Path | str, endpoint: str) -> dict[str, Any]:
    observed_at = utc_now_iso()

    def unavailable(reason: str) -> dict[str, Any]:
        return {
            "available": False, "availability": "unavailable", "error": reason,
            "observed_at": observed_at, "unknown_fields": [],
        }

    runtime = Path(runtime_root)
    config = read_json(runtime / "aibroker.json", {})
    base_url = config.get("base_url") if isinstance(config, dict) else None
    if not isinstance(base_url, str) or not base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        return unavailable("AIBroker endpoint not configured")
    try:
        with urlopen(base_url.rstrip("/") + endpoint, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        return unavailable(str(exc))
    if not isinstance(payload, dict):
        return unavailable("invalid AIBroker response")
    availability = payload.get("availability")
    if availability not in {"available", "stale", "unavailable"}:
        availability = "available"
    result = dict(payload)
    result.update({
        "available": availability != "unavailable",
        "availability": availability,
        "observed_at": payload.get("observed_at") or observed_at,
    })
    result.setdefault("unknown_fields", [])
    return result


def orchestration_payload(runtime_root: Path | str) -> dict[str, Any]:
    """Project the dispatcher ledger into an orchestration-ready view.

    A project is surfaced as ``UNBOUND`` while it holds a prepared Web Sol
    request that is not yet deliverable (``delivery_state=unbound``) — e.g. a
    completed Worker occurrence whose ChatGPT conversation is not bound or is
    not being watched yet. The dashboard uses this to instruct the owner to
    bind/rebind the conversation so delivery resumes with the frozen request
    identity. Read-only: the server never mutates the dispatcher ledger.
    """
    runtime = Path(runtime_root)
    data = read_json(runtime / DISPATCHER_STATE_FILE, None)
    projects: dict[str, Any] = {}
    if not isinstance(data, dict):
        return {"projects": projects}
    events = data.get("worker_done")
    if not isinstance(events, dict):
        return {"projects": projects}
    for project_id, record in events.items():
        if not isinstance(project_id, str) or not isinstance(record, dict):
            continue
        snapshot = read_json(runtime / "projects" / (project_id + ".json"), None)
        if (
            isinstance(snapshot, dict)
            and snapshot.get("conversation_binding") is None
            and snapshot.get("orchestration_ready") is True
        ):
            # A current direct-AI project no longer depends on browser transport.
            # Historical dispatcher records must not surface as stale UNBOUND gates.
            continue
        occurrences = record.get("occurrences")
        if not isinstance(occurrences, dict):
            continue
        blocked = [
            occurrence
            for occurrence in occurrences.values()
            if isinstance(occurrence, dict)
            and occurrence.get("state") == "prepared"
            and occurrence.get("delivery_state") == "unbound"
        ]
        if not blocked:
            continue
        latest = max(blocked, key=lambda value: str(value.get("prepared_at") or ""))
        projects[project_id] = {
            "state": "UNBOUND",
            "delivery_state": "unbound",
            "request_id": latest.get("request_id"),
            "binding_id": latest.get("binding_id"),
            "prepared_at": latest.get("prepared_at"),
        }
    return {"projects": projects}


def watchdog_payload(runtime_root: Path | str) -> dict[str, Any]:
    """Return the read-only projection of the progress watchdog state."""
    runtime = Path(runtime_root)
    state_file = runtime / "watchdog.json"
    if not state_file.is_file():
        return {"projects": {}, "degraded": False}

    from dev_orchestrator.core.project_status import _watchdog_view
    from dev_orchestrator.core.watchdog import WATCHDOG_SCHEMA_VERSION
    data = read_json(state_file, None)
    if not isinstance(data, dict):
        return {"projects": {}, "degraded": True, "degraded_reason": "unreadable state"}

    # R3-F3: Validate schema version before trusting the file's degraded flag.
    # If the coordinator loaded a future-version file, it ran degraded in memory but
    # _preserve_existing_state_file prevented it from writing the degraded flag back.
    # Reading degraded directly from the original file would miss the degraded state.
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version > WATCHDOG_SCHEMA_VERSION:
        return {
            "projects": {},
            "degraded": True,
            "degraded_reason": f"unsupported or invalid watchdog schema version: {version!r}",
        }

    degraded = bool(data.get("degraded", False))
    projects_dict: dict[str, Any] = {}
    projects_data = data.get("projects")
    if isinstance(projects_data, dict):
        for pid in projects_data.keys():
            view = _watchdog_view(runtime, pid)
            if view is not None:
                projects_dict[pid] = view

    quarantined = data.get("quarantined_projects")
    if isinstance(quarantined, dict):
        for pid, reason in quarantined.items():
            if pid not in projects_dict:
                projects_dict[pid] = {
                    "schema_version": 1,
                    "state": "degraded",
                    "degraded_reason": reason,
                }

    res: dict[str, Any] = {"projects": projects_dict, "degraded": degraded}
    if data.get("degraded_reason"):
        res["degraded_reason"] = data.get("degraded_reason")
    return res


def accounting_payload(runtime_root: Path | str, query: str = "") -> dict[str, Any]:
    """Build the read-only P11 report exposed by the 8770 dashboard."""
    values = parse_qs(query)
    project_id = values.get("project_id", [None])[0]
    task_id = values.get("task_id", [None])[0]
    role = values.get("role", [None])[0]
    if role is not None and role not in ROLES:
        raise ValueError("invalid accounting role filter")
    try:
        read = reporting_event_store(runtime_root).read()
    except (ValueError, EventWriteError) as exc:
        return {"available": False, "error": str(exc), "data_status": "unavailable"}
    if read.corruptions:
        return {
            "available": False,
            "error": "execution accounting ledger is corrupt",
            "data_status": "unavailable",
            "corruptions": [
                {
                    "offset": item.offset,
                    "line_number": item.line_number,
                    "reason": item.reason,
                    "sample_hex": item.sample_hex,
                    "byte_count": item.byte_count,
                    "sample_truncated": item.sample_truncated,
                    "torn_tail": item.torn_tail,
                }
                for item in read.corruptions
            ],
        }
    scoped = [
        event for event in read.events
        if (project_id is None or event.get("project_id") == project_id)
        and (task_id is None or event.get("task_id") == task_id)
        and (role is None or event.get("role") == role)
    ]
    end_text = values.get("end", [None])[0]
    start_text = values.get("start", [None])[0]
    now = utc_now()
    default_end = end_text is None
    if default_end:
        end_text = now.isoformat()
    if start_text is None:
        observed = [parse_utc(event.get("occurred_at")) for event in scoped]
        observed = [item for item in observed if item is not None]
        start_moment = min(observed) if observed else now - timedelta(seconds=1)
        if default_end and start_moment >= now:
            end_text = (start_moment + timedelta(seconds=1)).isoformat()
        start_text = start_moment.isoformat()
    report = build_p11_report(
        read.events,
        start_text,
        end_text,
        project_id=project_id,
        task_id=task_id,
        role=role,
    ).as_dict()
    return {"available": True, **report}


def _control_envelope(data: Any, *, warnings: list[str] | None = None, sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "data": data,
        "warnings": list(warnings or []),
        "sources": list(sources or []),
    }


def _control_project_configs(config_path: Path | str | None) -> dict[str, dict[str, Any]]:
    if config_path is None:
        return {}
    try:
        config = load_projects_config(config_path)
    except (OSError, ValueError):
        return {}
    return {
        str(row.get("project_id")): row for row in config.get("projects") or []
        if isinstance(row, dict) and row.get("project_id")
    }


def _source_availability(value: Any) -> str:
    if isinstance(value, dict) and value.get("availability") in {"available", "stale", "unavailable"}:
        return str(value["availability"])
    return "available" if not isinstance(value, dict) or value.get("available", True) else "unavailable"


def control_overview_payload(
    runtime_root: Path | str, config_path: Path | str | None = None,
    bridge_store: Any | None = None,
) -> dict[str, Any]:
    """Build the one-call P12 operator view from existing durable projections."""
    runtime = Path(runtime_root)
    raw_summary = read_json(runtime / "summary.json", _default_summary())
    snapshots = raw_summary.get("projects") if isinstance(raw_summary, dict) else None
    configs = _control_project_configs(config_path)
    projects = [
        project_control_view(
            row, runtime, configs.get(str(row.get("project_id") or row.get("id") or "")),
            bridge_store,
        )
        for row in (snapshots or []) if isinstance(row, dict)
    ]
    resources = broker_proxy_payload(runtime, "/api/resources")
    executions = broker_proxy_payload(runtime, "/api/executions")
    accounting = accounting_payload(runtime)
    monitor = monitor_payload(runtime)
    watchdog = watchdog_payload(runtime)
    watchdog_projects = watchdog.get("projects") if isinstance(watchdog, dict) else {}
    for project in projects:
        project["watchdog"] = (
            watchdog_projects.get(str(project.get("project_id") or project.get("id") or ""))
            if isinstance(watchdog_projects, dict) else None
        )
    conversations = ConversationControlStore(runtime)
    command_store = ControlCommandStore(runtime)
    control_health = command_store.health()
    warnings: list[str] = []
    sources: list[dict[str, Any]] = []
    for name, value in (
        ("monitor", monitor), ("watchdog", watchdog), ("accounting", accounting),
        ("broker_resources", resources), ("broker_executions", executions),
    ):
        status = _source_availability(value)
        sources.append({"name": name, "availability": status})
        if status == "unavailable":
            warnings.append(f"{name} unavailable: {value.get('error') or 'unknown error'}")
        elif status == "stale":
            warnings.append(f"{name} stale")
    sources.append({"name": "control", "availability": "degraded" if control_health.get("degraded") else "available"})
    if control_health.get("degraded"):
        warnings.append("control store degraded; inspect quarantined corruption evidence")
    return _control_envelope({
        "monitor": monitor,
        "projects": projects,
        "resources": resources,
        "executions": executions,
        "watchdog": watchdog,
        "accounting": accounting,
        "commands": command_store.recent(20),
        "control_health": control_health,
        "sessions": conversations.list_sessions(),
        "bindings": conversations.list_bindings(),
    }, warnings=warnings, sources=sources)


class DevOrchestratorHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server that owns the DevOrchestrator web heartbeat."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple,
        runtime_root: Path | str,
        web_root: Path | str,
        listen_label: str,
        started_at: str,
        enable_control: bool = False,
        config_path: Path | str | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.web_root = Path(web_root)
        self.listen_label = listen_label
        self.started_at = started_at
        self.control_enabled = bool(enable_control and is_loopback(listen_label))
        self.config_path = Path(config_path) if config_path is not None else None
        self.command_store = ControlCommandStore(self.runtime_root)
        self.conversation_store = ConversationControlStore(self.runtime_root)
        self.bridge_store: Any | None = None
        self.control_security = ControlSecurity(self.runtime_root) if self.control_enabled else None
        self._heartbeat_lock = threading.Lock()
        super().__init__(server_address, _DashboardHandler)
        self.touch_heartbeat(None)

    def touch_heartbeat(self, last_request_at: Optional[str]) -> None:
        """Atomically refresh ``web.json`` under a process-local lock."""
        port = 0
        try:
            port = int(self.server_address[1])
        except (TypeError, ValueError):
            pass
        value = {
            "state": "running",
            "pid": os.getpid(),
            "listen_address": self.listen_label,
            "port": port,
            "started_at": self.started_at,
            "last_request_at": last_request_at,
        }
        with self._heartbeat_lock:
            write_json(self.runtime_root / "web.json", value)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class _DashboardHandler(BaseHTTPRequestHandler):
    """Read dashboard plus daemon-only authenticated Control API mutations."""

    protocol_version = "HTTP/1.1"
    server_version = "DevOrchestrator/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence access log
        return

    # -- entry points -----------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:
        self._dispatch(head_only=True)

    def do_POST(self) -> None:
        if not self.server.control_enabled:
            self._dispatch_method_not_allowed()
            return
        self._dispatch_post()

    def _unsupported(self) -> None:
        self._dispatch_method_not_allowed()

    do_PUT = _unsupported
    do_DELETE = _unsupported
    do_PATCH = _unsupported
    def do_OPTIONS(self) -> None:
        if not self.server.control_enabled or not self._client_is_loopback():
            self._dispatch_method_not_allowed()
            return
        path = urlsplit(self.path).path
        if (
            path not in {
                "/api/v1/control/adapter-pairings/redeem",
                "/api/v1/control/session-heartbeats",
            }
            or self.headers.get("Origin") != "https://chatgpt.com"
        ):
            self._dispatch_method_not_allowed()
            return
        self._send(
            204, "No Content", "application/json; charset=utf-8", b"", False,
            {
                "Access-Control-Allow-Origin": "https://chatgpt.com",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Access-Control-Max-Age": "300",
                "Vary": "Origin",
            },
        )
        self._touch_after_request()
    do_TRACE = _unsupported
    do_CONNECT = _unsupported

    # -- plumbing ---------------------------------------------------------
    def _send(
        self,
        status: int,
        reason: str,
        content_type: str,
        body: bytes,
        head_only: bool,
        extra_headers: Optional[dict] = None,
    ) -> None:
        self.send_response(status, reason)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def _error(
        self,
        status: int,
        reason: str,
        message: str,
        head_only: bool,
        extra_headers: Optional[dict] = None,
    ) -> None:
        payload = {"error": reason, "message": message}
        self._send(
            status,
            reason,
            "application/json; charset=utf-8",
            _json_bytes(payload),
            head_only,
            extra_headers,
        )

    def _dispatch_method_not_allowed(self) -> None:
        self._error(
            405,
            "Method Not Allowed",
            "route does not support this method",
            self.command == "HEAD",
            {"Allow": _CONTROL_ALLOW_HEADER if self.server.control_enabled else _ALLOW_HEADER},
        )

    def _touch_after_request(self) -> None:
        try:
            self.server.touch_heartbeat(utc_now_iso())
        except Exception:  # noqa: BLE001 - heartbeat must never break serving
            pass

    # -- routing ----------------------------------------------------------
    def _dispatch(self, head_only: bool) -> None:
        try:
            self._route(head_only)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001 - 500 on unexpected handler errors
            try:
                self._error(
                    500, "Internal Server Error", "request failed", head_only
                )
            except Exception:  # noqa: BLE001
                self.close_connection = True
        finally:
            self._touch_after_request()

    def _route(self, head_only: bool) -> None:
        parsed = urlsplit(self.path)
        raw_path = parsed.path
        try:
            path = unquote(raw_path)
        except Exception:  # noqa: BLE001
            path = ""
        if ".." in path or "\\" in path:
            self._error(400, "Bad Request", "path traversal is not allowed", head_only)
            return

        static = _STATIC.get(path)
        if static is not None:
            filename, content_type = static
            static_path = self.server.web_root / filename
            try:
                body = static_path.read_bytes()
            except OSError:
                self._error(404, "Not Found", "route not found", head_only)
                return
            self._send(200, "OK", content_type, body, head_only)
            return

        runtime = self.server.runtime_root
        if path == "/api/v1/control/overview":
            payload = control_overview_payload(runtime, self.server.config_path, self.server.bridge_store)
            payload["data"]["control_enabled"] = self.server.control_enabled
            for project in payload["data"]["projects"]:
                project["latest_control"] = self.server.command_store.latest_for_project(
                    str(project.get("project_id") or project.get("id") or "")
                )
        elif path == "/api/v1/control/projects":
            overview = control_overview_payload(runtime, self.server.config_path, self.server.bridge_store)
            payload = _control_envelope(overview["data"]["projects"], warnings=overview["warnings"], sources=overview["sources"])
        elif path.startswith("/api/v1/control/projects/"):
            match = _CONTROL_PROJECT_PATH_RE.fullmatch(path)
            if not match:
                self._error(404, "Not Found", "route not found", head_only); return
            project = read_json(runtime / "projects" / f"{match.group(1)}.json", None)
            if not isinstance(project, dict):
                self._error(404, "Not Found", "project snapshot not found", head_only); return
            configs = _control_project_configs(self.server.config_path)
            payload = _control_envelope(
                project_control_view(project, runtime, configs.get(match.group(1)), self.server.bridge_store),
                sources=[{"name": "project_runtime", "availability": "available"}],
            )
        elif path == "/api/v1/control/resources":
            data = broker_proxy_payload(runtime, "/api/resources")
            availability = _source_availability(data)
            warning = str(data.get("error")) if availability == "unavailable" else ("AIBroker data is stale" if availability == "stale" else None)
            payload = _control_envelope(data, warnings=[warning] if warning else [], sources=[{"name": "aibroker", "availability": availability}])
        elif path == "/api/v1/control/executions":
            data = broker_proxy_payload(runtime, "/api/executions")
            availability = _source_availability(data)
            warning = str(data.get("error")) if availability == "unavailable" else ("AIBroker data is stale" if availability == "stale" else None)
            payload = _control_envelope(data, warnings=[warning] if warning else [], sources=[{"name": "aibroker", "availability": availability}])
        elif path == "/api/v1/control/sessions":
            payload = _control_envelope(self.server.conversation_store.list_sessions(), sources=[{"name": "conversation_sessions", "availability": "available"}])
        elif path == "/api/v1/control/bindings":
            payload = _control_envelope(self.server.conversation_store.list_bindings(), sources=[{"name": "conversation_bindings", "availability": "available"}])
        elif path.startswith("/api/v1/control/commands/"):
            match = _CONTROL_COMMAND_PATH_RE.fullmatch(path)
            if not match:
                self._error(404, "Not Found", "route not found", head_only); return
            command = self.server.command_store.get(match.group(1))
            if command is None:
                self._error(404, "Not Found", "command not found", head_only); return
            payload = _control_envelope(command, sources=[{"name": "control_command_store", "availability": "available"}])
        elif path == "/api/monitor":
            payload = monitor_payload(runtime)
        elif path == "/api/summary":
            payload = read_json(runtime / "summary.json", _default_summary())
        elif path == "/api/events":
            payload = {"items": read_last_jsonl(runtime / "history" / "events.jsonl", self._query_limit(parsed.query))}
        elif path == "/api/runs":
            payload = {"items": read_last_jsonl(runtime / "history" / "runs.jsonl", self._query_limit(parsed.query))}
        elif path == "/api/orchestration":
            payload = orchestration_payload(runtime)
        elif path == "/api/watchdog":
            payload = watchdog_payload(runtime)
        elif path == "/api/accounting":
            try:
                payload = accounting_payload(runtime, parsed.query)
            except ValueError as exc:
                self._error(400, "Bad Request", str(exc), head_only)
                return
        elif path == "/api/broker/resources":
            payload = broker_proxy_payload(runtime, "/api/resources")
        elif path == "/api/broker/executions":
            payload = broker_proxy_payload(runtime, "/api/executions")
        elif path == "/api/broker/usage":
            payload = broker_proxy_payload(runtime, "/api/usage")
        elif path.startswith("/api/projects/"):
            match = _PROJECT_PATH_RE.fullmatch(path)
            if not match:
                self._error(404, "Not Found", "route not found", head_only)
                return
            project_path = runtime / "projects" / (match.group(1) + ".json")
            if not project_path.is_file():
                self._error(404, "Not Found", "project snapshot not found", head_only)
                return
            payload = read_json(project_path, {})
        else:
            self._error(404, "Not Found", "route not found", head_only)
            return
        self._send(
            200,
            "OK",
            "application/json; charset=utf-8",
            _json_bytes(payload),
            head_only,
        )

    def _client_is_loopback(self) -> bool:
        return is_loopback(str(self.client_address[0]))

    def _same_origin(self) -> bool:
        security = self.server.control_security
        return bool(security and security.valid_origin(
            self.headers.get("Origin"), self.headers.get("Host"), int(self.server.server_address[1])
        ))

    def _fetch_metadata_ok(self) -> bool:
        value = self.headers.get("Sec-Fetch-Site")
        return value is None or value in {"same-origin", "none"}

    def _owner_authorized(self) -> bool:
        security = self.server.control_security
        if security is None or not self._client_is_loopback():
            return False
        bearer = security.bearer_authorized(self.headers.get("Authorization"))
        if bearer:
            return self.headers.get("Origin") is None or self._same_origin()
        return self._same_origin() and self._fetch_metadata_ok() and security.browser_authorized(
            self.headers.get("Cookie"), self.headers.get("X-DevOrch-CSRF")
        )

    def _read_control_json(self) -> dict[str, Any] | None:
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._error(415, "Unsupported Media Type", "control request must be application/json", False)
            return None
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_CONTROL_BODY:
            self._error(413 if length > _MAX_CONTROL_BODY else 400, "Payload Too Large" if length > _MAX_CONTROL_BODY else "Bad Request", "invalid control request size", False)
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            self._error(400, "Bad Request", "request body must be valid JSON", False)
            return None
        if not isinstance(value, dict):
            self._error(400, "Bad Request", "request body must be a JSON object", False)
            return None
        return value

    def _dispatch_post(self) -> None:
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            self._error(500, "Internal Server Error", "request failed", False)
        finally:
            self._touch_after_request()

    def _route_post(self) -> None:
        if not self._client_is_loopback():
            self._error(403, "Forbidden", "control mutations require a loopback peer", False); return
        path = urlsplit(self.path).path
        security = self.server.control_security
        if security is None:
            self._dispatch_method_not_allowed(); return
        if path == "/api/v1/control/browser-sessions":
            if not self._same_origin() or not self._fetch_metadata_ok():
                self._error(403, "Forbidden", "browser session requires same-origin request", False); return
            session_id, csrf = security.create_browser_session()
            self._send(201, "Created", "application/json; charset=utf-8", _json_bytes(_control_envelope({"csrf_token": csrf, "expires_in_seconds": security.SESSION_TTL_SECONDS})), False,
                       {"Set-Cookie": f"devorch_control={session_id}; HttpOnly; SameSite=Strict; Path=/api/v1/control"})
            return
        if path == "/api/v1/control/adapter-pairings/redeem":
            if self.headers.get("Origin") != "https://chatgpt.com":
                self._error(403, "Forbidden", "pairing redemption requires the ChatGPT origin", False); return
            value = self._read_control_json()
            if value is None: return
            try:
                result = security.redeem_pairing(value.get("pairing_id"), value.get("code"))
            except ValueError as exc:
                self._error(400, "Bad Request", str(exc), False); return
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(result)), False,
                       {"Access-Control-Allow-Origin": "https://chatgpt.com", "Vary": "Origin"})
            return
        if path == "/api/v1/control/session-heartbeats":
            origin = self.headers.get("Origin")
            if origin not in (None, "https://chatgpt.com") or not (security.adapter_authorized(self.headers.get("Authorization")) or security.bearer_authorized(self.headers.get("Authorization"))):
                self._error(401, "Unauthorized", "valid heartbeat capability required", False); return
            value = self._read_control_json()
            if value is None: return
            try:
                session = self.server.conversation_store.heartbeat(
                    value.get("adapter"), value.get("binding_id"), title=value.get("title"),
                    url=value.get("url"), tab_instance_id=value.get("tab_instance_id"),
                )
            except ValueError as exc:
                self._error(400, "Bad Request", str(exc), False); return
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(session)), False,
                       {"Access-Control-Allow-Origin": "https://chatgpt.com", "Vary": "Origin"} if origin else None)
            return
        if not self._owner_authorized():
            self._error(401, "Unauthorized", "valid control authorization required", False); return
        if path == "/api/v1/control/adapter-pairings":
            self._send(201, "Created", "application/json; charset=utf-8", _json_bytes(_control_envelope(security.create_pairing())), False); return
        revoke = _PAIRING_REVOKE_PATH_RE.fullmatch(path)
        if revoke:
            try:
                result = security.revoke_pairing(revoke.group(1))
            except ValueError as exc:
                self._error(404, "Not Found", str(exc), False); return
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(result)), False); return
        if path != "/api/v1/control/commands":
            self._error(404, "Not Found", "route not found", False); return
        value = self._read_control_json()
        if value is None: return
        command_id = value.get("command_id")
        try:
            existing = self.server.command_store.get(command_id) if isinstance(command_id, str) else None
            record = submit_control_command(
                self.server.runtime_root, value.get("project_id"), value.get("action"),
                command_id=command_id, expected=value.get("expected"), target=value.get("target"),
                source="control_api",
            )
        except ControlCommandConflictError as exc:
            self._error(409, "Conflict", str(exc), False); return
        except (ValueError, TypeError) as exc:
            self._error(400, "Bad Request", str(exc), False); return
        self._send(200 if existing else 202, "OK" if existing else "Accepted", "application/json; charset=utf-8", _json_bytes(_control_envelope(record)), False)

    @staticmethod
    def _query_limit(query: str) -> int:
        values = parse_qs(query).get("limit")
        if not values:
            return 20
        try:
            return max(1, min(100, int(values[0])))
        except ValueError:
            return 20


def make_server(
    host: str, port: int, runtime_root: Path | str, web_root: Path | str,
    *, enable_control: bool = False, config_path: Path | str | None = None,
) -> DevOrchestratorHTTPServer:
    """Create (and bind) the dashboard server; runtime heartbeat is written now."""
    runtime = Path(runtime_root)
    runtime.mkdir(parents=True, exist_ok=True)
    started_at = utc_now_iso()
    return DevOrchestratorHTTPServer(
        (host, port), runtime, Path(web_root), host, started_at,
        enable_control=enable_control, config_path=config_path,
    )


def run_web(
    listen: str,
    port: int,
    runtime_root: Path | str,
    web_root: Path | str,
) -> int:
    """Run the dashboard server until interrupted (the CLI ``web`` command).

    Writes ``web.pid`` on start and removes it on graceful exit. The server is
    stopped via the PID recorded in ``web.pid``/``web.json`` only.
    """
    from dev_orchestrator.storage.json_store import write_text

    runtime = Path(runtime_root)
    runtime.mkdir(parents=True, exist_ok=True)
    server = make_server(listen, port, runtime, web_root)
    pid_path = runtime / "web.pid"
    write_text(pid_path, str(os.getpid()))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
    return 0
