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
from uuid import uuid4

from dev_orchestrator.core.dispatcher import DISPATCHER_STATE_FILE
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.accounting import ROLES, build_p11_report, reporting_event_store
from dev_orchestrator.accounting.events import EventWriteError
from dev_orchestrator.control.command_store import (
    ControlCommandConflictError,
    ControlCommandStore,
)
from dev_orchestrator.control.security import (
    CapabilityStoreUnavailableError,
    CapabilityVerdict,
    ControlSecurity,
    is_loopback,
)
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.core.websol_health import WebSolHealthStore
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
_CONTROL_PROJECT_BLOCKERS_PATH_RE = re.compile(r"^/api/v1/control/projects/([A-Za-z0-9_-]+)/blockers$")
_CONTROL_COMMAND_PATH_RE = re.compile(r"^/api/v1/control/commands/([A-Za-z0-9_-]+)$")
_CONTROL_JOB_LOGS_PATH_RE = re.compile(r"^/api/v1/control/jobs/([A-Za-z0-9_-]+)/logs$")
_CONTROL_JOB_PATH_RE = re.compile(r"^/api/v1/control/jobs/([A-Za-z0-9_-]+)$")
_PAIRING_REVOKE_PATH_RE = re.compile(r"^/api/v1/control/adapter-pairings/([A-Za-z0-9_-]+)/revoke$")
_CAPABILITY_REVOKE_PATH_RE = re.compile(r"^/api/v1/control/web-bridge-capabilities/([A-Za-z0-9_-]+)/revoke$")
_TRANSPORT_CAPABILITY_REVOKE_PATH_RE = re.compile(r"^/api/v1/control/(?:transport-capabilities|transport/capabilities)/([A-Za-z0-9_-]+)/revoke$")
_MOBILE_DEVICE_REVOKE_PATH_RE = re.compile(r"^/api/v1/control/mobile/devices/([A-Za-z0-9_-]+)/revoke$")
_CONTROL_REVIEWS_PATH_RE = re.compile(r"^/api/v1/control/reviews$")
_CONTROL_REVIEW_SESSION_PATH_RE = re.compile(r"^/api/v1/control/reviews/([A-Za-z0-9_:-]+)$")
_CONTROL_REVIEW_FINDINGS_PATH_RE = re.compile(r"^/api/v1/control/reviews/([A-Za-z0-9_:-]+)/findings$")
_CONTROL_REVIEW_COVERAGE_PATH_RE = re.compile(r"^/api/v1/control/reviews/([A-Za-z0-9_:-]+)/coverage$")
_CONTROL_REVIEW_ARTIFACT_PATH_RE = re.compile(r"^/api/v1/control/reviews/([A-Za-z0-9_:-]+)/artifacts/([A-Za-z0-9_.-]+)$")

_ALLOW_HEADER = "GET, HEAD"
_CONTROL_ALLOW_HEADER = "GET, HEAD, POST, OPTIONS"
_MAX_CONTROL_BODY = 64 * 1024
_MAX_STAGE_WRITE_BODY = 16 * 1024 * 1024

_SELF_AUTHENTICATING_TRANSPORT_PATHS = {
    "/api/v1/control/transport/exec",
    "/api/v1/control/transport/spawn",
    "/api/v1/control/transport/poll",
    "/api/v1/control/transport/cancel",
    "/api/v1/control/transport/stage-write",
    "/api/v1/control/transport/write",
}


