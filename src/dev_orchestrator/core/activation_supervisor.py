"""Per-tick self-recovery and activation supervisor for DevOrchestrator.

Runs once per daemon tick strictly after watchdog.advance.
Coordinates recovery of orphaned, un-registered, unstructured readiness,
and transient failure states under bounded recovery budgets.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.core import control_commands
from dev_orchestrator.control.surface import _active_execution, project_identity
from dev_orchestrator.core.activation import (
    load_activation_requests,
    reconcile_project_registration,
)
from dev_orchestrator.core.blockers import explain_block
from dev_orchestrator.core.execution_context import (
    classify_continuation,
    context_is_stale,
    get_context,
    load_execution_contexts,
    set_next_action,
    update_context,
)
from dev_orchestrator.core.execution_intent import (
    check_intent_budgets,
    clear_intent_backoff,
    compute_recovery_fingerprint,
    load_execution_intents,
    record_intent_action,
    set_intent_backoff,
    terminate_intent,
)
from dev_orchestrator.core.readiness import migrate_legacy_readiness
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.task_status import parse_task_status
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

logger = logging.getLogger(__name__)


class ActivationSupervisor:
    """Daemon-authoritative project activation and self-recovery supervisor."""

    def __init__(
        self,
        runtime_root: Path | str,
        *,
        controls: Any = None,
        executor: Any = None,
        progress_channel: Any = None,
        watchdog: Any = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.controls = controls
        self.executor = executor
        self.progress_channel = progress_channel
        self.watchdog = watchdog

    def _emit_milestone(
        self,
        project_id: str,
        milestone: str,
        task_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        if self.progress_channel is not None and hasattr(self.progress_channel, "emit"):
            try:
                self.progress_channel.emit(
                    project_id,
                    milestone,
                    task_id=task_id,
                    details=details,
                )
            except Exception:
                pass

    def advance(
        self,
        config: Path | str | dict[str, Any],
        summary: dict[str, Any],
        *,
        executor: Any = None,
        now: Optional[datetime] = None,
        watchdog: Any = None,
    ) -> list[dict[str, Any]]:
        """Advance the activation supervisor for one daemon tick."""
        exec_inst = executor or self.executor
        tick_now = now or datetime.now(timezone.utc)
        cfg_path: Optional[Path] = None
        raw_config: dict[str, Any] = {}

        if isinstance(config, (str, Path)):
            cfg_path = Path(config)
            if cfg_path.is_file():
                try:
                    import json
                    raw_config = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
                except Exception:
                    raw_config = {}
        elif isinstance(config, dict):
            raw_config = config
            cfg_path = self.runtime_root.parent / "config" / "projects.json"

        snapshots_by_id: dict[str, dict[str, Any]] = {}
        if isinstance(summary, dict) and isinstance(summary.get("projects"), list):
            for s in summary["projects"]:
                if isinstance(s, dict):
                    pid = str(s.get("project_id") or s.get("id") or "").strip()
                    if pid:
                        snapshots_by_id[pid] = s

        intents_data = load_execution_intents(self.runtime_root)
        active_intents = {
            pid: intent
            for pid, intent in intents_data.get("intents", {}).items()
            if isinstance(intent, dict) and intent.get("state") in {"pending", "active"}
        }

        if not active_intents:
            return []

        outcomes: list[dict[str, Any]] = []

        # Consume watchdog handoff records if present
        watchdog_inst = watchdog or self.watchdog
        watchdog_file = self.runtime_root / "watchdog.json"
        watchdog_data = read_json(watchdog_file, None)
        watchdog_dirty = False

        for project_id, intent in list(active_intents.items()):
            # 1. Check if execution is currently active
            active_exec = _active_execution(self.runtime_root, project_id)
            snapshot = snapshots_by_id.get(project_id)
            worker = snapshot.get("worker") if (snapshot and isinstance(snapshot.get("worker"), dict)) else {}
            is_worker_active = (
                (active_exec is not None and str(active_exec.get("state") or "") in {"launching", "running"})
                or (worker.get("kind") == "task" and worker.get("state") in {"starting", "running"})
            )
            if is_worker_active:
                terminate_intent(
                    self.runtime_root,
                    project_id,
                    "satisfied",
                    reason="target execution launched",
                )
                outcomes.append({
                    "project_id": project_id,
                    "status": "intent_satisfied",
                    "reason": "target execution launched",
                })
                continue

            # 2. Find project configuration if registered
            project_config = None
            if isinstance(raw_config.get("projects"), list):
                for p in raw_config["projects"]:
                    if isinstance(p, dict) and (p.get("project_id") == project_id or p.get("id") == project_id):
                        project_config = p
                        break

            repo_path = None
            if project_config:
                repo_path = project_config.get("repo_path") or project_config.get("root")
            if not repo_path:
                repo_path = intent.get("repo_path")

            self_healing_cfg = project_config.get("self_healing") if project_config else None
            if isinstance(self_healing_cfg, dict) and not self_healing_cfg.get("enabled", True):
                terminate_intent(
                    self.runtime_root,
                    project_id,
                    "stopped",
                    failure_class="lifecycle",
                    reason="self-healing disabled by project configuration",
                )
                outcomes.append({
                    "project_id": project_id,
                    "status": "self_healing_disabled",
                })
                continue

            # 3. Check and consume watchdog handoff
            epoch_id = intent.get("recovery_epoch_id")
            if watchdog_inst is not None and hasattr(watchdog_inst, "consume_recovery_handoff"):
                handoff = watchdog_inst.consume_recovery_handoff(project_id, epoch_id=epoch_id)
                if handoff:
                    record_intent_action(self.runtime_root, project_id)
            elif isinstance(watchdog_data, dict):
                prow = watchdog_data.get("projects", {}).get(project_id)
                if isinstance(prow, dict) and prow.get("recovery_handoff"):
                    handoff = prow["recovery_handoff"]
                    handoff_epoch = handoff.get("recovery_epoch_id")
                    if not epoch_id or not handoff_epoch or epoch_id == handoff_epoch:
                        record_intent_action(self.runtime_root, project_id)
                        prow.pop("recovery_handoff", None)
                        watchdog_dirty = True

            # 4. Derive structured blockers
            blockers = explain_block(
                project_config=project_config,
                snapshot=snapshot,
                repo_path=repo_path,
                runtime_root=self.runtime_root,
                config_path=cfg_path,
                action=intent.get("requested_action") or "continue",
                project_id=project_id,
            )

            # Execution context continuity and git-anchor revalidation
            context = get_context(self.runtime_root, project_id)
            git_info = snapshot.get("git") if isinstance(snapshot, dict) else {}
            curr_head = git_info.get("head") if isinstance(git_info, dict) else None
            telemetry = snapshot.get("telemetry") if isinstance(snapshot, dict) else {}
            curr_task_id = telemetry.get("task_id") if isinstance(telemetry, dict) else None
            self_healing_cfg = project_config.get("self_healing") if project_config else None

            if context and context_is_stale(context, curr_head, curr_task_id):
                self._emit_milestone(
                    project_id,
                    "EXECUTION_CONTEXT_STALE",
                    task_id=curr_task_id or context.get("task_id"),
                    details={
                        "old_anchor": context.get("git_anchor"),
                        "current_head": curr_head,
                        "old_task_id": context.get("task_id"),
                        "current_task_id": curr_task_id,
                    },
                )
                truth = read_repository_truth(repo_path or "") if repo_path else None
                if truth and truth.valid and not truth.dirty:
                    parsed_st = parse_task_status(snapshot.get("next_status") if isinstance(snapshot, dict) else None)
                    next_act = "plan" if parsed_st.is_pending_design() else "execute"
                    context = update_context(
                        self.runtime_root,
                        project_id,
                        git_anchor=curr_head,
                        task_id=curr_task_id,
                        next_action=next_act,
                        disposition="advance",
                        idle_ticks=0,
                    )

            # 5. Classify continuation
            disposition, next_action, disp_reason = classify_continuation(
                snapshot, intent, blockers, context, project_config=project_config
            )

            # Genuine Owner Gate preservation
            if disposition == "owner_gate":
                owner_gate_blocker = next(
                    (b for b in blockers if b.code in {"OWNER_GATE_PRESENT", "OWNER_PAUSED"} or b.failure_class == "owner_gate"),
                    None,
                )
                b_code = owner_gate_blocker.code if owner_gate_blocker else "OWNER_GATE"
                terminate_intent(
                    self.runtime_root,
                    project_id,
                    "owner_gate",
                    failure_class="owner_gate",
                    blocker_code=b_code,
                    reason=disp_reason,
                )
                update_context(self.runtime_root, project_id, disposition="owner_gate")
                outcomes.append({
                    "project_id": project_id,
                    "status": "owner_gate_preserved",
                    "blocker_code": b_code,
                })
                continue

            if disposition == "owner_stop":
                terminate_intent(
                    self.runtime_root,
                    project_id,
                    "stopped",
                    failure_class="owner_stop",
                    blocker_code="OWNER_PAUSED",
                    reason=disp_reason,
                )
                update_context(self.runtime_root, project_id, disposition="owner_stop")
                outcomes.append({
                    "project_id": project_id,
                    "status": "owner_stop",
                    "reason": disp_reason,
                })
                continue

            if disposition == "terminal_success":
                terminate_intent(
                    self.runtime_root,
                    project_id,
                    "satisfied",
                    reason="current task is terminal",
                )
                update_context(self.runtime_root, project_id, disposition="terminal_success", next_action="none")
                outcomes.append({
                    "project_id": project_id,
                    "status": "terminal_success",
                    "reason": "current task is terminal",
                })
                continue

            if disposition == "lifecycle_hold":
                top_blocker = blockers[0]
                terminate_intent(
                    self.runtime_root,
                    project_id,
                    "stopped",
                    failure_class="lifecycle",
                    blocker_code=top_blocker.code,
                    reason=f"task lifecycle state not ready to run: {getattr(top_blocker, 'observed', top_blocker.code)}",
                )
                update_context(self.runtime_root, project_id, disposition="hold")
                outcomes.append({
                    "project_id": project_id,
                    "status": "lifecycle_hold",
                    "blocker_code": top_blocker.code,
                })
                continue

            if disposition == "terminal_failure":
                code = blockers[0].code if blockers else "TERMINAL_BLOCKER"
                terminate_intent(
                    self.runtime_root,
                    project_id,
                    "exhausted",
                    failure_class="terminal",
                    blocker_code=code,
                    reason=disp_reason,
                )
                update_context(self.runtime_root, project_id, disposition="terminal_failure")
                outcomes.append({
                    "project_id": project_id,
                    "status": "terminal_blocker",
                    "blocker_code": code,
                    "reason": disp_reason,
                })
                continue

            if disposition == "transient_infrastructure":
                top_b = blockers[0]
                backoff_secs = int(self_healing_cfg.get("backoff_seconds") or 5) if self_healing_cfg else 5
                backoff_until_str = intent.get("backoff_until")
                if backoff_until_str:
                    try:
                        backoff_until_dt = parse_utc(backoff_until_str)
                    except Exception:
                        backoff_until_dt = None
                    if backoff_until_dt and tick_now < backoff_until_dt:
                        outcomes.append({
                            "project_id": project_id,
                            "status": "transient_backoff_waiting",
                            "backoff_until": backoff_until_str,
                            "blocker_code": top_b.code,
                        })
                        continue
                    else:
                        clear_intent_backoff(self.runtime_root, project_id)
                        disposition = "remediate"
                else:
                    backoff_until_dt = tick_now + timedelta(seconds=backoff_secs)
                    set_intent_backoff(self.runtime_root, project_id, backoff_until_dt.isoformat())
                    outcomes.append({
                        "project_id": project_id,
                        "status": "transient_backoff_scheduled",
                        "backoff_seconds": backoff_secs,
                        "backoff_until": backoff_until_dt.isoformat(),
                        "blocker_code": top_b.code,
                    })
                    continue

            if disposition == "hold":
                # Active role execution: hold without burning recovery budget
                worker = snapshot.get("worker") if isinstance(snapshot, dict) and isinstance(snapshot.get("worker"), dict) else {}
                w_active = worker.get("kind") == "task" and worker.get("state") in {"starting", "running"}
                planner = snapshot.get("planner") if isinstance(snapshot, dict) and isinstance(snapshot.get("planner"), dict) else {}
                p_active = planner.get("state") in {"planning", "reviewing", "applying", "remediating"}
                reviewer = snapshot.get("reviewer") if isinstance(snapshot, dict) and isinstance(snapshot.get("reviewer"), dict) else {}
                r_active = reviewer.get("state") in {"launching", "running"}

                if w_active or p_active or r_active:
                    role_name = "worker" if w_active else ("planner" if p_active else "reviewer")
                    update_context(self.runtime_root, project_id, disposition="hold", active_role=role_name, idle_ticks=0)
                    self._emit_milestone(
                        project_id,
                        "CONTINUATION_HOLD",
                        task_id=intent.get("task_id"),
                        details={"role": role_name, "reason": disp_reason},
                    )
                    outcomes.append({
                        "project_id": project_id,
                        "status": "continuation_hold",
                        "role": role_name,
                        "reason": disp_reason,
                    })
                    continue

                # Idle tick detection: at most one idle tick before emitting CONTINUATION_FAULT
                curr_idle = int(context.get("idle_ticks", 0)) if isinstance(context, dict) else 0
                if curr_idle >= 1:
                    self._emit_milestone(
                        project_id,
                        "CONTINUATION_FAULT",
                        task_id=intent.get("task_id"),
                        details={"idle_ticks": curr_idle, "reason": "continuation idle fault"},
                    )
                    update_context(self.runtime_root, project_id, idle_ticks=0, disposition="advance")
                    disposition = "advance"
                    next_action = "continue"
                else:
                    update_context(self.runtime_root, project_id, idle_ticks=curr_idle + 1, disposition="hold")
                    self._emit_milestone(
                        project_id,
                        "CONTINUATION_HOLD",
                        task_id=intent.get("task_id"),
                        details={"idle_ticks": curr_idle + 1, "reason": disp_reason},
                    )
                    outcomes.append({
                        "project_id": project_id,
                        "status": "continuation_hold",
                        "idle_ticks": curr_idle + 1,
                        "reason": disp_reason,
                    })
                    continue

            # 6. Check budget / livelock exhaustion
            is_exhausted, exhaust_reason, blocker_code = check_intent_budgets(intent, self_healing_cfg, now=tick_now)
            if is_exhausted:
                terminate_intent(
                    self.runtime_root,
                    project_id,
                    "exhausted",
                    failure_class="recovery_exhausted",
                    blocker_code=blocker_code,
                    reason=exhaust_reason,
                )
                self._emit_milestone(
                    project_id,
                    "RECOVERY_EXHAUSTED",
                    task_id=intent.get("task_id"),
                    details={
                        "source": "activation_supervisor",
                        "blocker_code": blocker_code,
                        "reason": exhaust_reason,
                    },
                )
                update_context(self.runtime_root, project_id, disposition="exhausted")
                outcomes.append({
                    "project_id": project_id,
                    "status": "exhausted",
                    "blocker_code": blocker_code,
                    "reason": exhaust_reason,
                })
                continue

            # 7. Advance forward transition under budget
            if disposition == "advance":
                eff_task_id = intent.get("task_id") or (snapshot.get("telemetry", {}).get("task_id") if isinstance(snapshot, dict) else None) or "notask"
                fp = compute_recovery_fingerprint(
                    project_id=project_id,
                    task_id=eff_task_id,
                    lifecycle_state=str(snapshot.get("lifecycle_state") or snapshot.get("state") if snapshot else "IDLE"),
                    blocker_code="NO_BLOCKER",
                    git_anchor=curr_head,
                    worker_state=snapshot.get("worker", {}).get("state") if snapshot else None,
                )
                updated_intent, _, _, _ = record_intent_action(self.runtime_root, project_id, fingerprint=fp)
                cmd_id = f"cmd-rec-{project_id}-{eff_task_id}-{epoch_id or 'noepoch'}-fwd-{updated_intent.get('actions_used', 1)}"
                try:
                    eff_snapshot = snapshot or {"project_id": project_id, "repo_path": str(repo_path) if repo_path else None, "state": "IDLE"}
                    exp_identity = project_identity(eff_snapshot, self.runtime_root)
                    if epoch_id:
                        exp_identity["recovery_epoch_id"] = epoch_id
                    control_commands.submit_control_command(
                        self.runtime_root,
                        project_id=project_id,
                        action=intent.get("requested_action") or "continue",
                        command_id=cmd_id,
                        expected=exp_identity,
                        source="activation_supervisor",
                    )
                    if self.controls is not None and cfg_path:
                        self.controls.advance(cfg_path, summary, exec_inst)
                    self._emit_milestone(
                        project_id,
                        "CONTINUATION_DISPATCHED",
                        task_id=eff_task_id,
                        details={"command_id": cmd_id, "action": next_action},
                    )
                    update_context(
                        self.runtime_root,
                        project_id,
                        next_action=next_action,
                        disposition="advance",
                        git_anchor=curr_head,
                        idle_ticks=0,
                    )
                    post_active = _active_execution(self.runtime_root, project_id)
                    if post_active is not None and str(post_active.get("state") or "") in {"launching", "running"}:
                        terminate_intent(
                            self.runtime_root,
                            project_id,
                            "satisfied",
                            reason="target execution launched",
                        )
                    outcomes.append({
                        "project_id": project_id,
                        "status": "transition_submitted",
                        "continuation": "continuation_dispatched",
                        "command_id": cmd_id,
                        "action": next_action,
                    })
                except Exception as exc:
                    outcomes.append({
                        "project_id": project_id,
                        "status": "transition_failed",
                        "error": str(exc),
                    })
                continue

            top_blocker = blockers[0]
            fp = compute_recovery_fingerprint(
                project_id=project_id,
                task_id=intent.get("task_id") or (snapshot.get("telemetry", {}).get("task_id") if snapshot else None),
                lifecycle_state=str(snapshot.get("lifecycle_state") or snapshot.get("state") if snapshot else "UNREGISTERED"),
                blocker_code=top_blocker.code,
                git_anchor=curr_head,
                worker_state=snapshot.get("worker", {}).get("state") if snapshot else None,
            )
            updated_intent, _, _, _ = record_intent_action(self.runtime_root, project_id, fingerprint=fp)
            remediated = False
            remediation_name = ""

            # Case A: Unregistered / Orphan project reconciliation
            if top_blocker.code in {"PROJECT_NOT_REGISTERED", "ORPHANED_PROJECT_STATE"}:
                act_data = load_activation_requests(self.runtime_root)
                candidate_request = None
                for req in act_data.get("requests", {}).values():
                    if isinstance(req, dict) and req.get("project_id") == project_id and req.get("state") == "pending":
                        candidate_request = req
                        break
                if candidate_request and cfg_path:
                    norm_project, err = reconcile_project_registration(
                        candidate_request, cfg_path, self.runtime_root
                    )
                    if norm_project is not None:
                        remediated = True
                        remediation_name = "reconcile_registration"
                        project_config = norm_project
                        # Reload config
                        try:
                            import json
                            raw_config = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
                        except Exception:
                            pass
                    else:
                        if "REGISTRATION_TEMPLATE_MISSING" in err:
                            terminate_intent(
                                self.runtime_root,
                                project_id,
                                "exhausted",
                                failure_class="terminal",
                                blocker_code="REGISTRATION_TEMPLATE_MISSING",
                                reason=err,
                            )
                            outcomes.append({
                                "project_id": project_id,
                                "status": "registration_failed",
                                "reason": err,
                            })
                            continue

            # Case B: Unstructured legacy readiness migration
            elif top_blocker.code in {"READINESS_TOKEN_UNSTRUCTURED", "READINESS_TASK_ID_MISMATCH"}:
                p_entry = project_config or {"project_id": project_id, "repo_path": repo_path}
                snap_entry = snapshot or {"repo_path": repo_path}
                ok, msg, record = migrate_legacy_readiness(
                    p_entry, snap_entry, self.runtime_root, recovery_epoch_id=epoch_id
                )
                if ok:
                    remediated = True
                    remediation_name = "migrate_readiness"
                else:
                    logger.info("readiness migration skipped or failed for %s: %s", project_id, msg)

            # Case C: Transient infrastructure
            elif top_blocker.failure_class == "transient_infrastructure":
                remediated = True
                remediation_name = "transient_backoff"

            # If remediated, drive the original forward transition automatically!
            if remediated:
                outcomes.append({
                    "project_id": project_id,
                    "status": "remediated",
                    "action": remediation_name,
                    "remediation": remediation_name,
                })
                # Re-submit the forward transition
                eff_task_id = intent.get("task_id") or (snapshot.get("telemetry", {}).get("task_id") if isinstance(snapshot, dict) else None) or "notask"
                cmd_id = f"cmd-rec-{project_id}-{eff_task_id}-{epoch_id or 'noepoch'}-rem-{updated_intent.get('actions_used', 1)}"
                try:
                    eff_snapshot = snapshot or {"project_id": project_id, "repo_path": str(repo_path) if repo_path else None, "state": "IDLE"}
                    exp_identity = project_identity(eff_snapshot, self.runtime_root)
                    if epoch_id:
                        exp_identity["recovery_epoch_id"] = epoch_id
                    control_commands.submit_control_command(
                        self.runtime_root,
                        project_id=project_id,
                        action=intent.get("requested_action") or "continue",
                        command_id=cmd_id,
                        expected=exp_identity,
                        source="activation_supervisor",
                    )
                    if self.controls is not None and cfg_path:
                        self.controls.advance(cfg_path, summary, exec_inst)
                    post_active = _active_execution(self.runtime_root, project_id)
                    if post_active is not None and str(post_active.get("state") or "") in {"launching", "running"}:
                        terminate_intent(
                            self.runtime_root,
                            project_id,
                            "satisfied",
                            reason="target execution launched",
                        )
                except Exception as exc:
                    logger.warning("forward transition submission failed for %s: %s", project_id, exc)

        if watchdog_inst is None and watchdog_dirty and isinstance(watchdog_data, dict):
            try:
                write_json(watchdog_file, watchdog_data, indent=2)
            except Exception:
                pass

        return outcomes
