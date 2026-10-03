"""Automated failure harvesting with named detector registry and durable anomaly derivation."""
from __future__ import annotations

from pathlib import Path
import json
from typing import Any, Callable, Sequence

from dev_orchestrator.control.command_store import ControlCommandStore
from dev_orchestrator.incidents.attribution import classify_owner_action
from dev_orchestrator.incidents.capture import capture_incident
from dev_orchestrator.incidents.liveness import resolve_role_liveness
from dev_orchestrator.incidents.obligations import resolve_progress_obligation
from dev_orchestrator.incidents.owner_gate import resolve_owner_gate_authority
from dev_orchestrator.incidents.policy import resolve_incident_policy
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now, utc_now_iso


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

        # Check progress obligation and require stale task evidence before
        # classifying a stall.  READY_TO_RUN/PENDING states are normal for a
        # bounded window and must not become incidents on their first tick.
        policy = resolve_incident_policy(config)
        obligation = resolve_progress_obligation(proj, policy=policy)
        if obligation.get("legal_wait") or obligation.get("is_terminal"):
            continue

        lifecycle = str(proj.get("lifecycle_state") or proj.get("state") or proj.get("status") or "").upper()
        if lifecycle in {"DONE", "COMPLETED", "TERMINAL", "IDLE"}:
            continue

        # task_active is a top-level project-status field; check both top-level
        # and nested activity for compatibility with synthetic test fixtures.
        activity = proj.get("activity") if isinstance(proj.get("activity"), dict) else {}
        task_active = bool(proj.get("task_active")) or bool(activity.get("task_active"))
        if task_active:
            continue
        telemetry = proj.get("telemetry") if isinstance(proj.get("telemetry"), dict) else {}
        age = telemetry.get("watchdog_safe_activity_age_seconds")
        if not isinstance(age, (int, float)):
            age = telemetry.get("last_activity_age_seconds")
        if not isinstance(age, (int, float)):
            age = activity.get("watchdog_safe_activity_age_seconds")
        if not isinstance(age, (int, float)):
            age = activity.get("age_seconds")
        if not isinstance(age, (int, float)):
            # Fall back to timestamp derivation if numeric age is absent
            ts_str = (
                activity.get("last_task_activity_at")
                or (activity.get("watchdog_safe") or {}).get("last_activity_at")
                or activity.get("last_meaningful_progress_at")
                or activity.get("last_activity_at")
                or telemetry.get("last_task_activity_at")
                or telemetry.get("watchdog_safe_activity_at")
                or telemetry.get("last_activity_at")
                or proj.get("last_task_activity_at")
            )
            if ts_str:
                ts_dt = parse_utc(ts_str)
                if ts_dt is not None:
                    current_now = parse_utc(now) if now is not None else utc_now()
                    if current_now is not None:
                        age = (current_now - ts_dt).total_seconds()
        if not isinstance(age, (int, float)):
            continue  # fail closed: unknown task-evidence age is not a stall
        stale_after = max(
            float(policy.get("stale_task_activity_threshold_seconds", 300)),
            float(policy.get("progress_obligation_window_seconds", 300)),
        )
        if float(age) < stale_after:
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