def _get_transport_for_host(
    runtime_root: Path,
    host_id: Optional[str] = None,
    *,
    operation: str = "read_file",
    command_ref: Optional[str] = None,
    effect_class: str = "read_only",
    policy_digest: Optional[str] = None,
):
    from dev_orchestrator.transport.hosts import get_transport_for_host
    return get_transport_for_host(
        runtime_root=runtime_root,
        host_id=host_id,
        operation=operation,
        command_ref=command_ref,
        effect_class=effect_class,
        policy_digest=policy_digest,
    )


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
        self.websol_health_store = WebSolHealthStore(self.runtime_root)
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
                "/api/v1/control/adapter-capabilities/renew",
                "/api/v1/control/session-heartbeats",
                "/api/v1/control/bridge/requests",
                "/api/v1/control/web-bridge/requests",
            }
            or self.headers.get("Origin") != "https://chatgpt.com"
        ):
            self._dispatch_method_not_allowed()
            return
        self._send(
            204, "No Content", "application/json; charset=utf-8", b"", False,
            {
                "Access-Control-Allow-Origin": "https://chatgpt.com",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
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
            match_blockers = _CONTROL_PROJECT_BLOCKERS_PATH_RE.fullmatch(path)
            if match_blockers:
                project_id = match_blockers.group(1)
                project = read_json(runtime / "projects" / f"{project_id}.json", None)
                summary = read_json(runtime / "summary.json", {})
                if isinstance(summary, dict) and isinstance(summary.get("projects"), list):
                    projected = next((
                        item for item in summary["projects"]
                        if isinstance(item, dict) and str(item.get("project_id") or item.get("id") or "") == project_id
                    ), None)
                    if isinstance(projected, dict):
                        project = projected
                configs = _control_project_configs(self.server.config_path)
                from dev_orchestrator.core.blockers import explain_block, blocker_payload
                blockers = explain_block(
                    project_id=project_id,
                    project_config=configs.get(project_id),
                    snapshot=project,
                    runtime_root=runtime,
                    config_path=self.server.config_path,
                    action="continue",
                )
                payload = _control_envelope(
                    blocker_payload(blockers),
                    sources=[{"name": "project_blockers", "availability": "available"}],
                )
            else:
                match = _CONTROL_PROJECT_PATH_RE.fullmatch(path)
                if not match:
                    self._error(404, "Not Found", "route not found", head_only); return
                project = read_json(runtime / "projects" / f"{match.group(1)}.json", None)
                if not isinstance(project, dict):
                    self._error(404, "Not Found", "project snapshot not found", head_only); return
                # The per-project mirror is intentionally monitor-local and may be
                # overwritten with raw IDLE before orchestration lifecycle overlay.
                # Control capability must use the daemon's projected summary truth.
                summary = read_json(runtime / "summary.json", {})
                if isinstance(summary, dict) and isinstance(summary.get("projects"), list):
                    projected = next((
                        item for item in summary["projects"]
                        if isinstance(item, dict) and str(item.get("project_id") or item.get("id") or "") == match.group(1)
                    ), None)
                    if isinstance(projected, dict):
                        project = projected
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
        elif path == "/api/v1/control/websol-health":
            if not self._owner_authorized():
                self._error(401, "Unauthorized", "valid control authorization required", head_only); return
            qs = parse_qs(parsed.query)
            proj_id = qs.get("project_id", [None])[0]
            adapter = qs.get("adapter", ["chatgpt_web"])[0]
            binding_id = qs.get("binding_id", [None])[0]
            health_store = getattr(self.server, "websol_health_store", None)
            if health_store is None:
                health_store = WebSolHealthStore(runtime)
            if proj_id and binding_id:
                rec = health_store.get(proj_id, adapter, binding_id)
                data = rec.to_dict() if rec else None
            else:
                data = [r.to_dict() for r in health_store.list_all()]
            payload = _control_envelope(data, sources=[{"name": "websol_health", "availability": "available"}])
        elif path == "/api/v1/control/mobile/devices":
            if not self._owner_authorized():
                self._error(401, "Unauthorized", "valid control authorization required", head_only); return
            devices = self.server.control_security.list_mobile_devices() if self.server.control_security else []
            payload = _control_envelope(devices, sources=[{"name": "mobile_devices", "availability": "available"}])
        elif path.startswith("/api/v1/control/commands/"):
            match = _CONTROL_COMMAND_PATH_RE.fullmatch(path)
            if not match:
                self._error(404, "Not Found", "route not found", head_only); return
            command = self.server.command_store.get(match.group(1))
            if command is None:
                self._error(404, "Not Found", "command not found", head_only); return
            payload = _control_envelope(command, sources=[{"name": "control_command_store", "availability": "available"}])
        elif path == "/api/v1/control/logs":
            if not self._owner_authorized():
                self._error(401, "Unauthorized", "valid control authorization required", head_only); return
            from dev_orchestrator.control.logs import build_control_logs
            try:
                payload = build_control_logs(runtime, parsed.query)
            except ValueError as exc:
                self._error(400, "Bad Request", str(exc), head_only); return
        elif path == "/api/v1/control/jobs":
            if not self._owner_authorized():
                self._error(401, "Unauthorized", "valid control authorization required", head_only); return
            from dev_orchestrator.jobs.models import JobCorruptionError
            from dev_orchestrator.jobs.store import ExecutionJobStore
            store = ExecutionJobStore(runtime, read_only=True)
            qs = parse_qs(parsed.query)
            proj_filter = qs.get("project_id", [None])[0]
            try:
                jobs_list = store.list(project_id=proj_filter)
            except JobCorruptionError as exc:
                self._error(500, "Internal Server Error", f"job store corruption: {exc}", head_only); return
            payload = _control_envelope(jobs_list, sources=[{"name": "execution_jobs", "availability": "available"}])
        elif path.startswith("/api/v1/control/jobs/"):
            match_logs = _CONTROL_JOB_LOGS_PATH_RE.fullmatch(path)
            if match_logs:
                if not self._owner_authorized():
                    self._error(401, "Unauthorized", "valid control authorization required", head_only); return
                from dev_orchestrator.jobs.models import JobCorruptionError
                from dev_orchestrator.jobs.store import ExecutionJobStore
                from dev_orchestrator.jobs.logs import BoundedNDJSONLog
                store = ExecutionJobStore(runtime, read_only=True)
                job_id = match_logs.group(1)
                try:
                    rec = store.get(job_id)
                except JobCorruptionError as exc:
                    self._error(500, "Internal Server Error", f"job record corrupted: {exc}", head_only); return
                if rec is None:
                    self._error(404, "Not Found", "job not found", head_only); return
                qs = parse_qs(parsed.query)
                cursor = int(qs.get("cursor", [0])[0])
                limit = int(qs.get("limit", [100])[0])
                log_reader = BoundedNDJSONLog(store._job_dir(job_id) / "log.ndjson")
                logs_data = log_reader.read_paginated(cursor=cursor, limit=limit)
                payload = _control_envelope(logs_data, sources=[{"name": "job_logs", "availability": "available"}])
            else:
                match_job = _CONTROL_JOB_PATH_RE.fullmatch(path)
                if not match_job:
                    self._error(404, "Not Found", "route not found", head_only); return
                if not self._owner_authorized():
                    self._error(401, "Unauthorized", "valid control authorization required", head_only); return
                from dev_orchestrator.control.logs import redact_secrets
                from dev_orchestrator.jobs.models import JobCorruptionError
                from dev_orchestrator.jobs.store import ExecutionJobStore
                store = ExecutionJobStore(runtime, read_only=True)
                job_id = match_job.group(1)
                try:
                    rec = store.get(job_id)
                except JobCorruptionError as exc:
                    self._error(500, "Internal Server Error", f"job record corrupted: {exc}", head_only); return
                if rec is None:
                    self._error(404, "Not Found", "job not found", head_only); return
                redacted_rec = redact_secrets(rec.to_dict())
                payload = _control_envelope(redacted_rec, sources=[{"name": "execution_jobs", "availability": "available"}])

        elif path == "/api/v1/control/reviews":
            if not self._owner_authorized():
                self._error(401, "Unauthorized", "valid control authorization required", head_only); return
            from dev_orchestrator.control.logs import redact_secrets
            from dev_orchestrator.review.store import ReviewSessionStore
            store = ReviewSessionStore(runtime, read_only=True)
            qs = parse_qs(parsed.query)
            proj_filter = qs.get("project_id", [None])[0]
            sessions = store.list_sessions(project_id=proj_filter)
            payload = _control_envelope(redact_secrets(sessions), sources=[{"name": "review_sessions", "availability": "available"}])

        elif path.startswith("/api/v1/control/reviews/"):
            if not self._owner_authorized():
                self._error(401, "Unauthorized", "valid control authorization required", head_only); return
            from dev_orchestrator.control.logs import redact_secrets
            from dev_orchestrator.review.store import ReviewSessionStore
            store = ReviewSessionStore(runtime, read_only=True)

            match_findings = _CONTROL_REVIEW_FINDINGS_PATH_RE.fullmatch(path)
            match_coverage = _CONTROL_REVIEW_COVERAGE_PATH_RE.fullmatch(path)
            match_artifact = _CONTROL_REVIEW_ARTIFACT_PATH_RE.fullmatch(path)
            match_session = _CONTROL_REVIEW_SESSION_PATH_RE.fullmatch(path)

            if match_findings:
                session_id = match_findings.group(1)
                session = store.get_session(session_id)
                if session is None:
                    self._error(404, "Not Found", "review session not found", head_only); return
                findings_list = []
                if session.result:
                    findings_list = [f.to_dict() for f in session.result.findings]
                elif session.job_id:
                    from dev_orchestrator.jobs.store import ExecutionJobStore
                    jstore = ExecutionJobStore(runtime, read_only=True)
                    art = jstore.get_output_artifact(session.job_id, "findings.json")
                    if art and isinstance(art.get("content"), list):
                        findings_list = art["content"]
                qs = parse_qs(parsed.query)
                sev_filter = qs.get("severity", [None])[0]
                rule_filter = qs.get("rule_id", [None])[0]
                if sev_filter:
                    findings_list = [f for f in findings_list if isinstance(f, dict) and f.get("severity") == sev_filter]
                if rule_filter:
                    findings_list = [f for f in findings_list if isinstance(f, dict) and f.get("rule_id") == rule_filter]
                payload = _control_envelope(redact_secrets(findings_list), sources=[{"name": "review_findings", "availability": "available"}])

            elif match_coverage:
                session_id = match_coverage.group(1)
                session = store.get_session(session_id)
                if session is None:
                    self._error(404, "Not Found", "review session not found", head_only); return
                cov_data = {}
                if session.result and session.result.coverage:
                    cov_data = session.result.coverage.to_dict()
                elif session.job_id:
                    from dev_orchestrator.jobs.store import ExecutionJobStore
                    jstore = ExecutionJobStore(runtime, read_only=True)
                    art = jstore.get_output_artifact(session.job_id, "coverage.json")
                    if art and isinstance(art.get("content"), dict):
                        cov_data = art["content"]
                payload = _control_envelope(redact_secrets(cov_data), sources=[{"name": "review_coverage", "availability": "available"}])

            elif match_artifact:
                session_id = match_artifact.group(1)
                art_name = match_artifact.group(2)
                session = store.get_session(session_id)
                if session is None:
                    self._error(404, "Not Found", "review session not found", head_only); return
                if not session.job_id:
                    self._error(404, "Not Found", "review job not found", head_only); return
                if art_name in (".", ".."):
                    self._error(404, "Not Found", f"artifact {art_name!r} not found", head_only); return
                from dev_orchestrator.jobs.store import ExecutionJobStore
                jstore = ExecutionJobStore(runtime, read_only=True)
                try:
                    art = jstore.get_output_artifact(session.job_id, art_name)
                except ValueError:
                    self._error(404, "Not Found", f"artifact {art_name!r} not found", head_only); return
                if art is None:
                    self._error(404, "Not Found", f"artifact {art_name!r} not found", head_only); return
                art_clean = {k: v for k, v in art.items() if k != "raw_bytes"}
                payload = _control_envelope(redact_secrets(art_clean), sources=[{"name": "review_artifact", "availability": "available"}])

            elif match_session:
                session_id = match_session.group(1)
                session = store.get_session(session_id)
                if session is None:
                    self._error(404, "Not Found", "review session not found", head_only); return
                payload = _control_envelope(redact_secrets(session.to_dict()), sources=[{"name": "review_sessions", "availability": "available"}])

            else:
                self._error(404, "Not Found", "route not found", head_only); return

        elif path == "/api/v1/control/transport/hosts":
            if not self._authorized_control_read():
                self._error(401, "Unauthorized", "valid control authorization required", head_only); return
            from dev_orchestrator.transport.hosts import load_transport_hosts_config
            hosts_cfg = load_transport_hosts_config(runtime)
            payload = _control_envelope({
                "default_host": hosts_cfg.default_host,
                "hosts": {
                    h_id: {
                        "host_id": prof.host_id,
                        "candidate_order": prof.candidate_order,
                        "expected_host_identity": prof.expected_host_identity,
                        "response_limits": prof.response_limits,
                        "approved_policy_pins": prof.approved_policy_pins,
                        "os_family": prof.os_family,
                        "path_style": prof.path_style,
                        "helper_version": prof.helper_version,
                        "enabled": prof.enabled,
                    }
                    for h_id, prof in hosts_cfg.hosts.items()
                },
            })
        elif path == "/api/v1/control/transport-capabilities":
            if not self._authorized_control_read():
                self._error(401, "Unauthorized", "valid control authorization required", head_only); return
            from dev_orchestrator.control.security import list_transport_capabilities
            q_params = parse_qs(parsed.query)
            proj_id = q_params.get("project_id", [None])[0]
            caps = list_transport_capabilities(project_id=proj_id, runtime_root=runtime)
            payload = _control_envelope({"items": caps, "capabilities": caps, "count": len(caps)})
        elif path == "/api/v1/control/transport/capabilities":
            if not self._authorized_control_read():
                self._error(401, "Unauthorized", "valid control authorization required", head_only); return
            q_params = parse_qs(parsed.query)
            if "list" in q_params or "tokens" in q_params or ("project_id" in q_params and "host_id" not in q_params):
                from dev_orchestrator.control.security import list_transport_capabilities
                proj_id = q_params.get("project_id", [None])[0]
                caps = list_transport_capabilities(project_id=proj_id, runtime_root=runtime)
                payload = _control_envelope({"items": caps, "capabilities": caps, "count": len(caps)})
            else:
                from dev_orchestrator.transport.hosts import HostCapabilityCache, load_transport_hosts_config
                host_id = q_params.get("host_id", ["local"])[0]
                hosts_cfg = load_transport_hosts_config(runtime)
                cache = HostCapabilityCache(runtime, hosts_config=hosts_cfg)
                try:
                    caps = cache.get_capabilities(host_id)
                except ValueError as exc:
                    self._error(404, "Not Found", str(exc), head_only); return
                payload = _control_envelope({
                    "host_id": caps.host_id,
                    "os_family": caps.os_family,
                    "path_style": caps.path_style,
                    "helper_version": caps.helper_version,
                    "jobs_config_valid": caps.jobs_config_valid,
                    "approved_policy_pins": caps.approved_policy_pins,
                    "response_limits": caps.response_limits,
                    "supported_operations": caps.supported_operations,
                    "probed_at": caps.probed_at,
                })
        elif path == "/api/v1/control/transport/operations":
            if not self._authorized_control_read():
                self._error(401, "Unauthorized", "valid control authorization required", head_only); return
            q_params = parse_qs(parsed.query)
            limit = int(q_params.get("limit", [50])[0])
            cursor = int(q_params.get("cursor", [0])[0])
            from dev_orchestrator.transport.observability import read_transport_operations
            ops_data = read_transport_operations(runtime, cursor=cursor, limit=limit)
            ops_data["operations"] = ops_data.get("items", [])
            ops_data["count"] = len(ops_data.get("items", []))
            payload = _control_envelope(ops_data)
        elif path == "/api/v1/control/transport/stat":
            q_params = parse_qs(parsed.query)
            path_param = q_params.get("path", [""])[0]
            project_id = q_params.get("project_id", [None])[0]
            host_id = q_params.get("host_id", ["local"])[0]
            auth_ok, auth_err, _, cap_id = self._validate_transport_auth(
                operation="stat",
                project_id=project_id or "",
                host_id=host_id,
                path=path_param,
                require_nonce=False,
            )
            if not auth_ok:
                self._error(401, "Unauthorized", auth_err or "valid control authorization required", head_only); return
            try:
                transport = _get_transport_for_host(runtime, host_id, operation="stat", effect_class="read_only")
                stat_res = transport.stat(path_param, host_id=host_id, project_id=project_id)
                payload = _control_envelope({
                    "status": stat_res.status,
                    "path": stat_res.path,
                    "exists": stat_res.exists,
                    "is_file": stat_res.is_file,
                    "is_dir": stat_res.is_dir,
                    "size_bytes": stat_res.size_bytes,
                    "modified_at": stat_res.modified_at,
                    "sha256": stat_res.sha256,
                    "host_identity": stat_res.host_identity,
                    "error": stat_res.error,
                })
            except Exception as exc:
                self._error(400, "Bad Request", str(exc), head_only); return

            try:
                from dev_orchestrator.transport.observability import log_transport_operation
                log_transport_operation(
                    runtime,
                    operation_id=f"stat-{uuid4().hex[:12]}",
                    operation="stat",
                    host_id=host_id,
                    selected_transport=getattr(getattr(transport, "last_selection", None), "selected_transport", "local"),
                    status=stat_res.status,
                    project_id=project_id,
                    candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
                    capability_id=cap_id,
                    error=stat_res.error,
                )
            except Exception:
                pass
        elif path == "/api/v1/control/transport/read":
            q_params = parse_qs(parsed.query)
            project_id = q_params.get("project_id", [""])[0]
            path_param = q_params.get("path", [""])[0]
            host_id = q_params.get("host_id", ["local"])[0]
            auth_ok, auth_err, _, cap_id = self._validate_transport_auth(
                operation="read_file",
                project_id=project_id,
                host_id=host_id,
                path=path_param,
                require_nonce=False,
            )
            if not auth_ok:
                self._error(401, "Unauthorized", auth_err or "valid control authorization required", head_only); return
            import base64
            from dev_orchestrator.transport.contracts import FileReadRequest
            max_bytes = int(q_params.get("max_bytes", [10 * 1024 * 1024])[0])
            offset_bytes = int(q_params.get("offset_bytes", [0])[0])
            try:
                transport = _get_transport_for_host(runtime, host_id, operation="read_file", effect_class="read_only")
                file_res = transport.read_file(FileReadRequest(project_id=project_id, path=path_param, host_id=host_id, max_bytes=max_bytes, offset_bytes=offset_bytes))
                b64_str = base64.b64encode(file_res.content_bytes).decode("ascii") if file_res.content_bytes else None
                payload = _control_envelope({
                    "status": file_res.status,
                    "path": file_res.path,
                    "content_sha256": file_res.content_sha256,
                    "size_bytes": file_res.size_bytes,
                    "content_base64": b64_str,
                    "host_identity": file_res.host_identity,
                    "error": file_res.error,
                })
            except Exception as exc:
                self._error(400, "Bad Request", str(exc), head_only); return

            try:
                from dev_orchestrator.transport.observability import log_transport_operation
                log_transport_operation(
                    runtime,
                    operation_id=f"read-{uuid4().hex[:12]}",
                    operation="read_file",
                    host_id=host_id,
                    selected_transport=getattr(getattr(transport, "last_selection", None), "selected_transport", "local"),
                    status=file_res.status,
                    project_id=project_id,
                    candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
                    capability_id=cap_id,
                    error=file_res.error,
                )
            except Exception:
                pass

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

    def _validate_transport_auth(
        self,
        *,
        operation: str,
        project_id: str = "",
        host_id: str = "local",
        command_ref: Optional[str] = None,
        path: Optional[str] = None,
        require_nonce: bool = True,
    ) -> tuple[bool, Optional[str], Optional[dict[str, Any]], Optional[str]]:
        if self._owner_authorized():
            return True, None, None, None
        security = self.server.control_security
        if security is None or not self._client_is_loopback():
            return False, "control security unavailable or non-loopback", None, None
        auth_header = self.headers.get("Authorization")
        if not auth_header:
            return False, "missing authorization header", None, None
        token = auth_header.split()[-1] if " " in auth_header else auth_header
        ok, reason, row = security.validate_transport_capability(
            token,
            project_id=project_id,
            host_id=host_id,
            operation=operation,
            command_ref=command_ref,
            path=path,
        )
        if not ok or not row:
            return False, f"transport capability unauthorized: {reason}", None, None
        if require_nonce:
            nonce = self.headers.get("X-DevOrch-Nonce")
            if not nonce or not security.consume_request_nonce(row["capability_id"], nonce):
                return False, "transport capability request nonce invalid or replayed", None, None
        return True, None, row, row.get("capability_id")

    def _authorized_control_read(
        self,
        operation: Optional[str] = None,
        project_id: str = "",
        host_id: str = "local",
        path: Optional[str] = None,
    ) -> bool:
        if self._owner_authorized():
            return True
        if operation is None:
            return False
        ok, _, _, _ = self._validate_transport_auth(
            operation=operation,
            project_id=project_id,
            host_id=host_id,
            path=path,
            require_nonce=False,
        )
        return ok

    def _read_control_json(self, max_bytes: int = _MAX_CONTROL_BODY) -> dict[str, Any] | None:
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._error(415, "Unsupported Media Type", "control request must be application/json", False)
            return None
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > max_bytes:
            self._error(413 if length > max_bytes else 400, "Payload Too Large" if length > max_bytes else "Bad Request", "invalid control request size", False)
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
        runtime = self.server.runtime_root
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
                if hasattr(self.server, "websol_health_store") and self.server.websol_health_store is not None:
                    self.server.websol_health_store.invalidate_generation(reason="pairing_redeemed")
            except ValueError as exc:
                self._error(400, "Bad Request", str(exc), False); return
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(result)), False,
                       {"Access-Control-Allow-Origin": "https://chatgpt.com", "Vary": "Origin"})
            return
        if path == "/api/v1/control/adapter-capabilities/renew":
            origin = self.headers.get("Origin")
            if origin not in (None, "https://chatgpt.com") and not self._same_origin():
                self._error(403, "Forbidden", "invalid origin for renewal", False); return
            value = self._read_control_json()
            if value is None: return
            auth_header = self.headers.get("Authorization")
            token_or_header = auth_header or (value.get("token") if isinstance(value, dict) else None)
            grace = value.get("grace_period_seconds", 300) if isinstance(value, dict) else 300
            try:
                renewal = security.renew_session_capability(token_or_header, grace_period_seconds=int(grace))
            except CapabilityStoreUnavailableError:
                self._error(503, "Service Unavailable", "capability_store_unavailable", False,
                            extra_headers={"Access-Control-Allow-Origin": "https://chatgpt.com", "Vary": "Origin"} if origin else None)
                return
            except ValueError as exc:
                self._error(400, "Bad Request", str(exc), False,
                            extra_headers={"Access-Control-Allow-Origin": "https://chatgpt.com", "Vary": "Origin"} if origin else None)
                return
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(renewal)), False,
                       {"Access-Control-Allow-Origin": "https://chatgpt.com", "Vary": "Origin"} if origin else None)
            return
        if path == "/api/v1/control/session-heartbeats":
            origin = self.headers.get("Origin")
            if origin not in (None, "https://chatgpt.com") and not self._same_origin():
                self._error(403, "Forbidden", "invalid origin for heartbeat", False); return
            auth_header = self.headers.get("Authorization")
            is_bearer = security.bearer_authorized(auth_header)
            cap_verdict, cap_reason, _ = security.capability_status(auth_header)

            if cap_verdict == CapabilityVerdict.UNAVAILABLE:
                self._error(503, "Service Unavailable", "capability_store_unavailable", False,
                            extra_headers={"Access-Control-Allow-Origin": "https://chatgpt.com", "Vary": "Origin"} if origin else None)
                return
            if not is_bearer and cap_verdict != CapabilityVerdict.VALID:
                reason = "revoked_capability" if cap_verdict == CapabilityVerdict.REVOKED else "unknown_capability"
                self._error(401, "Unauthorized", reason, False,
                            extra_headers={"Access-Control-Allow-Origin": "https://chatgpt.com", "Vary": "Origin"} if origin else None)
                return

            value = self._read_control_json()
            if value is None: return

            adapter = value.get("adapter")
            binding_id = value.get("binding_id")
            cap_id = security.capability_identity(auth_header) if not is_bearer else None
            cap_source = "capability" if not is_bearer else "bearer"

            try:
                session = self.server.conversation_store.heartbeat(
                    adapter, binding_id, title=value.get("title"),
                    url=value.get("url"), tab_instance_id=value.get("tab_instance_id"),
                    capability_id=cap_id, capability_source=cap_source,
                )
            except ValueError as exc:
                self._error(400, "Bad Request", str(exc), False); return

            proj_id = value.get("project_id")
            if not proj_id and self.server.config_path and self.server.config_path.exists():
                try:
                    cfg = load_projects_config(self.server.config_path)
                    for p in cfg.get("projects") or []:
                        b = p.get("conversation_binding") or {}
                        if b.get("adapter") == adapter and b.get("binding_id") == binding_id:
                            proj_id = p.get("project_id")
                            break
                except Exception:
                    pass

            websol_health = None
            if hasattr(self.server, "websol_health_store") and proj_id:
                h_rec = self.server.websol_health_store.get(proj_id, adapter, binding_id)
                websol_health = h_rec.to_dict() if h_rec else None

            response_payload = {
                **session,
                "session": session,
                "websol_health": websol_health,
                "capability_id": cap_id,
            }

            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(response_payload)), False,
                       {"Access-Control-Allow-Origin": "https://chatgpt.com", "Vary": "Origin"} if origin else None)
            return
        if path in {"/api/v1/control/bridge/requests", "/api/v1/control/web-bridge/requests"}:
            origin = self.headers.get("Origin")
            if origin not in (None, "https://chatgpt.com") and not self._same_origin():
                self._error(403, "Forbidden", "invalid origin for WebBridge request", False); return
            value = self._read_control_json()
            if value is None: return

            auth_header = self.headers.get("Authorization")
            is_master = security.bearer_authorized(auth_header)
            binding = value.get("binding") if isinstance(value.get("binding"), dict) else {}
            b_adapter = binding.get("adapter")
            b_id = binding.get("binding_id")
            project_id = value.get("project_id")

            if not is_master:
                if not isinstance(project_id, str) or not isinstance(b_id, str) or not isinstance(b_adapter, str):
                    self._error(400, "Bad Request", "WebBridge request requires project_id and binding", False); return
                ok, reason, cap_row = security.validate_web_bridge_capability(
                    auth_header, project_id=project_id, binding_id=b_id, adapter=b_adapter
                )
                if not ok:
                    self._error(401 if ("missing" in reason or "token" in reason) else 403, "Unauthorized" if ("missing" in reason or "token" in reason) else "Forbidden", reason, False); return

                # Check live conversation session
                session = self.server.conversation_store.session_status(b_adapter, b_id)
                if session.get("state") != "live":
                    self._error(403, "Forbidden", f"conversation session is {session.get('state')}", False); return

                # Check project binding
                bound = self.server.conversation_store.binding_for_project(project_id)
                if bound is None or bound.get("binding_id") != b_id or bound.get("adapter") != b_adapter:
                    self._error(403, "Forbidden", "project is not bound to the specified conversation", False); return

            from dev_orchestrator.control.web_bridge import (
                WebBridgeRequestStore,
                WebBridgeConflictError,
                WebBridgeFreshnessError,
                WebBridgeCorruptionError,
            )
            from dev_orchestrator.control.adapter import (
                ControlAdapterClient,
                ControlAdapterRevisionMismatchError,
                ControlAdapterError,
            )
            client = ControlAdapterClient(
                base_url=f"http://127.0.0.1:{self.server.server_address[1]}",
                token=security.token(),
                runtime_root=runtime,
            )
            store = WebBridgeRequestStore(runtime)
            try:
                result = store.handle_request(value, client)
            except WebBridgeConflictError as exc:
                self._error(409, "Conflict", str(exc), False); return
            except ControlAdapterRevisionMismatchError as exc:
                self._error(409, "Conflict", str(exc), False); return
            except (WebBridgeFreshnessError, ValueError, TypeError) as exc:
                self._error(400, "Bad Request", str(exc), False); return
            except WebBridgeCorruptionError as exc:
                self._error(500, "Internal Server Error", str(exc), False); return
            except ControlAdapterError as exc:
                self._error(400, "Bad Request", str(exc), False); return

            cors_header = {"Access-Control-Allow-Origin": "https://chatgpt.com", "Vary": "Origin"} if origin == "https://chatgpt.com" else None
            envelope = result if (isinstance(result, dict) and "data" in result and ("schema_version" in result or "version" in result)) else _control_envelope(result)
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(envelope), False, cors_header)
            return
        if path not in _SELF_AUTHENTICATING_TRANSPORT_PATHS and not self._owner_authorized():
            self._error(401, "Unauthorized", "valid control authorization required", False); return
        if path in ("/api/v1/control/transport-capabilities", "/api/v1/control/transport/capabilities"):
            value = self._read_control_json()
            if value is None: return
            try:
                allowed_ops = value.get("allowed_operations")
                if not isinstance(allowed_ops, list):
                    raise ValueError("allowed_operations must be a list of strings")
                cap = security.create_transport_capability(
                    project_id=str(value.get("project_id", "*")),
                    host_id=str(value.get("host_id", "local")),
                    allowed_operations=allowed_ops,
                    allowed_commands=value.get("allowed_commands"),
                    allowed_roots=value.get("allowed_roots"),
                    ttl_seconds=int(value.get("ttl_seconds", 3600)),
                    label=str(value.get("label", "")),
                )
            except (ValueError, TypeError) as exc:
                self._error(400, "Bad Request", str(exc), False); return
            self._send(201, "Created", "application/json; charset=utf-8", _json_bytes(_control_envelope(cap)), False); return
        revoke_tc = _TRANSPORT_CAPABILITY_REVOKE_PATH_RE.fullmatch(path)
        if revoke_tc:
            try:
                res = security.revoke_transport_capability(revoke_tc.group(1))
            except ValueError as exc:
                self._error(404, "Not Found", str(exc), False); return
            if not res.get("revoked") and res.get("error") == "not_found":
                self._error(404, "Not Found", "capability not found", False); return
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(res)), False); return
        if path == "/api/v1/control/adapter-pairings":
            self._send(201, "Created", "application/json; charset=utf-8", _json_bytes(_control_envelope(security.create_pairing())), False); return
        if path == "/api/v1/control/web-bridge-capabilities":
            value = self._read_control_json()
            if value is None: return
            try:
                cap = security.create_web_bridge_capability(
                    value.get("project_id"),
                    value.get("binding_id"),
                    value.get("adapter", "chatgpt"),
                    expires_in_seconds=value.get("expires_in_seconds", 3600),
                )
            except (ValueError, TypeError) as exc:
                self._error(400, "Bad Request", str(exc), False); return
            self._send(201, "Created", "application/json; charset=utf-8", _json_bytes(_control_envelope(cap)), False); return
        revoke_wb = _CAPABILITY_REVOKE_PATH_RE.fullmatch(path)
        if revoke_wb:
            try:
                res = security.revoke_capability(revoke_wb.group(1))
            except ValueError as exc:
                self._error(404, "Not Found", str(exc), False); return
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(res)), False); return
        revoke = _PAIRING_REVOKE_PATH_RE.fullmatch(path)
        if revoke:
            try:
                result = security.revoke_pairing(revoke.group(1))
            except ValueError as exc:
                self._error(404, "Not Found", str(exc), False); return
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(result)), False); return
        if path == "/api/v1/control/mobile/pairings":
            self._send(201, "Created", "application/json; charset=utf-8", _json_bytes(_control_envelope(security.create_mobile_pairing())), False); return
        if path == "/api/v1/control/mobile/devices/revoke-all":
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(security.revoke_all_mobile_devices())), False); return
        revoke_mob = _MOBILE_DEVICE_REVOKE_PATH_RE.fullmatch(path)
        if revoke_mob:
            try:
                res = security.revoke_mobile_device(revoke_mob.group(1))
            except ValueError as exc:
                self._error(404, "Not Found", str(exc), False); return
            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(res)), False); return
        if path == "/api/v1/control/projects/activate":
            value = self._read_control_json()
            if value is None: return
            repo_path_raw = value.get("repo_path")
            if not isinstance(repo_path_raw, str) or not repo_path_raw.strip():
                self._error(400, "Bad Request", "repo_path is required", False); return
            repo_path = Path(repo_path_raw).resolve(strict=False)
            if not repo_path.exists():
                self._error(400, "Bad Request", f"repository path does not exist: {repo_path}", False); return
            pid_raw = value.get("project_id")
            if pid_raw is not None:
                if not isinstance(pid_raw, str) or not re.fullmatch(r"^[A-Za-z0-9_-]+$", pid_raw.strip()):
                    self._error(400, "Bad Request", "project_id must contain only letters, digits, underscore, or hyphen", False); return
            from dev_orchestrator.core.activation import record_activation_request
            try:
                record = record_activation_request(
                    runtime_root=runtime,
                    repo_path=repo_path,
                    project_id=pid_raw,
                    config_path=self.server.config_path,
                    profile=value.get("profile"),
                    requested_action=value.get("action") or value.get("requested_action") or "continue",
                    source="control_api",
                )
            except Exception as exc:
                self._error(400, "Bad Request", str(exc), False); return
            self._send(201, "Created", "application/json; charset=utf-8", _json_bytes(_control_envelope(record, sources=[{"name": "activation_ledger", "availability": "available"}])), False); return
        if path == "/api/v1/control/transport/exec":
            value = self._read_control_json()
            if value is None: return
            project_id = value.get("project_id") or ""
            command_ref = value.get("command_ref")
            host_id = value.get("host_id", "local")
            idem_key = value.get("idempotency_key") or str(uuid4())
            auth_ok, auth_err, cap_row, cap_id = self._validate_transport_auth(
                operation="exec",
                project_id=project_id,
                host_id=host_id,
                command_ref=command_ref,
                require_nonce=True,
            )
            if not auth_ok:
                self._error(403, "Forbidden", auth_err or "forbidden", False); return

            from dev_orchestrator.transport.hosts import load_transport_hosts_config
            h_cfg = load_transport_hosts_config(runtime)
            is_local = (host_id == "local")
            p_digest = None
            ep_digest = None
            res_digest = None
            effect_cls = "read_only"
            if not is_local:
                profile = h_cfg.hosts.get(host_id)
                if profile is None:
                    self._error(400, "Bad Request", f"unknown host_id: {host_id}", False); return
                from dev_orchestrator.transport.ssh import SSHMachineTransport
                from dev_orchestrator.transport.contracts import MachineOperation
                ssh_t = SSHMachineTransport(profile)
                op_probe = MachineOperation(
                    project_id=project_id,
                    command_ref=command_ref,
                    idempotency_key=f"resolve-{idem_key}",
                    parameters=value.get("parameters"),
                    expected_working_directory=value.get("expected_working_directory"),
                )
                try:
                    resolved_info = ssh_t.resolve(op_probe)
                except Exception as exc:
                    self._error(400, "Bad Request", f"remote resolution failed: {exc}", False); return
                ep_digest = resolved_info.get("execution_policy_digest")
                res_digest = resolved_info.get("resolution_digest")
                p_digest = resolved_info.get("parameters_digest")
                effect_cls = resolved_info.get("effect_class", "read_only")
            else:
                from dev_orchestrator.jobs.config import load_jobs_config, resolve_execution_policy
                jobs_cfg = load_jobs_config(runtime / "execution-jobs.json")
                if jobs_cfg and project_id and command_ref:
                    ok_pol, _, resolved = resolve_execution_policy(
                        jobs_cfg,
                        project_id,
                        command_ref,
                        parameters=value.get("parameters"),
                        expected_working_directory=value.get("expected_working_directory"),
                    )
                    if ok_pol and resolved:
                        p_digest = resolved.parameters_digest
                        ep_digest = resolved.execution_policy_digest
                        res_digest = resolved.resolution_digest
                        effect_cls = resolved.effect_class

            try:
                transport = _get_transport_for_host(
                    runtime,
                    host_id,
                    operation="exec",
                    command_ref=command_ref,
                    effect_class=effect_cls,
                    policy_digest=ep_digest,
                )
                from dev_orchestrator.transport.contracts import MachineOperation
                import dataclasses
                op = MachineOperation(
                    project_id=project_id,
                    command_ref=command_ref,
                    idempotency_key=idem_key,
                    parameters=value.get("parameters"),
                    expected_working_directory=value.get("expected_working_directory"),
                    timeout_seconds=value.get("timeout_seconds"),
                )
                res = transport.exec(op)
            except Exception as exc:
                self._error(400, "Bad Request", str(exc), False); return

            try:
                from dev_orchestrator.transport.observability import log_transport_operation
                log_transport_operation(
                    runtime,
                    operation_id=getattr(res, "operation_id", idem_key),
                    operation="exec",
                    host_id=host_id,
                    selected_transport=getattr(getattr(transport, "last_selection", None), "selected_transport", "local"),
                    status=res.status,
                    project_id=project_id,
                    command_ref=command_ref,
                    parameters_names=sorted((value.get("parameters") or {}).keys()),
                    parameters_digest=getattr(res, "parameters_digest", p_digest),
                    execution_policy_digest=getattr(res, "execution_policy_digest", ep_digest),
                    resolution_digest=getattr(res, "resolution_digest", res_digest),
                    candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
                    capability_id=cap_id,
                    duration_seconds=res.duration_seconds,
                    exit_code=res.exit_code,
                    error=res.error,
                )
            except Exception:
                pass

            self._send(200 if res.status == "ok" else 400, "OK" if res.status == "ok" else "Bad Request", "application/json; charset=utf-8", _json_bytes(_control_envelope(dataclasses.asdict(res))), False)
            return

        if path == "/api/v1/control/transport/spawn":
            value = self._read_control_json()
            if value is None: return
            project_id = value.get("project_id") or ""
            command_ref = value.get("command_ref")
            host_id = value.get("host_id", "local")
            idem_key = value.get("idempotency_key") or str(uuid4())
            auth_ok, auth_err, cap_row, cap_id = self._validate_transport_auth(
                operation="spawn",
                project_id=project_id,
                host_id=host_id,
                command_ref=command_ref,
                require_nonce=True,
            )
            if not auth_ok:
                self._error(403, "Forbidden", auth_err or "forbidden", False); return

            from dev_orchestrator.transport.hosts import load_transport_hosts_config
            h_cfg = load_transport_hosts_config(runtime)
            is_local = (host_id == "local")
            p_digest = None
            ep_digest = None
            res_digest = None
            effect_cls = "effectful"
            if not is_local:
                profile = h_cfg.hosts.get(host_id)
                if profile is None:
                    self._error(400, "Bad Request", f"unknown host_id: {host_id}", False); return
                from dev_orchestrator.transport.ssh import SSHMachineTransport
                from dev_orchestrator.transport.contracts import MachineOperation
                ssh_t = SSHMachineTransport(profile)
                op_probe = MachineOperation(
                    project_id=project_id,
                    command_ref=command_ref,
                    idempotency_key=f"resolve-{idem_key}",
                    parameters=value.get("parameters"),
                    expected_working_directory=value.get("expected_working_directory"),
                )
                try:
                    resolved_info = ssh_t.resolve(op_probe)
                except Exception as exc:
                    self._error(400, "Bad Request", f"remote resolution failed: {exc}", False); return
                ep_digest = resolved_info.get("execution_policy_digest")
                res_digest = resolved_info.get("resolution_digest")
                p_digest = resolved_info.get("parameters_digest")
                effect_cls = resolved_info.get("effect_class", "effectful")
            else:
                from dev_orchestrator.jobs.config import load_jobs_config, resolve_execution_policy
                jobs_cfg = load_jobs_config(runtime / "execution-jobs.json")
                if jobs_cfg and project_id and command_ref:
                    ok_pol, _, resolved = resolve_execution_policy(
                        jobs_cfg,
                        project_id,
                        command_ref,
                        parameters=value.get("parameters"),
                        expected_working_directory=value.get("expected_working_directory"),
                    )
                    if ok_pol and resolved:
                        p_digest = resolved.parameters_digest
                        ep_digest = resolved.execution_policy_digest
                        res_digest = resolved.resolution_digest
                        effect_cls = resolved.effect_class

            try:
                transport = _get_transport_for_host(
                    runtime,
                    host_id,
                    operation="spawn",
                    command_ref=command_ref,
                    effect_class=effect_cls,
                    policy_digest=ep_digest,
                )
                from dev_orchestrator.transport.contracts import MachineOperation
                import dataclasses
                op = MachineOperation(
                    project_id=project_id,
                    command_ref=command_ref,
                    idempotency_key=idem_key,
                    parameters=value.get("parameters"),
                    expected_working_directory=value.get("expected_working_directory"),
                    input_digest=value.get("input_digest"),
                )
                res = transport.spawn(op)
            except Exception as exc:
                self._error(400, "Bad Request", str(exc), False); return

            try:
                from dev_orchestrator.transport.observability import log_transport_operation
                log_transport_operation(
                    runtime,
                    operation_id=getattr(res, "operation_id", idem_key),
                    operation="spawn",
                    host_id=host_id,
                    selected_transport=getattr(getattr(transport, "last_selection", None), "selected_transport", "local"),
                    status=res.status,
                    project_id=project_id,
                    command_ref=command_ref,
                    parameters_names=sorted((value.get("parameters") or {}).keys()),
                    parameters_digest=getattr(res, "parameters_digest", p_digest),
                    execution_policy_digest=getattr(res, "execution_policy_digest", ep_digest),
                    resolution_digest=getattr(res, "resolution_digest", res_digest),
                    candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
                    capability_id=cap_id,
                    duration_seconds=getattr(res, "duration_seconds", None),
                    exit_code=getattr(res, "exit_code", None),
                    error=res.error,
                )
            except Exception:
                pass

            self._send(202 if res.status in ("ok", "queued", "running") else 400, "Accepted" if res.status in ("ok", "queued", "running") else "Bad Request", "application/json; charset=utf-8", _json_bytes(_control_envelope(dataclasses.asdict(res))), False)
            return

        if path == "/api/v1/control/transport/poll":
            value = self._read_control_json()
            if value is None: return
            op_id = value.get("operation_id")
            host_id = value.get("host_id", "local")
            project_id = value.get("project_id") or ""
            auth_ok, auth_err, cap_row, cap_id = self._validate_transport_auth(
                operation="poll",
                project_id=project_id,
                host_id=host_id,
                require_nonce=True,
            )
            if not auth_ok:
                self._error(403, "Forbidden", auth_err or "forbidden", False); return

            try:
                transport = _get_transport_for_host(runtime, host_id, operation="poll", effect_class="read_only")
                import dataclasses
                res = transport.poll(op_id, host_id=host_id)
            except Exception as exc:
                self._error(400, "Bad Request", str(exc), False); return

            try:
                from dev_orchestrator.transport.observability import log_transport_operation
                log_transport_operation(
                    runtime,
                    operation_id=str(op_id),
                    operation="poll",
                    host_id=host_id,
                    selected_transport=getattr(getattr(transport, "last_selection", None), "selected_transport", "local"),
                    status=res.status,
                    project_id=project_id,
                    candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
                    capability_id=cap_id,
                    exit_code=res.exit_code,
                    error=res.error,
                )
            except Exception:
                pass

            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(dataclasses.asdict(res))), False)
            return

        if path == "/api/v1/control/transport/cancel":
            value = self._read_control_json()
            if value is None: return
            op_id = value.get("operation_id")
            host_id = value.get("host_id", "local")
            project_id = value.get("project_id") or ""
            reason = value.get("reason", "cancelled")
            auth_ok, auth_err, cap_row, cap_id = self._validate_transport_auth(
                operation="cancel",
                project_id=project_id,
                host_id=host_id,
                require_nonce=True,
            )
            if not auth_ok:
                self._error(403, "Forbidden", auth_err or "forbidden", False); return

            try:
                transport = _get_transport_for_host(runtime, host_id, operation="cancel", effect_class="effectful")
                import dataclasses
                res = transport.cancel(op_id, host_id=host_id, reason=reason)
            except Exception as exc:
                self._error(400, "Bad Request", str(exc), False); return

            try:
                from dev_orchestrator.transport.observability import log_transport_operation
                log_transport_operation(
                    runtime,
                    operation_id=str(op_id),
                    operation="cancel",
                    host_id=host_id,
                    selected_transport=getattr(getattr(transport, "last_selection", None), "selected_transport", "local"),
                    status=res.status,
                    project_id=project_id,
                    candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
                    capability_id=cap_id,
                    error=res.error,
                )
            except Exception:
                pass

            self._send(200, "OK", "application/json; charset=utf-8", _json_bytes(_control_envelope(dataclasses.asdict(res))), False)
            return

        if path == "/api/v1/control/transport/stage-write":
            value = self._read_control_json(max_bytes=_MAX_STAGE_WRITE_BODY)
            if value is None: return
            project_id = value.get("project_id") or ""
            host_id = value.get("host_id", "local")
            auth_ok, auth_err, cap_row, cap_id = self._validate_transport_auth(
                operation="stage_write",
                project_id=project_id,
                host_id=host_id,
                require_nonce=True,
            )
            if not auth_ok:
                self._error(403, "Forbidden", auth_err or "forbidden", False); return

            try:
                transport = _get_transport_for_host(runtime, host_id, operation="stage_write_content", effect_class="effectful")
                from dev_orchestrator.transport.contracts import WriteContentUpload
                import dataclasses
                upload = WriteContentUpload(
                    project_id=project_id,
                    host_id=host_id,
                    content_base64=value.get("content_base64"),
                    decoded_size_bytes=int(value.get("decoded_size_bytes") or 0),
                    content_sha256=value.get("content_sha256"),
                )
                staged = transport.stage_write_content(upload)
            except Exception as exc:
                self._error(400, "Bad Request", str(exc), False); return

            try:
                from dev_orchestrator.transport.observability import log_transport_operation
                log_transport_operation(
                    runtime,
                    operation_id=staged.content_ref,
                    operation="stage_write",
                    host_id=host_id,
                    selected_transport=getattr(getattr(transport, "last_selection", None), "selected_transport", "local"),
                    status="ok",
                    project_id=project_id,
                    candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
                    capability_id=cap_id,
                )
            except Exception:
                pass

            self._send(201, "Created", "application/json; charset=utf-8", _json_bytes(_control_envelope(dataclasses.asdict(staged))), False)
            return

        if path == "/api/v1/control/transport/write":
            value = self._read_control_json()
            if value is None: return
            project_id = value.get("project_id") or ""
            host_id = value.get("host_id", "local")
            target_path = value.get("target_path") or ""
            auth_ok, auth_err, cap_row, cap_id = self._validate_transport_auth(
                operation="write_file",
                project_id=project_id,
                host_id=host_id,
                path=target_path,
                require_nonce=True,
            )
            if not auth_ok:
                self._error(403, "Forbidden", auth_err or "forbidden", False); return

            try:
                transport = _get_transport_for_host(runtime, host_id, operation="write_file", effect_class="effectful")
                from dev_orchestrator.transport.contracts import FileWriteRequest
                import dataclasses
                write_req = FileWriteRequest(
                    project_id=project_id,
                    host_id=host_id,
                    target_path=target_path,
                    idempotency_key=value.get("idempotency_key") or str(uuid4()),
                    content_ref=value.get("content_ref"),
                    content_sha256=value.get("content_sha256"),
                    decoded_size_bytes=int(value.get("decoded_size_bytes") or 0),
                    if_absent=value.get("if_absent"),
                    expected_sha256=value.get("expected_sha256"),
                    expected_file_policy_digest=value.get("expected_file_policy_digest"),
                )
                f_res = transport.write_file(write_req)
            except Exception as exc:
                self._error(400, "Bad Request", str(exc), False); return

            try:
                from dev_orchestrator.transport.observability import log_transport_operation
                log_transport_operation(
                    runtime,
                    operation_id=write_req.idempotency_key,
                    operation="write_file",
                    host_id=host_id,
                    selected_transport=getattr(getattr(transport, "last_selection", None), "selected_transport", "local"),
                    status=f_res.status,
                    project_id=project_id,
                    candidate_reasons=getattr(getattr(transport, "last_selection", None), "candidate_rejections", None),
                    capability_id=cap_id,
                    error=f_res.error,
                )
            except Exception:
                pass

            st_code = 200 if f_res.status in ("ok", "applied") else 400
            res_dict = dataclasses.asdict(f_res)
            if "content_bytes" in res_dict:
                del res_dict["content_bytes"]
            self._send(st_code, "OK" if st_code == 200 else "Bad Request", "application/json; charset=utf-8", _json_bytes(_control_envelope(res_dict)), False)
            return

        if path != "/api/v1/control/commands":
            self._error(404, "Not Found", "route not found", False); return
        value = self._read_control_json()
        if value is None: return
        command_id = value.get("command_id")

        source_header = self.headers.get("X-DevO-Control-Source")
        source = "control_api"
        if source_header:
            claimed = source_header.strip()
            if claimed.startswith("mobile_gateway:"):
                if not security.bearer_authorized(self.headers.get("Authorization")):
                    self._error(403, "Forbidden", "mobile control source requires master bearer authorization", False)
                    return
                prefix, _, dev_id = claimed.partition(":")
                if not dev_id or not re.fullmatch(r"^[A-Za-z0-9_-]{1,64}$", dev_id):
                    self._error(400, "Bad Request", f"invalid mobile source syntax: {claimed!r}", False)
                    return
                ok, reason, _ = security.lookup_mobile_device(dev_id)
                if not ok:
                    self._error(403, "Forbidden", f"mobile device unauthorized: {reason}", False)
                    return
                source = claimed
            else:
                self._error(400, "Bad Request", f"unsupported control source header: {claimed!r}", False)
                return

        try:
            existing = self.server.command_store.get(command_id) if isinstance(command_id, str) else None
            record = submit_control_command(
                self.server.runtime_root, value.get("project_id"), value.get("action"),
                command_id=command_id, expected=value.get("expected"), target=value.get("target"),
                source=source,
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
