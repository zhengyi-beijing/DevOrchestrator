"""Conversation Control Plane service logic, independent of HTTP transport."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.control.binding_resolver import resolve_effective_projects
from dev_orchestrator.control.store import ConversationControlStore, ControlConflictError


class ControlNotFoundError(LookupError):
    """Requested project/session identity does not exist."""


class ControlPlaneService:
    def __init__(
        self,
        config_path: Path | str,
        runtime_root: Path | str,
        control_store: ConversationControlStore,
        bridge_store: BrowserBridgeStore,
    ) -> None:
        self.config_path = Path(config_path)
        self.runtime_root = Path(runtime_root)
        self.control_store = control_store
        self.bridge_store = bridge_store

    def _configured_projects(self) -> list[dict[str, Any]]:
        return list(load_projects_config(self.config_path).get("projects") or [])
    def _effective_projects(self) -> list[dict[str, Any]]:
        return resolve_effective_projects(self._configured_projects(), self.control_store)

    def _configured_project(self, project_id: str) -> dict[str, Any]:
        for project in self._configured_projects():
            if project.get("project_id") == project_id:
                return project
        raise ControlNotFoundError(f"unknown project {project_id!r}")

    def _effective_project(self, project_id: str) -> dict[str, Any]:
        for project in self._effective_projects():
            if project.get("project_id") == project_id:
                return project
        raise ControlNotFoundError(f"unknown project {project_id!r}")

    @staticmethod
    def _route(project: dict[str, Any]) -> Optional[tuple[str, str]]:
        binding = project.get("conversation_binding")
        if not project.get("orchestration_ready") or not isinstance(binding, dict):
            return None
        adapter = binding.get("adapter")
        binding_id = binding.get("binding_id")
        if not isinstance(adapter, str) or not adapter.strip():
            return None
        if not isinstance(binding_id, str) or not binding_id.strip():
            return None
        return adapter.strip(), binding_id.strip()

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.control_store.list_sessions()

    def list_bindings(self) -> list[dict[str, Any]]:
        return self.control_store.list_bindings()
    def list_projects(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for project in self._effective_projects():
            route = self._route(project)
            session = None
            if route is not None:
                session = self.control_store.session_status(route[0], route[1])
            rows.append({
                "project_id": project.get("project_id"),
                "name": project.get("name"),
                "conversation_binding": project.get("conversation_binding"),
                "conversation_binding_source": project.get("conversation_binding_source", "none"),
                "orchestration_ready": bool(project.get("orchestration_ready")),
                "binding_state": (
                    "unbound" if route is None else
                    ("bound" if session and session.get("state") == "live" else "stale")
                ),
                "last_seen_at": session.get("last_seen_at") if session else None,
            })
        return rows

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self.control_store.heartbeat(
            payload.get("adapter"), payload.get("binding_id"),
            title=payload.get("title"), url=payload.get("url"),
            tab_instance_id=payload.get("tab_instance_id"),
        )
        project_id = None
        for project in self._effective_projects():
            route = self._route(project)
            if route == (session["adapter"], session["binding_id"]):
                project_id = project.get("project_id")
                break
        return {"session": session, "project_id": project_id}
    def _require_live_target(self, adapter: str, binding_id: str) -> None:
        status = self.control_store.session_status(adapter, binding_id)
        if status.get("state") != "live":
            raise ControlConflictError("target conversation is not currently live")

    def _assert_route_available(self, project_id: str, adapter: str, binding_id: str) -> None:
        for other in self._effective_projects():
            other_id = str(other.get("project_id") or "")
            if other_id == project_id:
                continue
            if self._route(other) == (adapter, binding_id):
                raise ControlConflictError(
                    f"conversation is already bound to project {other_id}"
                )

    def _guard_no_active_claim(self, project_id: str) -> None:
        current = self._effective_project(project_id)
        route = self._route(current)
        if route is None:
            return
        if self.bridge_store.has_active_claim(route[0], route[1]):
            raise ControlConflictError(
                "project has an active claimed Web Sol request; rebind/unbind is blocked"
            )

    def bind(self, project_id: str, adapter: str, binding_id: str) -> dict[str, Any]:
        self._configured_project(project_id)
        self._require_live_target(adapter, binding_id)
        current = self._effective_project(project_id)
        current_route = self._route(current)
        if current_route is not None and current_route != (adapter, binding_id):
            raise ControlConflictError("project already has an effective binding; use rebind")
        self._assert_route_available(project_id, adapter, binding_id)
        return self.control_store.bind(project_id, adapter, binding_id)
    def rebind(self, project_id: str, adapter: str, binding_id: str) -> dict[str, Any]:
        self._configured_project(project_id)
        self._require_live_target(adapter, binding_id)
        self._guard_no_active_claim(project_id)
        self._assert_route_available(project_id, adapter, binding_id)
        return self.control_store.rebind(project_id, adapter, binding_id)

    def unbind(self, project_id: str) -> dict[str, Any]:
        self._configured_project(project_id)
        self._guard_no_active_claim(project_id)
        return self.control_store.unbind(project_id)
