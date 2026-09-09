"""Persistent explicit-owner control state for CCP6."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    return value.strip()


class OwnerControlStore:
    """Durable pause/action ledger; assistant text never writes this store."""

    def __init__(self, runtime_root: Path | str) -> None:
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.path = self.runtime_root / "owner-control.json"
        self._lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "projects": {}, "actions": {}}
    def _load(self) -> dict[str, Any]:
        data = read_json(self.path, None)
        if not isinstance(data, dict):
            return self._empty()
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        actions = data.get("actions") if isinstance(data.get("actions"), dict) else {}
        return {"version": 1, "projects": projects, "actions": actions}

    def project_state(self, project_id: str) -> dict[str, Any]:
        project_id = _require_text(project_id, "project_id")
        with self._lock:
            data = self._load()
            state = data["projects"].get(project_id)
        if not isinstance(state, dict):
            return {"project_id": project_id, "paused": False,
                    "suppress_static_starts": False}
        result = dict(state)
        result.setdefault("project_id", project_id)
        result.setdefault("paused", False)
        result.setdefault("suppress_static_starts", False)
        return result

    def is_paused(self, project_id: str) -> bool:
        return bool(self.project_state(project_id).get("paused"))

    def suppress_static_starts(self, project_id: str) -> bool:
        return bool(self.project_state(project_id).get("suppress_static_starts"))

    def adopt_runtime_control(self, project_id: str, *, action_id: str, action: str) -> dict[str, Any]:
        """Suppress legacy static starts without changing the current pause state."""
        project_id = _require_text(project_id, "project_id")
        action_id = _require_text(action_id, "action_id")
        action = _require_text(action, "action")
        with self._lock:
            data = self._load()
            previous = data["projects"].get(project_id)
            state = dict(previous) if isinstance(previous, dict) else {"project_id": project_id, "paused": False}
            state.update({"project_id": project_id, "suppress_static_starts": True,
                          "updated_at": utc_now_iso(), "last_action_id": action_id,
                          "last_action": action})
            data["projects"][project_id] = state
            write_json(self.path, data, indent=2)
            return dict(state)
    def set_paused(self, project_id: str, paused: bool, *, action_id: str,
                   action: str, reason: Optional[str] = None) -> dict[str, Any]:
        project_id = _require_text(project_id, "project_id")
        action_id = _require_text(action_id, "action_id")
        action = _require_text(action, "action")
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
                "last_action_id": action_id,
                "last_action": action,
            })
            if paused:
                state["paused_at"] = now
            else:
                state["resumed_at"] = now
            if reason:
                state["reason"] = str(reason)
            data["projects"][project_id] = state
            write_json(self.path, data, indent=2)
            return dict(state)
    def action(self, action_id: str) -> Optional[dict[str, Any]]:
        action_id = _require_text(action_id, "action_id")
        with self._lock:
            record = self._load()["actions"].get(action_id)
        return dict(record) if isinstance(record, dict) else None

    def record_action(self, action_id: str, project_id: str, action: str,
                      state: str, **fields: Any) -> dict[str, Any]:
        action_id = _require_text(action_id, "action_id")
        project_id = _require_text(project_id, "project_id")
        action = _require_text(action, "action")
        state = _require_text(state, "state")
        with self._lock:
            data = self._load()
            existing = data["actions"].get(action_id)
            if isinstance(existing, dict):
                if existing.get("project_id") != project_id or existing.get("action") != action:
                    raise ValueError("action_id already belongs to a different owner action")
                record = dict(existing)
            else:
                record = {"action_id": action_id, "project_id": project_id,
                          "action": action, "created_at": utc_now_iso()}
            record.update(fields)
            record["state"] = state
            record["updated_at"] = utc_now_iso()
            data["actions"][action_id] = record
            write_json(self.path, data, indent=2)
            return dict(record)

    def latest_action_for_gate(self, gate_request_id: str) -> Optional[dict[str, Any]]:
        gate_request_id = _require_text(gate_request_id, "gate_request_id")
        with self._lock:
            actions = self._load()["actions"]
        matches = [dict(v) for v in actions.values() if isinstance(v, dict)
                   and v.get("gate_request_id") == gate_request_id]
        return max(matches, key=lambda r: str(r.get("updated_at") or "")) if matches else None
