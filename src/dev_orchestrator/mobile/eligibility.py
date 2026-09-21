"""Shared mobile owner-gate eligibility predicate.

Used by both MobileProjectionService and ControlCommandCoordinator.
Checks pending gate, matching project/task/repo, clean unchanged repository truth,
live device capability, and active-claim exclusion on any bound conversation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.control.surface import _latest_owner_gate
from dev_orchestrator.mobile.authorizer import MobileDeviceAuthorizer


def mobile_owner_gate_eligibility(
    project: dict[str, Any],
    snapshot: dict[str, Any],
    runtime_root: Path | str,
    device_id: str,
    mobile_device_authorizer: Optional[MobileDeviceAuthorizer],
    conversation_store: Any = None,
    bridge_store: Any = None,
) -> tuple[bool, str, Optional[str]]:
    """Evaluate whether an owner gate can be approved by a mobile device.

    Returns:
        (available: bool, reason: str, gate_id: str | None)
    """
    if mobile_device_authorizer is None:
        return False, "mobile authorizer unavailable", None

    # 1. Validate device capability (tokenless)
    ok, auth_reason, principal = mobile_device_authorizer.lookup_mobile_device(device_id)
    if not ok or principal is None:
        return False, f"mobile device unauthorized: {auth_reason}", None

    runtime = Path(runtime_root)
    project_id = str(project.get("project_id") or snapshot.get("project_id") or snapshot.get("id") or "")
    if not project_id:
        return False, "project_id is missing", None

    # 2. Check pending planner owner gate
    gate = _latest_owner_gate(runtime, project_id)
    if not isinstance(gate, dict) or gate.get("gate_source") != "planner":
        return False, "no pending planner owner gate", None

    gate_id = str(gate.get("gate_id") or gate.get("request_id") or gate.get("plan_id") or "")
    if not gate_id:
        return False, "no pending planner owner gate", None

    if gate.get("project_id") != project_id:
        return False, "owner gate belongs to a different project", gate_id

    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    task_id = telemetry.get("task_id")
    if task_id and gate.get("task_id") and gate.get("task_id") != task_id:
        return False, "owner gate task does not match current task", gate_id

    repo_path = project.get("repo_path") or snapshot.get("repo_path")
    if repo_path and gate.get("repo_path") and gate.get("repo_path") != repo_path:
        return False, "owner gate repository does not match current project", gate_id

    # 3. Check clean unchanged repository truth
    if repo_path:
        try:
            truth = read_repository_truth(repo_path)
            if not truth.valid or truth.dirty:
                return False, "repository is dirty", gate_id
            if (
                (gate.get("branch") and truth.branch != gate.get("branch"))
                or (gate.get("head") and truth.head != gate.get("head"))
                or (gate.get("status_hash") and truth.status_hash != gate.get("status_hash"))
            ):
                return False, "repository changed since owner gate was opened", gate_id
        except Exception as exc:  # noqa: BLE001
            return False, f"failed reading repository truth: {exc}", gate_id

    # 4. Check active claimed conversation exclusion
    binding = None
    if conversation_store is not None:
        binding = conversation_store.binding_for_project(project_id)
    if binding is None:
        route = snapshot.get("conversation_binding")
        if isinstance(route, dict):
            binding = route
    if isinstance(binding, dict):
        if bridge_store is None:
            return False, "bridge claim state is unavailable", gate_id
        has_claim = getattr(bridge_store, "has_active_claim", None)
        if not callable(has_claim):
            return False, "bridge claim state is unavailable", gate_id
        adapter = str(binding.get("adapter") or "")
        binding_id = str(binding.get("binding_id") or "")
        if adapter and binding_id and has_claim(adapter, binding_id):
            return False, "active claimed request blocks owner-gate approval", gate_id

    return True, "exact pending planner gate can be approved via mobile", gate_id
