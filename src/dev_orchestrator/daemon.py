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
from dev_orchestrator.ai.runtime_config import load_aibroker_execution_port
from dev_orchestrator.core.dispatcher import dispatch_worker_done_events
from dev_orchestrator.core.control_commands import ControlCommandCoordinator
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.lifecycle_projection import overlay_orchestration_lifecycle
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.response_consumer import consume_websol_responses
from dev_orchestrator.core.progress import ProgressChannel
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.watchdog import WatchdogCoordinator
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
    executor: TransitionExecutor, reviewer: AIReviewerCoordinator | None = None,
    controls: ControlCommandCoordinator | None = None,
    watchdog: WatchdogCoordinator | None = None, *, pid: int,
) -> dict[str, Any]:
    """Run one ordered control-plane tick and return the projected summary."""
    raw_summary = run_monitor_once(config, runtime)
    write_project_statuses(raw_summary, runtime, phase="monitor", daemon_state="running", pid=pid)
    if controls is not None:
        controls.advance(config, raw_summary, executor)
    projected = executor.overlay_managed_runs(raw_summary)
    direct_review_projects = reviewer.enabled_project_ids(config) if reviewer is not None else frozenset()
    if reviewer is not None:
        reviewer.advance(config)
    planner_obj = getattr(controls, "planner", None) if controls is not None else None
    planner_state_fn = getattr(planner_obj, "state", None)
    reviewer_state_fn = getattr(reviewer, "state", None) if reviewer is not None else None
    projected = overlay_orchestration_lifecycle(
        projected,
        planner_state=planner_state_fn() if callable(planner_state_fn) else None,
        reviewer_state=reviewer_state_fn() if callable(reviewer_state_fn) else None,
    )
    browser_summary = dict(projected) if isinstance(projected, dict) else projected
    if isinstance(browser_summary, dict) and isinstance(browser_summary.get("projects"), list):
        browser_summary = dict(browser_summary)
        browser_summary["projects"] = [
            item for item in browser_summary["projects"]
            if not isinstance(item, dict) or str(item.get("project_id") or "") not in direct_review_projects
        ]
    dispatch_worker_done_events(browser_summary, bridge_store, runtime)
    write_project_statuses(projected, runtime, phase="dispatch", daemon_state="running", pid=pid)
    consume_websol_responses(browser_summary, bridge_store, runtime)
    write_project_statuses(projected, runtime, phase="decision", daemon_state="running", pid=pid)
    executor.advance(raw_summary, config, decision_summary=projected)
    projected = executor.overlay_managed_runs(raw_summary)
    projected = overlay_orchestration_lifecycle(
        projected,
        planner_state=planner_state_fn() if callable(planner_state_fn) else None,
        reviewer_state=reviewer_state_fn() if callable(reviewer_state_fn) else None,
    )
    if watchdog is not None:
        try:
            watchdog.advance(config, projected, executor=executor)
        except Exception:
            pass
    write_project_statuses(projected, runtime, phase="actuation", daemon_state="running", pid=pid)
    write_json(runtime / "summary.json", projected)
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
    web_thread: Optional[threading.Thread] = None
    bridge_thread: Optional[threading.Thread] = None
    try:
        server = make_server(listen, port, runtime, web_root)
        bridge_store = BrowserBridgeStore(runtime / "bridge", require_live_binding=True)
        bridge_server = make_bridge_server(bridge_listen, bridge_port, bridge_store)
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
    web_thread.start()
    bridge_thread.start()
    bridge_bound_port = int(bridge_server.server_address[1])
    ai_execution_port = load_aibroker_execution_port(runtime)
    progress_channel = ProgressChannel(runtime, bridge_store=bridge_store)
    transition_executor = TransitionExecutor(
        runtime, ai_execution_port=ai_execution_port, progress_channel=progress_channel
    )
    reviewer_coordinator = AIReviewerCoordinator(
        runtime, ai_execution_port, progress_channel=progress_channel
    )
    planner_coordinator = AIPlannerCoordinator(
        runtime, ai_execution_port, progress_channel=progress_channel
    )
    control_coordinator = ControlCommandCoordinator(runtime, planner_coordinator)
    watchdog_coordinator = WatchdogCoordinator(
        runtime, ai_execution_port=ai_execution_port, progress_channel=progress_channel
    )
    try:
        while True:
            last_error: Optional[str] = None
            try:
                _run_orchestration_tick(
                    config, runtime, bridge_store, transition_executor, reviewer_coordinator,
                    control_coordinator, watchdog=watchdog_coordinator, pid=pid
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
                 "bridge_listen_address": bridge_listen, "bridge_port": bridge_bound_port},
            )
            write_json(
                runtime / "bridge.json",
                _bridge_heartbeat(
                    state=state, pid=pid, listen=bridge_listen, port=bridge_bound_port,
                    started_at=started_at, last_error=last_error,
                ),
            )
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        for runner in (server, bridge_server):
            if runner is not None:
                runner.shutdown()
                runner.server_close()
        if web_thread is not None:
            web_thread.join(timeout=2)
        if bridge_thread is not None:
            bridge_thread.join(timeout=2)
        try:
            (runtime / "daemon.pid").unlink(missing_ok=True)
        except OSError:
            pass
    return 0