def _detect_planner_failure(
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
    planner_file = runtime_root / "ai-planner.json"
    if not planner_file.is_file():
        return anomalies
    data = read_json(planner_file, {})
    plans = data.get("plans") if isinstance(data, dict) else {}
    if not isinstance(plans, dict):
        return anomalies

    for plan_id, pdata in plans.items():
        if not isinstance(pdata, dict):
            continue
        pid = pdata.get("project_id")
        if not pid:
            continue
        tid = pdata.get("task_id")
        state = pdata.get("state")
        last_fail = str(pdata.get("planner_last_failure") or "")
        attempts = pdata.get("planner_attempts") or []

        has_failed_attempt = False
        if isinstance(attempts, list):
            for att in attempts:
                if not isinstance(att, dict):
                    continue
                if att.get("status") == "failed":
                    has_failed_attempt = True
                    att_idx = att.get("attempt", 1)
                    reason = str(att.get("reason") or last_fail or "").lower()
                    if "timeout" in reason or "timed out" in reason or "deadline" in reason:
                        fclass = "planner_timeout"
                    elif "malformed" in reason or "extraction" in reason or "json" in reason or "marker" in reason:
                        fclass = "malformed_ai_output"
                    else:
                        fclass = "planner_timeout" if "time" in reason else "malformed_ai_output"
                    semantic = {
                        "failure_class": fclass,
                        "role": "planner",
                        "stage": "planning",
                        "lifecycle_class": "planning",
                    }
                    occ_key = f"plan_fail:{pid}:{plan_id}:{att_idx}"
                    anomalies.append((str(pid), tid, "PLANNER_FAILURE", semantic, att, occ_key))

        if not has_failed_attempt and (state in ("failed", "blocked") or last_fail):
            reason = last_fail.lower()
            if "timeout" in reason or "timed out" in reason or "deadline" in reason:
                fclass = "planner_timeout"
            elif "malformed" in reason or "extraction" in reason or "json" in reason or "marker" in reason:
                fclass = "malformed_ai_output"
            else:
                fclass = "malformed_ai_output"
            semantic = {
                "failure_class": fclass,
                "role": "planner",
                "stage": "planning",
                "lifecycle_class": "planning",
            }
            occ_key = f"plan_fail:{pid}:{plan_id}:1"
            anomalies.append((str(pid), tid, "PLANNER_FAILURE", semantic, pdata, occ_key))

    return anomalies


def _detect_worker_loss(
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
        pid = rec.get("project_id")
        if not pid:
            continue
        tid = rec.get("task_id")
        inv_key = rec.get("invariant_key")
        term_outcome = rec.get("terminal_outcome")
        if rec.get("loss_detected") or term_outcome in ("explicitly_reconciled", "stale_predecessor_fenced") or rec.get("state") == "lost":
            if term_outcome == "missing_execution_record":
                fclass = "missing_execution_record"
            else:
                fclass = "worker_process_lost"
            semantic = {
                "failure_class": fclass,
                "role": "worker",
                "diagnosis_code": "worker_vanished_without_terminal_state",
                "lifecycle_class": "executing",
                "contract_class": "durability",
                "invariant_identifier": str(inv_key or "execution_terminal_invariant"),
            }
            occ_key = f"worker_loss:{pid}:{lkey}"
            anomalies.append((str(pid), tid, "ACTIONABLE_EXECUTION_LOSS", semantic, rec, occ_key))

    return anomalies


def _detect_reviewer_failure(
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
    rev_file = runtime_root / "ai-reviewer.json"
    if not rev_file.is_file():
        return anomalies
    data = read_json(rev_file, {})
    reviews = data.get("reviews") if isinstance(data, dict) else {}
    if not isinstance(reviews, dict):
        return anomalies

    for rid, rdata in reviews.items():
        if not isinstance(rdata, dict):
            continue
        pid = rdata.get("project_id")
        if not pid:
            continue
        tid = rdata.get("task_id")
        state = rdata.get("state")
        decision = str(rdata.get("decision") or "").lower()
        dec_id = rdata.get("decision_id") or "1"
        reason = str(rdata.get("reason") or "").lower()

        if state == "failed" or decision in ("reject", "remediate") or "reject" in reason:
            if "timeout" in reason or "timed out" in reason or "deadline" in reason:
                fclass = "reviewer_timeout"
            elif decision in ("reject", "remediate") or "reject" in reason:
                fclass = "reviewer_rejected"
            else:
                fclass = "reviewer_rejected"
            semantic = {
                "failure_class": fclass,
                "role": "reviewer",
                "stage": "reviewing",
                "lifecycle_class": "reviewing",
                "contract_class": "technical_review",
            }
            occ_key = f"rev_fail:{pid}:{rid}:{dec_id}"
            anomalies.append((str(pid), tid, "REVIEWER_FAILURE", semantic, rdata, occ_key))

    return anomalies


def _detect_transition_executor_failure(
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
    te_file = runtime_root / "transition-executor.json"
    if not te_file.is_file():
        return anomalies
    data = read_json(te_file, {})
    executions = data.get("executions") if isinstance(data, dict) else {}
    if not isinstance(executions, dict):
        return anomalies

    for eid, edata in executions.items():
        if not isinstance(edata, dict):
            continue
        pid = edata.get("project_id")
        if not pid:
            continue
        tid = edata.get("task_id")
        state = edata.get("state")
        reason = str(edata.get("reason") or "").lower()

        if state in ("blocked", "failed"):
            if "handoff" in reason and ("missing" in reason or "lost" in reason):
                fclass = "lost_handoff"
            elif "duplicate" in reason:
                fclass = "duplicate_request"
            elif "projection" in reason or "mismatch" in reason or "repository changed" in reason:
                fclass = "lifecycle_projection_mismatch"
            elif "contradict" in reason or "ambiguous" in reason or "unspecified" in reason:
                fclass = "unknown_requires_owner"
            else:
                fclass = "unknown_requires_owner"
            semantic = {
                "failure_class": fclass,
                "lifecycle_class": "handoff",
                "contract_class": "transition_executor",
            }
            idem_key = edata.get("handoff_idempotency_key") or eid
            occ_key = f"trans_fail:{pid}:{idem_key}"
            anomalies.append((str(pid), tid, "TRANSITION_EXECUTOR_FAILURE", semantic, edata, occ_key))

    return anomalies


def _detect_daemon_restart_failure(
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
    boots_file = runtime_root / "daemon-boots.jsonl"
    if not boots_file.is_file():
        return anomalies

    boots: list[dict[str, Any]] = []
    try:
        with boots_file.open("r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if line.strip():
                    boots.append(json.loads(line.strip()))
    except Exception:
        return anomalies

    if not boots:
        return anomalies
    latest_boot = boots[-1]
    boot_id = str(latest_boot.get("boot_id") or f"boot-{len(boots)}")

    te_file = runtime_root / "transition-executor.json"
    if not te_file.is_file():
        return anomalies
    data = read_json(te_file, {})
    executions = data.get("executions") if isinstance(data, dict) else {}
    if not isinstance(executions, dict):
        return anomalies

    for eid, edata in executions.items():
        if not isinstance(edata, dict):
            continue
        pid = edata.get("project_id")
        if not pid:
            continue
        tid = edata.get("task_id")
        state = edata.get("state")

        if state in ("running", "handoff"):
            if state == "handoff":
                fclass = "daemon_restart_during_handoff"
            else:
                fclass = "daemon_restart_during_execution"
            semantic = {
                "failure_class": fclass,
                "lifecycle_class": "executing" if state == "running" else "handoff",
                "contract_class": "daemon_lifecycle",
                "restart_kind": "handoff" if state == "handoff" else "execution",
            }
            occ_key = f"boot_restart:{pid}:{boot_id}:{eid}"
            anomalies.append((str(pid), tid, "DAEMON_RESTART_FAILURE", semantic, edata, occ_key))

    return anomalies


def _detect_aibroker_routing_failure(
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
    events_file = runtime_root / "execution-accounting" / "events.jsonl"
    if not events_file.is_file():
        return anomalies

    try:
        with events_file.open("r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line.strip())
                if not isinstance(row, dict):
                    continue
                ev_type = row.get("event_type")
                if ev_type == "provider_result_observed":
                    metadata = row.get("metadata") or {}
                    if not isinstance(metadata, dict):
                        continue
                    status = metadata.get("status")
                    if status in ("failed", "no_candidate"):
                        pid = row.get("project_id")
                        if not pid:
                            continue
                        tid = row.get("task_id")
                        req_id = row.get("request_id") or "req"
                        cls_hint = str(metadata.get("classification") or "").lower()
                        err_msg = str(metadata.get("error") or "").lower()

                        if cls_hint in {"quota_exhausted", "rate_limited"} or "quota" in err_msg or "rate limit" in err_msg:
                            fclass = "provider_quota_exhausted"
                        elif cls_hint in {"provider_temporarily_unavailable", "resource_unavailable"} or "provider" in err_msg or "candidate" in err_msg:
                            fclass = "provider_unavailable"
                        elif "broker" in err_msg or "connection refused" in err_msg:
                            fclass = "aibroker_unavailable"
                        else:
                            fclass = "provider_unavailable"
                        semantic = {
                            "failure_class": fclass,
                            "role": row.get("role", "worker"),
                            "contract_class": "broker_routing",
                        }
                        occ_key = f"broker_routing:{pid}:{req_id}"
                        anomalies.append((str(pid), tid, "AIBROKER_ROUTING_FAILURE", semantic, row, occ_key))

                elif ev_type == "attempt_outcome":
                    outcome = str(row.get("outcome") or "").lower()
                    phase = str(row.get("phase") or "").lower()
                    if outcome in ("failed", "no_candidate") and phase in ("retry", "failover"):
                        pid = row.get("project_id")
                        if not pid:
                            continue
                        tid = row.get("task_id")
                        att_id = row.get("attempt_id") or "att"
                        semantic = {
                            "failure_class": "provider_unavailable",
                            "role": row.get("role", "worker"),
                            "contract_class": "broker_routing",
                        }
                        occ_key = f"broker_routing:{pid}:{att_id}"
                        anomalies.append((str(pid), tid, "AIBROKER_ROUTING_FAILURE", semantic, row, occ_key))
    except Exception:
        return anomalies

    return anomalies


def _detect_execution_transport_failure(
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
    logs_file = runtime_root / "logs" / "transport-operations.ndjson"
    if not logs_file.is_file():
        return anomalies

    try:
        with logs_file.open("r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line.strip())
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status") or "").lower()
                err = row.get("error")
                if status in ("failed", "rejected") or err:
                    pid = row.get("project_id")
                    if not pid:
                        continue
                    op_id = row.get("operation_id") or row.get("request_id") or f"op-{hash(json.dumps(row))}"
                    semantic = {
                        "failure_class": "transport_unavailable",
                        "contract_class": "execution_transport",
                    }
                    occ_key = f"transport_op:{pid}:{op_id}"
                    anomalies.append((str(pid), None, "EXECUTION_TRANSPORT_FAILURE", semantic, row, occ_key))
    except Exception:
        return anomalies

    return anomalies


def _detect_durable_job_failure(
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
    jobs_file = runtime_root / "jobs" / "index.json"
    if not jobs_file.is_file():
        return anomalies
    data = read_json(jobs_file, {})
    jobs = data.get("jobs") if isinstance(data, dict) else {}
    if not isinstance(jobs, dict):
        return anomalies

    for jid, jdata in jobs.items():
        if not isinstance(jdata, dict):
            continue
        pid = jdata.get("project_id")
        if not pid:
            continue
        state = jdata.get("state")
        if state == "failed":
            fkind = str(jdata.get("failure_kind") or "").lower()
            if "transport" in fkind or "channel" in fkind or "connection" in fkind:
                fclass = "transport_unavailable"
            elif "duplicate" in fkind:
                fclass = "duplicate_request"
            elif "worker" in fkind or "loss" in fkind:
                fclass = "worker_process_lost"
            else:
                fclass = "transport_unavailable"
            semantic = {
                "failure_class": fclass,
                "contract_class": "durable_job",
            }
            occ_key = f"job_fail:{pid}:{jid}:{state}"
            anomalies.append((str(pid), None, "DURABLE_JOB_FAILURE", semantic, jdata, occ_key))

    return anomalies


DETECTORS: dict[str, Callable[..., list[tuple[str, str | None, str, dict[str, Any], dict[str, Any], str]]]] = {
    "planner_failure": _detect_planner_failure,
    "worker_loss": _detect_worker_loss,
    "reviewer_failure": _detect_reviewer_failure,
    "watchdog_recovery": _detect_watchdog_recovery,
    "transition_executor_failure": _detect_transition_executor_failure,
    "daemon_restart_failure": _detect_daemon_restart_failure,
    "aibroker_routing_failure": _detect_aibroker_routing_failure,
    "execution_transport_failure": _detect_execution_transport_failure,
    "durable_job_failure": _detect_durable_job_failure,
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

        seen_detectors = set()
        for detector_name, detector_fn in DETECTORS.items():
            if detector_fn in seen_detectors:
                continue
            seen_detectors.add(detector_fn)
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
