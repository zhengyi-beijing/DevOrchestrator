"""Stateless local control-command inbox for project-scoped actions."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from dev_orchestrator.config import load_projects_config
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

CONTROL_DIR = "control"
CONTROL_VERSION = 1
_SUPPORTED_ACTIONS = frozenset({"continue"})


def _nonblank(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _safe_command_id(value: Any) -> str | None:
    text = _nonblank(value)
    if text is None or len(text) > 128 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in text):
        return None
    return text


def _paths(runtime_root: Path | str) -> tuple[Path, Path]:
    root = Path(runtime_root) / CONTROL_DIR
    return root / "inbox", root / "history"


def submit_control_command(runtime_root: Path | str, project_id: str, action: str) -> dict[str, Any]:
    project = _nonblank(project_id)
    if project is None:
        raise ValueError("project_id must be nonblank")
    if action not in _SUPPORTED_ACTIONS:
        raise ValueError("unsupported control action")
    inbox, _ = _paths(runtime_root)
    command_id = str(uuid4())
    record = {
        "version": CONTROL_VERSION,
        "command_id": command_id,
        "project_id": project,
        "action": action,
        "state": "pending",
        "requested_at": utc_now_iso(),
    }
    write_json(inbox / (command_id + ".json"), record, indent=2)
    return record


def latest_control_result(runtime_root: Path | str, project_id: str) -> dict[str, Any] | None:
    _, history = _paths(runtime_root)
    rows: list[dict[str, Any]] = []
    if history.is_dir():
        for path in history.glob("*.json"):
            row = read_json(path, None)
            if isinstance(row, dict) and row.get("project_id") == project_id:
                rows.append(row)
    if not rows:
        return None
    return max(rows, key=lambda row: str(row.get("processed_at") or row.get("requested_at") or ""))


class ControlCommandCoordinator:
    """Daemon-owned consumer for atomic project control commands."""

    def __init__(self, runtime_root: Path | str, planner: AIPlannerCoordinator | None = None) -> None:
        self.runtime_root = Path(runtime_root)
        self.inbox, self.history = _paths(runtime_root)
        self.planner = planner
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
        outcomes: list[dict[str, Any]] = []
        outcomes.extend(self._sync_planner_terminals())
        outcomes.extend(self._resume_ready_plans(projects, snapshots, executor))
        if not self.inbox.is_dir():
            return outcomes
        for path in sorted(self.inbox.glob("*.json"), key=lambda item: item.name):
            record = read_json(path, None)
            raw_id = _safe_command_id(record.get("command_id")) if isinstance(record, dict) else None
            existing = read_json(self.history / (raw_id + ".json"), None) if raw_id else None
            if isinstance(existing, dict):
                path.unlink(missing_ok=True)
                outcomes.append(existing)
                continue
            outcome = self._consume_one(record, projects, snapshots, executor)
            write_json(self.history / (outcome["command_id"] + ".json"), outcome, indent=2)
            path.unlink(missing_ok=True)
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
            project = projects.get(project_id); snapshot = snapshots.get(project_id)
            if project is None or snapshot is None or snapshot.get("state") != "READY_TO_RUN":
                continue
            source_id = command_id + ":execute"
            launch = executor.start_control(project, snapshot, source_id)
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
