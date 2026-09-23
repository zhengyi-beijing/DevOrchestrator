"""Per-tick self-recovery and activation supervisor for DevOrchestrator.

Runs once per daemon tick strictly after watchdog.advance.
Coordinates recovery of orphaned, un-registered, unstructured readiness,
and transient failure states under bounded recovery budgets.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.core import control_commands
from dev_orchestrator.control.surface import _active_execution, project_identity
from dev_orchestrator.core.activation import (
    load_activation_requests,
    reconcile_project_registration,
)
from dev_orchestrator.core.blockers import explain_block
from dev_orchestrator.core.execution_intent import (
    check_intent_budgets,
    compute_recovery_fingerprint,
    load_execution_intents,
    record_intent_action,
    terminate_intent,
)
from dev_orchestrator.core.readiness import migrate_legacy_readiness
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

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
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.controls = controls
        self.executor = executor
        self.progress_channel = progress_channel

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
        watchdog_file = self.runtime_root / "watchdog.json"
        watchdog_data = read_json(watchdog_file, None)
        watchdog_dirty = False

        for project_id, intent in list(active_intents.items()):
            # 1. Check if execution is currently active
            active_exec = _active_execution(self.runtime_root, project_id)
            if active_exec is not None and str(active_exec.get("state") or "") in {"launching", "running"}:
                # Worker is actively running; no remediation needed this tick
                continue

            snapshot = snapshots_by_id.get(project_id)
            if snapshot:
                worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
                if worker.get("kind") == "task" and worker.get("state") in {"starting", "running"}:
                    continue

            # 2. Check and consume watchdog handoff
            epoch_id = intent.get("recovery_epoch_id")
            if isinstance(watchdog_data, dict):
                prow = watchdog_data.get("projects", {}).get(project_id)
                if isinstance(prow, dict) and prow.get("recovery_handoff"):
                    handoff = prow["recovery_handoff"]
                    handoff_epoch = handoff.get("recovery_epoch_id")
                    if not epoch_id or not handoff_epoch or epoch_id == handoff_epoch:
                        record_intent_action(self.runtime_root, project_id)
                        prow["recovery_handoff"] = None
                        watchdog_dirty = True

            # 3. Find project configuration if registered
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

            # 4. Derive structured blockers
            blockers = explain_block(
                project_config=project_config,
                snapshot=snapshot,
                repo_path=repo_path,
                runtime_root=self.runtime_root,
                config_path=cfg_path,
                action=intent.get("requested_action") or "continue",
            )

            # If no blockers, drive the forward transition!
            if not blockers:
                cmd_id = f"cmd-rec-{project_id}-{intent.get('task_id') or 'notask'}-{epoch_id or 'noepoch'}-{intent.get('actions_used', 0)}"
                try:
                    exp_identity = project_identity(snapshot, self.runtime_root) if snapshot else None
                    if exp_identity and epoch_id:
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
                    outcomes.append({
                        "project_id": project_id,
                        "status": "transition_submitted",
                        "command_id": cmd_id,
                    })
                except Exception as exc:
                    outcomes.append({
                        "project_id": project_id,
                        "status": "transition_failed",
                        "error": str(exc),
                    })
                continue

            top_blocker = blockers[0]

            # 5. Check budget / livelock exhaustion
            fp = compute_recovery_fingerprint(
                project_id=project_id,
                task_id=intent.get("task_id") or (snapshot.get("telemetry", {}).get("task_id") if snapshot else None),
                lifecycle_state=str(snapshot.get("lifecycle_state") or snapshot.get("state") if snapshot else "UNREGISTERED"),
                blocker_code=top_blocker.code,
                git_anchor=snapshot.get("git", {}).get("head") if snapshot else None,
                worker_state=snapshot.get("worker", {}).get("state") if snapshot else None,
            )

            self_healing_cfg = project_config.get("self_healing") if project_config else None
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
                outcomes.append({
                    "project_id": project_id,
                    "status": "exhausted",
                    "blocker_code": blocker_code,
                    "reason": exhaust_reason,
                })
                continue

            # 6. Genuine Owner Gate preservation
            if top_blocker.owner_gate_required or top_blocker.failure_class == "owner_gate":
                terminate_intent(
                    self.runtime_root,
                    project_id,
                    "owner_gate",
                    failure_class="owner_gate",
                    blocker_code=top_blocker.code,
                    reason="genuine owner gate active",
                )
                outcomes.append({
                    "project_id": project_id,
                    "status": "owner_gate_preserved",
                    "blocker_code": top_blocker.code,
                })
                continue

            # 7. Terminal failure handling
            if top_blocker.failure_class == "terminal":
                terminate_intent(
                    self.runtime_root,
                    project_id,
                    "exhausted",
                    failure_class="terminal",
                    blocker_code=top_blocker.code,
                    reason=f"terminal blocker: {top_blocker.code}",
                )
                outcomes.append({
                    "project_id": project_id,
                    "status": "terminal_blocker",
                    "blocker_code": top_blocker.code,
                })
                continue

            # 8. Bounded Remediation for recoverable conditions
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
                cmd_id = f"cmd-rec-{project_id}-{intent.get('task_id') or 'notask'}-{epoch_id or 'noepoch'}-{updated_intent.get('actions_used', 1)}"
                try:
                    exp_identity = project_identity(snapshot, self.runtime_root) if snapshot else None
                    if exp_identity and epoch_id:
                        exp_identity["recovery_epoch_id"] = epoch_id
                    submit_control_command(
                        self.runtime_root,
                        project_id=project_id,
                        action=intent.get("requested_action") or "continue",
                        command_id=cmd_id,
                        expected=exp_identity,
                        source="activation_supervisor",
                    )
                    if self.controls is not None and cfg_path:
                        self.controls.advance(cfg_path, summary, exec_inst)
                except Exception as exc:
                    logger.debug("forward transition submission: %s", exc)

        if watchdog_dirty and isinstance(watchdog_data, dict):
            try:
                write_json(watchdog_file, watchdog_data, indent=2)
            except Exception:
                pass

        return outcomes
