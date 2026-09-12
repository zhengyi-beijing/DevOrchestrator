"""DevOrchestrator command-line entry point (``python -m dev_orchestrator``).

Implements the accepted CLI contract:

- ``monitor --once [--config PATH] [--runtime-root PATH]`` — one read-only
  tick printing the normalized summary JSON.
- ``monitor --interval N`` — heartbeat loop writing ``monitor.pid``.
- ``web --listen IP --port N`` — long-running read-only dashboard.
- ``start-daemon/status-daemon/stop-daemon`` — one-process daemon lifecycle:
  the daemon runs the monitor loop, the read-only Web server and the Browser
  Bridge in the same PID and reports that PID in ``daemon.json``,
  ``monitor.json``, ``web.json`` and ``bridge.json``.
- ``start-monitor|status-monitor|stop-monitor`` and
  ``start-web|status-web|stop-web`` — legacy P2 lifecycle commands operating
  only on DevOrchestrator-owned PID/heartbeat files (compatibility paths).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from dev_orchestrator.config import (
    REPO_ROOT,
    load_projects_config,
    resolve_config_path,
    resolve_runtime_root,
    resolve_web_root,
)
from dev_orchestrator.daemon import run_daemon
from dev_orchestrator.ai.execution_port import MANAGED_INTERRUPT_REASON
from dev_orchestrator.ai.runtime_config import load_aibroker_execution_port
from dev_orchestrator.core.control_commands import latest_control_result, submit_control_command
from dev_orchestrator.core.project_status import project_runtime_status
from dev_orchestrator.monitor.project import run_monitor_once
from dev_orchestrator.platform.process import (
    executable_path,
    is_pid_alive,
    spawn_detached,
    terminate_pid,
    terminate_process_tree,
)
from dev_orchestrator.storage.json_store import (
    read_json,
    utc_now_iso,
    write_json,
    write_text,
)
from dev_orchestrator.web.server import monitor_payload, run_web

_INTERVAL_MIN = 5
_INTERVAL_MAX = 3600
_PORT_MIN = 1
_PORT_MAX = 65535


class CliError(Exception):
    """Fatal CLI error printed to stderr before exiting non-zero."""


def _print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _child_env() -> dict:
    env = dict(os.environ)
    src = str(REPO_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")
    return env


def _spawn_child(argv: Sequence[str]) -> Any:
    return spawn_detached(
        list(argv), cwd=str(REPO_ROOT), env=_child_env()
    )


def _read_pid_text(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _wait_for_child_ready(
    runtime: Path,
    pid_file_name: str,
    heartbeat_file_name: str,
    states: tuple,
    deadline_seconds: float = 8.0,
) -> Optional[dict]:
    """Wait until the freshly spawned child records its own pid + heartbeat.

    The spawn handle may refer to a launcher/supervisor process whose pid
    differs from the actual child (observed under sandboxed hosts), so
    readiness is established from the child's own ``<name>.pid`` file changing
    to a pid that matches the ``<name>.json`` heartbeat.
    """
    pid_file = runtime / pid_file_name
    heartbeat_path = runtime / heartbeat_file_name
    prior = _read_pid_text(pid_file)
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        pid_text = _read_pid_text(pid_file)
        if pid_text and pid_text != prior:
            try:
                recorded_pid = int(pid_text)
            except ValueError:
                recorded_pid = -1
            heartbeat = read_json(heartbeat_path)
            if (
                isinstance(heartbeat, dict)
                and heartbeat.get("state") in states
                and _as_int(heartbeat.get("pid"), -1) == recorded_pid
            ):
                return heartbeat
        time.sleep(0.1)
    return None


def _pid_file(runtime: Path, name: str) -> Path:
    return runtime / name


def _fail(message: str) -> "NoReturn":
    sys.stderr.write(message + "\n")
    raise SystemExit(1)


# --------------------------------------------------------------------------
# monitor / web long-running commands
# --------------------------------------------------------------------------

def _monitor_loop(config: Path, runtime: Path, interval: int, started_at: str) -> int:
    pid_path = runtime / "monitor.pid"
    heartbeat_path = runtime / "monitor.json"
    write_text(pid_path, str(os.getpid()))
    while True:
        last_error: Optional[str] = None
        try:
            run_monitor_once(config, runtime)
        except Exception as exc:  # noqa: BLE001 - degraded heartbeat, keep looping
            last_error = str(exc)
        write_json(
            heartbeat_path,
            {
                "state": "degraded" if last_error else "running",
                "pid": os.getpid(),
                "interval_seconds": interval,
                "started_at": started_at,
                "last_tick_at": utc_now_iso(),
                "last_error": last_error,
            },
        )
        time.sleep(interval)


def cmd_monitor(args: argparse.Namespace) -> int:
    interval = args.interval
    if not (_INTERVAL_MIN <= interval <= _INTERVAL_MAX):
        _fail("interval must be between {0} and {1} seconds".format(_INTERVAL_MIN, _INTERVAL_MAX))
    config = resolve_config_path(args.config)
    runtime = resolve_runtime_root(args.runtime_root)
    runtime.mkdir(parents=True, exist_ok=True)
    if args.once:
        summary = run_monitor_once(config, runtime)
        _print_json(summary)
        return 0
    try:
        return _monitor_loop(config, runtime, interval, utc_now_iso())
    except KeyboardInterrupt:
        return 0


def cmd_validate_config(args: argparse.Namespace) -> int:
    """Validate a portable project configuration without starting services."""
    from dev_orchestrator.adapters import get_project_adapter
    from dev_orchestrator.core.repository import read_repository_truth

    config_path = resolve_config_path(args.config)
    try:
        config = load_projects_config(config_path)
    except (OSError, ValueError) as exc:
        _print_json({
            "valid": False,
            "config": str(config_path),
            "project_count": 0,
            "projects": [],
            "error": str(exc),
        })
        return 1

    projects = []
    all_valid = True
    for project in config.get("projects") or []:
        adapter_error = ""
        try:
            get_project_adapter(str(project.get("adapter") or ""))
            adapter_valid = True
        except ValueError as exc:  # unknown/unregistered adapter: fail closed
            adapter_valid = False
            adapter_error = str(exc)
        truth = read_repository_truth(project.get("repo_path") or "")

        from dev_orchestrator.core.project_context import resolve_project_context
        ctx_decl = project.get("project_context") or {}
        ctx_enabled = bool(ctx_decl.get("enabled"))
        ctx_require_valid = bool(ctx_decl.get("require_valid", True))
        ctx_resolution = resolve_project_context(project)
        project_context_state = ctx_resolution.state
        project_context_error = ctx_resolution.reason if ctx_resolution.state == "invalid" else None
        context_valid = not (ctx_enabled and ctx_require_valid and ctx_resolution.state == "invalid")

        project_valid = adapter_valid and truth.valid and context_valid
        all_valid = all_valid and project_valid
        projects.append({
            "project_id": project.get("project_id"),
            "repo_path": project.get("repo_path"),
            "adapter": project.get("adapter"),
            "adapter_valid": adapter_valid,
            "adapter_error": adapter_error or None,
            "repository_valid": truth.valid,
            "repository_error": truth.error or None,
            "orchestration_ready": bool(project.get("orchestration_ready")),
            "project_context_state": project_context_state,
            "project_context_error": project_context_error,
        })

    _print_json({
        "valid": all_valid,
        "config": str(config_path),
        "project_count": len(projects),
        "projects": projects,
    })
    return 0 if all_valid else 1


def cmd_project_context(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    raw_pid = getattr(args, "project_id_opt", None) or getattr(args, "project_id", None)
    if not raw_pid:
        _fail("missing required project id (specify --project-id <id> or positional project_id)")
    project_id = _validated_project_id(raw_pid)
    try:
        config = load_projects_config(config_path)
    except (OSError, ValueError) as exc:
        _fail("cannot load config: {0}".format(exc))
    matched = None
    for p in config.get("projects") or []:
        if p.get("project_id") == project_id:
            matched = p
            break
    if matched is None:
        _fail("project {0!r} not found in config {1}".format(project_id, config_path))

    from dev_orchestrator.core.project_context import (
        context_status,
        render_context_block,
        resolve_project_context,
    )
    resolution = resolve_project_context(matched)
    status_payload = dict(context_status(resolution))
    ctx_decl = matched.get("project_context") if isinstance(matched.get("project_context"), dict) else {}
    max_chars = ctx_decl.get("max_chars", 6000)
    rendered = render_context_block(resolution.document, max_chars=max_chars) if resolution.document else ""
    status_payload["rendered_chars"] = len(rendered)
    status_payload["truncated"] = "[PROJECT_CONTEXT_TRUNCATED]" in rendered
    _print_json(status_payload)
    return 1 if resolution.state == "invalid" else 0


def _validated_project_id(value: str) -> str:
    text = str(value or "").strip()
    if not text or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in text):
        _fail("project_id must contain only letters, digits, underscore, or hyphen")
    return text


def cmd_project_status(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    project_id = _validated_project_id(args.project_id)
    snapshot = read_json(runtime / "projects" / (project_id + ".json"), None)
    if not isinstance(snapshot, dict) or snapshot.get("project_id") != project_id:
        _print_json({"project_id": project_id, "state": "not_found"})
        return 1
    projected = project_runtime_status(snapshot, runtime)
    payload = dict(projected)
    payload["latest_control"] = latest_control_result(runtime, project_id)
    _print_json(payload)
    return 0


def cmd_project_continue(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    project_id = _validated_project_id(args.project_id)
    if _recorded_pid(runtime, "daemon") is None:
        _fail("DevOrchestrator daemon is not running")
    snapshot = read_json(runtime / "projects" / (project_id + ".json"), None)
    if not isinstance(snapshot, dict) or snapshot.get("project_id") != project_id:
        _fail("project is not present in the current runtime")
    _print_json(submit_control_command(runtime, project_id, "continue"))
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    if not (_PORT_MIN <= args.port <= _PORT_MAX):
        _fail("port must be between {0} and {1}".format(_PORT_MIN, _PORT_MAX))
    runtime = resolve_runtime_root(args.runtime_root)
    web_root = resolve_web_root(args.web_root)
    return run_web(args.listen, args.port, runtime, web_root)


# --------------------------------------------------------------------------
# web lifecycle
# --------------------------------------------------------------------------

def cmd_start_web(args: argparse.Namespace) -> int:
    if not (_PORT_MIN <= args.port <= _PORT_MAX):
        _fail("port must be between {0} and {1}".format(_PORT_MIN, _PORT_MAX))
    runtime = resolve_runtime_root(args.runtime_root)
    runtime.mkdir(parents=True, exist_ok=True)
    pid_path = runtime / "web.pid"
    existing = _read_pid_file(pid_path)
    if existing is not None and is_pid_alive(existing):
        _fail("Web dashboard already running with PID {0}.".format(existing))
    daemon_pid = _recorded_pid(runtime, "daemon")
    if daemon_pid is not None:
        _fail_conflicting_process("Unified daemon", daemon_pid, "start-web (legacy dashboard)")
    web_root = resolve_web_root(args.web_root)

    child = _spawn_child(
        [
            executable_path(),
            "-m",
            "dev_orchestrator",
            "web",
            "--listen",
            args.listen,
            "--port",
            str(args.port),
            "--runtime-root",
            str(runtime),
            "--web-root",
            str(web_root),
        ]
    )
    heartbeat = _wait_for_child_ready(runtime, "web.pid", "web.json", ("running",))
    if heartbeat is None:
        recorded = _read_pid_file(pid_path)
        terminate_pid(child.pid)
        if recorded is not None and recorded != child.pid:
            terminate_pid(recorded)
        _fail("Web dashboard process started but heartbeat was not observed within 8 seconds.")
    _print_json(
        {
            "state": "running",
            "pid": heartbeat["pid"],
            "listen_address": args.listen,
            "port": args.port,
            "url": _display_url(args.listen, args.port),
        }
    )
    return 0


def cmd_status_web(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    heartbeat_path = runtime / "web.json"
    if not heartbeat_path.is_file():
        _print_json({"state": "not_started", "process_alive": False})
        return 0
    heartbeat = read_json(heartbeat_path)
    if not isinstance(heartbeat, dict):
        heartbeat = {}
    pid = _as_int(heartbeat.get("pid"), 0)
    alive = pid > 0 and is_pid_alive(pid)
    address = heartbeat.get("listen_address")
    address = address if isinstance(address, str) and address else "127.0.0.1"
    port = _as_int(heartbeat.get("port"), 8770)
    raw_state = heartbeat.get("state")
    if alive:
        state = raw_state if isinstance(raw_state, str) and raw_state else "running"
    else:
        state = "stopped"
    _print_json(
        {
            "state": state,
            "pid": pid,
            "process_alive": alive,
            "listen_address": address,
            "port": port,
            "started_at": heartbeat.get("started_at"),
            "last_request_at": heartbeat.get("last_request_at"),
            "url": _display_url(address, port),
        }
    )
    return 0


def cmd_stop_web(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    pid_path = runtime / "web.pid"
    heartbeat_path = runtime / "web.json"
    pid = _read_pid_file(pid_path) or 0
    if pid > 0 and is_pid_alive(pid):
        terminate_pid(pid)
    stopped = {"state": "stopped", "pid": pid, "stopped_at": utc_now_iso()}
    write_json(heartbeat_path, stopped)
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    _print_json(stopped)
    return 0


# --------------------------------------------------------------------------
# monitor lifecycle
# --------------------------------------------------------------------------

def cmd_start_monitor(args: argparse.Namespace) -> int:
    interval = args.interval
    if not (_INTERVAL_MIN <= interval <= _INTERVAL_MAX):
        _fail("interval must be between {0} and {1} seconds".format(_INTERVAL_MIN, _INTERVAL_MAX))
    runtime = resolve_runtime_root(args.runtime_root)
    runtime.mkdir(parents=True, exist_ok=True)
    pid_path = runtime / "monitor.pid"
    existing = _read_pid_file(pid_path)
    if existing is not None and is_pid_alive(existing):
        _fail("Monitor already running with PID {0}.".format(existing))
    daemon_pid = _recorded_pid(runtime, "daemon")
    if daemon_pid is not None:
        _fail_conflicting_process("Unified daemon", daemon_pid, "start-monitor (legacy monitor)")
    config = resolve_config_path(args.config)

    child = _spawn_child(
        [
            executable_path(),
            "-m",
            "dev_orchestrator",
            "monitor",
            "--interval",
            str(interval),
            "--config",
            str(config),
            "--runtime-root",
            str(runtime),
        ]
    )
    heartbeat = _wait_for_child_ready(
        runtime, "monitor.pid", "monitor.json", ("running", "degraded")
    )
    if heartbeat is None:
        recorded = _read_pid_file(pid_path)
        terminate_pid(child.pid)
        if recorded is not None and recorded != child.pid:
            terminate_pid(recorded)
        _fail("Monitor process started but heartbeat was not observed within 8 seconds.")
    _print_json(heartbeat)
    return 0


def cmd_status_monitor(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    heartbeat_path = runtime / "monitor.json"
    if not heartbeat_path.is_file():
        _print_json({"state": "not_started", "process_alive": False})
        return 0
    payload = monitor_payload(runtime)
    _print_json(
        {
            "state": payload["state"] if payload["process_alive"] else "stopped",
            "pid": payload["pid"],
            "process_alive": payload["process_alive"],
            "interval_seconds": payload["interval_seconds"],
            "started_at": payload["started_at"],
            "last_tick_at": payload["last_tick_at"],
            "heartbeat_age_seconds": payload["heartbeat_age_seconds"],
            "stale": payload["stale"],
            "last_error": payload["last_error"],
        }
    )
    return 0


def cmd_stop_monitor(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    pid_path = runtime / "monitor.pid"
    heartbeat_path = runtime / "monitor.json"
    pid = _read_pid_file(pid_path) or 0
    if pid > 0 and is_pid_alive(pid):
        terminate_pid(pid)
    stopped = {"state": "stopped", "pid": pid, "stopped_at": utc_now_iso()}
    write_json(heartbeat_path, stopped)
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    _print_json(stopped)
    return 0


# --------------------------------------------------------------------------
# unified daemon lifecycle (one process: monitor loop + web server)
# --------------------------------------------------------------------------

def cmd_daemon(args: argparse.Namespace) -> int:
    """Run the unified daemon until interrupted (spawned by start-daemon)."""
    if not (_INTERVAL_MIN <= args.interval <= _INTERVAL_MAX):
        _fail("interval must be between {0} and {1} seconds".format(_INTERVAL_MIN, _INTERVAL_MAX))
    if not (_PORT_MIN <= args.port <= _PORT_MAX):
        _fail("port must be between {0} and {1}".format(_PORT_MIN, _PORT_MAX))
    if not (_PORT_MIN <= args.bridge_port <= _PORT_MAX):
        _fail("bridge port must be between {0} and {1}".format(_PORT_MIN, _PORT_MAX))
    config = resolve_config_path(args.config)
    runtime = resolve_runtime_root(args.runtime_root)
    web_root = resolve_web_root(args.web_root)
    try:
        return run_daemon(
            config,
            runtime,
            web_root,
            args.interval,
            args.listen,
            args.port,
            args.bridge_listen,
            args.bridge_port,
        )
    except KeyboardInterrupt:
        return 0


def cmd_start_daemon(args: argparse.Namespace) -> int:
    if not (_INTERVAL_MIN <= args.interval <= _INTERVAL_MAX):
        _fail("interval must be between {0} and {1} seconds".format(_INTERVAL_MIN, _INTERVAL_MAX))
    if not (_PORT_MIN <= args.port <= _PORT_MAX):
        _fail("port must be between {0} and {1}".format(_PORT_MIN, _PORT_MAX))
    if not (_PORT_MIN <= args.bridge_port <= _PORT_MAX):
        _fail("bridge port must be between {0} and {1}".format(_PORT_MIN, _PORT_MAX))
    runtime = resolve_runtime_root(args.runtime_root)
    runtime.mkdir(parents=True, exist_ok=True)
    pid_path = runtime / "daemon.pid"
    existing = _read_pid_file(pid_path)
    if existing is not None and is_pid_alive(existing):
        _fail("Daemon already running with PID {0}.".format(existing))
    for name, label in (("monitor", "Legacy monitor"), ("web", "Legacy web dashboard")):
        legacy_pid = _recorded_pid(runtime, name)
        if legacy_pid is not None:
            _fail_conflicting_process(label, legacy_pid, "start-daemon")
    config = resolve_config_path(args.config)
    web_root = resolve_web_root(args.web_root)

    child = _spawn_child(
        [
            executable_path(),
            "-m",
            "dev_orchestrator",
            "daemon",
            "--config",
            str(config),
            "--runtime-root",
            str(runtime),
            "--web-root",
            str(web_root),
            "--listen",
            args.listen,
            "--port",
            str(args.port),
            "--bridge-listen",
            args.bridge_listen,
            "--bridge-port",
            str(args.bridge_port),
            "--interval",
            str(args.interval),
        ]
    )
    heartbeat = _wait_for_child_ready(
        runtime, "daemon.pid", "daemon.json", ("running", "degraded")
    )
    if heartbeat is None:
        recorded = _read_pid_file(pid_path)
        terminate_pid(child.pid)
        if recorded is not None and recorded != child.pid:
            terminate_pid(recorded)
        _fail("Daemon process started but heartbeat was not observed within 8 seconds.")
    _print_json(
        {
            "state": "running",
            "pid": heartbeat["pid"],
            "listen_address": args.listen,
            "port": args.port,
            "interval_seconds": args.interval,
            "url": _display_url(args.listen, args.port),
        }
    )
    return 0


def cmd_status_daemon(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    heartbeat_path = runtime / "daemon.json"
    if not heartbeat_path.is_file():
        _print_json({"state": "not_started", "process_alive": False})
        return 0
    heartbeat = read_json(heartbeat_path)
    if not isinstance(heartbeat, dict):
        heartbeat = {}
    pid = _as_int(heartbeat.get("pid"), 0)
    alive = pid > 0 and is_pid_alive(pid)
    address = heartbeat.get("listen_address")
    address = address if isinstance(address, str) and address else "127.0.0.1"
    port = _as_int(heartbeat.get("port"), 8770)
    raw_state = heartbeat.get("state")
    if alive:
        state = raw_state if isinstance(raw_state, str) and raw_state else "running"
    else:
        state = "stopped"
    _print_json(
        {
            "state": state,
            "pid": pid,
            "process_alive": alive,
            "interval_seconds": heartbeat.get("interval_seconds"),
            "started_at": heartbeat.get("started_at"),
            "last_tick_at": heartbeat.get("last_tick_at"),
            "last_error": heartbeat.get("last_error"),
            "listen_address": address,
            "port": port,
            "url": _display_url(address, port),
        }
    )
    return 0


def _interrupt_active_broker_dispatches(runtime: Path) -> list[dict[str, Any]]:
    ledger = read_json(runtime / "transition-executor.json", {})
    executions = ledger.get("executions") if isinstance(ledger, dict) else None
    rows = executions.values() if isinstance(executions, dict) else ()
    request_ids = sorted({
        str(row.get("broker_request_id")) for row in rows
        if isinstance(row, dict) and row.get("engine") == "aibroker"
        and row.get("state") in {"launching", "running"} and row.get("broker_request_id")
    })
    if not request_ids:
        return []
    try:
        port = load_aibroker_execution_port(runtime)
    except Exception as exc:
        return [{"state": "reconcile_failed", "error": str(exc)}]
    if port is None:
        return [{"state": "reconcile_failed", "error": "AIBroker execution port unavailable"}]
    results = []
    for request_id in request_ids:
        try:
            fact = port.status(request_id)
            if isinstance(fact, dict) and fact.get("status") == "running":
                fact = port.interrupt(request_id, MANAGED_INTERRUPT_REASON)
            results.append({"request_id": request_id, "fact": fact})
        except Exception as exc:
            results.append({"request_id": request_id, "state": "reconcile_failed", "error": str(exc)})
    return results


def cmd_stop_daemon(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    pid_path = runtime / "daemon.pid"
    heartbeat_path = runtime / "daemon.json"
    pid = _read_pid_file(pid_path) or 0
    daemon_was_alive = pid > 0 and is_pid_alive(pid)
    tree_stopped = False
    if daemon_was_alive:
        tree_stopped = terminate_process_tree(pid)
    broker_interrupts = _interrupt_active_broker_dispatches(runtime) if daemon_was_alive and tree_stopped else []
    stopped = {"state": "stopped", "pid": pid, "stopped_at": utc_now_iso(),
               "tree_stopped": tree_stopped, "broker_interrupts": broker_interrupts}
    write_json(heartbeat_path, stopped)
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    _print_json(stopped)
    return 0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _read_pid_file(path: Path) -> Optional[int]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _recorded_pid(runtime: Path, name: str) -> Optional[int]:
    """Live PID recorded for a DevOrchestrator lifecycle process ``name``.

    Only the DevOrchestrator-owned ``<name>.pid`` file is consulted; a stale
    (dead) PID is not a conflict.
    """
    pid = _read_pid_file(_pid_file(runtime, name + ".pid"))
    if pid is not None and is_pid_alive(pid):
        return pid
    return None


def _fail_conflicting_process(conflict: str, pid: int, starter: str) -> "NoReturn":
    _fail(
        "{0} already running with PID {1}; {2} would start a second "
        "DevOrchestrator process on this computer (one daemon per computer).".format(
            conflict, pid, starter
        )
    )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _display_url(address: str, port: int) -> str:
    if address in ("0.0.0.0", "::"):
        return "http://localhost:{0}/".format(port)
    return "http://{0}:{1}/".format(address, port)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev_orchestrator",
        description="Project-neutral DevOrchestrator multi-project service.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    validate_config = sub.add_parser(
        "validate-config", help="validate project config/repositories without starting services"
    )
    validate_config.add_argument("--config", default=None, help="path to projects.json")

    project_context = sub.add_parser("project-context", help="inspect durable project context resolution")
    project_context.add_argument("project_id", nargs="?", default=None, help="project id (positional)")
    project_context.add_argument("--project-id", dest="project_id_opt", default=None, help="project id (named)")
    project_context.add_argument("--config", default=None, help="path to projects.json")

    project_status = sub.add_parser("project-status", help="read one project status by project_id")
    project_status.add_argument("project_id")
    project_status.add_argument("--runtime-root", default=None)

    project_continue = sub.add_parser("project-continue", help="queue a stateless continue command for one project")
    project_continue.add_argument("project_id")
    project_continue.add_argument("--runtime-root", default=None)

    monitor = sub.add_parser("monitor", help="run one tick (--once) or the heartbeat loop")
    monitor.add_argument("--once", action="store_true", help="run a single tick and print the summary")
    monitor.add_argument("--interval", type=int, default=60, help="loop interval in seconds (5..3600)")
    monitor.add_argument("--config", default=None, help="path to projects.json")
    monitor.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    web = sub.add_parser("web", help="run the read-only dashboard server")
    web.add_argument("--listen", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8770)
    web.add_argument("--runtime-root", default=None)
    web.add_argument("--web-root", default=None)

    start_web = sub.add_parser("start-web", help="start the dashboard as a detached process")
    start_web.add_argument("--listen", default="127.0.0.1")
    start_web.add_argument("--port", type=int, default=8770)
    start_web.add_argument("--runtime-root", default=None)
    start_web.add_argument("--web-root", default=None)

    status_web = sub.add_parser("status-web", help="report dashboard process status")
    status_web.add_argument("--runtime-root", default=None)

    stop_web = sub.add_parser("stop-web", help="stop the recorded dashboard process")
    stop_web.add_argument("--runtime-root", default=None)

    start_monitor = sub.add_parser("start-monitor", help="start the monitor loop as a detached process")
    start_monitor.add_argument("--interval", type=int, default=60)
    start_monitor.add_argument("--config", default=None)
    start_monitor.add_argument("--runtime-root", default=None)

    status_monitor = sub.add_parser("status-monitor", help="report monitor process status")
    status_monitor.add_argument("--runtime-root", default=None)

    stop_monitor = sub.add_parser("stop-monitor", help="stop the recorded monitor process")
    stop_monitor.add_argument("--runtime-root", default=None)

    daemon = sub.add_parser(
        "daemon",
        help="run the unified daemon (monitor loop + read-only web + bridge, one PID)",
    )
    daemon.add_argument("--listen", default="127.0.0.1")
    daemon.add_argument("--port", type=int, default=8770)
    daemon.add_argument("--bridge-listen", default="127.0.0.1")
    daemon.add_argument("--bridge-port", type=int, default=8765)
    daemon.add_argument("--interval", type=int, default=60, help="loop interval in seconds (5..3600)")
    daemon.add_argument("--config", default=None)
    daemon.add_argument("--runtime-root", default=None)
    daemon.add_argument("--web-root", default=None)

    start_daemon = sub.add_parser("start-daemon", help="start the unified daemon as a detached process")
    start_daemon.add_argument("--listen", default="127.0.0.1")
    start_daemon.add_argument("--port", type=int, default=8770)
    start_daemon.add_argument("--bridge-listen", default="127.0.0.1")
    start_daemon.add_argument("--bridge-port", type=int, default=8765)
    start_daemon.add_argument("--interval", type=int, default=60)
    start_daemon.add_argument("--config", default=None)
    start_daemon.add_argument("--runtime-root", default=None)
    start_daemon.add_argument("--web-root", default=None)

    status_daemon = sub.add_parser("status-daemon", help="report unified daemon process status")
    status_daemon.add_argument("--runtime-root", default=None)

    stop_daemon = sub.add_parser("stop-daemon", help="stop the recorded unified daemon process")
    stop_daemon.add_argument("--runtime-root", default=None)

    return parser


_COMMANDS = {
    "validate-config": cmd_validate_config,
    "project-context": cmd_project_context,
    "project-status": cmd_project_status,
    "project-continue": cmd_project_continue,
    "monitor": cmd_monitor,
    "web": cmd_web,
    "daemon": cmd_daemon,
    "start-daemon": cmd_start_daemon,
    "status-daemon": cmd_status_daemon,
    "stop-daemon": cmd_stop_daemon,
    "start-web": cmd_start_web,
    "status-web": cmd_status_web,
    "stop-web": cmd_stop_web,
    "start-monitor": cmd_start_monitor,
    "status-monitor": cmd_status_monitor,
    "stop-monitor": cmd_stop_monitor,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2
    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        return int(handler(args))
    except CliError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
