"""Owner-authorized NEXT_TASK and reviewed-remediation Worker actuation.

Contract: docs/TRANSITION_EXECUTOR_CONTRACT.md.  This Core component is the
only V1 execution boundary.  Browser transport and Decision Guard remain
non-executing; this executor acts only after their accepted disposition and an
explicit local project execution policy.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.ai.contracts import AIRoleRequest
from dev_orchestrator.ai.execution_port import AIExecutionPort, MANAGED_INTERRUPT_REASON
from dev_orchestrator.agents.base import AgentBackend
from dev_orchestrator.agents.backends.agy import AgyBackend
from dev_orchestrator.agents.backends.dsh import DshBackend
from dev_orchestrator.agents.models import AgentRequest, AgentResult, AgentRole, AgentRunState, QuotaState
from dev_orchestrator.agents.registry import BackendRegistry
from dev_orchestrator.agents.router import AgentRouter
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.project_status import write_execution_status
from dev_orchestrator.core.websol import NextAction, WebSolEvent, WebSolRole
from dev_orchestrator.monitor.telemetry import extract_task_id
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

ACTUATION_FILE = "transition-executor.json"
_LEDGER_VERSION = 1
_ACTIVE_STATES = frozenset({"launching", "running"})
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})

_DEFAULT_WORKER_PROMPT = (
    "Execute exactly one bounded task from agent/next.md. Read repository "
    "instructions and agent/CURRENT.md first. Implement the task, run the "
    "required tests, update agent/CURRENT.md and agent/result.md, and update "
    "agent/next.md to the next executable task when appropriate. Commit the "
    "completed bounded task locally so the working tree is clean; do not push. "
    "Stop after one task."
)

_DEFAULT_REMEDIATION_PROMPT = (
    "Remediate the current bounded task in place. Read agent/next.md, "
    "agent/CURRENT.md, agent/result.md and the current uncommitted diff first. "
    "Do not advance to a new task. Fix every acceptance gap in the current task, "
    "run all required build/tests, update the agent status/result files, and commit "
    "the completed current task locally so the working tree is clean; do not push. "
    "Stop after the current task is complete."
)


@dataclass(frozen=True)
class ActuationLaunch:
    project_id: str
    source_request_id: str
    task_id: str
    backend_id: str
    state: str


def _empty_ledger() -> dict[str, Any]:
    return {"version": _LEDGER_VERSION, "executions": {}}


def _project_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(project.get("project_id")): project
        for project in (config.get("projects") or [])
        if isinstance(project, dict) and project.get("project_id")
    }


def _snapshot_map(summary: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(summary, dict) or not isinstance(summary.get("projects"), list):
        return {}
    return {
        str(snapshot.get("project_id")): snapshot
        for snapshot in summary["projects"]
        if isinstance(snapshot, dict) and snapshot.get("project_id")
    }


def _current_task_id(snapshot: dict[str, Any]) -> Optional[str]:
    telemetry = snapshot.get("telemetry")
    if isinstance(telemetry, dict):
        value = telemetry.get("task_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return extract_task_id(snapshot.get("next_title"))


def _external_worker_active(snapshot: dict[str, Any]) -> bool:
    worker = snapshot.get("worker")
    if not isinstance(worker, dict) or worker.get("kind") != "task":
        return False
    return worker.get("state") in ("starting", "running")


_SUPPORTED_BACKENDS = frozenset({"agy", "dsh"})
_SUPPORTED_EXECUTION_ENGINES = frozenset({"legacy", "aibroker"})

_RETRYABLE_PROVIDER_FAILURE_MARKERS = (
    "individual quota reached",
    "quota exhausted",
    "quota reached",
)


def _retryable_provider_failure(result: AgentResult) -> bool:
    if result.state is not AgentRunState.FAILED:
        return False
    text = (str(result.stderr or "") + "\n" + str(result.stdout or "")).casefold()
    return any(marker in text for marker in _RETRYABLE_PROVIDER_FAILURE_MARKERS)


def _non_blank_config(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _execution_policy(project: dict[str, Any]) -> tuple[Optional[dict[str, Any]], str]:
    raw = project.get("execution")
    if not isinstance(raw, dict):
        return None, "execution config missing"
    if raw.get("enabled") is not True:
        return None, "execution is disabled"
    if raw.get("owner_authorized") is not True:
        return None, "execution is not owner-authorized"
    allowed = raw.get("allowed_next_actions")
    supported_actions = {NextAction.CONTINUE_CURRENT_STAGE.value, NextAction.NEXT_TASK.value}
    if not isinstance(allowed, list) or not allowed:
        return None, "allowed_next_actions must be a non-empty list"
    if any(value not in supported_actions for value in allowed):
        return None, "allowed_next_actions contains unsupported V1 action"
    if len(set(allowed)) != len(allowed):
        return None, "allowed_next_actions must not contain duplicates"

    engine = raw.get("engine", "legacy")
    if not isinstance(engine, str) or engine not in _SUPPORTED_EXECUTION_ENGINES:
        return None, "execution.engine must be legacy or aibroker"
    worker_quality = raw.get("worker_quality", "balanced")
    if worker_quality not in ("economy", "balanced", "high"):
        return None, "execution.worker_quality must be economy, balanced, or high"
    worker_timeout = raw.get("worker_timeout_seconds", 14400)
    if isinstance(worker_timeout, bool) or not isinstance(worker_timeout, (int, float)) or worker_timeout <= 0:
        return None, "execution.worker_timeout_seconds must be positive"

    preferred_ids: list[str] = []
    backend_configs: dict[str, dict[str, Any]] = {}
    if engine == "legacy":
        preferred = raw.get("preferred_backends")
        if not isinstance(preferred, list) or not preferred:
            return None, "preferred_backends must be a non-empty list"
        if any(not isinstance(value, str) or not value.strip() for value in preferred):
            return None, "preferred_backends entries must be non-blank strings"
        preferred_ids = [value.strip() for value in preferred]
        if len(set(preferred_ids)) != len(preferred_ids):
            return None, "preferred_backends must not contain duplicates"
        unknown = [value for value in preferred_ids if value not in _SUPPORTED_BACKENDS]
        if unknown:
            return None, "unsupported execution backend: {0}".format(unknown[0])
        raw_backends = raw.get("backends", {})
        if not isinstance(raw_backends, dict):
            return None, "execution.backends must be an object"
        for backend_id in preferred_ids:
            config = raw_backends.get(backend_id, {})
            if not isinstance(config, dict):
                return None, "execution.backends.{0} must be an object".format(backend_id)
            normalized: dict[str, Any] = {}
            executable = config.get("executable")
            if executable is not None:
                executable = _non_blank_config(executable)
                if executable is None:
                    return None, "{0} executable must be a non-blank string".format(backend_id)
                normalized["executable"] = executable
            if backend_id == "agy":
                if "project" in config:
                    provider_project = _non_blank_config(config.get("project"))
                    if provider_project is None:
                        return None, "agy project must be a non-blank string"
                    normalized["project"] = provider_project
                for key in ("model", "effort", "mode"):
                    value = config.get(key)
                    if value is not None:
                        text = _non_blank_config(value)
                        if text is None:
                            return None, "agy {0} must be a non-blank string".format(key)
                        normalized[key] = text
                timeout = config.get("print_timeout")
                if timeout is not None:
                    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
                        return None, "agy print_timeout must be a positive integer"
                    normalized["print_timeout"] = timeout
            backend_configs[backend_id] = normalized

    prompt = raw.get("worker_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = _DEFAULT_WORKER_PROMPT
    remediation_prompt = raw.get("remediation_prompt")
    if not isinstance(remediation_prompt, str) or not remediation_prompt.strip():
        remediation_prompt = _DEFAULT_REMEDIATION_PROMPT
    bootstrap = raw.get("bootstrap")
    normalized_bootstrap = None
    if bootstrap is not None:
        if not isinstance(bootstrap, dict):
            return None, "execution.bootstrap must be an object"
        request_id = _non_blank_config(bootstrap.get("request_id"))
        task_id = _non_blank_config(bootstrap.get("task_id"))
        if request_id is None or task_id is None:
            return None, "execution.bootstrap requires non-blank request_id and task_id"
        normalized_bootstrap = {"request_id": request_id, "task_id": task_id}
    owner_start = raw.get("owner_start")
    normalized_owner_start = None
    if owner_start is not None:
        if not isinstance(owner_start, dict): return None, "execution.owner_start must be an object"
        request_id = _non_blank_config(owner_start.get("request_id")); task_id = _non_blank_config(owner_start.get("task_id"))
        if request_id is None or task_id is None: return None, "execution.owner_start requires non-blank request_id and task_id"
        normalized_owner_start = {"request_id": request_id, "task_id": task_id}

    return {
        "engine": engine,
        "worker_quality": worker_quality,
        "worker_timeout_seconds": float(worker_timeout),
        "preferred_backends": tuple(preferred_ids),
        "backends": backend_configs,
        "allowed_next_actions": frozenset(allowed),
        "worker_prompt": prompt.strip(),
        "remediation_prompt": remediation_prompt.strip(),
        "bootstrap": normalized_bootstrap,
        "owner_start": normalized_owner_start,
    }, ""


class TransitionExecutor:
    """Long-lived daemon-owned transition executor with project-scoped routing."""

    def __init__(
        self,
        runtime_root: Path | str,
        *,
        backend_overrides: Optional[dict[str, AgentBackend]] = None,
        ai_execution_port: Optional[AIExecutionPort] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.runtime_root / ACTUATION_FILE
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._ai_execution_port = ai_execution_port
        self._backend_overrides: dict[str, AgentBackend] = {}
        for backend_id, backend in (backend_overrides or {}).items():
            if backend_id not in _SUPPORTED_BACKENDS:
                raise ValueError("unsupported backend override: {0}".format(backend_id))
            if not isinstance(backend, AgentBackend):
                raise TypeError("backend override must implement AgentBackend")
            if backend.backend_id != backend_id:
                raise ValueError("backend override id mismatch for {0}".format(backend_id))
            self._backend_overrides[backend_id] = backend
        self._recover_interrupted_runs()

    def _backend_for_policy(self, backend_id: str, config: dict[str, Any]) -> AgentBackend:
        override = self._backend_overrides.get(backend_id)
        if override is not None:
            return override
        executable = config.get("executable")
        command_prefix = (str(executable),) if executable else None
        if backend_id == "dsh":
            return DshBackend(runtime_root=self.runtime_root, command_prefix=command_prefix)
        if backend_id == "agy":
            kwargs: dict[str, Any] = {"runtime_root": self.runtime_root}
            if command_prefix is not None:
                kwargs["command_prefix"] = command_prefix
            for key in ("project", "model", "effort", "mode", "print_timeout"):
                if key in config:
                    kwargs[key] = config[key]
            return AgyBackend(**kwargs)
        raise ValueError("unsupported backend: {0}".format(backend_id))

    def _router_for_policy(
        self, policy: dict[str, Any]
    ) -> tuple[AgentRouter, dict[str, AgentBackend]]:
        registry = BackendRegistry()
        backends: dict[str, AgentBackend] = {}
        for backend_id in policy["preferred_backends"]:
            backend = self._backend_for_policy(backend_id, policy["backends"][backend_id])
            registry.register(backend)
            backends[backend_id] = backend
        return AgentRouter(registry), backends

    def _load_ledger(self) -> dict[str, Any]:
        data = read_json(self.ledger_path, None)
        if not isinstance(data, dict):
            return _empty_ledger()
        executions = data.get("executions")
        if not isinstance(executions, dict):
            executions = {}
        clean = {
            key: value for key, value in executions.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
        return {"version": _LEDGER_VERSION, "executions": clean}

    def _save_ledger(self, ledger: dict[str, Any]) -> None:
        write_json(self.ledger_path, ledger, indent=2)

    def _recover_interrupted_runs(self) -> None:
        with self._lock:
            ledger = self._load_ledger()
            changed = False
            for record in ledger["executions"].values():
                if record.get("state") not in _ACTIVE_STATES:
                    continue
                recovered_at = utc_now_iso()
                fact = None
                broker_request_id = record.get("broker_request_id")
                if record.get("engine") == "aibroker" and broker_request_id and self._ai_execution_port is not None:
                    status_fn = getattr(self._ai_execution_port, "status", None)
                    if callable(status_fn):
                        try:
                            fact = status_fn(str(broker_request_id))
                        except Exception as exc:
                            record["broker_recovery_error"] = str(exc)
                if isinstance(fact, dict):
                    record["dispatch_id"] = fact.get("dispatch_id") or record.get("dispatch_id")
                    record["decision_id"] = fact.get("decision_id") or record.get("decision_id")
                    record["execution_id"] = fact.get("execution_id") or record.get("execution_id")
                    record["session_id"] = fact.get("execution_session_id") or record.get("session_id")
                    if fact.get("resource_id"):
                        record["resource_context"] = {
                            "resource_id": fact.get("resource_id"), "provider": fact.get("provider"),
                            "account": fact.get("account"), "model": fact.get("model"),
                        }
                    record["broker_status"] = fact.get("status")
                    if fact.get("status") == "succeeded":
                        record["state"] = "completed"
                        record["completed_at"] = fact.get("finished_at") or recovered_at
                        record["reason"] = None
                    elif fact.get("status") == "failed" and fact.get("execution_error") != MANAGED_INTERRUPT_REASON:
                        record["state"] = "failed"
                        record["completed_at"] = fact.get("finished_at") or recovered_at
                        record["reason"] = fact.get("execution_error") or "Broker execution failed before daemon recovery"
                    else:
                        truth = read_repository_truth(record.get("repo_path") or "")
                        unchanged = bool(
                            truth.valid and truth.branch == record.get("branch") and truth.head == record.get("head")
                            and truth.status_hash == record.get("launch_status_hash")
                        )
                        managed_interrupt = fact.get("status") == "failed" and fact.get("execution_error") == MANAGED_INTERRUPT_REASON
                        record["state"] = "recovery_required"
                        record["recovery_safe_retry"] = bool(managed_interrupt and unchanged)
                        record["reason"] = (
                            "managed Broker interruption confirmed; repository unchanged; owner continue may retry"
                            if record["recovery_safe_retry"] else
                            "Broker recovery is unresolved or repository changed; automatic replay is forbidden"
                        )
                else:
                    record["state"] = "recovery_required"
                    record["recovery_safe_retry"] = False
                    record["reason"] = "daemon restarted while managed execution was active; Broker state is unavailable; automatic replay is forbidden"
                record["recovered_at"] = recovered_at
                changed = True
            if changed:
                self._save_ledger(ledger)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load_ledger())

    def overlay_managed_runs(self, summary: Any) -> Any:
        """Project managed-run truth onto a deep copy for WORKER_DONE dispatch."""
        projected = copy.deepcopy(summary)
        if not isinstance(projected, dict) or not isinstance(projected.get("projects"), list):
            return projected
        latest: dict[str, dict[str, Any]] = {}
        for record in self.state()["executions"].values():
            if not isinstance(record, dict) or record.get("state") not in (_ACTIVE_STATES | _TERMINAL_STATES):
                continue
            project_id = _non_blank_config(record.get("project_id"))
            if project_id is None:
                continue
            key = (str(record.get("started_at") or ""), str(record.get("source_request_id") or ""))
            previous = latest.get(project_id)
            previous_key = ((str(previous.get("started_at") or ""), str(previous.get("source_request_id") or "")) if previous else None)
            if previous_key is None or key > previous_key:
                latest[project_id] = record
        for snapshot in projected["projects"]:
            if not isinstance(snapshot, dict):
                continue
            record = latest.get(str(snapshot.get("project_id") or ""))
            if record is None:
                continue
            state = str(record.get("state") or "")
            worker_state = "starting" if state == "launching" else state
            snapshot["worker"] = {"kind":"task","state":worker_state,"process_alive":state in _ACTIVE_STATES,"pid":record.get("pid"),"started_at":record.get("started_at"),"updated_at":record.get("completed_at") or record.get("started_at"),"exit_code":record.get("exit_code"),"command":"managed {0}".format(record.get("backend_id") or "worker")}
            telemetry = snapshot.get("telemetry")
            telemetry = copy.deepcopy(telemetry) if isinstance(telemetry, dict) else {}
            telemetry["run_id"] = record.get("backend_run_id")
            telemetry["task_id"] = record.get("task_id")
            snapshot["telemetry"] = telemetry
            snapshot["state"] = ("WORKER_RUNNING" if state in _ACTIVE_STATES else ("WAITING_REVIEW" if state == "completed" else "WORKER_FAILED"))
        return projected

    @staticmethod
    def _active_project(ledger: dict[str, Any], project_id: str) -> bool:
        for record in ledger["executions"].values():
            if record.get("project_id") == project_id and record.get("state") in _ACTIVE_STATES:
                return True
        return False

    def _record_blocked(
        self, source_request_id: str, project_id: str, reason: str,
        *, task_id: Optional[str] = None, source_kind: str = "decision",
    ) -> None:
        with self._lock:
            ledger = self._load_ledger()
            if source_request_id in ledger["executions"]:
                return
            ledger["executions"][source_request_id] = {
                "project_id": project_id,
                "source_request_id": source_request_id,
                "source_kind": source_kind,
                "task_id": task_id,
                "state": "blocked",
                "reason": reason,
                "recorded_at": utc_now_iso(),
            }
            self._save_ledger(ledger)

    def _fresh_guard(
        self,
        project: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        expected_branch: str,
        expected_head: str,
        expected_task_id: Optional[str] = None,
        must_advance_from: Optional[str] = None,
        expected_status_hash: Optional[str] = None,
    ) -> tuple[Optional[str], str]:
        state = snapshot.get("state")
        if expected_status_hash is not None:
            if state not in ("READY_TO_RUN", "WAITING_REVIEW", "IDLE"):
                return None, "project state is not eligible for exact remediation"
        elif state != "READY_TO_RUN":
            return None, "project is not READY_TO_RUN"
        if _external_worker_active(snapshot):
            return None, "an external task Worker is already active"
        current_task = _current_task_id(snapshot)
        if current_task is None:
            return None, "current agent/next.md task id is unavailable"
        if expected_task_id is not None and current_task != expected_task_id:
            return None, "requested task id does not match current agent/next.md"
        if must_advance_from is not None and current_task == must_advance_from:
            return None, "NEXT_TASK refused because agent/next.md has not advanced"

        truth = read_repository_truth(project.get("repo_path") or "")
        if not truth.valid:
            return None, "repository truth unavailable"
        if truth.branch != expected_branch or truth.head != expected_head:
            return None, "repository changed after reviewed/bootstrapped truth"
        if expected_status_hash is not None:
            if truth.status_hash != expected_status_hash:
                return None, "reviewed dirty fingerprint changed before remediation"
        elif truth.dirty:
            return None, "repository is dirty"
        return current_task, ""

    def _launch(
        self,
        project: dict[str, Any],
        *,
        source_request_id: str,
        source_kind: str,
        task_id: str,
        source_task_id: Optional[str],
        branch: str,
        head: str,
        worker_prompt: str,
        policy: dict[str, Any],
    ) -> Optional[ActuationLaunch]:
        if policy.get("engine") == "aibroker":
            return self._launch_aibroker(
                project, source_request_id=source_request_id, source_kind=source_kind,
                task_id=task_id, source_task_id=source_task_id, branch=branch, head=head,
                worker_prompt=worker_prompt, policy=policy,
            )
        request = AgentRequest(
            project_id=str(project["project_id"]),
            role=AgentRole.WORKER,
            prompt=worker_prompt,
            working_directory=Path(str(project["repo_path"])),
            required_capabilities=frozenset({"code", "repository"}),
            preferred_backends=tuple(policy["preferred_backends"]),
        )
        try:
            router, backends = self._router_for_policy(policy)
            route = router.route(request)
        except Exception as exc:
            self._record_blocked(
                source_request_id, str(project["project_id"]),
                "backend construction failed: {0}".format(exc),
                task_id=task_id, source_kind=source_kind,
            )
            return None
        if route.selected_backend_id is None:
            self._record_blocked(
                source_request_id, str(project["project_id"]), route.reason,
                task_id=task_id, source_kind=source_kind,
            )
            return None
        backend = backends.get(route.selected_backend_id)
        if backend is None:
            self._record_blocked(
                source_request_id, str(project["project_id"]),
                "selected backend instance is unavailable",
                task_id=task_id, source_kind=source_kind,
            )
            return None
        fallback_ids = tuple(
            candidate.backend_id for candidate in route.candidates
            if candidate.eligible and candidate.backend_id != route.selected_backend_id
        )
        launch_truth = read_repository_truth(project.get("repo_path") or "")
        if (not launch_truth.valid or launch_truth.branch != branch or launch_truth.head != head):
            self._record_blocked(
                source_request_id, str(project["project_id"]),
                "repository changed before Worker launch",
                task_id=task_id, source_kind=source_kind,
            )
            return None

        with self._lock:
            ledger = self._load_ledger()
            if source_request_id in ledger["executions"]:
                return None
            project_id = str(project["project_id"])
            if self._active_project(ledger, project_id):
                ledger["executions"][source_request_id] = {
                    "project_id": project_id,
                    "source_request_id": source_request_id,
                    "source_kind": source_kind,
                    "task_id": task_id,
                    "state": "blocked",
                    "reason": "another managed Worker is already active",
                    "recorded_at": utc_now_iso(),
                }
                self._save_ledger(ledger)
                return None
            started_at = utc_now_iso()
            ledger["executions"][source_request_id] = {
                "project_id": project_id,
                "source_request_id": source_request_id,
                "source_kind": source_kind,
                "source_task_id": source_task_id,
                "task_id": task_id,
                "branch": branch,
                "head": head,
                "repo_path": str(project["repo_path"]),
                "backend_id": route.selected_backend_id,
                "state": "launching",
                "started_at": started_at,
                "review_state": "pending",
                "launch_status_hash": launch_truth.status_hash,
            }
            self._save_ledger(ledger)
            status_record = copy.deepcopy(ledger["executions"][source_request_id])
        write_execution_status(status_record, self.runtime_root)

        thread = threading.Thread(
            target=self._run_worker_thread,
            args=(source_request_id, request, backend, backends, fallback_ids),
            name="devorch-worker-" + project_id,
            daemon=True,
        )
        with self._lock:
            self._threads[source_request_id] = thread
        thread.start()
        return ActuationLaunch(
            project_id=project_id,
            source_request_id=source_request_id,
            task_id=task_id,
            backend_id=route.selected_backend_id,
            state="launching",
        )


    def _launch_aibroker(
        self,
        project: dict[str, Any],
        *,
        source_request_id: str,
        source_kind: str,
        task_id: str,
        source_task_id: Optional[str],
        branch: str,
        head: str,
        worker_prompt: str,
        policy: dict[str, Any],
    ) -> Optional[ActuationLaunch]:
        project_id = str(project["project_id"])
        if self._ai_execution_port is None:
            self._record_blocked(
                source_request_id, project_id, "AIBroker execution port is not configured",
                task_id=task_id, source_kind=source_kind,
            )
            return None
        launch_truth = read_repository_truth(project.get("repo_path") or "")
        if not launch_truth.valid or launch_truth.branch != branch or launch_truth.head != head:
            self._record_blocked(
                source_request_id, project_id, "repository changed before Worker launch",
                task_id=task_id, source_kind=source_kind,
            )
            return None
        role_run_id = "worker-" + source_request_id.replace(":", "-")
        broker_request_id = "ai-worker:" + source_request_id
        request = AIRoleRequest(
            project_id=project_id,
            task_run_id=task_id,
            stage_run_id=source_kind,
            role_run_id=role_run_id,
            request_id=broker_request_id,
            role="worker",
            quality=str(policy.get("worker_quality") or "balanced"),
            prompt=worker_prompt,
            working_directory=Path(str(project["repo_path"])),
            timeout_seconds=float(policy.get("worker_timeout_seconds") or 14400),
            metadata={"source_request_id": source_request_id, "source_kind": source_kind},
        )
        with self._lock:
            ledger = self._load_ledger()
            if source_request_id in ledger["executions"]:
                return None
            if self._active_project(ledger, project_id):
                ledger["executions"][source_request_id] = {
                    "project_id": project_id, "source_request_id": source_request_id,
                    "source_kind": source_kind, "task_id": task_id, "state": "blocked",
                    "reason": "another managed Worker is already active", "recorded_at": utc_now_iso(),
                }
                self._save_ledger(ledger)
                return None
            ledger["executions"][source_request_id] = {
                "project_id": project_id,
                "source_request_id": source_request_id,
                "source_kind": source_kind,
                "source_task_id": source_task_id,
                "task_id": task_id,
                "branch": branch,
                "head": head,
                "repo_path": str(project["repo_path"]),
                "engine": "aibroker",
                "backend_id": "aibroker",
                "broker_request_id": broker_request_id,
                "role_run_id": role_run_id,
                "state": "launching",
                "started_at": utc_now_iso(),
                "review_state": "pending",
                "launch_status_hash": launch_truth.status_hash,
            }
            self._save_ledger(ledger)
            status_record = copy.deepcopy(ledger["executions"][source_request_id])
        write_execution_status(status_record, self.runtime_root)
        thread = threading.Thread(
            target=self._run_broker_worker_thread,
            args=(source_request_id, request),
            name="devorch-broker-worker-" + project_id,
            daemon=True,
        )
        with self._lock:
            self._threads[source_request_id] = thread
        thread.start()
        return ActuationLaunch(project_id, source_request_id, task_id, "aibroker", "launching")

    def _run_broker_worker_thread(self, source_request_id: str, request: AIRoleRequest) -> None:
        try:
            result = self._ai_execution_port.execute(request) if self._ai_execution_port else None
            if result is None:
                raise RuntimeError("AIBroker execution port became unavailable")
        except Exception as exc:
            self._update_record(
                source_request_id, state="failed",
                reason="AIBroker worker lifecycle error: {0}".format(exc),
                completed_at=utc_now_iso(),
            )
            return
        resource = result.resource_context
        resource_payload = None
        if resource is not None:
            resource_payload = {
                "resource_id": resource.resource_id,
                "provider": resource.provider,
                "account": resource.account,
                "model": resource.model,
            }
        if result.status == "succeeded":
            state = "completed"
        elif result.status == "cancelled":
            state = "cancelled"
        else:
            state = "failed"
        self._update_record(
            source_request_id,
            state=state,
            completed_at=utc_now_iso(),
            broker_status=result.status,
            dispatch_id=result.dispatch_id,
            decision_id=result.decision_id,
            execution_id=result.execution_id,
            session_id=result.session_id,
            backend_run_id=result.execution_id or result.dispatch_id,
            resource_context=resource_payload,
            usage_source=result.usage_source,
            reason=result.error,
        )

    def _update_record(self, source_request_id: str, **changes: Any) -> None:
        with self._lock:
            ledger = self._load_ledger()
            record = ledger["executions"].get(source_request_id)
            if not isinstance(record, dict):
                return
            record.update(changes)
            self._save_ledger(ledger)
            status_record = copy.deepcopy(record)
        write_execution_status(status_record, self.runtime_root)

    def _run_worker_thread(
        self, source_request_id: str, request: AgentRequest, backend: AgentBackend,
        backends: dict[str, AgentBackend], fallback_ids: tuple[str, ...],
    ) -> None:
        try:
            asyncio.run(
                self._run_worker(source_request_id, request, backend, backends, fallback_ids)
            )
        except Exception as exc:  # fail closed; never auto-retry
            self._update_record(
                source_request_id,
                state="failed",
                reason="worker lifecycle error: {0}".format(exc),
                completed_at=utc_now_iso(),
            )

    async def _run_worker(
        self, source_request_id: str, request: AgentRequest, backend: AgentBackend,
        backends: dict[str, AgentBackend], fallback_ids: tuple[str, ...],
    ) -> None:
        run = await backend.start(request)
        self._update_record(
            source_request_id,
            state=run.state.value,
            backend_run_id=run.run_id,
            pid=run.pid,
            backend_id=run.backend_id,
        )
        result = await backend.collect(run.run_id)
        first_stdout = str(self.runtime_root / "agent-runs" / run.run_id / "stdout.log")
        first_stderr = str(self.runtime_root / "agent-runs" / run.run_id / "stderr.log")
        if _retryable_provider_failure(result) and fallback_ids:
            with self._lock:
                record = copy.deepcopy(
                    self._load_ledger()["executions"].get(source_request_id, {})
                )
            truth = read_repository_truth(request.working_directory)
            unchanged = (
                truth.valid
                and truth.branch == record.get("branch")
                and truth.head == record.get("head")
                and truth.status_hash == record.get("launch_status_hash")
            )
            if not unchanged:
                self._update_record(
                    source_request_id, state="failed", exit_code=result.exit_code,
                    completed_at=utc_now_iso(), stdout_path=first_stdout,
                    stderr_path=first_stderr,
                    reason="runtime fallback refused because repository changed after provider failure",
                )
                return
            fallback = None
            for fallback_id in fallback_ids:
                candidate = backends.get(fallback_id)
                if candidate is None:
                    continue
                try:
                    status = candidate.probe()
                    caps = candidate.capabilities()
                except Exception:
                    continue
                if (status.available and status.quota is not QuotaState.EXHAUSTED
                        and request.role in caps.roles
                        and request.required_capabilities.issubset(caps.tags)):
                    fallback = candidate
                    break
            if fallback is None:
                self._update_record(
                    source_request_id, state="failed", exit_code=result.exit_code,
                    completed_at=utc_now_iso(), stdout_path=first_stdout,
                    stderr_path=first_stderr,
                    reason="retryable provider failure but no healthy fallback backend is available",
                )
                return
            self._update_record(
                source_request_id,
                state="launching",
                fallback_from_backend=backend.backend_id,
                fallback_from_run_id=run.run_id,
                fallback_from_exit_code=result.exit_code,
                fallback_from_stdout_path=first_stdout,
                fallback_from_stderr_path=first_stderr,
                fallback_reason="retryable provider quota failure",
                fallback_attempted_at=utc_now_iso(),
                backend_id=fallback.backend_id,
            )
            run = await fallback.start(request)
            self._update_record(
                source_request_id, state=run.state.value, backend_run_id=run.run_id,
                pid=run.pid, backend_id=run.backend_id,
            )
            result = await fallback.collect(run.run_id)

        terminal = result.state.value
        if result.state not in (
            AgentRunState.COMPLETED, AgentRunState.FAILED, AgentRunState.CANCELLED,
        ):
            terminal = "failed"
        self._update_record(
            source_request_id, state=terminal, exit_code=result.exit_code,
            completed_at=utc_now_iso(),
            stdout_path=str(self.runtime_root / "agent-runs" / run.run_id / "stdout.log"),
            stderr_path=str(self.runtime_root / "agent-runs" / run.run_id / "stderr.log"),
        )

    def _load_decisions(self) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for filename in ("websol-decisions.json", "review-decisions.json"):
            data = read_json(self.runtime_root / filename, None)
            if not isinstance(data, dict) or not isinstance(data.get("decisions"), dict):
                continue
            for request_id, record in data["decisions"].items():
                if not isinstance(request_id, str) or not isinstance(record, dict):
                    continue
                if request_id in merged and merged[request_id] != record:
                    raise RuntimeError("conflicting decision identity across decision ledgers")
                merged[request_id] = record
        return merged

    @staticmethod
    def _project_has_execution_history(ledger: dict[str, Any], project_id: str) -> bool:
        return any(
            record.get("project_id") == project_id and record.get("state") != "blocked"
            for record in ledger["executions"].values()
            if isinstance(record, dict)
        )

    def _advance_decisions(
        self,
        projects: dict[str, dict[str, Any]],
        snapshots: dict[str, dict[str, Any]],
    ) -> list[ActuationLaunch]:
        launches: list[ActuationLaunch] = []
        decisions = self._load_decisions()
        ordered = sorted(
            decisions.items(),
            key=lambda item: (str(item[1].get("consumed_at") or ""), item[0]),
        )
        accepted = {
            ("next", NextAction.NEXT_TASK.value),
            ("remediate", NextAction.CONTINUE_CURRENT_STAGE.value),
        }
        for request_id, record in ordered:
            with self._lock:
                if request_id in self._load_ledger()["executions"]:
                    continue
            if record.get("disposition") != "apply":
                continue
            decision = _non_blank_config(record.get("decision"))
            next_action = _non_blank_config(record.get("next_action"))
            if (decision, next_action) not in accepted:
                continue
            project_id = _non_blank_config(record.get("project_id"))
            if project_id is None:
                continue
            task_id = _non_blank_config(record.get("task_id"))
            branch = _non_blank_config(record.get("branch"))
            head = _non_blank_config(record.get("head"))
            if task_id is None or branch is None or head is None:
                self._record_blocked(request_id, project_id, "decision identity is incomplete")
                continue
            if record.get("role") != WebSolRole.REVIEWER.value or record.get("event") != WebSolEvent.WORKER_DONE.value:
                self._record_blocked(
                    request_id, project_id, "V1 actuation requires WORKER_DONE reviewer flow",
                    task_id=task_id,
                )
                continue
            project = projects.get(project_id)
            snapshot = snapshots.get(project_id)
            if project is None or snapshot is None:
                self._record_blocked(
                    request_id, project_id,
                    "project configuration or monitor snapshot unavailable",
                    task_id=task_id,
                )
                continue
            policy, error = _execution_policy(project)
            if policy is None:
                self._record_blocked(request_id, project_id, error, task_id=task_id)
                continue
            if next_action not in policy["allowed_next_actions"]:
                self._record_blocked(
                    request_id, project_id,
                    "next_action is not owner-authorized by project execution policy",
                    task_id=task_id,
                )
                continue
            if decision == "remediate":
                reviewed_hash = _non_blank_config(record.get("review_status_hash"))
                if reviewed_hash is None:
                    self._record_blocked(
                        request_id, project_id,
                        "remediation decision lacks reviewed dirty fingerprint",
                        task_id=task_id, source_kind="remediation",
                    )
                    continue
                # Exact remediation is anchored to the reviewed task identity and
                # reviewed repository truth, not to the task currently advertised
                # by agent/next.md. A completed Worker may already have advanced
                # next.md for NEXT_TASK handoff before review; remediation must
                # still repair the reviewed task without starting that later task.
                _guard_task, guard_error = self._fresh_guard(
                    project, snapshot,
                    expected_branch=branch, expected_head=head,
                    expected_status_hash=reviewed_hash,
                )
                launch_task = task_id if _guard_task is not None else None
                source_kind = "remediation"
                prompt = (
                    str(policy["remediation_prompt"])
                    + "\nReviewed task identity: {0}. Remediate only this reviewed task. "
                    + "If agent/next.md already advertises a later task, do not implement "
                    + "that later task; preserve the handoff unless the review gap itself "
                    + "requires correcting it."
                ).format(task_id)
            else:
                launch_task, guard_error = self._fresh_guard(
                    project, snapshot,
                    expected_branch=branch, expected_head=head,
                    must_advance_from=task_id,
                )
                source_kind = "decision"
                prompt = str(policy["worker_prompt"])
            if launch_task is None:
                self._record_blocked(
                    request_id, project_id, guard_error,
                    task_id=task_id, source_kind=source_kind,
                )
                continue
            launch = self._launch(
                project,
                source_request_id=request_id,
                source_kind=source_kind,
                task_id=launch_task,
                source_task_id=task_id,
                branch=branch,
                head=head,
                worker_prompt=prompt,
                policy=policy,
            )
            if launch is not None:
                launches.append(launch)
        return launches

    def start_control(
        self, project: dict[str, Any], snapshot: dict[str, Any], source_request_id: str
    ) -> Optional[ActuationLaunch]:
        """Start the current executable task for one stateless owner continue command."""
        project_id = str(project.get("project_id") or "")
        policy, error = _execution_policy(project)
        if policy is None:
            self._record_blocked(source_request_id, project_id, error, source_kind="control")
            return None
        truth = read_repository_truth(project.get("repo_path") or "")
        if not truth.valid:
            self._record_blocked(source_request_id, project_id, "repository truth unavailable", source_kind="control")
            return None
        with self._lock:
            ledger = self._load_ledger()
            broker_rows = [
                row for row in ledger["executions"].values()
                if isinstance(row, dict) and row.get("project_id") == project_id and row.get("engine") == "aibroker"
            ]
            latest_any_broker = max(
                broker_rows, key=lambda row: str(row.get("completed_at") or row.get("recovered_at") or row.get("started_at") or ""),
                default=None,
            )
            if latest_any_broker is not None and latest_any_broker.get("state") == "recovery_required" and not latest_any_broker.get("recovery_safe_retry"):
                self._record_blocked(source_request_id, project_id, "latest AIBroker Worker recovery is not safe to retry", source_kind="control")
                return None
            completed_broker = [row for row in broker_rows if row.get("state") == "completed"]
            latest_broker = max(
                completed_broker, key=lambda row: str(row.get("completed_at") or row.get("started_at") or ""),
                default=None,
            )
            if latest_broker is not None and latest_broker is latest_any_broker:
                worker_request_id = str(latest_broker.get("source_request_id") or "")
                review_id = "ai_review:" + worker_request_id
                reviews_raw = read_json(self.runtime_root / "ai-reviewer.json", {})
                reviews = reviews_raw.get("reviews") if isinstance(reviews_raw, dict) else None
                review = reviews.get(review_id) if isinstance(reviews, dict) else None
                if not isinstance(review, dict) or review.get("state") != "completed":
                    self._record_blocked(source_request_id, project_id, "latest AIBroker Worker review is unresolved", source_kind="control")
                    return None
                if review_id not in ledger["executions"]:
                    self._record_blocked(source_request_id, project_id, "latest AIBroker Worker review transition is not yet applied", source_kind="control")
                    return None
        task_id = _current_task_id(snapshot)
        if task_id is None:
            self._record_blocked(source_request_id, project_id, "current task id is unavailable", source_kind="control")
            return None
        launch_task, guard_error = self._fresh_guard(
            project, snapshot, expected_branch=truth.branch, expected_head=truth.head,
            expected_task_id=task_id,
        )
        if launch_task is None:
            self._record_blocked(source_request_id, project_id, guard_error, task_id=task_id, source_kind="control")
            return None
        return self._launch(
            project, source_request_id=source_request_id, source_kind="control",
            task_id=launch_task, source_task_id=None, branch=truth.branch, head=truth.head,
            worker_prompt=str(policy["worker_prompt"]), policy=policy,
        )

    def _advance_owner_start(
        self, projects: dict[str, dict[str, Any]], snapshots: dict[str, dict[str, Any]],
    ) -> list[ActuationLaunch]:
        launches: list[ActuationLaunch] = []
        for project_id, project in projects.items():
            policy, _ = _execution_policy(project)
            if policy is None or policy.get("owner_start") is None: continue
            token = policy["owner_start"]; request_id = str(token["request_id"]); task_id = str(token["task_id"])
            with self._lock:
                if request_id in self._load_ledger()["executions"]: continue
            snapshot = snapshots.get(project_id)
            if snapshot is None: self._record_blocked(request_id, project_id, "monitor snapshot unavailable", task_id=task_id, source_kind="owner_start"); continue
            truth = read_repository_truth(project.get("repo_path") or "")
            if not truth.valid: self._record_blocked(request_id, project_id, "repository truth unavailable", task_id=task_id, source_kind="owner_start"); continue
            launch_task, error = self._fresh_guard(project, snapshot, expected_branch=truth.branch, expected_head=truth.head, expected_task_id=task_id)
            if launch_task is None: self._record_blocked(request_id, project_id, error, task_id=task_id, source_kind="owner_start"); continue
            launch = self._launch(project, source_request_id=request_id, source_kind="owner_start", task_id=launch_task, source_task_id=None, branch=truth.branch, head=truth.head, worker_prompt=str(policy["worker_prompt"]), policy=policy)
            if launch is not None: launches.append(launch)
        return launches

    def _advance_bootstrap(
        self,
        projects: dict[str, dict[str, Any]],
        snapshots: dict[str, dict[str, Any]],
    ) -> list[ActuationLaunch]:
        launches: list[ActuationLaunch] = []
        for project_id, project in projects.items():
            policy, _ = _execution_policy(project)
            if policy is None or policy.get("bootstrap") is None:
                continue
            bootstrap = policy["bootstrap"]
            request_id = str(bootstrap["request_id"])
            task_id = str(bootstrap["task_id"])
            with self._lock:
                ledger = self._load_ledger()
                if request_id in ledger["executions"]:
                    continue
                if self._project_has_execution_history(ledger, project_id):
                    continue
            snapshot = snapshots.get(project_id)
            if snapshot is None:
                self._record_blocked(
                    request_id, project_id, "monitor snapshot unavailable",
                    task_id=task_id, source_kind="bootstrap",
                )
                continue
            truth = read_repository_truth(project.get("repo_path") or "")
            if not truth.valid:
                self._record_blocked(
                    request_id, project_id, "repository truth unavailable",
                    task_id=task_id, source_kind="bootstrap",
                )
                continue
            launch_task, guard_error = self._fresh_guard(
                project,
                snapshot,
                expected_branch=truth.branch,
                expected_head=truth.head,
                expected_task_id=task_id,
            )
            if launch_task is None:
                self._record_blocked(
                    request_id, project_id, guard_error,
                    task_id=task_id, source_kind="bootstrap",
                )
                continue
            launch = self._launch(
                project,
                source_request_id=request_id,
                source_kind="bootstrap",
                task_id=launch_task,
                source_task_id=None,
                branch=truth.branch,
                head=truth.head,
                worker_prompt=str(policy["worker_prompt"]),
                policy=policy,
            )
            if launch is not None:
                launches.append(launch)
        return launches

    def advance(
        self,
        summary: Any,
        config_path: Path | str,
        *,
        decision_summary: Any = None,
    ) -> list[ActuationLaunch]:
        """Act on durable decisions, one-shot owner starts, then optional bootstrap.

        Decision actuation may use the managed-run projected snapshot that was
        reviewed and consumed in the same daemon tick. Owner-start/bootstrap
        gates intentionally remain anchored to the raw monitor snapshot.
        """
        config = load_projects_config(config_path)
        projects = _project_map(config)
        raw_snapshots = _snapshot_map(summary)
        decision_snapshots = _snapshot_map(
            summary if decision_summary is None else decision_summary
        )
        launches = self._advance_decisions(projects, decision_snapshots)
        launches.extend(self._advance_owner_start(projects, raw_snapshots))
        launches.extend(self._advance_bootstrap(projects, raw_snapshots))
        return launches
