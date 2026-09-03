"""One-process DevOrchestrator daemon (monitor loop + read-only Web server).

The preferred runtime entrypoint is a single OS process per computer:

    daemon
      -> writes ``daemon.pid`` / ``daemon.json`` for its own live PID
      -> runs the monitor loop for all configured projects
      -> serves the read-only Web dashboard from a thread in the same PID

``daemon.json``, ``monitor.json`` and ``web.json`` all report the same live
PID in daemon mode. The existing separate monitor/Web commands remain P2
compatibility paths but are no longer the target deployment architecture.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

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


def run_daemon(
    config: Path | str,
    runtime_root: Path | str,
    web_root: Path | str,
    interval: int,
    listen: str,
    port: int,
) -> int:
    """Run the unified daemon until interrupted.

    The monitor loop runs on the calling thread and the Web server on a
    daemon thread, so one PID owns both. Stops are handled by the recorded
    ``daemon.pid`` only (``stop-daemon``); nothing here starts Workers or
    advances tasks.
    """
    runtime = Path(runtime_root)
    runtime.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    started_at = utc_now_iso()
    write_text(runtime / "daemon.pid", str(pid))

    try:
        server = make_server(listen, port, runtime, web_root)
    except Exception:
        # Never leave a live-looking pid file behind when the Web bind fails.
        try:
            (runtime / "daemon.pid").unlink(missing_ok=True)
        except OSError:
            pass
        raise

    web_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="devorchestrator-web",
        daemon=True,
    )
    web_thread.start()
    try:
        while True:
            last_error: Optional[str] = None
            try:
                run_monitor_once(config, runtime)
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
                {
                    **heartbeat,
                    "listen_address": listen,
                    "port": port,
                },
            )
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        web_thread.join(timeout=2)
        try:
            (runtime / "daemon.pid").unlink(missing_ok=True)
        except OSError:
            pass
    return 0
