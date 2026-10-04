"""Durable owner pause state enforced at the final Worker launch gate."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json


class OwnerControlStore:
    def __init__(self, runtime_root: Path | str) -> None:
        self.path = Path(runtime_root) / "owner-control.json"
        self._lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "projects": {}}

    def _load(self) -> dict[str, Any]:
        value = read_json(self.path, None)
        projects = value.get("projects") if isinstance(value, dict) else None
        return {"version": 1, "projects": projects if isinstance(projects, dict) else {}}

    def project_state(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            value = self._load()["projects"].get(project_id)
        result = dict(value) if isinstance(value, dict) else {"project_id": project_id}
        result.setdefault("paused", False)
        result.setdefault("suppress_static_starts", False)
        return result

    def is_paused(self, project_id: str) -> bool:
        return bool(self.project_state(project_id).get("paused"))

    def suppress_static_starts(self, project_id: str) -> bool:
        return bool(self.project_state(project_id).get("suppress_static_starts"))

    def set_paused(
        self, project_id: str, paused: bool, *, command_id: str, action: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if not project_id or not command_id:
            raise ValueError("project_id and command_id are required")
        now = utc_now_iso()
        with self._lock:
            data = self._load()
            previous = data["projects"].get(project_id)
            state = dict(previous) if isinstance(previous, dict) else {}
            state.update({
                "project_id": project_id,
                "paused": bool(paused),
                "suppress_static_starts": True,
                "updated_at": now,
                "last_command_id": command_id,
                "last_action": action,
            })
            state["paused_at" if paused else "resumed_at"] = now
            if not paused:
                if isinstance(state.get("owner_gate"), dict):
                    state["owner_gate"]["active"] = False
                state["owner_gate_id"] = None
            if reason:
                state["reason"] = reason
            data["projects"][project_id] = state
            write_json(self.path, data, indent=2)
            return dict(state)

    def record_gate(
        self,
        project_id: str,
        gate_id: str,
        reason: str | None = None,
        active: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Record a durable owner gate for a project.

        When active is True, sets paused=True and suppress_static_starts=True to inhibit
        background actuation until explicitly resumed or approved.
        """
        if not project_id or not gate_id:
            raise ValueError("project_id and gate_id are required")
        now = utc_now_iso()
        with self._lock:
            data = self._load()
            previous = data["projects"].get(project_id)
            state = dict(previous) if isinstance(previous, dict) else {}
            gate_record = {
                "gate_id": gate_id,
                "reason": reason or "",
                "active": bool(active),
                "created_at": state.get("owner_gate", {}).get("created_at") if isinstance(state.get("owner_gate"), dict) else now,
                "updated_at": now,
                **kwargs,
            }
            state.update({
                "project_id": project_id,
                "owner_gate": gate_record if active else None,
                "owner_gate_id": gate_id if active else None,
                "updated_at": now,
            })
            if active:
                state["paused"] = True
                state["suppress_static_starts"] = True
                state["paused_at"] = now
                state["last_action"] = "owner_gate"
                state["last_command_id"] = gate_id
                if reason:
                    state["reason"] = reason
            data["projects"][project_id] = state
            write_json(self.path, data, indent=2)
            return dict(state)

    def latest_gate(self, project_id: str) -> dict[str, Any] | None:
        """Return the active owner gate dictionary for a project if one exists."""
        state = self.project_state(project_id)
        gate = state.get("owner_gate")
        return dict(gate) if isinstance(gate, dict) and gate.get("active") is not False else None
