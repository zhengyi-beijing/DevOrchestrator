"""Conversation Control Plane service logic, independent of HTTP transport."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.control.binding_resolver import resolve_effective_projects
from dev_orchestrator.control.store import ConversationControlStore, ControlConflictError
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.monitor.telemetry import extract_task_id
from dev_orchestrator.storage.json_store import read_json


class ControlNotFoundError(LookupError):
    """Requested project/session identity does not exist."""


class ControlPlaneService:
    def __init__(
        self,
        config_path: Path | str,
        runtime_root: Path | str,
        control_store: ConversationControlStore,
        bridge_store: BrowserBridgeStore,
        transition_executor: Optional[TransitionExecutor] = None,
        owner_store: Optional[OwnerControlStore] = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.runtime_root = Path(runtime_root)
        self.control_store = control_store
        self.bridge_store = bridge_store
        self.owner_store = owner_store or OwnerControlStore(self.runtime_root)
        self.transition_executor = transition_executor or TransitionExecutor(
            self.runtime_root, owner_store=self.owner_store
        )

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

    def _runtime_snapshot(self, project_id: str) -> dict[str, Any]:
        value = read_json(self.runtime_root / "projects" / (project_id + ".json"), {})
        return value if isinstance(value, dict) else {}

    def _latest_owner_gate(self, project_id: str) -> Optional[dict[str, Any]]:
        data = read_json(self.runtime_root / "websol-decisions.json", {})
        decisions = data.get("decisions") if isinstance(data, dict) else None
        if not isinstance(decisions, dict):
            return None
        matches = [dict(record) for record in decisions.values()
                   if isinstance(record, dict) and record.get("project_id") == project_id
                   and record.get("disposition") == "owner_gate"
                   and isinstance(record.get("request_id"), str)]
        if not matches:
            return None
        gate = max(matches, key=lambda row: str(row.get("consumed_at") or ""))
        owner_action = self.owner_store.latest_action_for_gate(gate["request_id"])
        gate["owner_state"] = ("approved" if owner_action and owner_action.get("action") == "approve_next_stage"
                               and owner_action.get("state") == "accepted" else "pending")
        return gate

    @staticmethod
    def _snapshot_task_id(snapshot: dict[str, Any]) -> Optional[str]:
        telemetry = snapshot.get("telemetry")
        if (isinstance(telemetry, dict) and isinstance(telemetry.get("task_id"), str)
                and telemetry.get("task_id").strip()):
            return telemetry.get("task_id").strip()
        return extract_task_id(snapshot.get("next_title"))

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.control_store.list_sessions()

    def list_bindings(self) -> list[dict[str, Any]]:
        return self.control_store.list_bindings()
    def list_projects(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for project in self._effective_projects():
            project_id = str(project.get("project_id") or "")
            route = self._route(project)
            session = self.control_store.session_status(*route) if route is not None else None
            snapshot = self._runtime_snapshot(project_id)
            task_id = self._snapshot_task_id(snapshot)
            git = snapshot.get("git") if isinstance(snapshot.get("git"), dict) else {}
            gate = self._latest_owner_gate(project_id)
            owner_state = self.owner_store.project_state(project_id)
            gate_matches = bool(gate and gate.get("branch") == git.get("branch")
                                and gate.get("head") == git.get("head"))
            gate_pending = bool(gate and gate.get("owner_state") == "pending")
            ready = snapshot.get("state") == "READY_TO_RUN" and bool(task_id) and not bool(git.get("dirty"))
            rows.append({
                "project_id": project_id, "name": project.get("name"),
                "conversation_binding": project.get("conversation_binding"),
                "conversation_binding_source": project.get("conversation_binding_source", "none"),
                "orchestration_ready": bool(project.get("orchestration_ready")),
                "binding_state": "unbound" if route is None else ("bound" if session and session.get("state") == "live" else "stale"),
                "last_seen_at": session.get("last_seen_at") if session else None,
                "project_state": snapshot.get("state"), "current_task_id": task_id,
                "branch": git.get("branch"), "head": git.get("head"), "dirty": git.get("dirty"),
                "owner_paused": bool(owner_state.get("paused")), "owner_gate": gate,
                "can_approve_next_stage": gate_pending and gate_matches and not bool(git.get("dirty")),
                "can_start_current_task": ready and not gate_pending,
                "can_stop": True,
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

    def _require_bound_owner_caller(self, project_id: str, adapter: str, binding_id: str) -> None:
        current = self._effective_project(project_id)
        route = self._route(current)
        if route != (adapter, binding_id):
            raise ControlConflictError("owner action must originate from the project's bound conversation")
        self._require_live_target(adapter, binding_id)

    def _require_fresh_identity(self, project_id: str, branch: str, head: str):
        project = self._configured_project(project_id)
        truth = read_repository_truth(project.get("repo_path") or "")
        if not truth.valid:
            raise ControlConflictError("repository truth unavailable")
        if truth.branch != branch or truth.head != head:
            raise ControlConflictError("repository changed after owner action was rendered")
        return truth

    def owner_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "").strip()
        action = str(payload.get("action") or "").strip()
        action_id = str(payload.get("action_id") or "").strip()
        adapter = str(payload.get("adapter") or "").strip()
        binding_id = str(payload.get("binding_id") or "").strip()
        branch = str(payload.get("expected_branch") or "").strip()
        head = str(payload.get("expected_head") or "").strip()
        if action not in {"stop", "start_current_task", "approve_next_stage"}:
            raise ValueError("unsupported owner action")
        if not all((project_id, action_id, adapter, binding_id, branch, head)):
            raise ValueError("owner action requires project, action id, bound conversation, branch and head")
        existing = self.owner_store.action(action_id)
        identity = {
            "project_id": project_id, "action": action, "adapter": adapter,
            "binding_id": binding_id, "expected_branch": branch, "expected_head": head,
        }
        if action == "start_current_task":
            identity["expected_task_id"] = str(payload.get("expected_task_id") or "").strip()
        if action == "approve_next_stage":
            identity["gate_request_id"] = str(payload.get("gate_request_id") or "").strip()
            identity["gate_task_id"] = str(payload.get("gate_task_id") or "").strip()
        record_identity = {key: value for key, value in identity.items()
                           if key not in {"project_id", "action"}}
        if existing is not None:
            for key, value in identity.items():
                if existing.get(key) != value:
                    raise ControlConflictError("action_id replay identity mismatch")
            if existing.get("state") == "accepted":
                return {"owner_action": existing, "idempotent": True}
            if action != "start_current_task" or existing.get("state") != "launch_requested":
                raise ControlConflictError("owner action is not safely replayable")
        self._require_bound_owner_caller(project_id, adapter, binding_id)
        if action == "stop":
            self.transition_executor.set_owner_paused(
                project_id, True, action_id=action_id, action=action,
                reason="explicit owner stop/pause")
            record = self.owner_store.record_action(
                action_id, project_id, action, "accepted", effect="pause_future_launches",
                active_worker_unchanged=True, adapter=adapter, binding_id=binding_id,
                expected_branch=branch, expected_head=head,
            )
            return {"owner_action": record, "idempotent": False}

        truth = self._require_fresh_identity(project_id, branch, head)
        if self.bridge_store.has_active_claim(adapter, binding_id):
            raise ControlConflictError(
                "project has an active claimed Web Sol request; owner start/approve is blocked")
        gate = self._latest_owner_gate(project_id)
        if action == "approve_next_stage":
            if truth.dirty:
                raise ControlConflictError("repository is dirty; owner gate approval is blocked")
            gate_request_id = str(payload.get("gate_request_id") or "").strip()
            gate_task_id = str(payload.get("gate_task_id") or "").strip()
            if not gate or not gate_request_id or not gate_task_id:
                raise ControlConflictError("no exact pending owner gate is available")
            if gate.get("owner_state") != "pending":
                raise ControlConflictError("owner gate is already approved")
            if gate.get("request_id") != gate_request_id or gate.get("task_id") != gate_task_id:
                raise ControlConflictError("owner gate identity mismatch")
            if gate.get("branch") != branch or gate.get("head") != head:
                raise ControlConflictError("owner gate repository identity mismatch")
            self.transition_executor.set_owner_paused(
                project_id, False, action_id=action_id, action=action)
            record = self.owner_store.record_action(
                action_id, project_id, action, "accepted", gate_request_id=gate_request_id,
                gate_task_id=gate_task_id, adapter=adapter, binding_id=binding_id,
                expected_branch=branch, expected_head=head, effect="gate_approved_no_worker_started",
            )
            return {"owner_action": record, "idempotent": False}

        # start_current_task never bypasses a still-pending owner gate.
        if gate and gate.get("owner_state") == "pending":
            raise ControlConflictError("pending owner gate must be explicitly approved before start")
        expected_task_id = str(payload.get("expected_task_id") or "").strip()
        if not expected_task_id:
            raise ValueError("start_current_task requires expected_task_id")
        request_id = "owner-control:" + action_id
        self.transition_executor.adopt_owner_control(
            project_id, action_id=action_id, action=action)
        if existing is not None and existing.get("state") == "launch_requested":
            prior = self.transition_executor.state().get("executions", {}).get(request_id)
            if isinstance(prior, dict):
                expected = {"project_id": project_id, "source_kind": "owner_control",
                            "task_id": expected_task_id, "branch": branch, "head": head}
                if any(prior.get(key) != value for key, value in expected.items()):
                    raise ControlConflictError("owner launch ledger identity mismatch")
                if prior.get("state") == "blocked":
                    self.owner_store.record_action(action_id, project_id, action, "rejected",
                                                   reason=str(prior.get("reason") or "launch blocked"), **record_identity)
                    raise ControlConflictError(str(prior.get("reason") or "owner launch was blocked"))
                self.transition_executor.set_owner_paused(
                project_id, False, action_id=action_id, action=action)
                record = self.owner_store.record_action(
                    action_id, project_id, action, "accepted", source_request_id=request_id,
                    backend_id=prior.get("backend_id"), effect="worker_launch", **record_identity)
                launch_view = {"project_id": project_id, "source_request_id": request_id,
                               "task_id": expected_task_id, "backend_id": prior.get("backend_id"),
                               "state": prior.get("state")}
                return {"owner_action": record, "launch": launch_view, "idempotent": True}
        if existing is None:
            self.owner_store.record_action(action_id, project_id, action, "launch_requested", **record_identity)
        try:
            launch = self.transition_executor.owner_control_launch(
                self.config_path, project_id=project_id, action_id=action_id,
                expected_task_id=expected_task_id, expected_branch=branch, expected_head=head,
            )
        except ValueError as exc:
            self.owner_store.record_action(action_id, project_id, action, "rejected", reason=str(exc), **record_identity)
            raise ControlConflictError(str(exc)) from exc
        self.owner_store.set_paused(project_id, False, action_id=action_id, action=action)
        record = self.owner_store.record_action(
            action_id, project_id, action, "accepted", source_request_id=launch.source_request_id,
            backend_id=launch.backend_id, effect="worker_launch", **record_identity)
        return {"owner_action": record, "launch": launch.__dict__, "idempotent": False}

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
