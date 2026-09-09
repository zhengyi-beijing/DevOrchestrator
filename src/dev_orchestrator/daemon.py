"""One-process DevOrchestrator daemon (monitor loop + read-only Web + Bridge).

The preferred runtime entrypoint is a single OS process per computer:

    daemon
      -> writes ``daemon.pid`` / ``daemon.json`` for its own live PID
      -> runs the monitor loop for all configured projects
      -> serves the read-only Web dashboard from a thread in the same PID
      -> serves the dedicated Browser Bridge listener from a thread in the
         same PID (``bridge.json`` heartbeat reports that same live PID)

``daemon.json``, ``monitor.json``, ``web.json`` and ``bridge.json`` all report
the same live PID in daemon mode. The existing separate monitor/Web commands
remain P2 compatibility paths but are no longer the target deployment
architecture.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.bridge.server import make_bridge_server
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.control.server import make_control_server
from dev_orchestrator.control.service import ControlPlaneService
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.core.dispatcher import dispatch_worker_done_events
from dev_orchestrator.core.response_consumer import consume_websol_responses
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.project_status import write_project_statuses
from dev_orchestrator.monitor.project import run_monitor_once
from dev_orchestrator.storage.json_store import utc_now_iso, write_json, write_text
from dev_orchestrator.web.server import make_server


def _monitor_heartbeat(
    *, state: str, pid: int, interval: int, started_at: str, last_error: Optional[str]
) -> dict[str, Any]:
    return {
        "state": state,
        "pid": pid,
        "interval_seconds": interval,
        "started_at": started_at,
        "last_tick_at": utc_now_iso(),
        "last_error": last_error,
    }


def _bridge_heartbeat(
    *,
    state: str,
    pid: int,
    listen: str,
    port: int,
    started_at: str,
    last_error: Optional[str],
) -> dict[str, Any]:
    return {
        "state": state,
        "pid": pid,
        "listen_address": listen,
        "port": port,
        "started_at": started_at,
        "last_tick_at": utc_now_iso(),
        "last_error": last_error,
    }


def _run_orchestration_tick(
    config: Path | str, runtime: Path, bridge_store: BrowserBridgeStore,
    executor: TransitionExecutor, *, pid: int,
) -> dict[str, Any]:
    """Run one ordered control-plane tick and return the projected summary."""
    raw_summary = run_monitor_once(config, runtime)
    write_project_statuses(raw_summary, runtime, phase="monitor", daemon_state="running", pid=pid)
    projected = executor.overlay_managed_runs(raw_summary)
    dispatch_worker_done_events(projected, bridge_store, runtime)
    write_project_statuses(projected, runtime, phase="dispatch", daemon_state="running", pid=pid)
    consume_websol_responses(projected, bridge_store, runtime)
    write_project_statuses(projected, runtime, phase="decision", daemon_state="running", pid=pid)
    executor.advance(raw_summary, config, decision_summary=projected)
    projected = executor.overlay_managed_runs(raw_summary)
    write_project_statuses(projected, runtime, phase="actuation", daemon_state="running", pid=pid)
    return projected


def run_daemon(
    config: Path | str,
    runtime_root: Path | str,
    web_root: Path | str,
    interval: int,
    listen: str,
    port: int,
    bridge_listen: str = "127.0.0.1",
    bridge_port: int = 8765,
    control_listen: str = "127.0.0.1",
    control_port: int = 8766,
) -> int:
    """Run the unified daemon until interrupted.

    The monitor loop runs on the calling thread; the Web dashboard and the
    Browser Bridge each run on their own daemon thread, so one PID owns all
    three surfaces. After each successful monitor tick the daemon dispatches
    ``WORKER_DONE`` requests into each project's exact binding queue, consumes
    newly ``RESPONDED`` Bridge responses through the Decision Guard, then lets
    the owner-authorized Transition Executor act on durable APPLY+NEXT_TASK
    decisions. Project-local ``.devorch/status.json`` mirrors are refreshed at
    each control-plane phase. Stops are handled by ``stop-daemon``.
    """
    runtime = Path(runtime_root)
    runtime.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    started_at = utc_now_iso()
    write_text(runtime / "daemon.pid", str(pid))

    server = None
    bridge_server = None
    control_server = None
    web_thread: Optional[threading.Thread] = None
    bridge_thread: Optional[threading.Thread] = None
    control_thread: Optional[threading.Thread] = None
    try:
        server = make_server(listen, port, runtime, web_root)
        bridge_store = BrowserBridgeStore(runtime / "bridge", require_live_binding=True)
        bridge_server = make_bridge_server(bridge_listen, bridge_port, bridge_store)
        control_store = ConversationControlStore(runtime)
        owner_store = OwnerControlStore(runtime)
        transition_executor = TransitionExecutor(runtime, owner_store=owner_store)
        control_service = ControlPlaneService(
            config, runtime, control_store, bridge_store,
            transition_executor=transition_executor, owner_store=owner_store,
        )
        control_server = make_control_server(control_listen, control_port, control_service)
    except Exception:
        # Never leave a live-looking pid file behind when a bind fails.
        try:
            (runtime / "daemon.pid").unlink(missing_ok=True)
        except OSError:
            pass
        if server is not None:
            server.server_close()
        if bridge_server is not None:
            bridge_server.server_close()
        if control_server is not None:
            control_server.server_close()
        raise

    web_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="devorchestrator-web",
        daemon=True,
    )
    bridge_thread = threading.Thread(
        target=bridge_server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="devorchestrator-bridge",
        daemon=True,
    )
    control_thread = threading.Thread(
        target=control_server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="devorchestrator-control",
        daemon=True,
    )
    web_thread.start()
    bridge_thread.start()
    control_thread.start()
    bridge_bound_port = int(bridge_server.server_address[1])
    control_bound_port = int(control_server.server_address[1])
    try:
        while True:
            last_error: Optional[str] = None
            try:
                _run_orchestration_tick(
                    config, runtime, bridge_store, transition_executor, pid=pid
                )
            except Exception as exc:  # noqa: BLE001 - degraded heartbeat, keep looping
                last_error = str(exc)
            state = "degraded" if last_error else "running"
            heartbeat = _monitor_heartbeat(
                state=state, pid=pid, interval=interval,
                started_at=started_at, last_error=last_error,
            )
            write_json(runtime / "monitor.json", heartbeat)
            write_json(
                runtime / "daemon.json",
                {**heartbeat, "listen_address": listen, "port": port,
                 "bridge_listen_address": bridge_listen, "bridge_port": bridge_bound_port,
                 "control_listen_address": control_listen, "control_port": control_bound_port},
            )
            write_json(
                runtime / "bridge.json",
                _bridge_heartbeat(
                    state=state, pid=pid, listen=bridge_listen, port=bridge_bound_port,
                    started_at=started_at, last_error=last_error,
                ),
            )
            write_json(
                runtime / "control.json",
                _bridge_heartbeat(
                    state=state, pid=pid, listen=control_listen, port=control_bound_port,
                    started_at=started_at, last_error=last_error,
                ),
            )
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        for runner in (server, bridge_server, control_server):
            if runner is not None:
                runner.shutdown()
                runner.server_close()
        if web_thread is not None:
            web_thread.join(timeout=2)
        if bridge_thread is not None:
            bridge_thread.join(timeout=2)
        if control_thread is not None:
            control_thread.join(timeout=2)
        try:
            (runtime / "daemon.pid").unlink(missing_ok=True)
        except OSError:
            pass
    return 0
