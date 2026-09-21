"""Read-only Mobile Projection Service.

Composes authoritative project status, control view, watchdog state, and recovery
epoch into mobile-facing payloads.
Enforces that:
1. Only read-only in-process composition is performed; no coordinators are invoked
   and no runtime lifecycle stores are mutated.
2. Only MOBILE_CONTROL_ACTIONS are exposed.
3. approve_owner_gate uses the shared mobile_owner_gate_eligibility predicate.
4. Watchdog classification and recovery_epoch fields are copied verbatim.
5. progress_observation_state is surfaced as 'authoritative', 'stale', or 'unavailable'.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from dev_orchestrator.control.surface import _latest_owner_gate, project_control_view
from dev_orchestrator.mobile.authorizer import MobileDeviceAuthorizer, MobileDevicePrincipal
from dev_orchestrator.mobile.eligibility import mobile_owner_gate_eligibility
from dev_orchestrator.storage.json_store import parse_utc, read_json

MOBILE_CONTROL_ACTIONS = frozenset({
    "continue",
    "pause",
    "resume",
    "stop",
    "retry",
    "reconcile",
    "approve_owner_gate",
})


class MobileProjectionService:
    """Authenticated, read-only in-process composition service for mobile clients."""

    def __init__(
        self,
        runtime_root: Path | str,
        config_provider: Any = None,
        mobile_device_authorizer: Optional[MobileDeviceAuthorizer] = None,
        conversation_store: Any = None,
        bridge_store: Any = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.config_provider = config_provider
        self.mobile_device_authorizer = mobile_device_authorizer
        self.conversation_store = conversation_store
        self.bridge_store = bridge_store

    def _resolve_project_config(self, project_id: str) -> Optional[dict[str, Any]]:
        if self.config_provider is None:
            return None
        if callable(getattr(self.config_provider, "get_project", None)):
            return self.config_provider.get_project(project_id)
        if isinstance(self.config_provider, dict):
            for row in self.config_provider.get("projects") or []:
                if isinstance(row, dict) and row.get("project_id") == project_id:
                    return row
        return None

    def project_view(
        self,
        snapshot: dict[str, Any],
        project_config: Optional[dict[str, Any]] = None,
        principal: Optional[MobileDevicePrincipal] = None,
    ) -> dict[str, Any]:
        """Compose a mobile-specific projection for a single project."""
        project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
        cfg = project_config or self._resolve_project_config(project_id) or {}

        # 1. Base view from existing authoritative surface projection
        base = project_control_view(
            snapshot,
            self.runtime_root,
            project_config=cfg,
            bridge_store=self.bridge_store,
        )

        identity = base.get("control_identity") or {}
        raw_controls = base.get("controls") or []

        # 2. Filter to strict MOBILE_CONTROL_ACTIONS
        controls = []
        device_id = principal.device_id if principal else ""

        for ctrl in raw_controls:
            action = ctrl.get("action")
            if action not in MOBILE_CONTROL_ACTIONS:
                continue

            ctrl_copy = copy.deepcopy(ctrl)
            if action == "approve_owner_gate":
                # Evaluate mobile-specific owner-gate eligibility
                avail, reason, gate_id = mobile_owner_gate_eligibility(
                    cfg,
                    snapshot,
                    self.runtime_root,
                    device_id,
                    self.mobile_device_authorizer,
                    self.conversation_store,
                    self.bridge_store,
                )
                ctrl_copy["available"] = avail
                ctrl_copy["reason"] = reason
                if gate_id:
                    ctrl_copy["target"] = {"gate_id": gate_id}
            controls.append(ctrl_copy)

        # 3. Read watchdog state verbatim and evaluate progress_observation_state
        watchdog_data = read_json(self.runtime_root / "watchdog.json", None)
        watchdog_row = None
        recovery_epoch = None
        watchdog_state = None
        watchdog_reason = None
        progress_observation_state = "unavailable"

        if isinstance(watchdog_data, dict):
            projects = watchdog_data.get("projects")
            if isinstance(projects, dict):
                watchdog_row = projects.get(project_id)

        if isinstance(watchdog_row, dict):
            recovery_epoch = watchdog_row.get("recovery_epoch")
            watchdog_state = (
                watchdog_row.get("last_diagnosis")
                or watchdog_row.get("classification")
                or watchdog_row.get("state")
            )
            watchdog_reason = watchdog_row.get("reason") or watchdog_row.get("last_error")
            if not watchdog_reason and isinstance(watchdog_row.get("attempts"), dict):
                for att in reversed(list(watchdog_row["attempts"].values())):
                    if isinstance(att, dict) and att.get("reason"):
                        watchdog_reason = att.get("reason")
                        break
            if not watchdog_reason and isinstance(watchdog_row.get("stall"), dict):
                watchdog_reason = watchdog_row["stall"].get("reason")

            last_checked_at = watchdog_row.get("last_checked_at")
            last_check_epoch = watchdog_row.get("last_check_epoch")
            last_check = 0.0
            if last_check_epoch is not None:
                try:
                    last_check = float(last_check_epoch)
                except (ValueError, TypeError):
                    pass
            elif last_checked_at is not None:
                dt = parse_utc(last_checked_at)
                if dt is not None:
                    last_check = dt.timestamp()

            now = time.time()
            # Stale after 120 seconds without fresh watchdog check
            if last_check > 0 and (now - last_check < 120):
                progress_observation_state = "authoritative"
            else:
                progress_observation_state = "stale"

        # 4. Read owner gate details
        owner_gate = None
        for cand in [
            base.get("gate"),
            watchdog_row.get("owner_gate") if isinstance(watchdog_row, dict) else None,
            _latest_owner_gate(self.runtime_root, project_id),
        ]:
            if isinstance(cand, dict):
                owner_gate = copy.deepcopy(cand)
                if not owner_gate.get("state"):
                    owner_gate["state"] = "owner_gate"
                break

        result = {
            "schema_version": 1,
            "project_id": project_id,
            "revision": identity.get("revision"),
            "lifecycle_state": identity.get("lifecycle_state"),
            "task_id": identity.get("task_id"),
            "progress_observation_state": progress_observation_state,
            "watchdog_state": watchdog_state,
            "watchdog_reason": watchdog_reason,
            "recovery_epoch": recovery_epoch,
            "control_identity": identity,
            "controls": controls,
            "active_roles": base.get("active_roles") or [],
            "active_execution": base.get("active_execution"),
            "owner_gate": owner_gate,
            "owner_control": base.get("owner_control") or {},
            "telemetry": snapshot.get("telemetry") or {},
            "git": identity.get("git") or snapshot.get("git") or {},
            "next_status": snapshot.get("next_status"),
        }
        return result

    def overview(
        self,
        summary: dict[str, Any],
        config: Optional[dict[str, Any]] = None,
        principal: Optional[MobileDevicePrincipal] = None,
    ) -> dict[str, Any]:
        """Compose mobile overview containing all configured/active projects."""
        projects_list = []
        raw_projects = summary.get("projects") if isinstance(summary, dict) else None
        if isinstance(raw_projects, list):
            for row in raw_projects:
                if isinstance(row, dict) and row.get("project_id"):
                    projects_list.append(self.project_view(row, principal=principal))

        daemon_info = read_json(self.runtime_root / "daemon.json", {})
        watchdog_info = read_json(self.runtime_root / "watchdog.json", {})

        return {
            "schema_version": 1,
            "observed_at": summary.get("observed_at") if isinstance(summary, dict) else None,
            "daemon": {
                "state": daemon_info.get("state", "unknown"),
                "pid": daemon_info.get("pid"),
                "last_tick_at": daemon_info.get("last_tick_at"),
            },
            "watchdog": {
                "degraded": bool(watchdog_info.get("degraded", False)),
            },
            "projects": projects_list,
        }
