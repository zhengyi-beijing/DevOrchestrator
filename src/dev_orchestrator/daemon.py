"""One-process DevOrchestrator daemon (monitor + Control API + Bridge).

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

import logging
from datetime import datetime, timedelta, timezone

from dev_orchestrator.bridge.server import make_bridge_server
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.ai.runtime_config import load_aibroker_execution_port
from dev_orchestrator.accounting.runtime import load_accounting_runtime
from dataclasses import asdict
from dev_orchestrator.core.dispatcher import DISPATCHER_STATE_FILE, dispatch_worker_done_events
from dev_orchestrator.core.control_commands import ControlCommandCoordinator
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.lifecycle_projection import overlay_orchestration_lifecycle
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.response_consumer import consume_websol_responses
from dev_orchestrator.core.progress import ProgressChannel
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.watchdog import WatchdogCoordinator
from dev_orchestrator.core.activation_supervisor import ActivationSupervisor
from dev_orchestrator.core.project_status import write_project_statuses
from dev_orchestrator.core.websol_health import (
    DEFAULT_PROBE_FAILURE_RESET_SECONDS,
    WebSolAvailability,
    WebSolHealth,
    WebSolHealthStore,
    collect_websol_signals,
    evaluate_websol_availability,
)
from dev_orchestrator.core.websol_failover import FailoverEngine, WebSolFailoverStore
from dev_orchestrator.core.websol_probe import DEFAULT_PROBE_TIMEOUT_SECONDS, run_websol_probe
from dev_orchestrator.incidents import capture_incident
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.security import ControlSecurity
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.jobs.recovery import JobRecoveryCoordinator
from dev_orchestrator.monitor.project import run_monitor_once
from dev_orchestrator.storage.json_store import parse_utc, utc_now, utc_now_iso, write_json, write_text
from dev_orchestrator.web.server import make_server

logger = logging.getLogger(__name__)


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
    watchdog: WatchdogCoordinator | None = None,
    job_recovery: JobRecoveryCoordinator | None = None,
    supervisor: ActivationSupervisor | None = None,
    health_store: WebSolHealthStore | None = None,
    failover_engine: FailoverEngine | None = None, *, pid: int,
) -> dict[str, Any]:
    """Run one ordered control-plane tick and return the projected summary."""
    watchdog_error: Optional[str] = None
    supervisor_error: Optional[str] = None
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

    if health_store is None:
        health_store = WebSolHealthStore(runtime)

    # Synchronize and track browser_bridge bindings across ticks
    current_bindings: dict[str, str] = {}
    for item in browser_summary.get("projects") or []:
        if not isinstance(item, dict):
            continue
        p_id = str(item.get("project_id") or "")
        binding = item.get("conversation_binding")
        if isinstance(binding, dict) and binding.get("transport") == "browser_bridge":
            if binding.get("require_truthful_availability") is False or binding.get("require_availability") is False:
                continue
            adp = str(binding.get("adapter") or "chatgpt_web")
            bid = str(binding.get("binding_id") or "")
            if bid:
                current_bindings[p_id] = f"{adp}:{bid}"
    health_store.sync_bindings(current_bindings)

    # 1. Evaluate Web Sol health for active project bindings
    for item in browser_summary.get("projects") or []:
        if not isinstance(item, dict):
            continue
        p_id = str(item.get("project_id") or "")
        binding = item.get("conversation_binding")
        if not isinstance(binding, dict) or binding.get("transport") != "browser_bridge":
            continue
        if binding.get("require_truthful_availability") is False or binding.get("require_availability") is False:
            continue
        adp = str(binding.get("adapter") or "chatgpt_web")
        bid = str(binding.get("binding_id") or "")
        if not bid:
            continue
        try:
            conv_store = getattr(controls, "conversation_store", None) or ConversationControlStore(runtime)
            sec = getattr(controls, "control_security", None) or ControlSecurity(runtime)
            signals = collect_websol_signals(
                runtime,
                p_id,
                adp,
                bid,
                configured_binding=binding,
                bridge_store=bridge_store,
                conversation_store=conv_store,
                security=sec,
                health_store=health_store,
            )
            p_info = health_store.get_probe_info(p_id, adp, bid) or {}
            curr_gen = health_store.current_generation()
            probe_gen = int(p_info.get("generation", 0))
            probe_passed = bool(p_info.get("success") is True and probe_gen == curr_gen)
            avail, reason = evaluate_websol_availability(
                signals,
                probe_generation=probe_gen,
                current_probe_generation=curr_gen,
                probe_passed=probe_passed,
            )
            now_iso = utc_now_iso()
            health = WebSolHealth(
                project_id=p_id,
                adapter=adp,
                binding_id=bid,
                availability=avail.value,
                evaluated_at=now_iso,
                valid_until=(utc_now() + timedelta(seconds=60)).isoformat(),
                reason=reason,
                signals=signals,
                probe_generation=health_store.current_generation(),
            )
            health_store.put(health)

            if health.availability == WebSolAvailability.PAIRING_REQUIRED.value:
                capture_incident(
                    runtime_root=runtime,
                    project_id=p_id,
                    task_id=None,
                    classification="pairing_required",
                    semantic={"status": "pairing_required", "adapter": adp, "binding_id": bid},
                    evidence={"availability": health.availability, "signals": [asdict(s) if hasattr(s, "__dataclass_fields__") or hasattr(s, "__dict__") else (s if isinstance(s, dict) else str(s)) for s in health.signals.values()]},
                    occurrence_key=f"pairing_required:{p_id}:{adp}:{bid}",
                )
            elif health.availability == WebSolAvailability.PROBE_FAILED.value:
                capture_incident(
                    runtime_root=runtime,
                    project_id=p_id,
                    task_id=None,
                    classification="probe_failed",
                    semantic={"status": "probe_failed", "adapter": adp, "binding_id": bid},
                    evidence={"availability": health.availability, "signals": [asdict(s) if hasattr(s, "__dataclass_fields__") or hasattr(s, "__dict__") else (s if isinstance(s, dict) else str(s)) for s in health.signals.values()]},
                    occurrence_key=f"probe_failed:{p_id}:{adp}:{bid}",
                )

            probe_timeout = float(
                binding.get("probe_timeout_seconds")
                or binding.get("probe_timeout")
                or getattr(bridge_store, "max_claim_lifetime_seconds", None)
                or DEFAULT_PROBE_TIMEOUT_SECONDS
            )
            probe_reset_sec = float(
                binding.get("probe_reset_seconds")
                or binding.get("probe_failure_reset_seconds")
                or DEFAULT_PROBE_FAILURE_RESET_SECONDS
            )

            if health_store.should_probe(
                p_id, adp, bid,
                interval_seconds=300,
                reset_seconds=probe_reset_sec,
                bridge_store=bridge_store,
            ):
                health_store.mark_probe_in_flight(p_id, adp, bid)
                def _do_probe(proj: str, a: str, b: str, timeout: float) -> None:
                    try:
                        pres = run_websol_probe(
                            bridge_store, a, b,
                            generation=health_store.current_generation(),
                            timeout_seconds=timeout,
                        )
                        health_store.record_probe_result(
                            project_id=proj, adapter=a, binding_id=b,
                            ok=pres.success,
                            error=pres.reason if not pres.success else None,
                            duration_seconds=pres.duration_seconds,
                        )
                    except Exception as exc:
                        logger.exception("Web Sol background probe failed for project=%s adapter=%s binding=%s: %s", proj, a, b, exc)
                    finally:
                        health_store.clear_probe_in_flight(proj, a, b)
                threading.Thread(target=_do_probe, args=(p_id, adp, bid, probe_timeout), daemon=True).start()
        except Exception as exc:
            logger.exception("Web Sol health evaluation failed for project=%s adapter=%s binding=%s: %s", p_id, adp, bid, exc)

    # 2. Run failover reconciliation
    if failover_engine is not None:
        try:
            failover_engine.reconcile()
        except Exception as exc:
            logger.exception("Web Sol failover engine reconcile failed: %s", exc)

    # 3. Dispatch worker_done events with availability_provider
    def _daemon_availability_provider(p: str, a: str, b: str) -> Any:
        proj_item = None
        for itm in browser_summary.get("projects") or []:
            if isinstance(itm, dict) and itm.get("project_id") == p:
                proj_item = itm
                break
        cb = (proj_item.get("conversation_binding") or {}) if isinstance(proj_item, dict) else {}
        if cb.get("require_truthful_availability") is False or cb.get("require_availability") is False:
            return None
        is_browser_bridge = cb.get("transport") == "browser_bridge" or a == "chatgpt_web"
        if (is_browser_bridge or cb.get("require_truthful_availability") or cb.get("require_availability")) and health_store:
            return health_store.get(p, a, b)
        return None

    active_avail_provider = _daemon_availability_provider
    accounting = getattr(executor, "accounting", None)
    failure_memory = getattr(executor, "failure_memory", None)
    dispatch_worker_done_events(
        browser_summary,
        bridge_store,
        runtime,
        accounting,
        failure_memory,
        getattr(executor, "failure_memory_max_chars", 2000),
        active_avail_provider,
    )
    write_project_statuses(projected, runtime, phase="dispatch", daemon_state="running", pid=pid)
    if accounting is None:
        consume_websol_responses(browser_summary, bridge_store, runtime)
    else:
        consume_websol_responses(
            browser_summary, bridge_store, runtime, accounting=accounting
        )
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
            # The watchdog's READY_TO_RUN launch-gap detector must see the
            # monitor's current lifecycle truth.  ``projected`` is a UI and
            # dispatch view: it deliberately overlays the most recent
            # terminal managed execution, which can relabel a newly ready
            # task as WORKER_FAILED.  Active executions remain guarded by the
            # executor state supplied to WatchdogCoordinator.
            # Watchdog needs monitor truth plus current orchestration-role truth.
            # Feeding raw_summary alone hides an active Planner/Reviewer behind
            # a stale prior-task lifecycle (for example OWNER_GATE), making
            # ACTIVE_LIFECYCLE_STATES monitoring silently inapplicable.
            # Do not feed the managed-run projection here: a terminal run from
            # the previous task can still relabel a newly active task.
            planner_st = planner_state_fn() if callable(planner_state_fn) else None
            reviewer_st = reviewer_state_fn() if callable(reviewer_state_fn) else None
            if planner_st or reviewer_st:
                watchdog_summary = overlay_orchestration_lifecycle(
                    raw_summary,
                    planner_state=planner_st,
                    reviewer_state=reviewer_st,
                )
            else:
                watchdog_summary = raw_summary
            import inspect
            sig = inspect.signature(watchdog.advance)
            if "planner" in sig.parameters:
                watchdog.advance(config, watchdog_summary, executor=executor, planner=planner_obj, reviewer=reviewer)
            else:
                watchdog.advance(config, watchdog_summary, executor=executor)
        except Exception as _wd_exc:
            watchdog.record_tick_error(_wd_exc)
            watchdog_error = str(_wd_exc)
    if supervisor is not None:
        try:
            planner_st = planner_state_fn() if callable(planner_state_fn) else None
            reviewer_st = reviewer_state_fn() if callable(reviewer_state_fn) else None
            if planner_st or reviewer_st:
                supervisor_summary = overlay_orchestration_lifecycle(
                    raw_summary,
                    planner_state=planner_st,
                    reviewer_state=reviewer_st,
                )
            else:
                supervisor_summary = raw_summary
            supervisor.advance(config, supervisor_summary, executor=executor, watchdog=watchdog)
        except Exception as _sup_exc:
            supervisor_error = str(_sup_exc)
    try:
        from dev_orchestrator.incidents import harvest_tick
        harvest_tick(
            runtime_root=runtime,
            config=config if isinstance(config, dict) else None,
            summary=projected,
            executor=executor,
            watchdog=watchdog,
            planner=planner_obj,
            reviewer=reviewer,
        )
    except Exception as _harv_exc:
        projected = dict(projected) if isinstance(projected, dict) else {"projects": [], "summary": projected}
        projected["_harvesting_tick_error"] = str(_harv_exc)
    if job_recovery is not None:
        try:
            job_recovery.advance()
        except Exception:
            pass
    write_project_statuses(projected, runtime, phase="actuation", daemon_state="running", pid=pid)
    write_json(runtime / "summary.json", projected)
    if watchdog_error is not None:
        projected = dict(projected) if isinstance(projected, dict) else {"projects": [], "summary": projected}
        projected["_watchdog_tick_error"] = watchdog_error
    if supervisor_error is not None:
        projected = dict(projected) if isinstance(projected, dict) else {"projects": [], "summary": projected}
        projected["_supervisor_tick_error"] = supervisor_error
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
    owner_store = OwnerControlStore(runtime)
    conversation_store = ConversationControlStore(runtime)
    try:
        server = make_server(
            listen, port, runtime, web_root,
            enable_control=True, config_path=config,
        )
        # HTTP heartbeats and daemon-owned binding commands must serialize
        # through the same in-process store instance.
        server.conversation_store = conversation_store
        bridge_store = BrowserBridgeStore(runtime / "bridge", require_live_binding=True)
        server.bridge_store = bridge_store
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
    accounting_runtime = load_accounting_runtime(runtime, config)
    accounting = accounting_runtime.recorder if accounting_runtime is not None else None
    failure_memory = accounting_runtime.failure_memory if accounting_runtime is not None else None
    failure_memory_max_chars = (
        accounting_runtime.prompt_max_chars if accounting_runtime is not None else 2000
    )
    ai_execution_port = load_aibroker_execution_port(runtime, accounting=accounting)
    progress_channel = ProgressChannel(runtime, bridge_store=bridge_store)
    transition_executor = TransitionExecutor(
        runtime,
        ai_execution_port=ai_execution_port,
        progress_channel=progress_channel,
        accounting=accounting,
        failure_memory=failure_memory,
        failure_memory_max_chars=failure_memory_max_chars,
        owner_store=owner_store,
    )
    reviewer_coordinator = AIReviewerCoordinator(
        runtime,
        ai_execution_port,
        progress_channel=progress_channel,
        accounting=accounting,
        failure_memory=failure_memory,
        failure_memory_max_chars=failure_memory_max_chars,
    )
    planner_coordinator = AIPlannerCoordinator(
        runtime,
        ai_execution_port,
        progress_channel=progress_channel,
        accounting=accounting,
        failure_memory=failure_memory,
        failure_memory_max_chars=failure_memory_max_chars,
    )
    mobile_server = None
    mobile_thread: Optional[threading.Thread] = None
    mobile_authorizer = server.control_security

    control_coordinator = ControlCommandCoordinator(
        runtime, planner_coordinator, accounting=accounting, reviewer=reviewer_coordinator,
        owner_store=owner_store, conversation_store=conversation_store,
        bridge_store=bridge_store,
        mobile_device_authorizer=mobile_authorizer,
    )

    from dev_orchestrator.config import load_projects_config
    from dev_orchestrator.control.adapter import ControlAdapterClient
    from dev_orchestrator.mobile.gateway import make_mobile_gateway
    from dev_orchestrator.mobile.projection import MobileProjectionService

    cfg_dict = load_projects_config(config)
    mobile_cfg = cfg_dict.get("mobile_gateway") if isinstance(cfg_dict, dict) else None
    if isinstance(mobile_cfg, dict) and mobile_cfg.get("enabled"):
        m_listen = str(mobile_cfg.get("listen_address") or "")
        m_port = int(mobile_cfg.get("port") or 8780)
        try:
            m_proj = MobileProjectionService(
                runtime, config_provider=cfg_dict,
                mobile_device_authorizer=mobile_authorizer,
                conversation_store=conversation_store,
                bridge_store=bridge_store,
            )
            m_adapter = ControlAdapterClient(
                base_url=f"http://127.0.0.1:{port}",
                token=mobile_authorizer.token() if mobile_authorizer else None,
                runtime_root=runtime,
            )
            mobile_server = make_mobile_gateway(
                m_listen, m_port, runtime,
                authorizer=mobile_authorizer,
                projection_service=m_proj,
                control_client=m_adapter,
                config_path=config,
            )
            mobile_thread = threading.Thread(
                target=mobile_server.serve_forever,
                kwargs={"poll_interval": 0.5},
                name="devorchestrator-mobile",
                daemon=True,
            )
            mobile_thread.start()

            if progress_channel is not None:
                def _on_progress_mobile(notif: Any) -> None:
                    try:
                        from dataclasses import asdict
                        mobile_server.broadcast_event("progress", asdict(notif))
                        mobile_server.evaluate_and_broadcast_alerts()
                    except Exception:
                        pass
                progress_channel.add_listener(_on_progress_mobile)
        except Exception as exc:
            write_json(runtime / "mobile-gateway.json", {
                "schema_version": 1,
                "state": "unavailable",
                "pid": pid,
                "listen_address": m_listen,
                "port": m_port,
                "started_at": started_at,
                "last_tick_at": utc_now_iso(),
                "active_streams": 0,
                "error": str(exc),
            })
    else:
        write_json(runtime / "mobile-gateway.json", {
            "schema_version": 1,
            "state": "disabled",
            "pid": pid,
            "started_at": started_at,
            "last_tick_at": utc_now_iso(),
            "active_streams": 0,
        })
    watchdog_coordinator = WatchdogCoordinator(
        runtime, ai_execution_port=ai_execution_port, progress_channel=progress_channel,
        planner=planner_coordinator, reviewer=reviewer_coordinator,
    )
    activation_supervisor = ActivationSupervisor(
        runtime, controls=control_coordinator, executor=transition_executor,
        progress_channel=progress_channel, watchdog=watchdog_coordinator,
    )
    job_recovery_coordinator = JobRecoveryCoordinator(
        runtime, accounting=accounting
    )
    health_store = WebSolHealthStore(runtime)
    health_store.invalidate_generation(reason="daemon_startup")
    if server is not None:
        server.websol_health_store = health_store
    failover_store = WebSolFailoverStore(runtime)
    failover_engine = FailoverEngine(
        failover_store=failover_store,
        bridge_store=bridge_store,
        reviewer_coordinator=reviewer_coordinator,
        dispatcher_ledger_path=runtime / DISPATCHER_STATE_FILE,
        health_store=health_store,
    )
    try:
        job_recovery_coordinator.recover()
    except Exception:
        pass
    try:
        while True:
            last_error: Optional[str] = None
            try:
                tick_result = _run_orchestration_tick(
                    config, runtime, bridge_store, transition_executor, reviewer_coordinator,
                    control_coordinator, watchdog=watchdog_coordinator,
                    job_recovery=job_recovery_coordinator,
                    supervisor=activation_supervisor,
                    health_store=health_store,
                    failover_engine=failover_engine,
                    pid=pid
                )
                if isinstance(tick_result, dict):
                    if tick_result.get("_watchdog_tick_error"):
                        last_error = str(tick_result["_watchdog_tick_error"])
                    elif tick_result.get("_supervisor_tick_error"):
                        last_error = str(tick_result["_supervisor_tick_error"])
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
            if mobile_server is not None:
                try:
                    mobile_server.touch_health(state)
                    mobile_server.evaluate_and_broadcast_alerts()
                except Exception:
                    pass
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if mobile_server is not None:
            try:
                mobile_server.close_all_streams()
            except Exception:
                pass
            mobile_server.shutdown()
            mobile_server.server_close()
        for runner in (server, bridge_server):
            if runner is not None:
                runner.shutdown()
                runner.server_close()
        if web_thread is not None:
            web_thread.join(timeout=2)
        if bridge_thread is not None:
            bridge_thread.join(timeout=2)
        if mobile_thread is not None:
            mobile_thread.join(timeout=2)
        try:
            (runtime / "daemon.pid").unlink(missing_ok=True)
        except OSError:
            pass
    return 0
