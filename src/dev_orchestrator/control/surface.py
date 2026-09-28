"""Pure project identity and capability projection for Control API v1."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from dev_orchestrator.core.lifecycle_authority import TERMINAL_LIFECYCLE_STATES
from dev_orchestrator.core.lifecycle_projection import _latest
from dev_orchestrator.core.project_status import project_runtime_status
from dev_orchestrator.core.task_status import parse_task_status
from dev_orchestrator.storage.json_store import read_json

from .command_store import EXPECTED_IDENTITY_FIELDS
from .owner_store import OwnerControlStore
from .reconcile import resolve_reconcile_candidate, resolve_retry_candidate, resolve_rereview_candidate
from .store import ConversationControlStore


def latest_owner_gate(runtime: Path | str, project_id: str) -> dict[str, Any] | None:
    rt = Path(runtime)
    watchdog = read_json(rt / "watchdog.json", {})
    projects = watchdog.get("projects") if isinstance(watchdog, dict) else None
    row = projects.get(project_id) if isinstance(projects, dict) else None
    gate = row.get("owner_gate") if isinstance(row, dict) else None
    if isinstance(gate, dict):
        result = copy.deepcopy(gate)
        result["gate_source"] = "watchdog"
        if not result.get("state"):
            result["state"] = "owner_gate"
        return result
    planner = read_json(rt / "ai-planner.json", {})
    plans = planner.get("plans") if isinstance(planner, dict) else None
    matches = [value for value in (plans or {}).values() if isinstance(value, dict) and value.get("project_id") == project_id and value.get("state") == "owner_gate"]
    if matches:
        result = copy.deepcopy(max(matches, key=lambda value: str(value.get("completed_at") or value.get("started_at") or "")))
        result["gate_source"] = "planner"
        return result
    # Technical-review remediation exhaustion is also an owner gate. It is
    # durable in ai-reviewer.json and must participate in the same control
    # identity as planner/watchdog gates, subject to rereview_of consumption
    # and latest-review-wins semantics.
    reviewer = read_json(rt / "ai-reviewer.json", {})
    reviews = reviewer.get("reviews") if isinstance(reviewer, dict) else None
    if not isinstance(reviews, dict):
        return None
    consumed = {
        str(row.get("rereview_of") or "")
        for row in reviews.values()
        if isinstance(row, dict) and row.get("project_id") == project_id
    }
    consumed.discard("")
    latest_review = _latest(reviews, project_id)
    if not isinstance(latest_review, dict):
        return None
    latest_review_id = str(latest_review.get("review_id") or "")
    if latest_review_id in consumed:
        return None
    if (
        str(latest_review.get("state") or "") == "completed"
        and latest_review.get("decision") == "owner_gate"
    ):
        result = copy.deepcopy(latest_review)
        result["gate_source"] = "reviewer"
        return result
    return None


_latest_owner_gate = latest_owner_gate


def is_bounded_review_remediation_gate(gate: Any) -> bool:
    """Identify the one legacy review gate an owner may continue narrowly."""
    if not isinstance(gate, dict):
        return False
    findings = gate.get("review_findings")
    blocking = [
        row for row in findings
        if isinstance(row, dict)
        and str(row.get("summary") or "").strip().upper().startswith("BLOCKING:")
    ] if isinstance(findings, list) else []
    try:
        remediation_round = int(gate.get("remediation_round") or 0)
        max_rounds = int(gate.get("max_remediation_rounds") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        gate.get("gate_source") == "reviewer"
        and gate.get("state") == "completed"
        and gate.get("decision") == "owner_gate"
        and gate.get("next_action") == "stop"
        and str(gate.get("reason") or "").startswith(
            "technical review remediation budget exhausted ("
        )
        and remediation_round >= max_rounds > 0
        and gate.get("remediation_extension_granted") is not True
        and len(blocking) == 1
    )


def project_identity(snapshot: dict[str, Any], runtime_root: Path | str) -> dict[str, Any]:
    runtime = Path(runtime_root)
    projected = project_runtime_status(snapshot, runtime)
    project_id = str(projected.get("project_id") or projected.get("id") or "")
    git = projected.get("git") if isinstance(projected.get("git"), dict) else {}
    telemetry = projected.get("telemetry") if isinstance(projected.get("telemetry"), dict) else {}
    gate = _latest_owner_gate(runtime, project_id)
    active_lifecycles = {
        "EXECUTING", "REVIEWING", "PLANNING", "REVIEWING_PLAN",
        "APPLYING_PLAN", "REMEDIATING_PLAN", "DONE", "COMPLETED", "TERMINAL"
    }
    if (
        isinstance(gate, dict)
        and projected.get("lifecycle_state") not in active_lifecycles
        and str(projected.get("state") or "").upper() not in {"DONE", "COMPLETED", "TERMINAL"}
    ):
        if gate.get("gate_source") == "reviewer":
            if str(gate.get("state") or "") == "completed" and gate.get("decision") == "owner_gate":
                projected["lifecycle_state"] = "OWNER_GATE"
        elif str(gate.get("state") or "") in {"owner_gate", "completed"}:
            projected["lifecycle_state"] = "OWNER_GATE"
    binding = ConversationControlStore(runtime).runtime_record_for_project(project_id)
    if binding is None:
        route = projected.get("conversation_binding")
        if isinstance(route, dict):
            binding = {
                "state": "static", "adapter": route.get("adapter"),
                "binding_id": route.get("binding_id"),
            }
    paused = OwnerControlStore(runtime).project_state(project_id)
    value = {
        "project_id": project_id,
        "repo_path": projected.get("repo_path"),
        "branch": git.get("branch"),
        "head": git.get("head"),
        "dirty": bool(git.get("dirty")),
        "status_hash": git.get("status_hash"),
        "task_id": telemetry.get("task_id"),
        "lifecycle_state": projected.get("lifecycle_state") or projected.get("state"),
        "gate_id": (gate.get("gate_id") or gate.get("request_id") or gate.get("plan_id") or gate.get("review_id")) if gate else None,
        "paused": bool(paused.get("paused")),
        "binding_state": binding.get("state") if isinstance(binding, dict) else None,
        "binding_id": binding.get("binding_id") if isinstance(binding, dict) else None,
        "binding_adapter": binding.get("adapter") if isinstance(binding, dict) else None,
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"revision": "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest(), **value}


def validate_expected(expected: Any, snapshot: dict[str, Any], runtime_root: Path | str) -> tuple[bool, str, dict[str, Any]]:
    observed = project_identity(snapshot, runtime_root)
    if not isinstance(expected, dict):
        return False, "control command requires complete expected project identity", observed
    missing = [key for key in EXPECTED_IDENTITY_FIELDS if key not in expected]
    if missing:
        return False, "control command expected identity missing fields: " + ", ".join(missing), observed
    for key in EXPECTED_IDENTITY_FIELDS:
        if expected.get(key) != observed.get(key):
            return False, f"stale project identity: {key} changed", observed
    if "recovery_epoch_id" in expected and expected.get("recovery_epoch_id") is not None:
        obs_epoch_id = None
        watchdog_state = read_json(Path(runtime_root) / "watchdog.json", {})
        prow = watchdog_state.get("projects", {}).get(observed.get("project_id"), {}) if isinstance(watchdog_state, dict) else {}
        epoch = prow.get("recovery_epoch") if isinstance(prow, dict) else None
        if isinstance(epoch, dict):
            obs_epoch_id = epoch.get("id")
        if not obs_epoch_id:
            obs_epoch_id = snapshot.get("recovery_epoch_id") or (snapshot.get("recovery_epoch", {}).get("id") if isinstance(snapshot.get("recovery_epoch"), dict) else None)
        if expected.get("recovery_epoch_id") != obs_epoch_id:
            return False, "stale project identity: recovery_epoch_id changed", observed
    return True, "", observed


def _active_execution(runtime: Path, project_id: str) -> dict[str, Any] | None:
    ledger = read_json(runtime / "transition-executor.json", {})
    rows = ledger.get("executions") if isinstance(ledger, dict) else None
    matches = [row for row in (rows or {}).values() if isinstance(row, dict) and row.get("project_id") == project_id and row.get("state") in {"launching", "running"}]
    return copy.deepcopy(max(matches, key=lambda row: str(row.get("started_at") or ""))) if matches else None


def project_control_view(
    snapshot: dict[str, Any], runtime_root: Path | str,
    project_config: dict[str, Any] | None = None,
    bridge_store: Any | None = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root)
    projected = project_runtime_status(snapshot, runtime)
    project_id = str(projected.get("project_id") or projected.get("id") or "")
    gate = _latest_owner_gate(runtime, project_id)
    active_lifecycles = {
        "EXECUTING", "REVIEWING", "PLANNING", "REVIEWING_PLAN",
        "APPLYING_PLAN", "REMEDIATING_PLAN",
    }
    terminal_lifecycles = {"DONE", "COMPLETED", "TERMINAL"}
    if (
        isinstance(gate, dict)
        and projected.get("lifecycle_state") not in active_lifecycles
        and projected.get("lifecycle_state") not in terminal_lifecycles
        and str(projected.get("state") or "").upper() not in terminal_lifecycles
    ):
        if gate.get("gate_source") == "reviewer":
            if str(gate.get("state") or "") == "completed" and gate.get("decision") == "owner_gate":
                projected["lifecycle_state"] = "OWNER_GATE"
        elif str(gate.get("state") or "") in {"owner_gate", "completed"}:
            projected["lifecycle_state"] = "OWNER_GATE"
    identity = project_identity(projected, runtime)
    owner = OwnerControlStore(runtime).project_state(project_id)
    conversations = ConversationControlStore(runtime)
    runtime_binding = conversations.runtime_record_for_project(project_id)
    binding = runtime_binding
    if binding is None and isinstance(projected.get("conversation_binding"), dict):
        binding = {
            "project_id": project_id, "state": "bound", "provenance": "static",
            **projected["conversation_binding"],
        }
    sessions = conversations.list_sessions()
    active = _active_execution(runtime, project_id)
    active_roles: list[dict[str, Any]] = []
    if active is not None:
        active_roles.append({
            "role": active.get("role") or "implementer", "state": active.get("state"),
            "request_id": active.get("broker_request_id") or active.get("source_request_id"),
        })
    reviewer = projected.get("reviewer")
    if isinstance(reviewer, dict) and reviewer.get("state") in {"launching", "running"}:
        active_roles.append({"role": "reviewer", "state": reviewer.get("state"), "request_id": reviewer.get("review_id")})
    planner = projected.get("planner")
    if isinstance(planner, dict) and planner.get("state") in {"planning", "reviewing", "applying", "remediating"}:
        role = {"planning": "planner", "reviewing": "reviewer", "applying": "planner", "remediating": "remediator"}[str(planner.get("state"))]
        active_roles.append({"role": role, "state": planner.get("state"), "request_id": planner.get("plan_id")})
    lifecycle = str(identity.get("lifecycle_state") or projected.get("lifecycle_state") or projected.get("state") or "")
    unsupported_stop_role_active = (
        lifecycle in {"PLANNING", "REVIEWING_PLAN", "REMEDIATING_PLAN", "APPLYING_PLAN", "REVIEWING"}
        or any(
            row.get("role") in {"planner", "reviewer", "remediator"}
            for row in active_roles
        )
    )
    stoppable_active = not unsupported_stop_role_active and (
        active is None
        or (active.get("engine") == "aibroker" and bool(active.get("broker_request_id")))
    )
    next_status = str(projected.get("next_status") or "")
    parsed_next_status = parse_task_status(projected.get("next_status"))
    paused = bool(owner.get("paused"))
    execution_ready = False
    planning_ready = False
    if isinstance(project_config, dict):
        from dev_orchestrator.core.transition_executor import _execution_policy
        execution_ready = _execution_policy(project_config)[0] is not None
        roles = project_config.get("ai_roles")
        planner_config = roles.get("planner") if isinstance(roles, dict) else None
        planning_ready = isinstance(planner_config, dict) and planner_config.get("enabled") is True
    planning_start_ready = lifecycle in {"IDLE", "PLAN_FAILED", "PENDING_DESIGN"} and parsed_next_status.is_pending_design() and planning_ready
    recoverable_plan_gate = (
        isinstance(gate, dict)
        and gate.get("gate_source") == "planner"
        and str(gate.get("reason") or "").startswith("plan remediation hit its bound after ")
        and gate.get("review_decision") == "reject"
    )
    recovery_continue_ready = (
        parsed_next_status.is_pending_design()
        and planning_ready
        and recoverable_plan_gate
        and not bool(identity.get("dirty"))
    )
    review_recovery_continue_ready = (
        is_bounded_review_remediation_gate(gate)
        and execution_ready
        and active is None
        and not bool(identity.get("dirty"))
        and gate.get("project_id") == project_id
        and gate.get("task_id") == identity.get("task_id")
        and gate.get("branch") == identity.get("branch")
    )
    authoritative = projected.get("authoritative_lifecycle")
    authoritative_state = (
        str(authoritative.get("lifecycle_state") or "").upper()
        if isinstance(authoritative, dict) else ""
    )
    task_terminal = (
        authoritative_state in TERMINAL_LIFECYCLE_STATES
        or lifecycle.upper() in TERMINAL_LIFECYCLE_STATES
        or (parsed_next_status.is_completed() and (
            lifecycle.upper() in {"IDLE", "COMPLETED", "TERMINAL", "DONE"}
            or str(projected.get("state") or "").upper()
            in {"IDLE", "COMPLETED", "TERMINAL", "DONE"}
        ))
    )
    eligible_continue = not paused and not task_terminal and (
        (gate is None and ((lifecycle == "READY_TO_RUN" and execution_ready) or planning_start_ready))
        or recovery_continue_ready
        or review_recovery_continue_ready
    )
    retry_target, retry_reason = resolve_retry_candidate(projected, runtime, project_config)
    safe_retry = retry_target is not None and not task_terminal
    rereview_target, rereview_reason = resolve_rereview_candidate(projected, runtime, project_config)
    if task_terminal:
        retry_target = None
        retry_reason = "current task is terminal; stale review/continue is audit history only"
        rereview_target = None
        rereview_reason = "current task is terminal; stale review/continue is audit history only"
    bound = isinstance(binding, dict) and binding.get("state") == "bound"
    claim_guard_known = not bound or bridge_store is not None
    active_claim = False
    if bound and bridge_store is not None:
        has_active_claim = getattr(bridge_store, "has_active_claim", None)
        claim_guard_known = callable(has_active_claim)
        if claim_guard_known:
            active_claim = bool(has_active_claim(
                str(binding.get("adapter") or ""), str(binding.get("binding_id") or "")
            ))
    binding_change_available = bound and claim_guard_known and not active_claim
    live_ids = {str(row.get("binding_id")) for row in sessions if row.get("state") == "live"}
    other_live_binding = bound and bool(live_ids - {str(binding.get("binding_id"))})
    if active_claim:
        binding_change_reason = "active claimed request blocks binding change"
    elif bound and not claim_guard_known:
        binding_change_reason = "bridge claim state is unavailable"
    elif not bound:
        binding_change_reason = "project has no runtime binding"
    else:
        binding_change_reason = "runtime binding exists"
    gate_id = identity.get("gate_id")
    planner_gate = gate is not None and gate.get("gate_source") == "planner" and bool(gate_id)
    bound_live = bound and str(binding.get("binding_id")) in live_ids
    gate_binding = gate.get("conversation_binding") if planner_gate else None
    gate_binding_matches = (
        isinstance(gate_binding, dict) and bound
        and gate_binding.get("adapter") == binding.get("adapter")
        and gate_binding.get("binding_id") == binding.get("binding_id")
    )
    approve_available = (
        planner_gate and gate_binding_matches and bound_live
        and claim_guard_known and not active_claim
        and not bool(identity.get("dirty"))
    )
    if not planner_gate:
        approve_reason = "no pending planner owner gate"
    elif not gate_binding_matches:
        approve_reason = "owner gate does not match the exact bound conversation"
    elif not bound_live:
        approve_reason = "bound conversation is not live"
    elif not claim_guard_known:
        approve_reason = "bridge claim state is unavailable"
    elif active_claim:
        approve_reason = "active claimed request blocks owner-gate approval"
    elif identity.get("dirty"):
        approve_reason = "repository is dirty"
    else:
        approve_reason = "exact pending planner gate can be approved"
    reconcile_target, reconcile_reason = resolve_reconcile_candidate(projected, runtime, project_config)
    if task_terminal:
        reconcile_target = None
        reconcile_reason = "current task is terminal; stale review/continue is audit history only"
    if unsupported_stop_role_active:
        stop_reason = "active AI role does not support managed interruption; use pause"
    elif active and stoppable_active:
        stop_reason = "pause and interrupt exact supported execution"
    elif not active:
        stop_reason = "pause future launches"
    else:
        stop_reason = "active execution does not support managed interruption"
    controls = [
        {
            "action": "continue",
            "available": eligible_continue,
            "reason": (
                "current task is terminal; stale review/continue is audit history only"
                if task_terminal
                else (
                    "recover bounded technical plan review"
                    if recovery_continue_ready and eligible_continue
                    else (
                        "continue exact owner-authorized review remediation"
                        if review_recovery_continue_ready and eligible_continue
                        else ("current state can continue" if eligible_continue else "project is paused or not continuable")
                    )
                )
            ),
        },
        {"action": "pause", "available": not paused, "reason": "prevent future launches" if not paused else "project is already paused"},
        {"action": "resume", "available": paused, "reason": "clear launch barrier" if paused else "project is not paused"},
        {"action": "stop", "available": not paused and stoppable_active, "reason": stop_reason},
        {
            "action": "retry", "available": safe_retry,
            "reason": "exact failed technical review can be retried" if safe_retry else retry_reason,
            **({"target_id": retry_target["target_id"]} if retry_target is not None else {}),
        },
        {
            "action": "rereview", "available": rereview_target is not None,
            "reason": "stale technical review can be re-reviewed at current descendant HEAD" if rereview_target is not None else rereview_reason,
            **({"target_id": rereview_target["target_id"]} if rereview_target is not None else {}),
        },
        {
            "action": "reconcile", "available": reconcile_target is not None,
            "reason": "exact stale technical review can be re-anchored" if reconcile_target is not None else reconcile_reason,
            **({"target_id": reconcile_target["target_id"]} if reconcile_target is not None else {}),
        },
        {"action": "approve_owner_gate", "available": approve_available, "reason": approve_reason},
        {"action": "bind_conversation", "available": not bound and bool(live_ids), "reason": "live sessions are available" if live_ids else "no live session is available"},
        {"action": "unbind_conversation", "available": binding_change_available, "reason": binding_change_reason},
        {"action": "rebind_conversation", "available": binding_change_available and other_live_binding, "reason": "another live session is available" if binding_change_available and other_live_binding else ("no other live session is available" if binding_change_available else binding_change_reason)},
    ]
    for item in controls:
        item["required_expected_fields"] = list(EXPECTED_IDENTITY_FIELDS)
    from dev_orchestrator.core.blockers import explain_block, blocker_payload
    blockers = explain_block(
        project_config=project_config,
        snapshot=projected,
        runtime_root=runtime,
    )
    from dev_orchestrator.core.activity_telemetry import resolve_activity_telemetry
    activity_data = resolve_activity_telemetry(projected, runtime, project_id=project_id)
    result = copy.deepcopy(projected)
    result.update({
        "control_identity": identity,
        "controls": controls,
        "blockers": blocker_payload(blockers),
        "owner_control": owner,
        "conversation": {
            "binding": binding, "live_session_ids": sorted(live_ids),
            "active_claim": active_claim if claim_guard_known else None,
        },
        "active_execution": active,
        "active_roles": active_roles,
        "gate": gate,
        "activity": activity_data,
    })
    return result
