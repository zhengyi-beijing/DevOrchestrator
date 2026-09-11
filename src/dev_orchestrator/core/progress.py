"""Transport-only Progress Channel for lifecycle milestone notifications.

The Progress Channel emits milestone notifications to a project's bound ChatGPT
conversation without model inference and without consuming AIBroker model quota.
It supports quiet, normal, and verbose notification levels, with deduplication,
rate-limiting, and restart-safe idempotency.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from uuid import uuid4

from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

PROGRESS_LEVEL_QUIET = "quiet"
PROGRESS_LEVEL_NORMAL = "normal"
PROGRESS_LEVEL_VERBOSE = "verbose"
_ALL_LEVELS = frozenset({PROGRESS_LEVEL_QUIET, PROGRESS_LEVEL_NORMAL, PROGRESS_LEVEL_VERBOSE})

QUIET_MILESTONES = frozenset({
    "OWNER_GATE",
    "BLOCKED",
    "TEST_FAILED",
    "TASK_COMPLETE",
})

NORMAL_MILESTONES = QUIET_MILESTONES | frozenset({
    "PLAN_STARTED",
    "PLAN_ACCEPTED",
    "WORKER_STARTED",
    "WORKER_DONE",
    "REVIEW_STARTED",
    "REMEDIATE",
    "REVIEW_ACCEPTED",
    "NEXT_TASK",
})

VERBOSE_MILESTONES = NORMAL_MILESTONES | frozenset({
    "PLAN_REJECTED",
    "PLAN_FAILED",
    "WORKER_FAILED",
    "REVIEW_FAILED",
    "APPLYING_PLAN",
    "RECOVERY_REQUIRED",
    "TICK",
    "IDLE",
    "HEARTBEAT",
})


class ProgressMilestone(str, Enum):
    PLAN_STARTED = "PLAN_STARTED"
    PLAN_ACCEPTED = "PLAN_ACCEPTED"
    WORKER_STARTED = "WORKER_STARTED"
    WORKER_DONE = "WORKER_DONE"
    TEST_FAILED = "TEST_FAILED"
    REVIEW_STARTED = "REVIEW_STARTED"
    REMEDIATE = "REMEDIATE"
    REVIEW_ACCEPTED = "REVIEW_ACCEPTED"
    OWNER_GATE = "OWNER_GATE"
    BLOCKED = "BLOCKED"
    TASK_COMPLETE = "TASK_COMPLETE"
    NEXT_TASK = "NEXT_TASK"


@dataclass(frozen=True, slots=True)
class ProgressNotification:
    """One immutable milestone notification emitted by the Progress Channel."""

    notification_id: str
    project_id: str
    task_id: str
    milestone: str
    message: str
    timestamp: str
    level: str = PROGRESS_LEVEL_NORMAL
    binding_id: Optional[str] = None
    adapter: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)


class ProgressChannel:
    """Emits lifecycle milestone notifications to a bound ChatGPT conversation without model inference."""

    def __init__(
        self,
        runtime_root: Path | str,
        bridge_store: Optional[Any] = None,
        rate_limit_seconds: float = 0.0,
        project_resolver: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.bridge_store = bridge_store
        self.rate_limit_seconds = float(rate_limit_seconds)
        self.project_resolver = project_resolver
        self.state_file = self.runtime_root / "progress-channel.json"
        self._lock = threading.RLock()
        self._emitted: dict[str, str] = {}
        self._last_emitted_by_target: dict[str, str] = {}
        self._history: list[dict[str, Any]] = []
        self._project_bindings: dict[str, dict[str, Any]] = {}
        self._project_configs: dict[str, dict[str, Any]] = {}
        self._load_state()

    def _load_state(self) -> None:
        raw = read_json(self.state_file, {})
        if not isinstance(raw, dict):
            raw = {}
        self._emitted = dict(raw.get("emitted", {}))
        self._last_emitted_by_target = dict(raw.get("last_emitted_by_target", {}))
        self._history = list(raw.get("history", []))
        self._project_bindings = dict(raw.get("project_bindings", {}))
        self._project_configs = dict(raw.get("project_configs", {}))

    def _save_state(self) -> None:
        write_json(
            self.state_file,
            {
                "version": 1,
                "emitted": self._emitted,
                "last_emitted_by_target": self._last_emitted_by_target,
                "history": self._history[-200:],
                "project_bindings": self._project_bindings,
                "project_configs": self._project_configs,
            },
        )

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": 1,
                "emitted_count": len(self._emitted),
                "history_count": len(self._history),
                "emitted": dict(self._emitted),
                "history": list(self._history),
                "project_bindings": dict(self._project_bindings),
            }

    def set_project_resolver(self, resolver: Optional[Callable[[str], Optional[dict[str, Any]]]]) -> None:
        with self._lock:
            self.project_resolver = resolver

    def register_project(self, project: Mapping[str, Any]) -> None:
        if not isinstance(project, Mapping):
            return
        proj_id = str(project.get("project_id") or project.get("id") or "")
        if not proj_id:
            return
        with self._lock:
            changed = False
            binding = project.get("conversation_binding")
            if isinstance(binding, dict) and binding.get("adapter") and binding.get("binding_id"):
                if self._project_bindings.get(proj_id) != binding:
                    self._project_bindings[proj_id] = copy.deepcopy(dict(binding))
                    changed = True
            cfg = project.get("progress_channel")
            level = project.get("progress_level")
            if cfg is not None or level is not None:
                new_cfg = {
                    "progress_channel": copy.deepcopy(cfg) if isinstance(cfg, dict) else cfg,
                    "progress_level": level,
                }
                if self._project_configs.get(proj_id) != new_cfg:
                    self._project_configs[proj_id] = new_cfg
                    changed = True
            if changed:
                self._save_state()

    def register_projects(self, projects: Iterable[Mapping[str, Any]]) -> None:
        with self._lock:
            changed = False
            for project in projects:
                if not isinstance(project, Mapping):
                    continue
                proj_id = str(project.get("project_id") or project.get("id") or "")
                if not proj_id:
                    continue
                binding = project.get("conversation_binding")
                if isinstance(binding, dict) and binding.get("adapter") and binding.get("binding_id"):
                    if self._project_bindings.get(proj_id) != binding:
                        self._project_bindings[proj_id] = copy.deepcopy(dict(binding))
                        changed = True
                cfg = project.get("progress_channel")
                level = project.get("progress_level")
                if cfg is not None or level is not None:
                    new_cfg = {
                        "progress_channel": copy.deepcopy(cfg) if isinstance(cfg, dict) else cfg,
                        "progress_level": level,
                    }
                    if self._project_configs.get(proj_id) != new_cfg:
                        self._project_configs[proj_id] = new_cfg
                        changed = True
            if changed:
                self._save_state()

    def resolve_binding(self, project_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            cached = self._project_bindings.get(project_id)
            if isinstance(cached, dict) and cached.get("adapter") and cached.get("binding_id"):
                return copy.deepcopy(cached)
            if callable(self.project_resolver):
                try:
                    resolved = self.project_resolver(project_id)
                    if isinstance(resolved, dict):
                        b = resolved.get("conversation_binding")
                        if not (isinstance(b, dict) and b.get("adapter") and b.get("binding_id")):
                            if resolved.get("adapter") and resolved.get("binding_id"):
                                b = resolved
                        if isinstance(b, dict) and b.get("adapter") and b.get("binding_id"):
                            self._project_bindings[project_id] = copy.deepcopy(b)
                            self._save_state()
                            return copy.deepcopy(b)
                except Exception:
                    pass
            # Check summary.json
            summary_path = self.runtime_root / "summary.json"
            if summary_path.is_file():
                try:
                    summary = read_json(summary_path, {})
                    if isinstance(summary, dict) and isinstance(summary.get("projects"), list):
                        for item in summary["projects"]:
                            if isinstance(item, dict) and str(item.get("project_id") or "") == project_id:
                                b = item.get("conversation_binding")
                                if isinstance(b, dict) and b.get("adapter") and b.get("binding_id"):
                                    self._project_bindings[project_id] = copy.deepcopy(b)
                                    self._save_state()
                                    return copy.deepcopy(b)
                except Exception:
                    pass
            # Check transition-executor.json
            texec_path = self.runtime_root / "transition-executor.json"
            if texec_path.is_file():
                try:
                    texec = read_json(texec_path, {})
                    if isinstance(texec, dict) and isinstance(texec.get("executions"), dict):
                        for rec in texec["executions"].values():
                            if isinstance(rec, dict) and rec.get("project_id") == project_id:
                                b = rec.get("conversation_binding")
                                if isinstance(b, dict) and b.get("adapter") and b.get("binding_id"):
                                    self._project_bindings[project_id] = copy.deepcopy(b)
                                    self._save_state()
                                    return copy.deepcopy(b)
                except Exception:
                    pass
            # Check ai-planner.json
            plan_path = self.runtime_root / "ai-planner.json"
            if plan_path.is_file():
                try:
                    plans = read_json(plan_path, {})
                    if isinstance(plans, dict) and isinstance(plans.get("plans"), dict):
                        for rec in plans["plans"].values():
                            if isinstance(rec, dict) and rec.get("project_id") == project_id:
                                b = rec.get("conversation_binding")
                                if isinstance(b, dict) and b.get("adapter") and b.get("binding_id"):
                                    self._project_bindings[project_id] = copy.deepcopy(b)
                                    self._save_state()
                                    return copy.deepcopy(b)
                except Exception:
                    pass
            # Check ai-reviewer.json
            rev_path = self.runtime_root / "ai-reviewer.json"
            if rev_path.is_file():
                try:
                    revs = read_json(rev_path, {})
                    if isinstance(revs, dict) and isinstance(revs.get("reviews"), dict):
                        for rec in revs["reviews"].values():
                            if isinstance(rec, dict) and rec.get("project_id") == project_id:
                                b = rec.get("conversation_binding")
                                if isinstance(b, dict) and b.get("adapter") and b.get("binding_id"):
                                    self._project_bindings[project_id] = copy.deepcopy(b)
                                    self._save_state()
                                    return copy.deepcopy(b)
                except Exception:
                    pass
            return None

    def emit(
        self,
        project: Mapping[str, Any] | str,
        milestone: str,
        *,
        task_id: Optional[str] = None,
        message: Optional[str] = None,
        occurrence_key: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
        level: Optional[str] = None,
    ) -> Optional[ProgressNotification]:
        if isinstance(project, str):
            project_dict: dict[str, Any] = {"project_id": project}
        elif isinstance(project, Mapping):
            project_dict = dict(project)
        else:
            return None

        project_id = str(project_dict.get("project_id") or project_dict.get("id") or "")
        if not project_id:
            return None

        binding = project_dict.get("conversation_binding")
        if isinstance(binding, dict) and binding.get("adapter") and binding.get("binding_id"):
            with self._lock:
                if self._project_bindings.get(project_id) != binding:
                    self._project_bindings[project_id] = copy.deepcopy(dict(binding))
                    self._save_state()
        else:
            resolved_binding = self.resolve_binding(project_id)
            if resolved_binding:
                binding = resolved_binding
                project_dict["conversation_binding"] = resolved_binding

        if project_dict.get("progress_channel") is None and project_dict.get("progress_level") is None:
            with self._lock:
                cached_cfg = self._project_configs.get(project_id)
            if cached_cfg:
                if cached_cfg.get("progress_channel") is not None:
                    project_dict["progress_channel"] = cached_cfg["progress_channel"]
                if cached_cfg.get("progress_level") is not None:
                    project_dict["progress_level"] = cached_cfg["progress_level"]

        # Determine level & enablement
        progress_cfg = project_dict.get("progress_channel")
        enabled = True
        configured_level = PROGRESS_LEVEL_NORMAL
        if isinstance(progress_cfg, dict):
            enabled = bool(progress_cfg.get("enabled", True))
            raw_level = progress_cfg.get("level") or project_dict.get("progress_level") or PROGRESS_LEVEL_NORMAL
            configured_level = str(raw_level).lower()
        elif isinstance(progress_cfg, bool):
            enabled = progress_cfg
            configured_level = str(project_dict.get("progress_level") or PROGRESS_LEVEL_NORMAL).lower()
        elif project_dict.get("progress_level"):
            configured_level = str(project_dict.get("progress_level")).lower()

        if not enabled:
            return None
        if configured_level not in _ALL_LEVELS:
            configured_level = PROGRESS_LEVEL_NORMAL

        # Milestone admission by level
        effective_level = level or configured_level
        if configured_level == PROGRESS_LEVEL_QUIET:
            if milestone not in QUIET_MILESTONES:
                return None
        elif configured_level == PROGRESS_LEVEL_NORMAL:
            if milestone not in NORMAL_MILESTONES:
                return None
        elif configured_level == PROGRESS_LEVEL_VERBOSE:
            if milestone not in VERBOSE_MILESTONES:
                return None

        eff_task_id = str(task_id or project_dict.get("telemetry", {}).get("task_id") or project_dict.get("task_id") or "")
        eff_occurrence = str(occurrence_key or "")
        dedupe_key = f"{project_id}:{eff_task_id}:{milestone}:{eff_occurrence}"

        now_iso = utc_now_iso()

        with self._lock:
            # Deduplication
            if dedupe_key in self._emitted:
                return None

            # Rate limiting
            binding_key = ""
            if isinstance(binding, dict):
                binding_key = f"{binding.get('adapter')}:{binding.get('binding_id')}"
            target_key = binding_key or project_id

            if self.rate_limit_seconds > 0.0 and milestone not in ("OWNER_GATE", "BLOCKED", "TEST_FAILED"):
                last_time_str = self._last_emitted_by_target.get(target_key)
                if last_time_str:
                    try:
                        t_last = datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
                        t_now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                        if (t_now - t_last).total_seconds() < self.rate_limit_seconds:
                            return None
                    except Exception:
                        pass

            # Build human-readable notification message
            msg = message or self._default_message(project_id, eff_task_id, milestone, details)

            notification_id = f"prog-{uuid4().hex[:12]}"
            notif = ProgressNotification(
                notification_id=notification_id,
                project_id=project_id,
                task_id=eff_task_id,
                milestone=milestone,
                message=msg,
                timestamp=now_iso,
                level=effective_level,
                binding_id=binding.get("binding_id") if isinstance(binding, dict) else None,
                adapter=binding.get("adapter") if isinstance(binding, dict) else None,
                details=dict(details or {}),
            )

            # Persist in state
            self._emitted[dedupe_key] = now_iso
            self._last_emitted_by_target[target_key] = now_iso
            self._history.append(asdict(notif))
            self._save_state()

            # Transport delivery to bound conversation if binding exists
            if isinstance(binding, dict) and binding.get("adapter") and binding.get("binding_id"):
                adapter = str(binding["adapter"])
                b_id = str(binding["binding_id"])
                if self.bridge_store is not None and hasattr(self.bridge_store, "submit_progress"):
                    self.bridge_store.submit_progress(adapter, b_id, notif)

            return notif

    def history(self, project_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            if project_id is None:
                return copy.deepcopy(self._history)
            return [
                copy.deepcopy(item) for item in self._history
                if item.get("project_id") == project_id
            ]

    @staticmethod
    def _default_message(project_id: str, task_id: str, milestone: str, details: Optional[dict[str, Any]]) -> str:
        descs = {
            "PLAN_STARTED": "Planning cycle started",
            "PLAN_ACCEPTED": "Plan approved and frozen",
            "WORKER_STARTED": "Worker execution started",
            "WORKER_DONE": "Worker execution completed",
            "TEST_FAILED": "Tests failed",
            "REVIEW_STARTED": "Independent review started",
            "REMEDIATE": "Task remediation requested",
            "REVIEW_ACCEPTED": "Review accepted task completion",
            "OWNER_GATE": "Owner gate required",
            "BLOCKED": "Execution blocked",
            "TASK_COMPLETE": "Task successfully completed",
            "NEXT_TASK": "Advancing to next task",
        }
        desc = descs.get(milestone, f"Milestone {milestone}")
        reason = details.get("reason") if isinstance(details, dict) and details.get("reason") else ""
        reason_suffix = f" - {reason}" if reason else ""
        task_prefix = f" [{task_id}]" if task_id else ""
        return f"[DEVORCH_PROGRESS] [{project_id}]{task_prefix} {milestone}: {desc}{reason_suffix}"
