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
import http.client
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence
from uuid import uuid4

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
from dev_orchestrator.accounting.evidence import import_rdc_evidence
from dev_orchestrator.accounting.events import ROLES, EventWriteError, ExecutionEventStore
from dev_orchestrator.accounting.reporting import build_p11_report, reporting_event_store
from dev_orchestrator.accounting.runtime import load_accounting_runtime, load_accounting_settings
from dev_orchestrator.core.control_commands import latest_control_result
from dev_orchestrator.control.security import is_loopback
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
from dev_orchestrator.web.server import monitor_payload, run_web, watchdog_payload

_INTERVAL_MIN = 5
_INTERVAL_MAX = 3600
_PORT_MIN = 1
_PORT_MAX = 65535


class CliError(Exception):
    """Fatal CLI error printed to stderr before exiting non-zero."""


def _read_rdc_input(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CliError(f"cannot read RDC evidence input {path}: {exc}") from exc
    try:
        if text.lstrip().startswith("["):
            value = json.loads(text)
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise ValueError("JSON input must be an array of objects")
            return value
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        if any(not isinstance(item, dict) for item in rows):
            raise ValueError("JSONL records must be objects")
        return rows
    except (json.JSONDecodeError, ValueError) as exc:
        raise CliError(f"invalid RDC evidence input {path}: {exc}") from exc


def cmd_import_rdc_evidence(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    config = resolve_config_path(args.config)
    try:
        accounting_runtime = load_accounting_runtime(runtime, config)
    except (OSError, ValueError) as exc:
        raise CliError(str(exc)) from exc
    if accounting_runtime is None:
        raise CliError("execution_accounting must be enabled to import RDC evidence")
    rows = _read_rdc_input(Path(args.input))
    try:
        records = import_rdc_evidence(accounting_runtime.recorder, rows)
    except (TypeError, ValueError, OSError, EventWriteError) as exc:
        raise CliError(f"RDC evidence import failed: {exc}") from exc
    _print_json(
        {
            "imported": len(records),
            "first_sequence": records[0]["sequence"] if records else None,
            "last_sequence": records[-1]["sequence"] if records else None,
        }
    )
    return 0


def cmd_execution_report(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    store = None
    if args.config is not None:
        config = resolve_config_path(args.config)
        try:
            settings = load_accounting_settings(config)
        except (OSError, ValueError) as exc:
            raise CliError(str(exc)) from exc
        if settings is None:
            raise CliError("execution_accounting must be enabled to use the configured report path")
        store = ExecutionEventStore(runtime, relative_path=settings.event_path)
    try:
        read = (store or reporting_event_store(runtime)).read()
    except (ValueError, EventWriteError) as exc:
        raise CliError(f"cannot read execution evidence: {exc}") from exc
    if read.corruptions:
        raise CliError("execution accounting ledger is corrupt; report refused")
    try:
        report = build_p11_report(
            read.events,
            args.window_start,
            args.window_end,
            project_id=args.project_id,
            task_id=args.task_id,
            role=args.role,
        ).as_dict()
    except ValueError as exc:
        raise CliError(f"cannot build execution report: {exc}") from exc
    _print_json(report)
    if args.fail_on_gate and report["acceptance"]["status"] != "pass":
        return 1
    return 0


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


_FIRST_TICK_GRACE_SECONDS = 180.0


def _await_first_tick(
    runtime: Path, pid_path: Path, grace_seconds: float = _FIRST_TICK_GRACE_SECONDS,
) -> Optional[dict]:
    """Let an initialized daemon finish a slow first tick instead of killing it.

    A child that published ``starting`` has bound its servers and entered the
    control loop; it is not hung.  Its first tick is where pending lifecycle
    work runs after a restart, so terminating it there would interrupt that
    work mid-transaction.  Returns the ready heartbeat once the first tick
    completes, the ``starting`` heartbeat if the child is still alive when the
    grace period ends, or ``None`` when the child never finished initializing
    or has died -- only that last case warrants termination.
    """
    heartbeat_path = runtime / "daemon.json"
    recorded = _read_pid_file(pid_path)
    heartbeat = read_json(heartbeat_path)
    if not (
        recorded is not None
        and isinstance(heartbeat, dict)
        and heartbeat.get("state") == "starting"
        and _as_int(heartbeat.get("pid"), -1) == recorded
        and is_pid_alive(recorded)
    ):
        return None
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not is_pid_alive(recorded):
            return None
        heartbeat = read_json(heartbeat_path)
        if (
            isinstance(heartbeat, dict)
            and heartbeat.get("state") in ("running", "degraded")
            and _as_int(heartbeat.get("pid"), -1) == recorded
        ):
            return heartbeat
        time.sleep(0.2)
    heartbeat = read_json(heartbeat_path)
    if (
        isinstance(heartbeat, dict)
        and _as_int(heartbeat.get("pid"), -1) == recorded
        and is_pid_alive(recorded)
    ):
        return heartbeat
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


def _submit_control_api(
    runtime: Path, project_id: str, action: str, expected: dict[str, Any],
    *, target: dict[str, Any] | None = None, command_id: str | None = None,
) -> dict[str, Any]:
    """Use the authenticated 8770 contract; never bypass the daemon HTTP ingress."""
    heartbeat = read_json(runtime / "web.json", {})
    host = heartbeat.get("listen_address") if isinstance(heartbeat, dict) else None
    port = heartbeat.get("port") if isinstance(heartbeat, dict) else None
    if not isinstance(host, str) or not is_loopback(host):
        _fail("loopback 8770 Control API is unavailable")
    try:
        port = int(port)
        token = (runtime / "control" / "api-token").read_text(encoding="utf-8").strip()
    except (OSError, TypeError, ValueError):
        _fail("8770 Control API credentials are unavailable")
    if not token:
        _fail("8770 Control API credentials are unavailable")
    body = json.dumps({
        "schema_version": 1, "command_id": command_id or str(uuid4()),
        "project_id": project_id, "action": action,
        "expected": expected, "target": dict(target or {}),
    }, ensure_ascii=False).encode("utf-8")
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request(
            "POST", "/api/v1/control/commands", body=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        response = connection.getresponse()
        raw = response.read()
    except OSError as exc:
        _fail(f"8770 Control API request failed: {exc}")
    finally:
        connection.close()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _fail("8770 Control API returned an invalid response")
    if response.status not in {200, 202} or not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        message = payload.get("message") if isinstance(payload, dict) else None
        _fail(str(message or f"8770 Control API returned HTTP {response.status}"))
    return payload["data"]


def cmd_project_status(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    project_id = _validated_project_id(args.project_id)
    snapshot = read_json(runtime / "projects" / (project_id + ".json"), None)
    if not isinstance(snapshot, dict) or snapshot.get("project_id") != project_id:
        from dev_orchestrator.core.activation import detect_orphan_state
        from dev_orchestrator.core.blockers import explain_block, blocker_payload
        cfg = None
        cfg_path = getattr(args, "config", None)
        resolved_cfg_path = resolve_config_path(cfg_path) if cfg_path else None
        if resolved_cfg_path:
            try:
                cfg = load_projects_config(resolved_cfg_path)
            except Exception:
                pass
        orphan = detect_orphan_state(project_id, None, cfg, runtime)
        repo_for_block = Path(orphan["repo_path"]) if orphan and orphan.get("repo_path") else None
        blockers = explain_block(
            project_id=project_id,
            project_config=None,
            snapshot=None,
            repo_path=repo_for_block,
            runtime_root=runtime,
            config_path=resolved_cfg_path,
            action="continue",
        )
        not_found_payload: dict[str, Any] = {
            "project_id": project_id,
            "state": "not_found",
            "blockers": blocker_payload(blockers),
        }
        if orphan is not None:
            not_found_payload["orphan"] = orphan
        _print_json(not_found_payload)
        return 1
    from dev_orchestrator.control.surface import project_control_view
    project_config = None
    if getattr(args, "config", None) is not None:
        config = load_projects_config(resolve_config_path(args.config))
        project_config = next(
            (row for row in config.get("projects") or [] if row.get("project_id") == project_id),
            None,
        )
    projected = project_control_view(snapshot, runtime, project_config)
    payload = dict(projected)
    payload["latest_control"] = latest_control_result(runtime, project_id)
    _print_json(payload)
    return 0


def cmd_project_explain_block(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    project_id = getattr(args, "project_id", None)
    repo = getattr(args, "repo", None)
    if not project_id and not repo:
        _fail("either --project-id or --repo must be provided")
    config_path = resolve_config_path(args.config) if getattr(args, "config", None) else None
    from dev_orchestrator.core.blockers import explain_block, blocker_payload
    snapshot = None
    project_config = None
    if project_id:
        project_id = _validated_project_id(project_id)
        snapshot = read_json(runtime / "projects" / (project_id + ".json"), None)
        if config_path:
            try:
                cfg = load_projects_config(config_path)
                project_config = next(
                    (p for p in cfg.get("projects") or [] if p.get("project_id") == project_id),
                    None,
                )
            except Exception:
                pass
    blockers = explain_block(
        project_id=project_id,
        project_config=project_config,
        snapshot=snapshot,
        repo_path=Path(repo).resolve(strict=False) if repo else None,
        runtime_root=runtime,
        config_path=config_path,
        action="continue",
    )
    _print_json(blocker_payload(blockers))
    return 0


def cmd_project_activate(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    if not getattr(args, "repo", None):
        _fail("--repo is required")
    repo_path = Path(args.repo).resolve(strict=False)
    if not repo_path.exists():
        _fail(f"repository path does not exist: {repo_path}")
    from dev_orchestrator.core.activation import record_activation_request
    config_path = resolve_config_path(args.config) if getattr(args, "config", None) else None
    proj_id = _validated_project_id(args.project_id) if getattr(args, "project_id", None) else None
    try:
        request = record_activation_request(
            runtime_root=runtime,
            repo_path=repo_path,
            project_id=proj_id,
            config_path=config_path,
            profile=getattr(args, "profile", None),
            requested_action=getattr(args, "action", "continue") or "continue",
            source="cli",
        )
    except ValueError as exc:
        _fail(str(exc))
    _print_json(request)
    return 0


def cmd_project_continue(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    project_id = _validated_project_id(args.project_id)
    if _recorded_pid(runtime, "daemon") is None:
        _fail("DevOrchestrator daemon is not running")
    snapshot = read_json(runtime / "projects" / (project_id + ".json"), None)
    if not isinstance(snapshot, dict) or snapshot.get("project_id") != project_id:
        _fail("project is not present in the current runtime")
    from dev_orchestrator.control.surface import project_identity
    target = {"gate_id": args.gate_id} if getattr(args, "gate_id", None) else {}
    _print_json(_submit_control_api(
        runtime, project_id, "continue", project_identity(snapshot, runtime), target=target
    ))
    return 0


def cmd_project_control(args: argparse.Namespace) -> int:
    """Queue any closed P12 action using the same command envelope as 8770."""
    runtime = resolve_runtime_root(args.runtime_root)
    project_id = _validated_project_id(args.project_id)
    if _recorded_pid(runtime, "daemon") is None:
        _fail("DevOrchestrator daemon is not running")
    snapshot = read_json(runtime / "projects" / (project_id + ".json"), None)
    if not isinstance(snapshot, dict) or snapshot.get("project_id") != project_id:
        _fail("project is not present in the current runtime")
    from dev_orchestrator.control.surface import project_control_view
    config = load_projects_config(resolve_config_path(getattr(args, "config", None)))
    project_config = next((row for row in config.get("projects") or [] if row.get("project_id") == project_id), None)
    # Command CAS identity must be derived from the same projection that the
    # daemon command coordinator will observe. The daemon validates retry and
    # rereview against the orchestration lifecycle overlay (e.g. REVIEW_FAILED),
    # while all other controls retain their raw per-project snapshot semantics.
    control_snapshot = snapshot
    if args.action in {"retry", "rereview"}:
        from dev_orchestrator.core.project_status import project_runtime_status
        from dev_orchestrator.core.lifecycle_projection import overlay_orchestration_lifecycle
        reviewer_state = read_json(runtime / "ai-reviewer.json", None)
        overlay_snapshot = project_runtime_status(snapshot, runtime)
        overlay_res = overlay_orchestration_lifecycle(
            {"projects": [overlay_snapshot]},
            reviewer_state=reviewer_state if isinstance(reviewer_state, dict) else None,
        )
        if isinstance(overlay_res, dict) and isinstance(overlay_res.get("projects"), list) and overlay_res["projects"]:
            control_snapshot = overlay_res["projects"][0]
        if control_snapshot.get("lifecycle_state") not in {"REVIEW_FAILED", "RECOVERY_REQUIRED"}:
            summary = read_json(runtime / "summary.json", {})
            if isinstance(summary, dict) and isinstance(summary.get("projects"), list):
                summary_project = next((row for row in summary["projects"] if isinstance(row, dict) and row.get("project_id") == project_id), None)
                if isinstance(summary_project, dict) and summary_project.get("lifecycle_state") in {"REVIEW_FAILED", "RECOVERY_REQUIRED"}:
                    control_snapshot = {**snapshot, "lifecycle_state": summary_project["lifecycle_state"]}
    projected = project_control_view(control_snapshot, runtime, project_config)
    target = {}
    if args.binding_id and args.action in {"bind_conversation", "rebind_conversation"}:
        target.update({"adapter": args.adapter, "binding_id": args.binding_id})
    if args.target_id and args.action in {"retry", "rereview", "reconcile"}:
        target["target_id"] = args.target_id
    if args.action == "approve_owner_gate":
        # Use the projected exact pending gate unless the caller supplied an explicit target.
        gate_id = args.target_id or projected["control_identity"].get("gate_id")
        if gate_id:
            target["gate_id"] = gate_id
    _print_json(_submit_control_api(
        runtime, project_id, args.action, projected["control_identity"],
        command_id=args.command_id, target=target,
    ))
    return 0


def cmd_control_overview(args: argparse.Namespace) -> int:
    from dev_orchestrator.web.server import control_overview_payload
    _print_json(control_overview_payload(
        resolve_runtime_root(args.runtime_root), resolve_config_path(args.config)
    ))
    return 0


def cmd_mcp_adapter(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.control.mcp_adapter import run_mcp_adapter
    return run_mcp_adapter(
        base_url=getattr(args, "base_url", None),
        token=getattr(args, "token", None),
        runtime_root=str(runtime),
    )


def cmd_create_web_bridge_capability(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.control.security import ControlSecurity
    security = ControlSecurity(runtime)
    res = security.create_web_bridge_capability(
        args.project_id,
        args.binding_id,
        adapter=args.adapter,
        expires_in_seconds=args.ttl,
    )
    _print_json(res)
    return 0


def cmd_revoke_web_bridge_capability(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.control.security import ControlSecurity
    security = ControlSecurity(runtime)
    try:
        res = security.revoke_capability(args.capability_id)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    _print_json(res)
    return 0


def cmd_mobile_pair(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.control.security import ControlSecurity
    security = ControlSecurity(runtime)
    try:
        res = security.create_mobile_pairing(expires_in_seconds=args.ttl)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    _print_json(res)
    return 0



def cmd_watchdog_status(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    payload = watchdog_payload(runtime)
    raw_pid = getattr(args, "project_id", None)
    if raw_pid:
        project_id = _validated_project_id(raw_pid)
        projects = payload.get("projects") or {}
        if project_id not in projects:
            _print_json({"project_id": project_id, "state": "not_found"})
            return 1
        _print_json(projects[project_id])
        return 0
    _print_json(payload)
    return 0


def cmd_watchdog_clear_degraded(args: argparse.Namespace) -> int:
    """Clear watchdog degraded mode and persist a fresh valid state.

    This is the production recovery path after resolving a corrupt or
    future-version watchdog.json. Run this command while the daemon is stopped;
    the daemon will re-initialize with a clean state on next start.
    """
    from dev_orchestrator.core.watchdog import WatchdogCoordinator
    runtime = resolve_runtime_root(args.runtime_root)

    # R3-F5: Fail closed if the daemon is currently live.  Clearing degraded state while
    # the daemon is running risks a race where the daemon overwrites the freshly-cleared
    # state file, so we refuse the operation to protect operator intent.
    daemon_pid = _recorded_pid(runtime, "daemon")
    if daemon_pid is not None and is_pid_alive(daemon_pid):
        _fail(
            "Daemon is running (PID {0}). Stop the daemon before clearing watchdog degraded mode.".format(daemon_pid)
        )

    coordinator = WatchdogCoordinator(runtime)
    was_degraded = bool(coordinator.state().get("degraded"))

    # R3-F5: Require at least one verified quarantine file before overwriting.
    # If no quarantine file exists and the coordinator is degraded, the corrupt original
    # was never preserved — refuse to clear to prevent silent data loss.
    if was_degraded:
        quarantine_files = list(runtime.glob("watchdog.json.corrupt-*"))
        if not quarantine_files:
            _fail(
                "Watchdog is degraded but no quarantine backup (watchdog.json.corrupt-*) was found. "
                "Cannot safely clear degraded mode without a verified quarantine copy."
            )

    coordinator.clear_degraded()
    _print_json({
        "runtime_root": str(runtime),
        "was_degraded": was_degraded,
        "state": "cleared" if was_degraded else "not_degraded",
    })
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
        # An initialized daemon in a slow first tick is not hung; do not kill it.
        heartbeat = _await_first_tick(runtime, pid_path)
    if heartbeat is None:
        recorded = _read_pid_file(pid_path)
        terminate_pid(child.pid)
        if recorded is not None and recorded != child.pid:
            terminate_pid(recorded)
        _fail("Daemon process started but heartbeat was not observed within 8 seconds.")
    _print_json(
        {
            "state": "running" if heartbeat.get("state") != "starting" else "starting",
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
# execution jobs commands
# --------------------------------------------------------------------------

def cmd_jobs_list(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.jobs.service import JobService
    service = JobService(runtime)
    jobs = service.store.list(project_id=args.project_id)
    sys.stdout.write(json.dumps(jobs, indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_job_status(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.jobs.service import JobService
    service = JobService(runtime)
    rec = service.status(args.job_id)
    if rec is None:
        raise CliError(f"job {args.job_id} not found")
    sys.stdout.write(json.dumps(rec.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_job_logs(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.jobs.service import JobService
    service = JobService(runtime)
    try:
        data = service.logs(args.job_id, cursor=args.cursor, limit=args.limit)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_job_submit(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.jobs.models import JobSpec
    from dev_orchestrator.jobs.service import JobService
    service = JobService(runtime)
    spec = JobSpec(
        project_id=args.project_id,
        command_ref=args.command_ref,
        idempotency_key=args.idempotency_key,
        kind=getattr(args, "kind", "validation") or "validation",
        transport=getattr(args, "transport", "local") or "local",
        expected_working_directory=getattr(args, "expected_working_directory", None),
    )
    try:
        rec = service.submit(spec)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(rec.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_job_cancel(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.jobs.service import JobService
    service = JobService(runtime)
    try:
        rec = service.cancel(args.job_id, reason=args.reason or "cancelled")
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(rec.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_job_retry(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    if not getattr(args, "retry_request_id", None):
        raise CliError("--retry-request-id is required")
    from dev_orchestrator.jobs.service import JobService
    service = JobService(runtime)
    try:
        rec = service.retry(args.job_id, args.retry_request_id)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(rec.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_job_reconcile(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.jobs.service import JobService
    service = JobService(runtime)
    try:
        rec = service.reconcile(args.job_id)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(rec.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_transport_exec(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.transport import MachineOperation, get_transport_for_host
    import dataclasses
    host_id = getattr(args, "host_id", None) or "local"
    params = None
    if getattr(args, "parameters", None):
        try:
            params = json.loads(args.parameters)
        except Exception as exc:
            raise CliError(f"invalid JSON in --parameters: {exc}") from exc
    op = MachineOperation(
        project_id=args.project_id,
        command_ref=args.command_ref,
        idempotency_key=getattr(args, "idempotency_key", None) or str(uuid4()),
        parameters=params,
        host_id=host_id,
        expected_working_directory=getattr(args, "expected_working_directory", None),
        timeout_seconds=getattr(args, "timeout", None),
    )
    try:
        transport = get_transport_for_host(runtime, host_id=host_id, config_path=getattr(args, "config", None))
        res = transport.exec(op)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(dataclasses.asdict(res), indent=2, ensure_ascii=False) + "\n")
    return 0 if res.status == "ok" else 1


def cmd_transport_spawn(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.transport import MachineOperation, get_transport_for_host
    import dataclasses
    host_id = getattr(args, "host_id", None) or "local"
    params = None
    if getattr(args, "parameters", None):
        try:
            params = json.loads(args.parameters)
        except Exception as exc:
            raise CliError(f"invalid JSON in --parameters: {exc}") from exc
    op = MachineOperation(
        project_id=args.project_id,
        command_ref=args.command_ref,
        idempotency_key=getattr(args, "idempotency_key", None) or str(uuid4()),
        parameters=params,
        host_id=host_id,
        expected_working_directory=getattr(args, "expected_working_directory", None),
        input_digest=getattr(args, "input_digest", None),
        timeout_seconds=getattr(args, "timeout", None),
    )
    try:
        transport = get_transport_for_host(runtime, host_id=host_id, config_path=getattr(args, "config", None))
        res = transport.spawn(op)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(dataclasses.asdict(res), indent=2, ensure_ascii=False) + "\n")
    return 0 if res.status in ("ok", "queued", "running") else 1


def cmd_transport_poll(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.transport import get_transport_for_host
    import dataclasses
    host_id = getattr(args, "host_id", None) or "local"
    try:
        transport = get_transport_for_host(runtime, host_id=host_id, config_path=getattr(args, "config", None))
        res = transport.poll(args.operation_id, host_id=host_id)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(dataclasses.asdict(res), indent=2, ensure_ascii=False) + "\n")
    return 0 if res.status in ("ok", "queued", "running") else 1


def cmd_transport_cancel(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.transport import get_transport_for_host
    import dataclasses
    host_id = getattr(args, "host_id", None) or "local"
    reason = getattr(args, "reason", "cancelled") or "cancelled"
    try:
        transport = get_transport_for_host(runtime, host_id=host_id, config_path=getattr(args, "config", None))
        res = transport.cancel(args.operation_id, host_id=host_id, reason=reason)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(dataclasses.asdict(res), indent=2, ensure_ascii=False) + "\n")
    return 0 if res.status in ("ok", "cancelled") else 1


def cmd_transport_read(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.transport import FileReadRequest, get_transport_for_host
    import dataclasses
    import base64
    host_id = getattr(args, "host_id", None) or "local"
    req = FileReadRequest(
        project_id=args.project_id,
        path=args.path,
        host_id=host_id,
        max_bytes=getattr(args, "max_bytes", 10 * 1024 * 1024) or (10 * 1024 * 1024),
        offset_bytes=getattr(args, "offset_bytes", 0) or 0,
    )
    try:
        transport = get_transport_for_host(runtime, host_id=host_id, config_path=getattr(args, "config", None))
        res = transport.read_file(req)
    except Exception as exc:
        raise CliError(str(exc)) from exc

    out_file = getattr(args, "output", None)
    if out_file and res.content_bytes is not None:
        try:
            Path(out_file).write_bytes(res.content_bytes)
        except OSError as exc:
            raise CliError(f"failed to write output file {out_file}: {exc}") from exc

    d = dataclasses.asdict(res)
    if d.get("content_bytes") is not None:
        d["content_base64"] = base64.b64encode(d.pop("content_bytes")).decode("ascii")
    sys.stdout.write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    return 0 if res.status == "ok" else 1


def cmd_transport_stage_write(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.transport import WriteContentUpload, canonical_sha256, get_transport_for_host
    import dataclasses
    import hashlib
    import base64
    host_id = getattr(args, "host_id", None) or "local"

    content_b64 = getattr(args, "content_base64", None)
    file_path = getattr(args, "file", None)
    if file_path:
        p = Path(file_path)
        if not p.is_file():
            raise CliError(f"file not found: {file_path}")
        data = p.read_bytes()
        content_b64 = base64.b64encode(data).decode("ascii")
        size = len(data)
        sha = canonical_sha256(data)
    elif content_b64:
        try:
            data = base64.b64decode(content_b64, validate=True)
        except Exception as exc:
            raise CliError(f"invalid base64 content: {exc}") from exc
        size = len(data)
        sha = canonical_sha256(data)
    else:
        raise CliError("either --file or --content-base64 is required")

    upload = WriteContentUpload(
        project_id=args.project_id,
        host_id=host_id,
        content_base64=content_b64,
        decoded_size_bytes=size,
        content_sha256=sha,
    )
    try:
        transport = get_transport_for_host(runtime, host_id=host_id, config_path=getattr(args, "config", None))
        res = transport.stage_write_content(upload)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(dataclasses.asdict(res), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_transport_write(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.transport import FileWriteRequest, canonical_sha256, get_transport_for_host
    import dataclasses
    host_id = getattr(args, "host_id", None) or "local"
    req = FileWriteRequest(
        project_id=args.project_id,
        host_id=host_id,
        target_path=args.target_path,
        idempotency_key=getattr(args, "idempotency_key", None) or str(uuid4()),
        content_ref=args.content_ref,
        content_sha256=canonical_sha256(args.content_sha256) if args.content_sha256 else "",
        decoded_size_bytes=int(args.decoded_size_bytes),
        if_absent=getattr(args, "if_absent", None),
        expected_sha256=canonical_sha256(args.expected_sha256) if getattr(args, "expected_sha256", None) else None,
        expected_file_policy_digest=getattr(args, "expected_file_policy_digest", None),
    )
    try:
        transport = get_transport_for_host(runtime, host_id=host_id, config_path=getattr(args, "config", None))
        res = transport.write_file(req)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    d = dataclasses.asdict(res)
    if "content_bytes" in d:
        del d["content_bytes"]
    sys.stdout.write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    return 0 if res.status in ("ok", "applied") else 1


def cmd_transport_stat(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.transport import get_transport_for_host
    import dataclasses
    host_id = getattr(args, "host_id", None) or "local"
    try:
        transport = get_transport_for_host(runtime, host_id=host_id, config_path=getattr(args, "config", None))
        res = transport.stat(args.path, host_id=host_id, project_id=getattr(args, "project_id", None))
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(dataclasses.asdict(res), indent=2, ensure_ascii=False) + "\n")
    return 0 if res.status == "ok" else 1


def cmd_transport_capabilities(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.transport import get_transport_for_host
    import dataclasses
    host_id = getattr(args, "host_id", None) or "local"
    try:
        transport = get_transport_for_host(runtime, host_id=host_id, config_path=getattr(args, "config", None))
        res = transport.capabilities(host_id=host_id)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(dataclasses.asdict(res), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_transport_hosts(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.transport import load_transport_hosts_config
    import dataclasses
    cfg = load_transport_hosts_config(runtime, getattr(args, "config", None))
    out = {
        "default_host": cfg.default_host,
        "hosts": {hid: dataclasses.asdict(p) for hid, p in cfg.hosts.items()},
    }
    sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_review_submit(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.review.models import ReviewRequest
    from dev_orchestrator.review.harness import DefaultReviewerHarness
    from dev_orchestrator.core.repository import read_repository_truth
    from dev_orchestrator.config import load_projects_config
    from dev_orchestrator.storage.json_store import utc_now_iso

    config_path = resolve_config_path(args.config) if getattr(args, "config", None) else None
    repo_path = getattr(args, "repo_path", None)
    if not repo_path and config_path:
        try:
            cfg = load_projects_config(config_path)
            for p in cfg.get("projects", []):
                if p.get("project_id") == args.project_id:
                    repo_path = p.get("repo_path")
                    break
        except Exception:
            pass
    if not repo_path:
        repo_path = "."
    repo_path = str(Path(repo_path).resolve())

    truth = read_repository_truth(repo_path)
    if not truth.valid:
        raise CliError(f"repository truth invalid at {repo_path}")

    request_id = getattr(args, "request_id", None) or f"review-{truth.head[:8]}-{utc_now_iso().replace(':', '').replace('-', '')[:15]}"
    source_request_id = getattr(args, "source_request_id", None) or f"operator-{request_id}"
    diff_refs: dict[str, str] = {}
    if getattr(args, "base", None):
        diff_refs["base"] = str(args.base)
    if getattr(args, "head", None):
        diff_refs["head"] = str(args.head)

    req = ReviewRequest(
        request_id=request_id,
        project_id=args.project_id,
        task_id=getattr(args, "task_id", None) or "manual-review",
        source_request_id=source_request_id,
        mode=getattr(args, "mode", "diff") or "diff",
        diff_mode=getattr(args, "diff_mode", "workspace") or "workspace",
        diff_refs=diff_refs,
        scan_roots=list(getattr(args, "scan_roots", None) or []),
        rule_pack_path=getattr(args, "rule_pack", None),
        branch=truth.branch,
        head=truth.head,
        status_hash=truth.status_hash,
        transport=getattr(args, "transport", "local") or "local",
        metadata={"repo_path": repo_path},
    )

    harness = DefaultReviewerHarness(runtime)
    try:
        session = harness.submit(req)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(session.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_review_status(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.review.harness import DefaultReviewerHarness
    harness = DefaultReviewerHarness(runtime)
    try:
        session = harness.status(args.session_id)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(session.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_review_reconcile(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.review.harness import DefaultReviewerHarness
    harness = DefaultReviewerHarness(runtime)
    try:
        session = harness.reconcile(args.session_id)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(session.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_review_findings(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.review.harness import DefaultReviewerHarness
    from dev_orchestrator.review.models import to_sarif, ReviewFinding
    harness = DefaultReviewerHarness(runtime)
    try:
        session = harness.status(args.session_id)
        if session.result is not None:
            findings = list(session.result.findings)
        else:
            findings = []
            if session.job_id:
                art = harness.job_service.get_artifact(session.job_id, "findings.json")
                if art and isinstance(art.get("content"), list):
                    findings = [ReviewFinding.from_dict(f) for f in art["content"] if isinstance(f, dict)]
    except Exception as exc:
        raise CliError(str(exc)) from exc

    if getattr(args, "severity", None):
        findings = [f for f in findings if f.severity == args.severity]
    if getattr(args, "rule_id", None):
        findings = [f for f in findings if f.rule_id == args.rule_id]

    fmt = getattr(args, "format", "json") or "json"
    if fmt == "sarif":
        sarif_doc = to_sarif(findings)
        sys.stdout.write(json.dumps(sarif_doc, indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(json.dumps([f.to_dict() for f in findings], indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_incident_list(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.incidents.store import load_incident_store
    store = load_incident_store(runtime)
    families = store.index.get("families", {})
    records = []
    project_filter = getattr(args, "project_id", None)
    for fam_id, fam in families.items():
        if project_filter and fam.get("project_id") != project_filter:
            continue
        records.append(fam)
    records.sort(key=lambda r: (r.get("last_seen_at") or "", r.get("family_id") or ""), reverse=True)
    fmt = getattr(args, "format", "json") or "json"
    if fmt == "json":
        sys.stdout.write(json.dumps(records, indent=2, ensure_ascii=False) + "\n")
    else:
        for r in records:
            sys.stdout.write(f"{r.get('family_id')} | {r.get('project_id')} | {r.get('classification')} | recurrences={r.get('recurrence_count', 1)} | last_seen={r.get('last_seen_at')}\n")
    return 0


def cmd_incident_show(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.incidents.store import load_incident_store
    store = load_incident_store(runtime)
    family_id = args.family_id
    families = store.index.get("families", {})
    family = families.get(family_id)
    if not family:
        matching = [
            f for f in families.values()
            if f.get("family_id") == family_id or f.get("packet_id") == family_id or f.get("fingerprint") == family_id
        ]
        if matching:
            family = matching[0]
    if not family:
        raise CliError(f"incident family or packet '{family_id}' not found")

    packet_id = family.get("packet_id")
    packet_data = None
    if packet_id:
        p_path = store.packets_dir / f"{packet_id}.json"
        if p_path.is_file():
            try:
                packet_data = json.loads(p_path.read_text(encoding="utf-8"))
            except Exception:
                pass
    out = {
        "family": family,
        "packet": packet_data,
    }
    sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_candidate_list(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.incidents.store import load_incident_store
    store = load_incident_store(runtime)
    candidates = store.index.get("candidates", {})
    records = []
    project_filter = getattr(args, "project_id", None)
    for cand_id, cand in candidates.items():
        if project_filter and cand.get("project_id") != project_filter:
            continue
        records.append(cand)
    records.sort(key=lambda r: (r.get("created_at") or "", r.get("candidate_id") or ""), reverse=True)
    fmt = getattr(args, "format", "json") or "json"
    if fmt == "json":
        sys.stdout.write(json.dumps(records, indent=2, ensure_ascii=False) + "\n")
    else:
        for r in records:
            sys.stdout.write(f"{r.get('candidate_id')} | {r.get('status')} | pre_review={r.get('pre_review_digest')} | {r.get('created_at')}\n")
    return 0


def cmd_candidate_evaluate(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.incidents.evaluation import evaluate_promotion
    try:
        result = evaluate_promotion(runtime, args.candidate_id)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_candidate_review(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.incidents.review import record_candidate_review
    try:
        result = record_candidate_review(
            runtime,
            candidate_id=args.candidate_id,
            reviewer_id=args.reviewer_id,
            verdict=args.verdict,
            notes=getattr(args, "notes", "") or "",
        )
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_candidate_materialize(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.incidents.candidate import materialize_candidate
    config_path = resolve_config_path(args.config) if getattr(args, "config", None) else None
    try:
        result = materialize_candidate(runtime, args.candidate_id, config_path=config_path)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_candidate_promote(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.incidents.promotion import promote_candidate
    config_path = resolve_config_path(args.config) if getattr(args, "config", None) else None
    try:
        result = promote_candidate(
            runtime,
            candidate_id=args.candidate_id,
            config_path=config_path,
            expected_head=getattr(args, "expected_head", None),
            expected_branch=getattr(args, "expected_branch", None),
        )
    except Exception as exc:
        raise CliError(str(exc)) from exc
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0 if result.get("promoted") else 1


def cmd_pool_status(args: argparse.Namespace) -> int:
    from dev_orchestrator.pool.agy_pool import AGYResourcePool
    pool = AGYResourcePool()
    telemetry = pool.telemetry()
    sys.stdout.write(json.dumps(telemetry.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_agy_benchmark(args: argparse.Namespace) -> int:
    from dev_orchestrator.pool.replay_benchmark import AGYBenchmarkReplayRunner, SimulatedPort
    runner = AGYBenchmarkReplayRunner()
    sim_port = SimulatedPort(failure_on_agy1_task=getattr(args, "simulate_failure", None))
    work_dir = Path(".").resolve()
    result = runner.run_replay(sim_port, work_dir)
    sys.stdout.write(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_agy_route(args: argparse.Namespace) -> int:
    from dev_orchestrator.ai.contracts import AIRoleRequest, ResourceContext
    from dev_orchestrator.pool.agy_pool import AGYResourcePool
    from dev_orchestrator.pool.routing_policy import AGYFirstRoutingPolicy

    pool = AGYResourcePool()
    policy = AGYFirstRoutingPolicy(pool)

    prev_ctx = None
    if getattr(args, "worker_provider", None):
        prev_ctx = ResourceContext(
            provider=args.worker_provider,
            account=getattr(args, "worker_account", "default"),
            resource_id=f"{args.worker_provider}/{getattr(args, 'worker_account', 'default')}/model",
        )

    req = AIRoleRequest(
        project_id="cli",
        task_run_id=getattr(args, "task_id", None) or "cli_task",
        role=args.role,
        prompt="cli evaluation prompt",
        working_directory=Path(".").resolve(),
        independence=getattr(args, "independence", "none") or "none",
        previous_resource_context=prev_ctx,
    )

    history = []
    if getattr(args, "failure_signature", None):
        history.append({
            "provider": getattr(args, "failed_provider", "agy"),
            "resource_id": "agy/agy-1/gemini-3.8-flash-high",
            "failure_signature": args.failure_signature,
        })

    rec = policy.route(
        req,
        failure_history=history,
        strategy_changed=bool(getattr(args, "strategy_changed", False)),
        current_failure_signature=getattr(args, "failure_signature", None),
    )
    sys.stdout.write(json.dumps(rec.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_websol_status(args: argparse.Namespace) -> int:
    from dataclasses import asdict
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.core.websol_health import WebSolHealthStore
    from dev_orchestrator.core.websol_failover import WebSolFailoverStore
    health_store = WebSolHealthStore(runtime)
    entries = health_store.list_all()
    project_filter = getattr(args, "project_id", None)
    if project_filter:
        entries = [e for e in entries if e.project_id == project_filter]

    failover_store = WebSolFailoverStore(runtime)
    failover_records = failover_store.list_all()
    if project_filter:
        failover_records = [r for r in failover_records if r.project_id == project_filter]

    fmt = getattr(args, "format", "json") or "json"
    if fmt == "json":
        payload = {
            "health": [asdict(e) for e in entries],
            "failover": [asdict(r) for r in failover_records],
        }
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    else:
        if not entries:
            sys.stdout.write("No Web Sol health records recorded.\n")
        for e in entries:
            key = f"{e.project_id}/{e.adapter}/{e.binding_id}"
            state = e.availability
            reason = e.reason
            valid_until = e.valid_until
            generation = e.probe_generation
            sys.stdout.write(f"[{state}] {key} (reason={reason}, generation={generation}, valid_until={valid_until})\n")
            signals = e.signals or {}
            for sname, sdata in signals.items():
                if isinstance(sdata, dict):
                    sstatus = sdata.get("status")
                    sreason = sdata.get("reason", "")
                elif hasattr(sdata, "status"):
                    sstatus = sdata.status
                    sreason = getattr(sdata, "reason", "")
                else:
                    sstatus = str(sdata)
                    sreason = ""
                sys.stdout.write(f"  - {sname}: {sstatus} ({sreason})\n")
        if failover_records:
            sys.stdout.write("\nFailover Records:\n")
            for r in failover_records:
                sys.stdout.write(f"  {r.run_id}: state={r.state} attempts={r.attempts} updated_at={r.updated_at}\n")
    return 0


def cmd_websol_probe(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_root(args.runtime_root)
    from dev_orchestrator.bridge.store import BrowserBridgeStore
    from dev_orchestrator.core.websol_health import WebSolHealthStore
    from dev_orchestrator.core.websol_probe import run_websol_probe

    bridge_store = BrowserBridgeStore(runtime)
    health_store = WebSolHealthStore(runtime)

    project_id = getattr(args, "project_id", None)
    adapter = getattr(args, "adapter", "chatgpt_web") or "chatgpt_web"
    binding_id = getattr(args, "binding_id", None)

    if not project_id:
        config_path = resolve_config_path(getattr(args, "config", None))
        try:
            cfg = load_projects_config(config_path)
            for p in cfg.get("projects", []):
                cb = p.get("conversation_binding") or {}
                if cb.get("adapter") == adapter or p.get("adapter") == adapter:
                    project_id = p.get("project_id")
                    if not binding_id:
                        binding_id = cb.get("binding_id")
                    break
        except Exception:
            pass

    if not project_id:
        project_id = "default"

    if not binding_id:
        bindings = bridge_store.list_bindings(adapter=adapter)
        if bindings:
            binding_id = bindings[0]
        else:
            binding_id = "default"

    curr_gen = health_store.current_generation()
    timeout = float(getattr(args, "timeout", 15.0) or 15.0)
    result = run_websol_probe(
        bridge_store,
        adapter,
        binding_id,
        generation=curr_gen,
        timeout_seconds=timeout,
    )
    health_store.record_probe_result(
        project_id=project_id,
        adapter=adapter,
        binding_id=binding_id,
        ok=result.success,
        error=result.reason if not result.success else None,
        duration_seconds=result.duration_seconds,
    )

    fmt = getattr(args, "format", "json") or "json"
    status_str = "success" if result.success else (result.error_class or "failed")
    res_dict = {
        "status": status_str,
        "success": result.success,
        "request_id": result.request_id,
        "nonce": result.nonce,
        "project_id": project_id,
        "adapter": adapter,
        "binding_id": binding_id,
        "duration_seconds": round(result.duration_seconds, 3),
        "reason": result.reason,
    }
    if fmt == "json":
        sys.stdout.write(json.dumps(res_dict, indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(f"Probe {result.request_id} [{'SUCCESS' if result.success else 'FAILED'}]: duration={result.duration_seconds:.2f}s error={result.reason}\n")

    return 0 if result.success else 1



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
    project_status.add_argument("--config", default=None, help="path to projects.json for exact capabilities")

    project_explain_block = sub.add_parser(
        "project-explain-block", help="derive canonical blocker list for a project or repo"
    )
    project_explain_block.add_argument("--project-id", default=None, help="registered project id")
    project_explain_block.add_argument("--repo", default=None, help="path to repository worktree")
    project_explain_block.add_argument("--config", default=None, help="path to projects.json")
    project_explain_block.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    project_activate = sub.add_parser(
        "project-activate", help="record an owner activation request for a repository"
    )
    project_activate.add_argument("--repo", required=True, help="path to repository worktree")
    project_activate.add_argument("--project-id", default=None, help="optional project id")
    project_activate.add_argument("--profile", default=None, help="activation profile name")
    project_activate.add_argument("--action", default="continue", help="requested initial action")
    project_activate.add_argument("--config", default=None, help="path to projects.json")
    project_activate.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    watchdog_status = sub.add_parser("watchdog-status", help="report progress watchdog status")
    watchdog_status.add_argument("--config", default=None, help="path to projects.json")
    watchdog_status.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")
    watchdog_status.add_argument("--project-id", default=None, help="optional project id to filter")

    watchdog_clear_degraded = sub.add_parser(
        "watchdog-clear-degraded",
        help="clear watchdog degraded mode after resolving corrupt/future-version state (run while daemon is stopped)",
    )
    watchdog_clear_degraded.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    project_continue = sub.add_parser("project-continue", help="queue a stateless continue command for one project")
    project_continue.add_argument("project_id")
    project_continue.add_argument("--runtime-root", default=None)
    project_continue.add_argument(
        "--gate-id",
        default=None,
        help="explicit owner-gate correlation id for wait-time accounting",
    )

    project_control = sub.add_parser("project-control", help="queue a closed P12 control action")
    project_control.add_argument("project_id")
    project_control.add_argument("action", choices=(
        "continue", "pause", "resume", "stop", "retry", "rereview", "reconcile",
        "approve_owner_gate", "bind_conversation", "unbind_conversation", "rebind_conversation",
    ))
    project_control.add_argument("--command-id", default=None)
    project_control.add_argument("--target-id", default=None)
    project_control.add_argument("--adapter", default="chatgpt_web")
    project_control.add_argument("--binding-id", default=None)
    project_control.add_argument("--runtime-root", default=None)
    project_control.add_argument("--config", default=None, help="path to projects.json")

    control_overview = sub.add_parser("control-overview", help="print the versioned P12 overview envelope")
    control_overview.add_argument("--runtime-root", default=None)
    control_overview.add_argument("--config", default=None, help="path to projects.json")

    mcp_adapter = sub.add_parser("mcp-adapter", help="run stdio JSON-RPC 2.0 MCP adapter for DevOrchestrator control")
    mcp_adapter.add_argument("--base-url", default=None, help="DevOrchestrator control HTTP base URL")
    mcp_adapter.add_argument("--token", default=None, help="control bearer token")
    mcp_adapter.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    create_cap = sub.add_parser("create-web-bridge-capability", help="issue a scoped web bridge capability token")
    create_cap.add_argument("project_id", help="target project ID")
    create_cap.add_argument("binding_id", help="browser conversation binding ID")
    create_cap.add_argument("--adapter", default="chatgpt_web", help="control adapter name")
    create_cap.add_argument("--ttl", type=int, default=3600, help="time-to-live in seconds")
    create_cap.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    revoke_cap = sub.add_parser("revoke-web-bridge-capability", help="revoke an active capability token")
    revoke_cap.add_argument("capability_id", help="capability ID to revoke")
    revoke_cap.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    mobile_pair = sub.add_parser("mobile-pair", help="mint a mobile pairing code via ControlSecurity")
    mobile_pair.add_argument("--ttl", type=int, default=300, help="pairing TTL in seconds (default: 300)")
    mobile_pair.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    import_rdc = sub.add_parser(
        "import-rdc-evidence",
        help="import normalized RDC invocation evidence into the accounting ledger",
    )
    import_rdc.add_argument("--input", required=True, help="JSON array or JSONL evidence path")
    import_rdc.add_argument("--config", default=None, help="path to projects.json")
    import_rdc.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    execution_report = sub.add_parser(
        "execution-report",
        help="render a P11 accounting/provider/RDC report and quantitative gates",
    )
    execution_report.add_argument("--window-start", required=True, help="inclusive ISO-8601 start")
    execution_report.add_argument("--window-end", required=True, help="inclusive ISO-8601 end")
    execution_report.add_argument("--project-id", default=None)
    execution_report.add_argument("--task-id", default=None)
    execution_report.add_argument("--role", choices=tuple(sorted(ROLES)), default=None)
    execution_report.add_argument("--config", default=None, help="optional projects.json for custom event path")
    execution_report.add_argument("--runtime-root", default=None)
    execution_report.add_argument("--fail-on-gate", action="store_true")

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

    jobs_list = sub.add_parser("jobs-list", help="list execution jobs")
    jobs_list.add_argument("--project-id", default=None, help="optional project id filter")
    jobs_list.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    job_status = sub.add_parser("job-status", help="get execution job status")
    job_status.add_argument("job_id", help="job ID")
    job_status.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    job_logs = sub.add_parser("job-logs", help="read paginated execution job logs")
    job_logs.add_argument("job_id", help="job ID")
    job_logs.add_argument("--cursor", type=int, default=0, help="cursor offset")
    job_logs.add_argument("--limit", type=int, default=100, help="maximum log lines to return")
    job_logs.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    job_submit = sub.add_parser("job-submit", help="submit a durable execution job")
    job_submit.add_argument("--project-id", required=True, help="project ID")
    job_submit.add_argument("--command-ref", required=True, help="command reference from execution-jobs.json")
    job_submit.add_argument("--idempotency-key", required=True, help="idempotency key")
    job_submit.add_argument("--kind", default="validation", help="job kind (default: validation)")
    job_submit.add_argument("--transport", choices=("local", "ssh"), default="local", help="execution transport")
    job_submit.add_argument("--expected-working-directory", default=None, help="expected working directory assertion")
    job_submit.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    job_cancel = sub.add_parser("job-cancel", help="cancel an active execution job")
    job_cancel.add_argument("job_id", help="job ID")
    job_cancel.add_argument("--reason", default="cancelled", help="cancellation reason")
    job_cancel.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    job_retry = sub.add_parser("job-retry", help="retry a terminal or recovery-safe execution job")
    job_retry.add_argument("job_id", help="job ID to retry")
    job_retry.add_argument("--retry-request-id", required=True, help="stable caller-provided retry request ID")
    job_retry.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    job_reconcile = sub.add_parser("job-reconcile", help="reconcile an ambiguous or interrupted execution job")
    job_reconcile.add_argument("job_id", help="job ID to reconcile")
    job_reconcile.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    t_exec = sub.add_parser("transport-exec", help="execute a read-only command via native machine transport")
    t_exec.add_argument("--project-id", required=True, help="project ID")
    t_exec.add_argument("--command-ref", required=True, help="command reference")
    t_exec.add_argument("--host-id", default="local", help="target execution host (default: local)")
    t_exec.add_argument("--parameters", default=None, help="JSON-encoded parameters object")
    t_exec.add_argument("--expected-working-directory", default=None, help="expected working directory assertion")
    t_exec.add_argument("--timeout", type=float, default=None, help="timeout in seconds")
    t_exec.add_argument("--idempotency-key", default=None, help="idempotency key")
    t_exec.add_argument("--config", default=None, help="path to projects.json / transport-hosts.json")
    t_exec.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    t_spawn = sub.add_parser("transport-spawn", help="spawn a durable operation via native machine transport")
    t_spawn.add_argument("--project-id", required=True, help="project ID")
    t_spawn.add_argument("--command-ref", required=True, help="command reference")
    t_spawn.add_argument("--host-id", default="local", help="target execution host (default: local)")
    t_spawn.add_argument("--parameters", default=None, help="JSON-encoded parameters object")
    t_spawn.add_argument("--expected-working-directory", default=None, help="expected working directory assertion")
    t_spawn.add_argument("--input-digest", default=None, help="client-asserted input digest")
    t_spawn.add_argument("--timeout", type=float, default=None, help="timeout in seconds")
    t_spawn.add_argument("--idempotency-key", default=None, help="idempotency key")
    t_spawn.add_argument("--config", default=None, help="path to projects.json / transport-hosts.json")
    t_spawn.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    t_poll = sub.add_parser("transport-poll", help="poll status of a spawned durable operation")
    t_poll.add_argument("operation_id", help="operation or job ID")
    t_poll.add_argument("--host-id", default="local", help="target execution host (default: local)")
    t_poll.add_argument("--config", default=None, help="path to config")
    t_poll.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    t_cancel = sub.add_parser("transport-cancel", help="cancel an active spawned durable operation")
    t_cancel.add_argument("operation_id", help="operation or job ID")
    t_cancel.add_argument("--host-id", default="local", help="target execution host (default: local)")
    t_cancel.add_argument("--reason", default="cancelled", help="cancellation reason")
    t_cancel.add_argument("--config", default=None, help="path to config")
    t_cancel.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    t_read = sub.add_parser("transport-read", help="read a scoped file via native machine transport")
    t_read.add_argument("--project-id", required=True, help="project ID")
    t_read.add_argument("--path", required=True, help="target path relative to file roots")
    t_read.add_argument("--host-id", default="local", help="target execution host (default: local)")
    t_read.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024, help="maximum bytes to read")
    t_read.add_argument("--offset-bytes", type=int, default=0, help="byte offset to start reading from")
    t_read.add_argument("--output", default=None, help="optional local file to write read bytes")
    t_read.add_argument("--config", default=None, help="path to config")
    t_read.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    t_stage = sub.add_parser("transport-stage-write", help="stage binary content for CAS write")
    t_stage.add_argument("--project-id", required=True, help="project ID")
    t_stage.add_argument("--host-id", default="local", help="target execution host (default: local)")
    t_stage.add_argument("--file", default=None, help="path to local file to stage")
    t_stage.add_argument("--content-base64", default=None, help="RFC 4648 Base64 encoded payload")
    t_stage.add_argument("--config", default=None, help="path to config")
    t_stage.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    t_write = sub.add_parser("transport-write", help="commit staged binary content via CAS write")
    t_write.add_argument("--project-id", required=True, help="project ID")
    t_write.add_argument("--target-path", required=True, help="target file path")
    t_write.add_argument("--content-ref", required=True, help="staged content ref")
    t_write.add_argument("--content-sha256", required=True, help="expected staged content SHA-256")
    t_write.add_argument("--decoded-size-bytes", type=int, required=True, help="decoded payload byte size")
    t_write.add_argument("--host-id", default="local", help="target execution host (default: local)")
    t_write.add_argument("--idempotency-key", default=None, help="idempotency key")
    t_write.add_argument("--if-absent", action="store_true", default=None, help="require target file to not exist")
    t_write.add_argument("--expected-sha256", default=None, help="expected existing file SHA-256 before overwrite")
    t_write.add_argument("--expected-file-policy-digest", default=None, help="expected policy digest")
    t_write.add_argument("--config", default=None, help="path to config")
    t_write.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    t_stat = sub.add_parser("transport-stat", help="stat a scoped path via native machine transport")
    t_stat.add_argument("--path", required=True, help="path to inspect")
    t_stat.add_argument("--host-id", default="local", help="target execution host (default: local)")
    t_stat.add_argument("--project-id", default=None, help="optional project ID")
    t_stat.add_argument("--config", default=None, help="path to config")
    t_stat.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    t_caps = sub.add_parser("transport-capabilities", help="query execution host capabilities")
    t_caps.add_argument("--host-id", default="local", help="target execution host (default: local)")
    t_caps.add_argument("--config", default=None, help="path to config")
    t_caps.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    t_hosts = sub.add_parser("transport-hosts", help="list configured execution transport hosts")
    t_hosts.add_argument("--config", default=None, help="path to config")
    t_hosts.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    review_submit = sub.add_parser("review-submit", help="submit a review session")
    review_submit.add_argument("--project-id", required=True, help="project ID")
    review_submit.add_argument("--task-id", default=None, help="task ID")
    review_submit.add_argument("--source-request-id", default=None, help="originating source or worker request ID")
    review_submit.add_argument("--request-id", default=None, help="custom review request ID")
    review_submit.add_argument("--mode", choices=("diff", "scan"), default="diff", help="review mode")
    review_submit.add_argument("--diff-mode", choices=("workspace", "range", "commit"), default="workspace", help="diff mode")
    review_submit.add_argument("--scan-roots", nargs="*", default=None, help="scan root directories")
    review_submit.add_argument("--rule-pack", default=None, help="relative path to rule pack JSON")
    review_submit.add_argument("--base", default=None, help="base git ref for range diff")
    review_submit.add_argument("--head", default=None, help="head git ref for range diff")
    review_submit.add_argument("--transport", choices=("local", "ssh"), default="local", help="job transport")
    review_submit.add_argument("--repo-path", default=None, help="path to repository root")
    review_submit.add_argument("--config", default=None, help="path to projects.json")
    review_submit.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    review_status = sub.add_parser("review-status", help="query review session status")
    review_status.add_argument("session_id", help="review session ID")
    review_status.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    review_reconcile = sub.add_parser("review-reconcile", help="reconcile an ambiguous or interrupted review session")
    review_reconcile.add_argument("session_id", help="review session ID to reconcile")
    review_reconcile.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    review_findings = sub.add_parser("review-findings", help="query findings for a review session")
    review_findings.add_argument("session_id", help="review session ID")
    review_findings.add_argument("--severity", default=None, help="filter by severity (blocking, warning, info)")
    review_findings.add_argument("--rule-id", default=None, help="filter by rule ID")
    review_findings.add_argument("--format", choices=("json", "sarif"), default="json", help="output format")
    review_findings.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    incident_list = sub.add_parser("incident-list", help="list harvested incident families")
    incident_list.add_argument("--project-id", default=None, help="filter by project ID")
    incident_list.add_argument("--format", choices=("json", "text"), default="json", help="output format")
    incident_list.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    incident_show = sub.add_parser("incident-show", help="show details of an incident family")
    incident_show.add_argument("family_id", help="incident family ID or packet ID")
    incident_show.add_argument("--format", choices=("json", "text"), default="json", help="output format")
    incident_show.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    candidate_list = sub.add_parser("candidate-list", help="list synthesized regression candidates")
    candidate_list.add_argument("--project-id", default=None, help="filter by project ID")
    candidate_list.add_argument("--format", choices=("json", "text"), default="json", help="output format")
    candidate_list.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    candidate_evaluate = sub.add_parser("candidate-evaluate", help="evaluate a candidate against the 5 executable gates")
    candidate_evaluate.add_argument("candidate_id", help="candidate ID")
    candidate_evaluate.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    candidate_review = sub.add_parser("candidate-review", help="record independent review for a candidate")
    candidate_review.add_argument("candidate_id", help="candidate ID")
    candidate_review.add_argument("--reviewer-id", required=True, help="independent reviewer identity")
    candidate_review.add_argument("--verdict", choices=("ACCEPTED", "REJECTED"), required=True, help="review verdict")
    candidate_review.add_argument("--notes", default="", help="review notes")
    candidate_review.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    candidate_materialize = sub.add_parser("candidate-materialize", help="materialize a candidate into staging tests_candidate/")
    candidate_materialize.add_argument("candidate_id", help="candidate ID")
    candidate_materialize.add_argument("--config", default=None, help="path to projects.json")
    candidate_materialize.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    candidate_promote = sub.add_parser("candidate-promote", help="promote a validated and reviewed candidate to regression owner tests_py/")
    candidate_promote.add_argument("candidate_id", help="candidate ID")
    candidate_promote.add_argument("--config", default=None, help="path to projects.json")
    candidate_promote.add_argument("--expected-head", default=None, help="expected git HEAD of regression owner")
    candidate_promote.add_argument("--expected-branch", default=None, help="expected git branch of regression owner")
    candidate_promote.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    pool_status = sub.add_parser("pool-status", help="display AGY resource pool telemetry")

    agy_benchmark = sub.add_parser("agy-benchmark", help="run representative replay benchmark for AGY pool")
    agy_benchmark.add_argument("--simulate-failure", default=None, help="task ID to inject failure on agy-1")

    agy_route = sub.add_parser("agy-route", help="evaluate routing decision for role and context")
    agy_route.add_argument("--role", required=True, help="AI role (planner, worker, debugger, reviewer)")
    agy_route.add_argument("--independence", default="none", choices=("none", "resource", "account", "provider"))
    agy_route.add_argument("--worker-provider", default=None)
    agy_route.add_argument("--worker-account", default=None)
    agy_route.add_argument("--failure-signature", default=None)
    agy_route.add_argument("--strategy-changed", action="store_true")

    websol_status = sub.add_parser("websol-status", help="display Web Sol health, signals, and failover state")
    websol_status.add_argument("--project-id", default=None, help="filter by project ID")
    websol_status.add_argument("--format", choices=("json", "text"), default="json", help="output format")
    websol_status.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    websol_probe = sub.add_parser("websol-probe", help="run an end-to-end Web Sol probe")
    websol_probe.add_argument("--project-id", default=None, help="project ID")
    websol_probe.add_argument("--adapter", default="chatgpt_web", help="adapter name (default: chatgpt_web)")
    websol_probe.add_argument("--binding-id", default=None, help="conversation binding ID")
    websol_probe.add_argument("--timeout", type=float, default=15.0, help="probe timeout in seconds")
    websol_probe.add_argument("--format", choices=("json", "text"), default="json", help="output format")
    websol_probe.add_argument("--config", default=None, help="path to projects.json")
    websol_probe.add_argument("--runtime-root", default=None, help="DevOrchestrator runtime root")

    return parser


_COMMANDS = {
    "validate-config": cmd_validate_config,
    "project-context": cmd_project_context,
    "project-status": cmd_project_status,
    "project-explain-block": cmd_project_explain_block,
    "project-activate": cmd_project_activate,
    "watchdog-status": cmd_watchdog_status,
    "watchdog-clear-degraded": cmd_watchdog_clear_degraded,
    "project-continue": cmd_project_continue,
    "project-control": cmd_project_control,
    "control-overview": cmd_control_overview,
    "mcp-adapter": cmd_mcp_adapter,
    "create-web-bridge-capability": cmd_create_web_bridge_capability,
    "revoke-web-bridge-capability": cmd_revoke_web_bridge_capability,
    "mobile-pair": cmd_mobile_pair,
    "import-rdc-evidence": cmd_import_rdc_evidence,
    "execution-report": cmd_execution_report,
    "jobs-list": cmd_jobs_list,
    "job-status": cmd_job_status,
    "job-logs": cmd_job_logs,
    "job-submit": cmd_job_submit,
    "job-cancel": cmd_job_cancel,
    "job-retry": cmd_job_retry,
    "job-reconcile": cmd_job_reconcile,
    "transport-exec": cmd_transport_exec,
    "transport-spawn": cmd_transport_spawn,
    "transport-poll": cmd_transport_poll,
    "transport-cancel": cmd_transport_cancel,
    "transport-read": cmd_transport_read,
    "transport-stage-write": cmd_transport_stage_write,
    "transport-write": cmd_transport_write,
    "transport-stat": cmd_transport_stat,
    "transport-capabilities": cmd_transport_capabilities,
    "transport-hosts": cmd_transport_hosts,
    "review-submit": cmd_review_submit,
    "review-status": cmd_review_status,
    "review-reconcile": cmd_review_reconcile,
    "review-findings": cmd_review_findings,
    "incident-list": cmd_incident_list,
    "incident-show": cmd_incident_show,
    "candidate-list": cmd_candidate_list,
    "candidate-evaluate": cmd_candidate_evaluate,
    "candidate-review": cmd_candidate_review,
    "candidate-materialize": cmd_candidate_materialize,
    "candidate-promote": cmd_candidate_promote,
    "pool-status": cmd_pool_status,
    "agy-benchmark": cmd_agy_benchmark,
    "agy-route": cmd_agy_route,
    "websol-status": cmd_websol_status,
    "websol-probe": cmd_websol_probe,
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
