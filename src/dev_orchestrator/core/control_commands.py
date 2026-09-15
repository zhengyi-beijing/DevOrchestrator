"""Stateless local control-command inbox for project-scoped actions."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Optional
from uuid import uuid4

from dev_orchestrator.config import load_projects_config
from dev_orchestrator.accounting import ExecutionRecorder
from dev_orchestrator.control.command_store import (
    CONTROL_ACTIONS,
    ControlCommandStore,
    safe_command_id,
)
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.store import ConversationConflictError, ConversationControlStore
from dev_orchestrator.control.surface import validate_expected
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

CONTROL_DIR = "control"
CONTROL_VERSION = 1
_SUPPORTED_ACTIONS = CONTROL_ACTIONS


def _nonblank(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _safe_command_id(value: Any) -> str | None:
    return safe_command_id(value)


def _continuation_id(source_request_id: str) -> str:
    digest = hashlib.sha256(source_request_id.encode("utf-8")).hexdigest()[:24]
    return "auto-" + digest


def _paths(runtime_root: Path | str) -> tuple[Path, Path]:
    root = Path(runtime_root) / CONTROL_DIR
    return root / "inbox", root / "history"


def submit_control_command(
    runtime_root: Path | str,
    project_id: str,
    action: str,
    *,
    command_id: Optional[str] = None,
    gate_id: Optional[str] = None,
    expected: Optional[dict[str, Any]] = None,
    target: Optional[dict[str, Any]] = None,
    source: str = "local_client",
) -> dict[str, Any]:
    project = _nonblank(project_id)
    if project is None:
        raise ValueError("project_id must be nonblank")
    if action not in _SUPPORTED_ACTIONS:
        raise ValueError("unsupported control action")
    if command_id is not None:
        cid = _safe_command_id(command_id)
        if cid is None:
            raise ValueError(f"invalid command_id: {command_id!r}")
    else:
        cid = str(uuid4())
    gate = _nonblank(gate_id)
    if gate_id is not None and gate is None:
        raise ValueError("gate_id must be nonblank when present")
    target_value = dict(target or {})
    if gate is not None:
        target_value["gate_id"] = gate
    value = {
        "schema_version": 1,
        "command_id": cid,
        "project_id": project,
        "action": action,
        "expected": dict(expected or {}),
        "target": target_value,
    }
    return ControlCommandStore(runtime_root).submit(value, source=source)


def latest_control_result(runtime_root: Path | str, project_id: str) -> dict[str, Any] | None:
    return ControlCommandStore(runtime_root).latest_for_project(project_id)


class ControlCommandCoordinator:
    """Daemon-owned consumer for atomic project control commands."""

    def __init__(
        self, runtime_root: Path | str, planner: AIPlannerCoordinator | None = None,
        accounting: ExecutionRecorder | None = None,
        *, owner_store: OwnerControlStore | None = None,
        conversation_store: ConversationControlStore | None = None,
        bridge_store: Any = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.inbox, self.history = _paths(runtime_root)
        self.command_store = ControlCommandStore(runtime_root)
        self.planner = planner
        self.accounting = accounting
        self.owner_store = owner_store or OwnerControlStore(runtime_root)
        self.conversation_store = conversation_store or ConversationControlStore(runtime_root)
        self.bridge_store = bridge_store
    @staticmethod
    def _snapshot_map(summary: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(summary, dict) or not isinstance(summary.get("projects"), list):
            return {}
        return {
            str(row.get("project_id")): row
            for row in summary["projects"]
            if isinstance(row, dict) and row.get("project_id")
        }

    @staticmethod
    def _project_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(row.get("project_id")): row
            for row in config.get("projects") or []
            if isinstance(row, dict) and row.get("project_id")
        }

    def advance(self, config_path: Path | str, summary: Any, executor: Any) -> list[dict[str, Any]]:
        config = load_projects_config(config_path)
        projects = self._project_map(config)
        snapshots = self._snapshot_map(summary)
        outcomes = self.command_store.repair_corruption()
        outcomes.extend(self._sync_planner_terminals())
        outcomes.extend(self._resume_ready_plans(projects, snapshots, executor))
        outcomes.extend(self._resume_decision_handoffs(projects, snapshots, executor))
        for path in self.command_store.pending_paths():
            record = read_json(path, None)
            if not isinstance(record, dict):
                outcomes.append(self.command_store.quarantine(path, "unreadable control command record"))
                continue
            raw_id = _safe_command_id(record.get("command_id")) if isinstance(record, dict) else None
            existing = read_json(self.history / (raw_id + ".json"), None) if raw_id else None
            if isinstance(existing, dict):
                path.unlink(missing_ok=True)
                outcomes.append(existing)
                continue
            outcome = self._consume_one(record, projects, snapshots, executor)
            outcome = self.command_store.settle(path, outcome)
            outcomes.append(outcome)
        return outcomes
    def _resume_decision_handoffs(
        self, projects: dict[str, dict[str, Any]], snapshots: dict[str, dict[str, Any]], executor: Any,
    ) -> list[dict[str, Any]]:
        if self.planner is None:
            return []
        raw = executor.state(); executions = raw.get("executions") if isinstance(raw, dict) else None
        if not isinstance(executions, dict):
            return []
        outcomes: list[dict[str, Any]] = []
        for source_id, row in sorted(executions.items()):
            if not isinstance(row, dict) or row.get("state") != "handoff" or row.get("outcome") != "planning_required" or row.get("handoff_consumed") is True:
                continue
            project_id = _nonblank(row.get("project_id")); next_task_id = _nonblank(row.get("next_task_id"))
            project = projects.get(project_id or ""); snapshot = snapshots.get(project_id or "")
            if project is None or snapshot is None or next_task_id is None:
                continue
            if self.owner_store.is_paused(project_id or ""):
                continue
            telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
            staged_successor = _nonblank(row.get("staged_successor"))
            is_staged = staged_successor is not None
            if not is_staged:
                if _nonblank(telemetry.get("task_id")) != next_task_id or "PENDING DESIGN" not in str(snapshot.get("next_status") or "").upper():
                    continue
            else:
                if (
                    _nonblank(telemetry.get("task_id")) != _nonblank(row.get("task_id"))
                    or not re.search(r"\bCOMPLETED?\b", str(snapshot.get("next_status") or ""), re.IGNORECASE)
                    or staged_successor != next_task_id
                ):
                    continue
            continuation_id = _continuation_id(source_id)
            history_path = self.history / (continuation_id + ".json")
            existing = read_json(history_path, None)
            if isinstance(existing, dict) and _nonblank(existing.get("plan_id")):
                executor.mark_handoff_consumed(source_id, continuation_id, str(existing["plan_id"]))
                outcomes.append(existing); continue
            if is_staged:
                plan_id, reason = self.planner.start_deferred(project, snapshot, continuation_id, row)
            else:
                plan_id, reason = self.planner.start(project, snapshot, continuation_id)
            now = utc_now_iso()
            if plan_id is None:
                executor.mark_handoff_blocked(source_id, "automatic planner handoff failed: " + reason)
                outcome = {"version":CONTROL_VERSION,"command_id":continuation_id,"project_id":project_id,"action":"continue","state":"blocked","source":"automatic_review_handoff","parent_request_id":source_id,"reason":reason,"processed_at":now}
            else:
                outcome = {"version":CONTROL_VERSION,"command_id":continuation_id,"project_id":project_id,"action":"continue","state":"accepted","source":"automatic_review_handoff","parent_request_id":source_id,"lifecycle_action":"plan","plan_id":plan_id,"reason":reason,"processed_at":now}
                write_json(history_path, outcome, indent=2)
                self.command_store.audit("command_updated", outcome)
                executor.mark_handoff_consumed(source_id, continuation_id, plan_id)
            if plan_id is None:
                write_json(history_path, outcome, indent=2)
                self.command_store.audit("command_updated", outcome)
            outcomes.append(outcome)
        return outcomes

    def _sync_planner_terminals(self) -> list[dict[str, Any]]:
        if self.planner is None:
            return []
        outcomes: list[dict[str, Any]] = []
        for plan in self.planner.terminal_records():
            command_id = _safe_command_id(plan.get("command_id")); plan_id = _nonblank(plan.get("plan_id"))
            if command_id is None or plan_id is None:
                continue
            history = read_json(self.history / (command_id + ".json"), {})
            if not isinstance(history, dict): history = {}
            plan_state = str(plan.get("state") or "failed")
            history.update({"state": "owner_gate" if plan_state == "owner_gate" else "blocked",
                            "lifecycle_action":"plan", "reason":str(plan.get("reason") or plan_state),
                            "processed_at":utc_now_iso(), "plan_state":plan_state})
            write_json(self.history / (command_id + ".json"), history, indent=2)
            self.command_store.audit("command_updated", history)
            self.planner.mark_control_synced(plan_id)
            outcomes.append(history)
        return outcomes

    def _resume_ready_plans(
        self, projects: dict[str, dict[str, Any]], snapshots: dict[str, dict[str, Any]], executor: Any,
    ) -> list[dict[str, Any]]:
        if self.planner is None:
            return []
        outcomes: list[dict[str, Any]] = []
        for plan in self.planner.ready_records():
            project_id = _nonblank(plan.get("project_id"))
            command_id = _safe_command_id(plan.get("command_id"))
            plan_id = _nonblank(plan.get("plan_id"))
            if project_id is None or command_id is None or plan_id is None:
                continue
            if self.owner_store.is_paused(project_id):
                continue
            project = projects.get(project_id); snapshot = snapshots.get(project_id)
            if project is None or snapshot is None:
                continue
            launch_snapshot = snapshot
            if snapshot.get("state") != "READY_TO_RUN":
                telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
                git = snapshot.get("git") if isinstance(snapshot.get("git"), dict) else {}
                approved_idle = (
                    snapshot.get("state") == "IDLE"
                    and "READY_TO_RUN" in str(snapshot.get("next_status") or "").upper()
                    and _nonblank(telemetry.get("task_id")) == _nonblank(plan.get("task_id"))
                    and _nonblank(git.get("head")) == _nonblank(plan.get("ready_head"))
                    and _nonblank(plan.get("ready_head")) is not None
                )
                if not approved_idle:
                    continue
                launch_snapshot = dict(snapshot)
                launch_snapshot["state"] = "READY_TO_RUN"
            source_id = command_id + ":execute"
            launch = executor.start_control(project, launch_snapshot, source_id)
            if launch is None:
                row = executor.state().get("executions", {}).get(source_id, {})
                reason = str(row.get("reason") or "approved plan Worker launch failed") if isinstance(row, dict) else "approved plan Worker launch failed"
                self.planner.mark_worker_blocked(plan_id, reason)
                continue
            self.planner.mark_worker_launched(plan_id, source_id)
            history = read_json(self.history / (command_id + ".json"), {})
            if not isinstance(history, dict): history = {}
            history.update({"state":"accepted","lifecycle_action":"execute","task_id":launch.task_id,"backend_id":launch.backend_id,"resumed_at":utc_now_iso()})
            write_json(self.history / (command_id + ".json"), history, indent=2)
            self.command_store.audit("command_updated", history)
            outcomes.append(history)
        return outcomes

    def _consume_one(
        self,
        record: Any,
        projects: dict[str, dict[str, Any]],
        snapshots: dict[str, dict[str, Any]],
        executor: Any,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        if not isinstance(record, dict):
            return self._blocked("invalid-" + str(uuid4()), None, None, "malformed control command", now)
        command_id = _safe_command_id(record.get("command_id"))
        if command_id is None:
            return self._blocked("invalid-" + str(uuid4()), _nonblank(record.get("project_id")), _nonblank(record.get("action")), "invalid command_id", now)
        if record.get("version") != CONTROL_VERSION or record.get("state") != "pending":
            return self._blocked(command_id, _nonblank(record.get("project_id")), _nonblank(record.get("action")), "invalid control command version or state", now, record)
        project_id = _nonblank(record.get("project_id"))
        action = _nonblank(record.get("action"))
        if action not in _SUPPORTED_ACTIONS:
            return self._blocked(command_id, project_id, action, "unsupported control action", now, record)
        if project_id is None or project_id not in projects:
            return self._blocked(command_id, project_id, action, "project is not configured", now, record)
        snapshot = snapshots.get(project_id)
        if snapshot is None:
            return self._blocked(command_id, project_id, action, "project snapshot is unavailable", now, record)
        if record.get("source") == "control_api" or (isinstance(record.get("expected"), dict) and record["expected"].get("revision")):
            valid, reason, observed = validate_expected(record.get("expected"), snapshot, self.runtime_root)
            if not valid:
                return self._blocked(command_id, project_id, action, reason, now, {**record, "observed": observed})
        target = record.get("target") if isinstance(record.get("target"), dict) else {}
        gate_id = _nonblank(target.get("gate_id"))
        if gate_id is not None and self.accounting is not None:
            telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
            self.accounting.close_owner_gate(
                gate_id,
                occurred_at=str(record.get("requested_at") or now),
                project_id=project_id,
                task_id=_nonblank(telemetry.get("task_id")),
                role="owner",
                request_id=command_id,
            )
        if action == "pause":
            state = self.owner_store.set_paused(
                project_id, True, command_id=command_id, action=action,
                reason="explicit owner pause",
            )
            return {**record, "state": "accepted", "processed_at": now, "effect": "pause_future_launches", "owner_control": state}
        if action == "resume":
            state = self.owner_store.set_paused(
                project_id, False, command_id=command_id, action=action
            )
            return {**record, "state": "accepted", "processed_at": now, "effect": "resume_future_launches", "owner_control": state}
        if action == "stop":
            return self._stop(record, project_id, command_id, now, executor)
        if action in {"bind_conversation", "unbind_conversation", "rebind_conversation"}:
            return self._conversation_action(record, snapshot, project_id, command_id, action, target, now)
        if action in {"retry", "reconcile", "approve_owner_gate"}:
            return self._blocked(command_id, project_id, action, "action is not available for the current projected target", now, record)
        if self.owner_store.is_paused(project_id):
            return self._blocked(command_id, project_id, action, "project is paused", now, record)
        next_status = str(snapshot.get("next_status") or "").upper()
        if "PENDING DESIGN" in next_status:
            if self.planner is None:
                return self._blocked(command_id, project_id, action, "planner coordinator unavailable", now, record)
            plan_id, reason = self.planner.start(projects[project_id], snapshot, command_id)
            if plan_id is None:
                return self._blocked(command_id, project_id, action, reason, now, record)
            return {**record, "state":"accepted", "processed_at":now, "lifecycle_action":"plan", "plan_id":plan_id, "reason":reason}
        launch = executor.start_control(projects[project_id], snapshot, command_id)
        if launch is not None:
            return {
                **record,
                "state": "accepted",
                "processed_at": now,
                "task_id": launch.task_id,
                "backend_id": launch.backend_id,
            }
        state = executor.state().get("executions", {}).get(command_id, {})
        reason = state.get("reason") if isinstance(state, dict) else None
        return self._blocked(command_id, project_id, action, str(reason or "control command was not launched"), now, record)

    def _stop(
        self, record: dict[str, Any], project_id: str, command_id: str,
        now: str, executor: Any,
    ) -> dict[str, Any]:
        state = executor.state()
        executions = state.get("executions") if isinstance(state, dict) else None
        active = [row for row in (executions or {}).values() if isinstance(row, dict) and row.get("project_id") == project_id and row.get("state") in {"launching", "running"}]
        latest = max(active, key=lambda row: str(row.get("started_at") or ""), default=None)
        if latest is not None and (latest.get("engine") != "aibroker" or not latest.get("broker_request_id")):
            return self._blocked(command_id, project_id, "stop", "active execution does not support managed interruption", now, record)
        owner = self.owner_store.set_paused(
            project_id, True, command_id=command_id, action="stop",
            reason="explicit owner stop",
        )
        result: dict[str, Any] = {**record, "state": "accepted", "processed_at": now, "effect": "pause_future_launches", "owner_control": owner}
        if latest is None:
            return result
        port = getattr(executor, "_ai_execution_port", None)
        interrupt = getattr(port, "interrupt", None)
        if not callable(interrupt):
            return {**result, "state": "failed", "reason": "pause retained; AIBroker interruption is unavailable"}
        try:
            fact = interrupt(str(latest["broker_request_id"]), "explicit P12 owner stop")
        except Exception as exc:  # noqa: BLE001 - pause remains the safe effect
            return {**result, "state": "failed", "reason": f"pause retained; interruption failed: {exc}"}
        return {**result, "effect": "pause_and_interrupt", "interruption": fact, "execution_id": latest.get("source_request_id")}

    def _conversation_action(
        self, record: dict[str, Any], snapshot: dict[str, Any], project_id: str,
        command_id: str, action: str, target: dict[str, Any], now: str,
    ) -> dict[str, Any]:
        current = self.conversation_store.binding_for_project(project_id)
        route = current if isinstance(current, dict) else (
            snapshot.get("conversation_binding")
            if self.conversation_store.runtime_record_for_project(project_id) is None
            and isinstance(snapshot.get("conversation_binding"), dict)
            else None
        )
        if action in {"unbind_conversation", "rebind_conversation"} and route is not None and self.bridge_store is not None:
            active = getattr(self.bridge_store, "has_active_claim", None)
            if callable(active) and active(str(route.get("adapter") or ""), str(route.get("binding_id") or "")):
                return self._blocked(command_id, project_id, action, "active claimed Web Sol request blocks binding change", now, record)
        try:
            if action == "unbind_conversation":
                binding = self.conversation_store.unbind(project_id)
            else:
                adapter = _nonblank(target.get("adapter"))
                binding_id = _nonblank(target.get("binding_id"))
                if adapter is None or binding_id is None:
                    return self._blocked(command_id, project_id, action, "binding target requires adapter and binding_id", now, record)
                if action == "bind_conversation":
                    binding = self.conversation_store.bind(project_id, adapter, binding_id)
                else:
                    binding = self.conversation_store.rebind(project_id, adapter, binding_id)
        except (ConversationConflictError, ValueError) as exc:
            return self._blocked(command_id, project_id, action, str(exc), now, record)
        return {**record, "state": "accepted", "processed_at": now, "effect": action, "binding": binding}
    @staticmethod
    def _blocked(
        command_id: str,
        project_id: str | None,
        action: str | None,
        reason: str,
        processed_at: str,
        original: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            **(original or {}),
            "version": CONTROL_VERSION,
            "command_id": command_id,
            "project_id": project_id,
            "action": action,
            "state": "blocked",
            "reason": reason,
            "processed_at": processed_at,
        }
