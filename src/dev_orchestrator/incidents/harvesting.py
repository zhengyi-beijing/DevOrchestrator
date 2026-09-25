"""Automated failure harvesting with named detector registry and durable anomaly derivation."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

from dev_orchestrator.control.command_store import ControlCommandStore
from dev_orchestrator.incidents.attribution import classify_owner_action
from dev_orchestrator.incidents.capture import capture_incident
from dev_orchestrator.incidents.liveness import resolve_role_liveness
from dev_orchestrator.incidents.obligations import resolve_progress_obligation
from dev_orchestrator.incidents.owner_gate import resolve_owner_gate_authority
from dev_orchestrator.incidents.policy import resolve_incident_policy
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso


# Detector functions return list of tuples: (project_id, task_id, classification, semantic, evidence, occurrence_key)

def _detect_watchdog_recovery(
    runtime_root: Path,
    config: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    executor: Any,
    watchdog: Any,
    planner: Any,
    reviewer: Any,
    now: Any,
) -> list[tuple[str, str | None, str, dict[str, Any], dict[str, Any], str]]:
    anomalies = []
    wd_file = runtime_root / "watchdog.json"
    if not wd_file.is_file():
        return anomalies
    data = read_json(wd_file, {})
    projects = data.get("projects") if isinstance(data, dict) else {}
    if not isinstance(projects, dict):
        return anomalies

    for pid, prow in projects.items():
        if not isinstance(prow, dict):
            continue
        history = prow.get("history") or []
        for att in history:
            if not isinstance(att, dict):
                continue
            diag = att.get("diagnosis")
            rec = att.get("recovery")
            att_key = att.get("attempt_key")
            if diag and rec and att_key:
                semantic = {
                    "failure_class": "watchdog_stall",
                    "diagnosis_code": str(diag),
                    "lifecycle_class": str(att.get("lifecycle_state") or "executing").lower(),
                    "contract_class": "liveness",
                }
                anomalies.append((
                    pid,
                    att.get("task_id"),
                    "WATCHDOG_RECOVERY",
                    semantic,
                    att,
                    f"wd_rec:{pid}:{att_key}",
                ))
    return anomalies


def _detect_execution_loss(
    runtime_root: Path,
    config: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    executor: Any,
    watchdog: Any,
    planner: Any,
    reviewer: Any,
    now: Any,
) -> list[tuple[str, str | None, str, dict[str, Any], dict[str, Any], str]]:
    anomalies = []
    lineage_file = runtime_root / "execution-lineage.json"
    if not lineage_file.is_file():
        return anomalies
    data = read_json(lineage_file, {})
    records = data.get("records") if isinstance(data, dict) else {}
    if not isinstance(records, dict):
        return anomalies

    for lkey, rec in records.items():
        if not isinstance(rec, dict):
            continue
        pid = str(rec.get("project_id") or "")
        inv_key = rec.get("invariant_key")
        term_outcome = rec.get("terminal_outcome")
        if term_outcome == "explicitly_reconciled" or rec.get("loss_detected"):
            semantic = {
                "failure_class": "execution_loss",
                "diagnosis_code": "worker_vanished_without_terminal_state",
                "lifecycle_class": "executing",
                "contract_class": "durability",
                "invariant_identifier": str(inv_key or "execution_terminal_invariant"),
            }
            anomalies.append((
                pid,
                rec.get("task_id"),
                "ACTIONABLE_EXECUTION_LOSS",
                semantic,
                rec,
                f"ex_loss:{pid}:{lkey}",
            ))
    return anomalies


def _detect_unconsumed_plan_or_review(
    runtime_root: Path,
    config: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    executor: Any,
    watchdog: Any,
    planner: Any,
    reviewer: Any,
    now: Any,
) -> list[tuple[str, str | None, str, dict[str, Any], dict[str, Any], str]]:
    anomalies = []
    # Check ai-reviewer for failed or blocked reviews
    rev_file = runtime_root / "ai-reviewer.json"
    if rev_file.is_file():
        data = read_json(rev_file, {})
        reviews = data.get("reviews") if isinstance(data, dict) else {}
        if isinstance(reviews, dict):
            for rid, rdata in reviews.items():
                if isinstance(rdata, dict) and rdata.get("state") == "failed":
                    pid = str(rdata.get("project_id") or "")
                    semantic = {
                        "failure_class": "reviewer_failure",
                        "diagnosis_code": "reviewer_failed",
                        "lifecycle_class": "reviewing",
                        "contract_class": "technical_review",
                    }
                    anomalies.append((
                        pid,
                        rdata.get("task_id"),
                        "UNCONSUMED_REVIEWER_FAILURE",
                        semantic,
                        rdata,
                        f"rev_fail:{pid}:{rid}",
                    ))
    return anomalies


def _detect_launch_gap(
    runtime_root: Path,
    config: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    executor: Any,
    watchdog: Any,
    planner: Any,
    reviewer: Any,
    now: Any,
) -> list[tuple[str, str | None, str, dict[str, Any], dict[str, Any], str]]:
    anomalies = []
    if not isinstance(summary, dict):
        return anomalies
    projects = summary.get("projects") or []
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        pid = str(proj.get("project_id") or proj.get("id") or "")
        state = str(proj.get("lifecycle_state") or proj.get("state") or "").upper()
        worker = proj.get("worker") if isinstance(proj.get("worker"), dict) else {}
        if state == "READY_TO_RUN" and not worker.get("process_alive") and not proj.get("active_execution"):
            wd = proj.get("watchdog") if isinstance(proj.get("watchdog"), dict) else {}
            if wd.get("diagnosis") == "ready_to_run_unlaunched":
                semantic = {
                    "failure_class": "launch_gap",
                    "diagnosis_code": "ready_to_run_unlaunched",
                    "lifecycle_class": "ready_to_run",
                    "contract_class": "launch_obligation",
                }
                anomalies.append((
                    pid,
                    proj.get("task_id"),
                    "LAUNCH_GAP",
                    semantic,
                    proj,
                    f"launch_gap:{pid}:{proj.get('task_id')}",
                ))
    return anomalies


def _detect_restart_reconcile_outcome_change(
    runtime_root: Path,
    config: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    executor: Any,
    watchdog: Any,
    planner: Any,
    reviewer: Any,
    now: Any,
) -> list[tuple[str, str | None, str, dict[str, Any], dict[str, Any], str]]:
    # Scans command store history for commands settled as blocked after restart
    anomalies = []
    cmd_store = ControlCommandStore(runtime_root)
    for cmd in cmd_store.recent(20):
        if cmd.get("state") == "blocked" and "reconcil" in str(cmd.get("action") or "").lower():
            cid = str(cmd.get("command_id") or "")
            pid = str(cmd.get("project_id") or "")
            semantic = {
                "failure_class": "reconciliation_outcome_change",
                "diagnosis_code": "state_desync",
                "lifecycle_class": "reconciliation",
                "contract_class": "lifecycle",
            }
            anomalies.append((
                pid,
                cmd.get("task_id"),
                "RECONCILE_OUTCOME_CHANGE",
                semantic,
                cmd,
                f"reconcile_chg:{cid}",
            ))
    return anomalies


def _detect_recovery_exhaustion_or_livelock(
    runtime_root: Path,
    config: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    executor: Any,
    watchdog: Any,
    planner: Any,
    reviewer: Any,
    now: Any,
) -> list[tuple[str, str | None, str, dict[str, Any], dict[str, Any], str]]:
    anomalies = []
    intent_file = runtime_root / "execution-intent.json"
    if not intent_file.is_file():
        return anomalies
    data = read_json(intent_file, {})
    intents = data.get("intents") if isinstance(data, dict) else {}
    if not isinstance(intents, dict):
        return anomalies

    for iid, idata in intents.items():
        if not isinstance(idata, dict):
            continue
        pid = str(idata.get("project_id") or "")
        state = idata.get("state")
        reason = str(idata.get("reason") or "")
        if state in {"exhausted", "livelock"} or "exhausted" in reason or "livelock" in reason:
            diag_code = "recovery_livelock_detected" if "livelock" in reason or state == "livelock" else "recovery_budget_exhausted"
            semantic = {
                "failure_class": "recovery_exhaustion",
                "diagnosis_code": diag_code,
                "lifecycle_class": "remediation",
                "contract_class": "recovery_budget",
            }
            anomalies.append((
                pid,
                idata.get("task_id"),
                "RECOVERY_EXHAUSTION_OR_LIVELOCK",
                semantic,
                idata,
                f"rec_exh:{pid}:{iid}",
            ))
    return anomalies


def _detect_control_only_intervention(
    runtime_root: Path,
    config: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    executor: Any,
    watchdog: Any,
    planner: Any,
    reviewer: Any,
    now: Any,
) -> list[tuple[str, str | None, str, dict[str, Any], dict[str, Any], str]]:
    anomalies = []
    cmd_store = ControlCommandStore(runtime_root)
    recent_cmds = cmd_store.recent(20)
    for cmd in recent_cmds:
        cid = str(cmd.get("command_id") or "")
        act = str(cmd.get("action") or "").lower()
        if act not in {"continue", "retry", "rereview", "reconcile", "restart"}:
            continue
        if cmd.get("state") != "accepted":
            continue

        pid = str(cmd.get("project_id") or "")
        before = cmd.get("pre_command_identity") or cmd.get("observed")
        after = None
        if isinstance(summary, dict):
            if isinstance(summary.get(pid), dict):
                after = summary.get(pid)
            elif isinstance(summary.get("projects"), list):
                for p in summary["projects"]:
                    if isinstance(p, dict) and str(p.get("project_id") or p.get("id") or "") == pid:
                        after = p
                        break

        classification = classify_owner_action(act, cmd, before, after)
        if classification == "CONTROL_ONLY":
            semantic = {
                "failure_class": "control_only_intervention",
                "diagnosis_code": "agent_stalled",
                "lifecycle_class": "control_ingress",
                "contract_class": "self_healing",
            }
            anomalies.append((
                pid,
                cmd.get("task_id"),
                "CONTROL_ONLY_INTERVENTION",
                semantic,
                cmd,
                f"ctrl_only:{cid}",
            ))
    return anomalies


def _detect_orchestrator_alive_task_stalled(
    runtime_root: Path,
    config: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    executor: Any,
    watchdog: Any,
    planner: Any,
    reviewer: Any,
    now: Any,
) -> list[tuple[str, str | None, str, dict[str, Any], dict[str, Any], str]]:
    anomalies = []
    if not isinstance(summary, dict):
        return anomalies
    projects = summary.get("projects") or []
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        pid = str(proj.get("project_id") or proj.get("id") or "")
        if not pid:
            continue

        # Check owner gate authority
        gate_auth = resolve_owner_gate_authority(runtime_root, proj)
        if gate_auth.get("pending") or gate_auth.get("paused") or not gate_auth.get("resolved"):
            continue

        # Check role liveness: requires all roles dead and none unknown
        liveness = resolve_role_liveness(runtime_root, pid, proj, planner, reviewer, executor)
        if liveness.get("any_unknown") or not liveness.get("all_dead"):
            continue

        # Check progress obligation
        obligation = resolve_progress_obligation(proj)
        if obligation.get("legal_wait") or obligation.get("is_terminal"):
            continue

        lifecycle = str(proj.get("lifecycle_state") or proj.get("state") or proj.get("status") or "").upper()
        if lifecycle in {"DONE", "COMPLETED", "TERMINAL", "IDLE"}:
            continue

        # Check task activity
        activity = proj.get("activity") if isinstance(proj.get("activity"), dict) else {}
        task_active = bool(activity.get("task_active"))
        if task_active:
            continue

        semantic = {
            "failure_class": "task_stalled",
            "diagnosis_code": "orchestrator_alive_task_stalled",
            "lifecycle_class": str(proj.get("lifecycle_state") or proj.get("state") or "unknown").lower(),
            "contract_class": "progress_obligation",
        }
        anomalies.append((
            pid,
            proj.get("task_id"),
            "ORCHESTRATOR_ALIVE_TASK_STALLED",
            semantic,
            {"liveness": liveness, "obligation": obligation, "snapshot": proj},
            f"orch_stalled:{pid}:{proj.get('task_id')}",
        ))
    return anomalies


DETECTORS: dict[str, Callable[..., list[tuple[str, str | None, str, dict[str, Any], dict[str, Any], str]]]] = {
    "watchdog_recovery": _detect_watchdog_recovery,
    "actionable_execution_loss": _detect_execution_loss,
    "execution_loss": _detect_execution_loss,
    "unconsumed_plan_or_review": _detect_unconsumed_plan_or_review,
    "launch_gap": _detect_launch_gap,
    "restart_reconcile_outcome_change": _detect_restart_reconcile_outcome_change,
    "recovery_cycle_exhaustion_or_livelock": _detect_recovery_exhaustion_or_livelock,
    "recovery_exhaustion_or_livelock": _detect_recovery_exhaustion_or_livelock,
    "control_only_intervention": _detect_control_only_intervention,
    "orchestrator_alive_task_stalled": _detect_orchestrator_alive_task_stalled,
}


def harvest_tick(
    runtime_root: Path | str,
    config: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    executor: Any = None,
    watchdog: Any = None,
    planner: Any = None,
    reviewer: Any = None,
    now: Any = None,
) -> list[dict[str, Any]]:
    """Execute harvesting detectors across subsystem records and capture incident families.

    Guarded: never raises through daemon tick paths.
    """
    runtime = Path(runtime_root)
    captured_families: list[dict[str, Any]] = []

    try:
        policy = resolve_incident_policy(config)
        if not policy.get("enabled"):
            return []

        for detector_name, detector_fn in DETECTORS.items():
            try:
                findings = detector_fn(
                    runtime_root=runtime,
                    config=config,
                    summary=summary,
                    executor=executor,
                    watchdog=watchdog,
                    planner=planner,
                    reviewer=reviewer,
                    now=now,
                )
                for pid, tid, classification, semantic, ev, occ_key in findings:
                    family = capture_incident(
                        runtime_root=runtime,
                        project_id=pid,
                        task_id=tid,
                        classification=classification,
                        semantic=semantic,
                        evidence=ev,
                        occurrence_key=occ_key,
                        config=config,
                    )
                    captured_families.append(family)
            except Exception:
                # Individual detector failure must not abort other detectors or daemon tick
                continue

    except Exception:
        # Top-level failure is fail-closed / no-op
        return []

    return captured_families
