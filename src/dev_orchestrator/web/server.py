"""Read-only stdlib HTTP server implementing the accepted P2 surface.

Contract: methods ``GET``/``HEAD`` only; static allowlist ``/``, ``/app.js``,
``/style.css``; API allowlist ``/api/monitor``, ``/api/summary``,
``/api/projects/<id>``, ``/api/events?limit=N``, ``/api/runs?limit=N``,
``/api/orchestration``, ``/api/watchdog``; history limits clamp to 1..100; unknown routes 404;
write methods 405 with ``Allow: GET, HEAD``; traversal/malformed paths 400.
``Cache-Control: no-store`` and ``X-Content-Type-Options: nosniff`` are always
present.

``/api/orchestration`` is a read-only projection of the dispatcher ledger: it
surfaces projects whose prepared Web Sol request cannot be delivered yet
(``delivery_state=unbound``) so the dashboard can ask the owner to bind/rebind
the ChatGPT conversation. The server never mutates the ledger.

The server reads DevOrchestrator runtime projections only; it never shells
into observed projects.
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import urlopen

from dev_orchestrator.core.dispatcher import DISPATCHER_STATE_FILE
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
_ALLOW_HEADER = "GET, HEAD"


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
    runtime = Path(runtime_root)
    config = read_json(runtime / "aibroker.json", {})
    base_url = config.get("base_url") if isinstance(config, dict) else None
    if not isinstance(base_url, str) or not base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        return {"available": False, "error": "AIBroker endpoint not configured"}
    try:
        with urlopen(base_url.rstrip("/") + endpoint, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        return {"available": False, "error": str(exc)}
    return {**payload, "available": True} if isinstance(payload, dict) else {"available": False, "error": "invalid AIBroker response"}


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
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.web_root = Path(web_root)
        self.listen_label = listen_label
        self.started_at = started_at
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
    """GET/HEAD-only handler; every response carries no-store/nosniff."""

    protocol_version = "HTTP/1.1"
    server_version = "DevOrchestrator/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence access log
        return

    # -- entry points -----------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:
        self._dispatch(head_only=True)

    def _unsupported(self) -> None:
        self._dispatch_method_not_allowed()

    do_POST = _unsupported
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
            "read-only dashboard supports GET and HEAD only",
            self.command == "HEAD",
            {"Allow": _ALLOW_HEADER},
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
        if path == "/api/monitor":
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
    host: str, port: int, runtime_root: Path | str, web_root: Path | str
) -> DevOrchestratorHTTPServer:
    """Create (and bind) the dashboard server; runtime heartbeat is written now."""
    runtime = Path(runtime_root)
    runtime.mkdir(parents=True, exist_ok=True)
    started_at = utc_now_iso()
    return DevOrchestratorHTTPServer(
        (host, port), runtime, Path(web_root), host, started_at
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
