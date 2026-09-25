"""Fail-closed owner gate authority resolution combining watchdog, planner, and control store."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.surface import latest_owner_gate, project_identity


def resolve_owner_gate_authority(
    runtime_root: Path | str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Resolve owner gate authority combining watchdog/planner gates and owner store state.

    Unresolved authority fails closed to pending=True.
    Returns:
        resolved: bool
        pending: bool
        gate_id: str | None
        gate_source: str | None
        paused: bool
        reason: str
    """
    runtime = Path(runtime_root)
    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
    if not project_id:
        return {
            "resolved": False,
            "pending": True,
            "gate_id": None,
            "gate_source": None,
            "paused": False,
            "reason": "missing_project_id",
        }

    try:
        paused = OwnerControlStore(runtime).is_paused(project_id)
    except Exception as exc:
        return {
            "resolved": False,
            "pending": True,
            "gate_id": None,
            "gate_source": None,
            "paused": False,
            "reason": f"owner_store_unreadable: {exc}",
        }

    try:
        gate = latest_owner_gate(runtime, project_id)
    except Exception as exc:
        return {
            "resolved": False,
            "pending": True,
            "gate_id": None,
            "gate_source": None,
            "paused": paused,
            "reason": f"gate_lookup_failed: {exc}",
        }

    try:
        identity = project_identity(snapshot, runtime)
        id_gate = identity.get("gate_id")
        id_paused = bool(identity.get("paused"))
    except Exception as exc:
        return {
            "resolved": False,
            "pending": True,
            "gate_id": None,
            "gate_source": None,
            "paused": paused,
            "reason": f"identity_projection_failed: {exc}",
        }

    eff_paused = paused or id_paused
    gate_id = None
    gate_source = None
    if isinstance(gate, dict):
        gate_id = gate.get("gate_id") or gate.get("id") or id_gate
        gate_source = gate.get("gate_source") or "watchdog"
    elif id_gate:
        gate_id = id_gate
        gate_source = "identity"

    if gate_id is not None:
        return {
            "resolved": True,
            "pending": True,
            "gate_id": str(gate_id),
            "gate_source": gate_source,
            "paused": eff_paused,
            "reason": "owner_gate_present",
        }

    if eff_paused:
        return {
            "resolved": True,
            "pending": True,
            "gate_id": None,
            "gate_source": None,
            "paused": True,
            "reason": "project_paused",
        }

    return {
        "resolved": True,
        "pending": False,
        "gate_id": None,
        "gate_source": None,
        "paused": False,
        "reason": "no_pending_gate",
    }
