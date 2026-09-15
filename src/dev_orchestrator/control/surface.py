"""Pure project identity and capability projection for Control API v1."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from dev_orchestrator.core.project_status import project_runtime_status
from dev_orchestrator.storage.json_store import read_json

from .owner_store import OwnerControlStore
from .store import ConversationControlStore


def _latest_owner_gate(runtime: Path, project_id: str) -> dict[str, Any] | None:
    watchdog = read_json(runtime / "watchdog.json", {})
    projects = watchdog.get("projects") if isinstance(watchdog, dict) else None
    row = projects.get(project_id) if isinstance(projects, dict) else None
    gate = row.get("owner_gate") if isinstance(row, dict) else None
    if isinstance(gate, dict):
        return copy.deepcopy(gate)
    planner = read_json(runtime / "ai-planner.json", {})
    plans = planner.get("plans") if isinstance(planner, dict) else None
    matches = [value for value in (plans or {}).values() if isinstance(value, dict) and value.get("project_id") == project_id and value.get("state") == "owner_gate"]
    return copy.deepcopy(max(matches, key=lambda value: str(value.get("completed_at") or value.get("started_at") or ""))) if matches else None


def project_identity(snapshot: dict[str, Any], runtime_root: Path | str) -> dict[str, Any]:
    runtime = Path(runtime_root)
    projected = project_runtime_status(snapshot, runtime)
    project_id = str(projected.get("project_id") or projected.get("id") or "")
    git = projected.get("git") if isinstance(projected.get("git"), dict) else {}
    telemetry = projected.get("telemetry") if isinstance(projected.get("telemetry"), dict) else {}
    gate = _latest_owner_gate(runtime, project_id)
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
        "branch": git.get("branch"),
        "head": git.get("head"),
        "task_id": telemetry.get("task_id"),
        "lifecycle_state": projected.get("lifecycle_state") or projected.get("state"),
        "gate_id": (gate.get("gate_id") or gate.get("request_id") or gate.get("plan_id")) if gate else None,
        "paused": bool(paused.get("paused")),
        "binding_state": binding.get("state") if isinstance(binding, dict) else None,
        "binding_id": binding.get("binding_id") if isinstance(binding, dict) else None,
        "binding_adapter": binding.get("adapter") if isinstance(binding, dict) else None,
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"revision": "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest(), **value}


def validate_expected(expected: Any, snapshot: dict[str, Any], runtime_root: Path | str) -> tuple[bool, str, dict[str, Any]]:
    observed = project_identity(snapshot, runtime_root)
    if not isinstance(expected, dict) or not isinstance(expected.get("revision"), str):
        return False, "control API command requires expected project revision", observed
    for key in ("revision", "branch", "head", "task_id", "lifecycle_state", "gate_id"):
        if key in expected and expected.get(key) != observed.get(key):
            return False, f"stale project identity: {key} changed", observed
    return True, "", observed


def _active_execution(runtime: Path, project_id: str) -> dict[str, Any] | None:
    ledger = read_json(runtime / "transition-executor.json", {})
    rows = ledger.get("executions") if isinstance(ledger, dict) else None
    matches = [row for row in (rows or {}).values() if isinstance(row, dict) and row.get("project_id") == project_id and row.get("state") in {"launching", "running"}]
    return copy.deepcopy(max(matches, key=lambda row: str(row.get("started_at") or ""))) if matches else None


def project_control_view(snapshot: dict[str, Any], runtime_root: Path | str) -> dict[str, Any]:
    runtime = Path(runtime_root)
    projected = project_runtime_status(snapshot, runtime)
    project_id = str(projected.get("project_id") or projected.get("id") or "")
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
    stoppable_active = (
        active is None
        or (active.get("engine") == "aibroker" and bool(active.get("broker_request_id")))
    )
    lifecycle = str(identity.get("lifecycle_state") or "")
    next_status = str(projected.get("next_status") or "")
    paused = bool(owner.get("paused"))
    eligible_continue = not paused and (
        lifecycle == "READY_TO_RUN" or "PENDING DESIGN" in next_status.upper()
    )
    # Recovery ledgers expose evidence today, but no exact owner retry adapter
    # exists yet. Keep the action visible and explicitly unavailable.
    safe_retry = False
    bound = isinstance(binding, dict) and binding.get("state") == "bound"
    live_ids = {str(row.get("binding_id")) for row in sessions if row.get("state") == "live"}
    controls = [
        {"action": "continue", "available": eligible_continue, "reason": "current state can continue" if eligible_continue else "project is paused or not continuable"},
        {"action": "pause", "available": not paused, "reason": "prevent future launches" if not paused else "project is already paused"},
        {"action": "resume", "available": paused, "reason": "clear launch barrier" if paused else "project is not paused"},
        {"action": "stop", "available": not paused and stoppable_active, "reason": "pause and interrupt exact supported execution" if active and stoppable_active else ("pause future launches" if not active else "active execution does not support managed interruption")},
        {"action": "retry", "available": safe_retry, "reason": "recovery-required state" if safe_retry else "no safe exact retry target"},
        {"action": "reconcile", "available": False, "reason": "no explicit safe reconcile target is projected"},
        {"action": "approve_owner_gate", "available": False, "reason": "no universal owner-gate adapter is available"},
        {"action": "bind_conversation", "available": not bound and bool(live_ids), "reason": "live sessions are available" if live_ids else "no live session is available"},
        {"action": "unbind_conversation", "available": bound, "reason": "runtime binding exists" if bound else "project has no runtime binding"},
        {"action": "rebind_conversation", "available": bound and bool(live_ids - {str(binding.get('binding_id'))}), "reason": "another live session is available" if bound else "project is not runtime-bound"},
    ]
    for item in controls:
        item["required_expected_fields"] = ["revision", "branch", "head", "task_id", "lifecycle_state"]
    result = copy.deepcopy(projected)
    result.update({
        "control_identity": identity,
        "controls": controls,
        "owner_control": owner,
        "conversation": {"binding": binding, "live_session_ids": sorted(live_ids)},
        "active_execution": active,
    })
    return result
