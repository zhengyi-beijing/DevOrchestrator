"""Owner-authorized NEXT_TASK and reviewed-remediation Worker actuation.

Contract: docs/TRANSITION_EXECUTOR_CONTRACT.md.  This Core component is the
only V1 execution boundary.  Browser transport and Decision Guard remain
non-executing; this executor acts only after their accepted disposition and an
explicit local project execution policy.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from dev_orchestrator.ai.contracts import AIRoleRequest, ROLE_RESOURCE_FAILURES, ResourceContext
from dev_orchestrator.ai.execution_port import AIExecutionPort, MANAGED_INTERRUPT_REASON
from dev_orchestrator.accounting import ExecutionRecorder, FailureMemory, environment_for_project
from dev_orchestrator.agents.base import AgentBackend
from dev_orchestrator.agents.backends.agy import AgyBackend
from dev_orchestrator.agents.backends.dsh import DshBackend
from dev_orchestrator.agents.models import AgentRequest, AgentResult, AgentRole, AgentRunState, QuotaState
from dev_orchestrator.agents.registry import BackendRegistry
from dev_orchestrator.agents.router import AgentRouter
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.staged_roadmap import read_successor
from dev_orchestrator.core.successor_consistency import (
    SuccessorResolution,
    reconcile_roadmap_successor,
    resolve_successor,
)
from dev_orchestrator.core.lifecycle_authority import (
    TERMINAL_LIFECYCLE_STATES,
    active_owners,
    advertised_task_id,
    epoch_for,
    evaluate_lifecycle_invariants,
    invariant_payload,
    lifecycle_state_from_snapshot,
    new_authority,
    open_declaration_gate,
    resolve_declaration_gate,
    source_ownership_blockers,
    task_has_terminal_settlement,
    transition_id_for,
)
from dev_orchestrator.core.control_plane_contract import (
    evaluate_launch_declaration,
    inject_control_plane_contract,
    load_control_plane_declaration,
)
from dev_orchestrator.core.task_status import parse_task_status
from dev_orchestrator.core.workflow_policy import inject_workflow_policy

from dev_orchestrator.core.execution_lifecycle import (
    ObligationPersistError,
    close_lineage_record,
    load_execution_lineage,
    open_execution_obligation,
    record_execution_observation,
)
from dev_orchestrator.core.project_status import write_execution_status
from dev_orchestrator.core.websol import NextAction, WebSolEvent, WebSolRole
from dev_orchestrator.monitor.telemetry import extract_task_id
from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

logger = logging.getLogger(__name__)

ACTUATION_FILE = "transition-executor.json"
# Key used when a contained actuation fault cannot be attributed to a project.
_UNATTRIBUTED_PROJECT = "_unattributed"
_LEDGER_VERSION = 2
# The exact block ControlCommandCoordinator records when the Planner refuses a
# staged handoff solely because HEAD moved since it was recorded.
_STALE_ANCHOR_HANDOFF_REFUSAL = (
    "automatic planner handoff failed: repository moved since review"
)
_TRANSIENT_DIRTY_HANDOFF_REFUSAL = (
    "automatic planner handoff failed: planner requires a clean repository"
)
_ACTIVE_STATES = frozenset({"launching", "running"})
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "explicitly_reconciled"})

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
    return {
        "version": _LEDGER_VERSION,
        "executions": {},
        "lifecycle": {},
        "transitions": {},
    }


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


def _advertised_task_id(snapshot: dict[str, Any]) -> Optional[str]:
    advertised = extract_task_id(snapshot.get("next_title"))
    return advertised or _current_task_id(snapshot)


def _readiness_allows_launch(
    snapshot: dict[str, Any],
    *,
    anchor_task_id: Optional[str] = None,
    predecessor_task_id: Optional[str] = None,
) -> bool:
    readiness = snapshot.get("readiness")
    if isinstance(readiness, dict):
        if readiness.get("code") == "READINESS_TASK_ID_MISMATCH":
            file_task = readiness.get("task_id")
            if anchor_task_id is not None and file_task == anchor_task_id:
                return True
            if predecessor_task_id is not None and file_task == predecessor_task_id:
                if readiness.get("migration_candidate") == "ready_to_run":
                    return True
                if parse_task_status(snapshot.get("next_status")).is_ready_to_run():
                    return True
            return False
        if not readiness.get("valid", True) or readiness.get("stale", False) or readiness.get("state") == "invalid":
            return False
        if readiness.get("code") in {
            "READINESS_SCHEMA_INVALID",
            "READINESS_TOKEN_UNRESOLVABLE",
        }:
            return False
    return True


def _next_task_ready(
    snapshot: dict[str, Any],
    *,
    anchor_task_id: Optional[str] = None,
    predecessor_task_id: Optional[str] = None,
) -> bool:
    if not _readiness_allows_launch(
        snapshot,
        anchor_task_id=anchor_task_id,
        predecessor_task_id=predecessor_task_id,
    ):
        return False
    readiness = snapshot.get("readiness")
    if isinstance(readiness, dict):
        if readiness.get("state") == "ready_to_run":
            return True
        if readiness.get("code") == "READINESS_TASK_ID_MISMATCH":
            file_task = readiness.get("task_id")
            if predecessor_task_id is not None and file_task == predecessor_task_id:
                if readiness.get("migration_candidate") == "ready_to_run":
                    return True
                return parse_task_status(snapshot.get("next_status")).is_ready_to_run()
            if anchor_task_id is not None and file_task == anchor_task_id:
                return True
        if readiness.get("source") == "legacy_markdown":
            return parse_task_status(snapshot.get("next_status")).is_ready_to_run()
        return False
    return parse_task_status(snapshot.get("next_status")).is_ready_to_run()


def _task_marked_complete(snapshot: dict[str, Any]) -> bool:
    return parse_task_status(snapshot.get("next_status")).is_completed()



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

_PRE_EXECUTION_WORKTREE_SAFETY_MARKER = "worktreeunsafeerror"
_NEGATIVE_PROVIDER_EVIDENCE_FIELDS = (
    "session_id", "provider_output_observed", "first_output_at",
)
_GENERATED_ONLY_PORCELAIN_ENTRIES = frozenset({"?? graphify-out/"})
_LEGACY_WORKTREE_UNSAFE_REASON = (
    "worktreeunsafeerror: dirty worktree requires deterministic recovery before writable reuse"
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


def is_pre_provider_worktree_unsafe_failure(record: dict[str, Any]) -> bool:
    """Recognize the one durable, no-provider-work remediation failure shape.

    This shared predicate is the recovery contract for both Worker retry and
    stale-review re-anchoring.  Allocation identifiers alone never establish
    provider work; any session, output, or work evidence fails closed.
    """
    if (
        record.get("engine") != "aibroker"
        or record.get("source_kind") != "remediation"
        or record.get("state") != "failed"
    ):
        return False
    reason = str(record.get("reason") or "").casefold()
    if _PRE_EXECUTION_WORKTREE_SAFETY_MARKER not in reason:
        return False
    if record.get("broker_status") != "failed":
        return False
    if "session_id" not in record or record.get("session_id") is not None:
        return False
    if (
        _non_blank_config(record.get("output")) is not None
        or record.get("provider_work_observed") is True
        or record.get("provider_output_observed") is True
        or record.get("first_output_at") is not None
    ):
        return False
    has_modern_negative_evidence = all(
        name in record for name in _NEGATIVE_PROVIDER_EVIDENCE_FIELDS
    )
    if has_modern_negative_evidence:
        return (
            record.get("provider_output_observed") is False
            and record.get("first_output_at") is None
        )
    return (
        str(record.get("reason") or "").casefold() == _LEGACY_WORKTREE_UNSAFE_REASON
        and all(record.get(name) for name in (
            "dispatch_id", "decision_id", "execution_id", "resource_context",
        ))
    )


def _legacy_not_ready_block(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and record.get("state") == "blocked"
        and record.get("source_kind") == "decision"
        and record.get("reason") == "project is not READY_TO_RUN"
    )


def _legacy_no_next_settle(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and record.get("state") == "settled"
        and record.get("source_kind") == "decision"
        and record.get("outcome") == "task_complete"
        and record.get("reason") in (
            "reviewed task is COMPLETE and no next executable task is advertised",
            "review accepted current READY_TO_RUN task and no next executable task is advertised",
        )
        and not record.get("next_task_id")
    )


def _legacy_not_advanced_block(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and record.get("state") == "blocked"
        and record.get("source_kind") == "decision"
        and record.get("reason") == "NEXT_TASK refused because agent/next.md has not advanced"
    )


def _transient_remediation_state_block(record: Any) -> bool:
    """A reviewer REMEDIATE blocked only by a stale lifecycle snapshot may replay.

    The decision/repository identity is revalidated on every replay.  No other
    blocked remediation reason is eligible, so repository, worktree, owner
    pause, provider, or concurrency failures remain durable barriers.
    """
    return (
        isinstance(record, dict)
        and record.get("state") == "blocked"
        and record.get("source_kind") == "remediation"
        and record.get("reason") == "project state is not eligible for exact remediation"
    )


def _is_declaration_gate_replayable(record: Any, authority: Any, current_head: str) -> bool:
    """A launch request blocked by CONTROL_PLANE_DECLARATION_REQUIRED may replay against a new HEAD,
    or on the same HEAD if the block was caused by a transient evaluation failure."""
    if not (isinstance(record, dict) and record.get("state") == "blocked"):
        return False
    if record.get("blocked_gate_code") != "CONTROL_PLANE_DECLARATION_REQUIRED":
        return False
    if not (isinstance(authority, dict) and isinstance(authority.get("owner_gate"), dict)):
        return False
    gate = authority["owner_gate"]
    if gate.get("code") != "CONTROL_PLANE_DECLARATION_REQUIRED":
        return False
    if gate.get("gate_id") != record.get("gate_id"):
        return False

    is_transient = (
        (record.get("gate_id") or "").endswith(":unevaluable")
        or "unevaluable" in str(record.get("reason") or "")
        or bool((record.get("evidence") or {}).get("transient"))
    )
    if str(record.get("head") or "") == current_head:
        return is_transient

    return True


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
        progress_channel: Optional[Any] = None,
        accounting: ExecutionRecorder | None = None,
        failure_memory: FailureMemory | None = None,
        failure_memory_max_chars: int = 2000,
        owner_store: OwnerControlStore | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.runtime_root / ACTUATION_FILE
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._ai_execution_port = ai_execution_port
        self._progress_channel = progress_channel
        self.accounting = accounting
        self.failure_memory = failure_memory
        self.failure_memory_max_chars = failure_memory_max_chars
        self.owner_store = owner_store or OwnerControlStore(self.runtime_root)
        self._actuation_errors: dict[str, dict[str, Any]] = {}
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

    def record_actuation_error(
        self,
        project_id: str | None,
        error: BaseException | str,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
        source_kind: str = "decision",
        phase: str = "decision_actuation",
    ) -> dict[str, Any]:
        """Record one contained actuation fault against a single project.

        A contained fault is deliberately not written to the execution ledger:
        it must never consume or settle the decision or handoff it failed to
        actuate, so the next tick retries it. It is instead surfaced through
        ``drain_actuation_errors`` so the daemon keeps the heartbeat degraded
        and publishes a per-project error rather than swallowing the fault.
        """
        detail = (
            "{0}: {1}".format(type(error).__name__, error)
            if isinstance(error, BaseException) else str(error)
        )
        entry = {
            "project_id": project_id,
            "phase": phase,
            "source_kind": source_kind,
            "request_id": request_id,
            "task_id": task_id,
            "error": detail,
            "recorded_at": utc_now_iso(),
        }
        logger.exception(
            "contained actuation fault project=%s phase=%s request=%s: %s",
            project_id, phase, request_id, detail,
        )
        key = project_id or _UNATTRIBUTED_PROJECT
        with self._lock:
            # First fault wins: actuation phases run in order, so the earliest
            # fault carries the decision or handoff identity that failed, and a
            # later phase failing for the same root cause must not hide it.
            previous = self._actuation_errors.get(key)
            if isinstance(previous, dict):
                previous["fault_count"] = int(previous.get("fault_count") or 1) + 1
                phases = previous.setdefault("phases", [previous.get("phase")])
                if phase not in phases:
                    phases.append(phase)
                return previous
            entry["fault_count"] = 1
            entry["phases"] = [phase]
            self._actuation_errors[key] = entry
        if project_id and self._progress_channel is not None:
            try:
                self._progress_channel.emit(
                    {"project_id": project_id}, "BLOCKED",
                    task_id=task_id,
                    occurrence_key="{0}:actuation-fault".format(request_id or phase),
                    details={
                        "code": "ACTUATION_FAULT_CONTAINED",
                        "reason": detail,
                        "phase": phase,
                        "retryable": True,
                    },
                )
            except Exception:  # noqa: BLE001 - reporting must never mask the fault
                logger.exception("progress emit failed for contained actuation fault")
        return entry

    def drain_actuation_errors(self) -> dict[str, dict[str, Any]]:
        """Return and clear the contained actuation faults seen since last drain."""
        with self._lock:
            errors = self._actuation_errors
            self._actuation_errors = {}
        return errors

    def actuation_errors(self) -> dict[str, dict[str, Any]]:
        """Return the currently recorded contained actuation faults."""
        with self._lock:
            return copy.deepcopy(self._actuation_errors)

    def set_owner_paused(
        self, project_id: str, paused: bool, *, command_id: str,
        action: str, reason: str | None = None,
    ) -> dict[str, Any]:
        """Serialize pause changes with final Worker launch checks."""
        with self._lock:
            return self.owner_store.set_paused(
                project_id, paused, command_id=command_id, action=action, reason=reason
            )

    def _launch_barrier_reason(self, project_id: str, source_kind: str) -> str | None:
        state = self.owner_store.project_state(project_id)
        if state.get("paused"):
            return "owner pause blocked Worker launch"
        if source_kind in {"owner_start", "bootstrap"} and state.get("suppress_static_starts"):
            return "runtime owner control suppresses legacy static launch"
        return None

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
        lifecycle = data.get("lifecycle")
        transitions = data.get("transitions")
        return {
            "version": _LEDGER_VERSION,
            "executions": clean,
            "lifecycle": {
                key: value for key, value in (lifecycle or {}).items()
                if isinstance(key, str) and isinstance(value, dict)
            } if isinstance(lifecycle, dict) else {},
            "transitions": {
                key: value for key, value in (transitions or {}).items()
                if isinstance(key, str) and isinstance(value, dict)
            } if isinstance(transitions, dict) else {},
        }

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
                    fact_request_id = fact.get("request_id")
                    if (
                        not isinstance(fact_request_id, str)
                        or not fact_request_id.strip()
                        or fact_request_id.strip() != str(broker_request_id)
                    ):
                        record["broker_recovery_error"] = (
                            "Broker recovery status request_id is missing or does not match the durable broker_request_id"
                        )
                        fact = None
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
                        try:
                            close_lineage_record(
                                self.runtime_root,
                                str(record.get("project_id") or ""),
                                str(record.get("source_request_id") or ""),
                                terminal_outcome="completed",
                                completed_at=record["completed_at"],
                            )
                        except Exception:
                            pass
                    elif fact.get("status") == "failed" and fact.get("execution_error") != MANAGED_INTERRUPT_REASON:
                        record["state"] = "failed"
                        record["completed_at"] = fact.get("finished_at") or recovered_at
                        record["reason"] = fact.get("execution_error") or "Broker execution failed before daemon recovery"
                        try:
                            close_lineage_record(
                                self.runtime_root,
                                str(record.get("project_id") or ""),
                                str(record.get("source_request_id") or ""),
                                terminal_outcome="failed",
                                reason=record["reason"],
                                completed_at=record["completed_at"],
                            )
                        except Exception:
                            pass
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
            for transition in ledger.setdefault("transitions", {}).values():
                if not isinstance(transition, dict):
                    continue
                source_id = _non_blank_config(transition.get("source_request_id"))
                state = str(transition.get("state") or "")
                execution = ledger["executions"].get(source_id) if source_id else None
                if execution is None and state == "ready":
                    handoff = transition.get("handoff_record")
                    if isinstance(handoff, dict) and source_id:
                        ledger["executions"][source_id] = copy.deepcopy(handoff)
                        execution = ledger["executions"][source_id]
                        transition["replayed_at"] = utc_now_iso()
                        changed = True
                if isinstance(execution, dict) and execution.get("handoff_consumed") is True:
                    if state not in {"published", "completed"}:
                        transition["state"] = "published"
                        transition["published_at"] = execution.get("handoff_consumed_at") or utc_now_iso()
                        transition["updated_at"] = utc_now_iso()
                        changed = True
                    project_id = str(transition.get("project_id") or "")
                    authority = ledger.setdefault("lifecycle", {}).get(project_id)
                    if isinstance(authority, dict):
                        authority["generation"] = int(transition.get("generation") or authority.get("generation") or 0)
                        authority["source_task_id"] = transition.get("source_task_id")
                        authority["current_task_id"] = transition.get("target_task_id")
                        authority["active_transition_id"] = transition.get("transition_id")
                        authority["updated_at"] = utc_now_iso()
                        authority["recovery_epoch_id"] = epoch_for(authority)
                        changed = True
            if changed:
                self._save_ledger(ledger)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load_ledger())

    def reconcile_lifecycle_authority(
        self,
        summary: Any,
        *,
        planner_state: Any = None,
        reviewer_state: Any = None,
    ) -> dict[str, Any]:
        """Converge repository/role projections into the one runtime authority.

        A repository successor published while source-task ownership still
        exists is retained as a projection only.  The authority advances after
        that ownership is terminal and a deterministic transition record proves
        the lineage.
        """
        snapshots = _snapshot_map(summary)
        decisions_state = read_json(self.runtime_root / "review-decisions.json", {})
        if not isinstance(decisions_state, dict):
            decisions_state = {}
        with self._lock:
            ledger = self._load_ledger()
            lifecycle = ledger.setdefault("lifecycle", {})
            transitions = ledger.setdefault("transitions", {})
            for project_id, snapshot in snapshots.items():
                repo_task = advertised_task_id(snapshot)
                if not repo_task:
                    continue
                authority = lifecycle.get(project_id)
                owners = active_owners(
                    project_id, ledger,
                    planner_state if isinstance(planner_state, dict) else None,
                    reviewer_state if isinstance(reviewer_state, dict) else None,
                )
                owner_tasks = {
                    str(owner.get("task_id") or "") for owner in owners
                    if owner.get("task_id")
                }
                if not isinstance(authority, dict):
                    initial_task = next(iter(owner_tasks)) if len(owner_tasks) == 1 else repo_task
                    initial_state = lifecycle_state_from_snapshot(snapshot)
                    authority = new_authority(project_id, initial_task, initial_state)
                    lifecycle[project_id] = authority

                prior_gate = authority.get("owner_gate")
                prior_gate_owners = (
                    prior_gate.get("owners") if isinstance(prior_gate, dict) else None
                )
                legacy_owner_gate = (
                    isinstance(prior_gate, dict)
                    and prior_gate.get("code") == "SINGLE_ACTIVE_LIFECYCLE_OWNER"
                    and isinstance(prior_gate_owners, list)
                    and prior_gate_owners
                    and all(
                        isinstance(owner, dict) and owner.get("role") == "review_obligation"
                        for owner in prior_gate_owners
                    )
                )
                if (
                    not owners
                    and legacy_owner_gate
                    and int(authority.get("generation") or 0) == 0
                ):
                    prior_task = authority.get("current_task_id")
                    for transition in transitions.values():
                        if (
                            isinstance(transition, dict)
                            and transition.get("project_id") == project_id
                            and transition.get("state") in {"intent", "waiting_source"}
                            and transition.get("source_task_id") == prior_task
                        ):
                            transition["state"] = "owner_gate"
                            transition["reason"] = "superseded historical review-obligation migration"
                            transition["updated_at"] = utc_now_iso()
                    authority.update({
                        "current_task_id": repo_task,
                        "source_task_id": None,
                        "active_transition_id": None,
                        "active_owner": None,
                        "owner_gate": None,
                        "lifecycle_state": lifecycle_state_from_snapshot(snapshot),
                        "legacy_bootstrap_reconciled": {
                            "from_task_id": prior_task,
                            "reason": "historical resolved review obligations are not active owners",
                            "recorded_at": utc_now_iso(),
                        },
                    })

                current_task = str(authority.get("current_task_id") or repo_task)
                terminally_settled = (
                    repo_task == current_task
                    and task_has_terminal_settlement(
                        ledger, project_id, current_task,
                    )
                )
                if terminally_settled:
                    # A settled task_complete row is durable terminal evidence.
                    # Markdown/readiness can lag (the live P18 incident) and
                    # must never resurrect the same authoritative task.  A
                    # future successor remains possible: once its handoff is
                    # consumed, current_task_id changes and this task-scoped
                    # fence no longer applies.
                    authority["active_owner"] = None
                    if not authority.get("owner_gate"):
                        authority["lifecycle_state"] = "COMPLETE"
                elif len(owner_tasks) > 1:
                    authority["owner_gate"] = {
                        "code": "SINGLE_ACTIVE_LIFECYCLE_OWNER",
                        "reason": "active lifecycle ownership spans multiple tasks",
                        "owners": copy.deepcopy(owners),
                        "recorded_at": utc_now_iso(),
                    }
                    authority["lifecycle_state"] = "OWNER_GATE"
                elif len(owner_tasks) == 1:
                    owner_task = next(iter(owner_tasks))
                    authority["current_task_id"] = owner_task
                    authority["active_owner"] = copy.deepcopy(owners[0] if len(owners) == 1 else owners)
                    roles = {str(owner.get("role") or "") for owner in owners}
                    # A review obligation whose actuation is durably blocked can
                    # never drain by itself, so pinning the authority to it
                    # while the repository has advanced is an unbounded stall
                    # with no owner-visible state.  Gate instead.
                    blocked_obligations = [
                        owner for owner in owners
                        if owner.get("role") == "review_obligation"
                        and owner.get("actuation_state") in {"blocked", "recovery_required"}
                    ]
                    if authority.get("owner_gate"):
                        authority["lifecycle_state"] = "OWNER_GATE"
                    elif "worker" in roles:
                        authority["lifecycle_state"] = "EXECUTING"
                    elif blocked_obligations and repo_task != owner_task:
                        prior_blocked_gate = authority.get("owner_gate")
                        same_gate = (
                            isinstance(prior_blocked_gate, dict)
                            and prior_blocked_gate.get("code") == "NEXT_TASK_WITHOUT_HANDOFF"
                            and prior_blocked_gate.get("authority_task_id") == owner_task
                            and prior_blocked_gate.get("repository_task_id") == repo_task
                        )
                        authority["owner_gate"] = {
                            "code": "NEXT_TASK_WITHOUT_HANDOFF",
                            "reason": "review actuation is durably blocked and cannot drain autonomously",
                            "repository_task_id": repo_task,
                            "authority_task_id": owner_task,
                            "owners": copy.deepcopy(blocked_obligations),
                            # Durable: repeated ticks must not re-stamp the gate.
                            "recorded_at": (
                                prior_blocked_gate.get("recorded_at") if same_gate
                                else utc_now_iso()
                            ),
                        }
                        authority["lifecycle_state"] = "OWNER_GATE"
                    elif "reviewer" in roles or "review_obligation" in roles:
                        authority["lifecycle_state"] = "REVIEWING"
                    elif "planner" in roles:
                        authority["lifecycle_state"] = "PLANNING"
                    current_task = owner_task
                    if repo_task != owner_task:
                        resolution = resolve_successor(snapshot.get("repo_path") or snapshot.get("root") or "", owner_task)
                        if resolution.successor_task_id == repo_task and resolution.kind in {"successor", "inconsistent"}:
                            generation = int(authority.get("generation") or 0) + 1
                            tid = transition_id_for(project_id, owner_task, repo_task, generation)
                            transitions.setdefault(tid, {
                                "transition_id": tid,
                                "project_id": project_id,
                                "source_task_id": owner_task,
                                "target_task_id": repo_task,
                                "generation": generation,
                                "state": "waiting_source",
                                "idempotency_key": tid,
                                "evidence": {"source": resolution.evidence, "reason": resolution.reason},
                                "recorded_at": utc_now_iso(),
                                "updated_at": utc_now_iso(),
                            })
                            authority["active_transition_id"] = tid
                            authority["source_task_id"] = owner_task
                else:
                    authority["active_owner"] = None
                    if repo_task != current_task:
                        matching = [
                            row for row in transitions.values()
                            if isinstance(row, dict)
                            and row.get("project_id") == project_id
                            and row.get("source_task_id") == current_task
                            and row.get("target_task_id") == repo_task
                            and row.get("state") in {
                                "intent", "waiting_source", "ready", "published",
                                "completed", "waiting_recovery",
                            }
                        ]
                        if not matching:
                            resolution = resolve_successor(
                                snapshot.get("repo_path") or snapshot.get("root") or "", current_task,
                            )
                            if resolution.successor_task_id == repo_task and resolution.kind in {"successor", "inconsistent"}:
                                generation = int(authority.get("generation") or 0) + 1
                                tid = transition_id_for(project_id, current_task, repo_task, generation)
                                transition = {
                                    "transition_id": tid,
                                    "project_id": project_id,
                                    "source_task_id": current_task,
                                    "target_task_id": repo_task,
                                    "generation": generation,
                                    "state": "intent",
                                    "idempotency_key": tid,
                                    "evidence": {"source": "recovered_repository_projection"},
                                    "recorded_at": utc_now_iso(),
                                    "updated_at": utc_now_iso(),
                                }
                                transitions[tid] = transition
                                matching = [transition]
                        publishable = [
                            row for row in matching
                            if row.get("state") in {"published", "completed"}
                        ]
                        if publishable:
                            transition = max(publishable, key=lambda row: int(row.get("generation") or 0))
                            transition["state"] = "completed"
                            transition["updated_at"] = utc_now_iso()
                            authority["generation"] = int(transition.get("generation") or authority.get("generation") or 0)
                            authority["source_task_id"] = current_task
                            authority["current_task_id"] = repo_task
                            authority["active_transition_id"] = transition.get("transition_id")
                            if not (isinstance(authority.get("owner_gate"), dict) and authority["owner_gate"].get("code") == "CONTROL_PLANE_DECLARATION_REQUIRED"):
                                authority["owner_gate"] = None
                        elif matching:
                            # Repository publication alone is only a projection.
                            # Keep the predecessor authoritative until the durable
                            # handoff is replayed and consumed by the Planner.
                            transition = max(matching, key=lambda row: int(row.get("generation") or 0))
                            if transition.get("state") == "waiting_source":
                                transition["state"] = "intent"
                                transition["ownership_blockers"] = []
                                transition["updated_at"] = utc_now_iso()
                            authority["active_transition_id"] = transition.get("transition_id")
                            authority["source_task_id"] = current_task
                            authority["lifecycle_state"] = "TRANSITIONING"
                        else:
                            authority["owner_gate"] = {
                                "code": "CURRENT_TASK_MATCHES_ACTIVE_EXECUTION",
                                "reason": "repository task changed without an authoritative transition",
                                "repository_task_id": repo_task,
                                "authority_task_id": current_task,
                                "recorded_at": utc_now_iso(),
                            }
                            authority["lifecycle_state"] = "OWNER_GATE"
                    if authority.get("current_task_id") == repo_task and not authority.get("owner_gate"):
                        authority["lifecycle_state"] = lifecycle_state_from_snapshot(snapshot)
                if authority.get("owner_gate"):
                    authority["lifecycle_state"] = "OWNER_GATE"

                authority["repository_projection"] = {
                    "task_id": repo_task,
                    "state": lifecycle_state_from_snapshot(snapshot),
                    "matches_authority": repo_task == authority.get("current_task_id"),
                }
                authority["updated_at"] = utc_now_iso()
                authority["recovery_epoch_id"] = epoch_for(authority)
                findings = evaluate_lifecycle_invariants(
                    snapshot=snapshot,
                    executor_state=ledger,
                    planner_state=planner_state if isinstance(planner_state, dict) else None,
                    reviewer_state=reviewer_state if isinstance(reviewer_state, dict) else None,
                    # NEXT_TASK_WITHOUT_HANDOFF is derived only from the
                    # decisions ledger.  Omitting it here made the authority of
                    # record attest that the invariant held while the Watchdog,
                    # reading the same evaluator with the ledger, reported it
                    # violated for the same project and tick.
                    decisions_state=decisions_state,
                )
                authority["invariants"] = invariant_payload(findings)
            self._save_ledger(ledger)
            return copy.deepcopy(ledger)

    def overlay_lifecycle_authority(self, summary: Any) -> Any:
        projected = copy.deepcopy(summary)
        if not isinstance(projected, dict) or not isinstance(projected.get("projects"), list):
            return projected
        ledger = self.state()
        authorities = ledger.get("lifecycle", {})
        for snapshot in projected["projects"]:
            if not isinstance(snapshot, dict):
                continue
            project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
            authority = authorities.get(project_id) if isinstance(authorities, dict) else None
            if not isinstance(authority, dict):
                continue
            telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
            telemetry = copy.deepcopy(telemetry)
            telemetry["task_id"] = authority.get("current_task_id")
            snapshot["telemetry"] = telemetry
            snapshot["authoritative_lifecycle"] = copy.deepcopy(authority)
            state = str(authority.get("lifecycle_state") or "UNKNOWN")
            snapshot["lifecycle_state"] = state
            if state == "EXECUTING":
                snapshot["state"] = "WORKER_RUNNING"
            elif state in {"PLANNING", "REVIEWING", "OWNER_GATE"}:
                snapshot["state"] = state
            elif state == "PENDING_DESIGN":
                snapshot["state"] = "IDLE"
        return projected

    def overlay_managed_runs(self, summary: Any) -> Any:
        """Project managed-run truth onto a deep copy for WORKER_DONE dispatch."""
        projected = copy.deepcopy(summary)
        if not isinstance(projected, dict) or not isinstance(projected.get("projects"), list):
            return projected
        latest: dict[str, dict[str, Any]] = {}
        ledger = self.state(); executions = ledger["executions"]
        lifecycle_barrier: dict[str, str] = {}
        for row in executions.values():
            if not isinstance(row, dict) or row.get("state") not in {"settled", "handoff"}:
                continue
            project_id = _non_blank_config(row.get("project_id"))
            stamp = _non_blank_config(row.get("recorded_at"))
            if project_id and stamp and stamp > lifecycle_barrier.get(project_id, ""):
                lifecycle_barrier[project_id] = stamp
        reviews_raw = read_json(self.runtime_root / "ai-reviewer.json", {})
        reviews = reviews_raw.get("reviews") if isinstance(reviews_raw, dict) else None
        reviews = reviews if isinstance(reviews, dict) else {}
        for record in executions.values():
            if not isinstance(record, dict) or record.get("state") not in (_ACTIVE_STATES | _TERMINAL_STATES):
                continue
            if record.get("engine") == "aibroker" and record.get("state") == "completed":
                source_id = _non_blank_config(record.get("source_request_id"))
                review_id = "ai_review:" + source_id if source_id else None
                review = reviews.get(review_id) if review_id else None
                transition = executions.get(review_id) if review_id else None
                if (
                    isinstance(review, dict) and review.get("state") == "completed"
                    and isinstance(transition, dict) and transition.get("state") in {"settled", "handoff"}
                ):
                    continue
            project_id = _non_blank_config(record.get("project_id"))
            if project_id is None:
                continue
            barrier = lifecycle_barrier.get(project_id)
            record_stamp = str(record.get("completed_at") or record.get("started_at") or "")
            if barrier and record_stamp and record_stamp <= barrier:
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
            advertised_task_id = _advertised_task_id(snapshot)
            record_task_id = _non_blank_config(record.get("task_id"))
            if (
                state in {"failed", "cancelled", "explicitly_reconciled"}
                and advertised_task_id is not None
                and record_task_id is not None
                and advertised_task_id != record_task_id
            ):
                is_ready = _next_task_ready(snapshot) or (snapshot.get("state") == "READY_TO_RUN" and _readiness_allows_launch(snapshot))
                next_updated = parse_utc(snapshot.get("next_updated_at"))
                record_completed = parse_utc(
                    record.get("completed_at") or record.get("recorded_at") or record.get("started_at")
                )
                if (
                    is_ready
                    or _task_marked_complete(snapshot)
                    or (
                        next_updated is not None
                        and record_completed is not None
                        and next_updated > record_completed
                    )
                ):
                    # The repository's task authority advanced after this terminal
                    # failure. Keep the historical execution in the ledger, but do
                    # not let it relabel the newer task as WORKER_FAILED.
                    continue
            worker_state = "starting" if state == "launching" else state
            snapshot["worker"] = {"kind":"task","state":worker_state,"process_alive":state in _ACTIVE_STATES,"pid":record.get("pid"),"started_at":record.get("started_at"),"updated_at":record.get("completed_at") or record.get("started_at"),"exit_code":record.get("exit_code"),"command":"managed {0}".format(record.get("backend_id") or "worker")}
            telemetry = snapshot.get("telemetry")
            telemetry = copy.deepcopy(telemetry) if isinstance(telemetry, dict) else {}
            telemetry["run_id"] = record.get("backend_run_id")
            telemetry["task_id"] = record.get("task_id")
            snapshot["telemetry"] = telemetry
            snapshot["state"] = (
                "WORKER_RUNNING" if state in _ACTIVE_STATES
                else ("WAITING_REVIEW" if state == "completed"
                else (snapshot.get("state") or "READY_TO_RUN" if state == "explicitly_reconciled"
                else "WORKER_FAILED"))
            )
            if record.get("engine") == "aibroker":
                snapshot["worker"]["engine"] = "aibroker"
                snapshot["broker_execution"] = {
                    "broker_request_id": record.get("broker_request_id"),
                    "role_run_id": record.get("role_run_id"),
                    "state": state,
                    "started_at": record.get("started_at"),
                    "engine": "aibroker",
                }
        return self.overlay_lifecycle_authority(projected)

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
        if self._progress_channel is not None:
            self._progress_channel.emit(
                {"project_id": project_id}, "BLOCKED",
                task_id=task_id, occurrence_key=source_request_id,
                details={"reason": reason},
            )

    def _record_settled(
        self, source_request_id: str, project_id: str, reason: str,
        *, task_id: Optional[str] = None, outcome: str = "task_complete",
    ) -> None:
        with self._lock:
            ledger = self._load_ledger()
            existing = ledger["executions"].get(source_request_id)
            if existing is not None and not (
                _legacy_not_ready_block(existing) or _legacy_not_advanced_block(existing)
            ):
                return
            ledger["executions"][source_request_id] = {
                "project_id": project_id, "source_request_id": source_request_id,
                "source_kind": "decision", "task_id": task_id,
                "state": "settled", "outcome": outcome, "reason": reason,
                "recorded_at": utc_now_iso(),
                **({"legacy_reconciled_from": existing} if existing is not None else {}),
            }
            if outcome == "task_complete" and task_id:
                lifecycle = ledger.setdefault("lifecycle", {})
                authority = lifecycle.get(project_id)
                if not isinstance(authority, dict):
                    authority = new_authority(project_id, task_id, "COMPLETE")
                    lifecycle[project_id] = authority
                if authority.get("current_task_id") in (None, task_id):
                    authority["current_task_id"] = task_id
                    authority["active_owner"] = None
                    if not authority.get("owner_gate"):
                        authority["lifecycle_state"] = "COMPLETE"
                    authority["updated_at"] = utc_now_iso()
                    authority["recovery_epoch_id"] = epoch_for(authority)
            self._save_ledger(ledger)
        if self._progress_channel is not None:
            self._progress_channel.emit(
                {"project_id": project_id}, "TASK_COMPLETE",
                task_id=task_id, occurrence_key=source_request_id,
                details={"reason": reason},
            )

    def _record_handoff(
        self, source_request_id: str, project_id: str, reviewed_task_id: str,
        next_task_id: str, reason: str,
        *,
        staged: Any = None,
        reviewed_branch: str | None = None,
        reviewed_head: str | None = None,
        reviewed_ready: bool = False,
        successor_evidence: str | None = None,
    ) -> bool:
        planner_state = read_json(self.runtime_root / "ai-planner.json", {})
        reviewer_state = read_json(self.runtime_root / "ai-reviewer.json", {})
        with self._lock:
            ledger = self._load_ledger()
            existing = ledger["executions"].get(source_request_id)
            if existing is not None and not (
                _legacy_not_ready_block(existing)
                or _legacy_no_next_settle(existing)
                or _legacy_not_advanced_block(existing)
            ):
                return existing.get("state") == "handoff"
            lifecycle = ledger.setdefault("lifecycle", {})
            authority = lifecycle.get(project_id)
            if not isinstance(authority, dict):
                authority = new_authority(project_id, reviewed_task_id, "REVIEWING")
                lifecycle[project_id] = authority
            prior_transition = next((
                row for row in ledger.setdefault("transitions", {}).values()
                if isinstance(row, dict)
                and row.get("project_id") == project_id
                and row.get("source_task_id") == reviewed_task_id
                and row.get("target_task_id") == next_task_id
                and row.get("state") not in {"failed", "owner_gate"}
            ), None)
            generation = (
                int(prior_transition.get("generation") or 0)
                if isinstance(prior_transition, dict)
                else int(authority.get("generation") or 0) + 1
            )
            transition_id = (
                str(prior_transition.get("transition_id"))
                if isinstance(prior_transition, dict) and prior_transition.get("transition_id")
                else transition_id_for(project_id, reviewed_task_id, next_task_id, generation)
            )
            transition = ledger["transitions"].setdefault(transition_id, {
                "transition_id": transition_id,
                "idempotency_key": transition_id,
                "project_id": project_id,
                "source_request_id": source_request_id,
                "source_task_id": reviewed_task_id,
                "target_task_id": next_task_id,
                "generation": generation,
                "state": "intent",
                "recorded_at": utc_now_iso(),
                "evidence": {
                    "successor": successor_evidence or ("roadmap" if staged is not None else "repository_projection"),
                    "reviewed_branch": reviewed_branch,
                    "reviewed_head": reviewed_head,
                },
            })
            source_owners = source_ownership_blockers(
                project_id, reviewed_task_id, ledger,
                planner_state if isinstance(planner_state, dict) else None,
                reviewer_state if isinstance(reviewer_state, dict) else None,
            )
            reconciled_orphans = [
                owner for owner in source_owners
                if (
                    # The accepted, fresh-truth review that is creating this
                    # handoff validly transfers older pending review
                    # obligations for the same task. Its branch/HEAD/status
                    # anchors cover the aggregate repository result.
                    owner.get("role") == "review_obligation"
                    or (
                        owner.get("role") == "reviewer"
                        and owner.get("source_request_id")
                        and owner.get("source_request_id") not in ledger["executions"]
                    )
                )
            ]
            blockers = [
                owner for owner in source_owners
                if str(owner.get("id") or "") != source_request_id
                and owner not in reconciled_orphans
            ]
            if blockers:
                transition["state"] = "waiting_source"
                transition["ownership_blockers"] = copy.deepcopy(blockers)
                transition["updated_at"] = utc_now_iso()
                authority["current_task_id"] = reviewed_task_id
                authority["active_transition_id"] = transition_id
                authority["source_task_id"] = reviewed_task_id
                authority["lifecycle_state"] = "REVIEWING"
                authority["updated_at"] = utc_now_iso()
                authority["recovery_epoch_id"] = epoch_for(authority)
                self._save_ledger(ledger)
                return False
            row: dict[str, Any] = {
                "project_id": project_id, "source_request_id": source_request_id,
                "source_kind": "decision", "task_id": reviewed_task_id,
                "source_task_id": reviewed_task_id,
                "target_task_id": next_task_id,
                "next_task_id": next_task_id, "state": "handoff",
                "outcome": "planning_required", "reason": reason,
                "recorded_at": utc_now_iso(),
                "lifecycle_transition_id": transition_id,
                "transition_generation": generation,
                "successor_evidence": successor_evidence or ("roadmap" if staged is not None else "repository_projection"),
                # `handoff` is the successor activation work item.  The
                # reviewed task itself is terminal at this point and must
                # never be eligible for a new planner/Worker launch.
                "predecessor_terminal_state": "settled",
                "predecessor_settled_at": utc_now_iso(),
                **({"legacy_reconciled_from": existing} if existing is not None else {}),
            }
            if staged is not None:
                row["staged_successor"] = staged.successor_task_id
                row["staged_spec_path"] = staged.spec_path
                row["staged_spec_sha256"] = staged.spec_sha256
                row["reviewed_branch"] = reviewed_branch
                row["reviewed_head"] = reviewed_head
                row["reviewed_ready"] = bool(reviewed_ready)
            ledger["executions"][source_request_id] = row
            transition.update({
                "source_request_id": source_request_id,
                "state": "ready",
                "ownership_blockers": [],
                "handoff_record": copy.deepcopy(row),
                "updated_at": utc_now_iso(),
                "reconciled_orphan_owners": copy.deepcopy(reconciled_orphans),
            })
            authority["current_task_id"] = reviewed_task_id
            authority["source_task_id"] = reviewed_task_id
            authority["active_transition_id"] = transition_id
            authority["lifecycle_state"] = "TRANSITIONING"
            authority["updated_at"] = utc_now_iso()
            authority["recovery_epoch_id"] = epoch_for(authority)
            self._save_ledger(ledger)
        if self._progress_channel is not None:
            self._progress_channel.emit(
                {"project_id": project_id}, "NEXT_TASK",
                task_id=next_task_id, occurrence_key=source_request_id,
                details={"reason": reason, "previous_task_id": reviewed_task_id},
            )
        return True

    def mark_handoff_consumed(
        self, source_request_id: str, continuation_id: str, plan_id: str,
    ) -> None:
        with self._lock:
            ledger = self._load_ledger()
            record = ledger["executions"].get(source_request_id)
            if not isinstance(record, dict) or record.get("state") != "handoff":
                return
            record["handoff_consumed"] = True
            record["continuation_id"] = continuation_id
            record["plan_id"] = plan_id
            record["handoff_consumed_at"] = utc_now_iso()
            transition_id = record.get("lifecycle_transition_id")
            transition = ledger.setdefault("transitions", {}).get(transition_id)
            if isinstance(transition, dict):
                transition["state"] = "published"
                transition["continuation_id"] = continuation_id
                transition["plan_id"] = plan_id
                transition["published_at"] = utc_now_iso()
                transition["updated_at"] = transition["published_at"]
            project_id = str(record.get("project_id") or "")
            authority = ledger.setdefault("lifecycle", {}).get(project_id)
            if isinstance(authority, dict):
                authority["generation"] = int(record.get("transition_generation") or authority.get("generation") or 0)
                authority["source_task_id"] = record.get("source_task_id") or record.get("task_id")
                authority["current_task_id"] = record.get("target_task_id") or record.get("next_task_id")
                authority["active_transition_id"] = transition_id
                if not (isinstance(authority.get("owner_gate"), dict) and authority["owner_gate"].get("code") == "CONTROL_PLANE_DECLARATION_REQUIRED"):
                    authority["lifecycle_state"] = "PLANNING"
                    authority["owner_gate"] = None
                else:
                    authority["lifecycle_state"] = "OWNER_GATE"
                authority["updated_at"] = utc_now_iso()
                authority["recovery_epoch_id"] = epoch_for(authority)
            self._save_ledger(ledger)

    def reopen_stale_anchor_handoff(
        self, source_request_id: str, *, allow_transient_dirty: bool = False,
        current_head: str | None = None,
    ) -> bool:
        """Reopen a bounded, revalidated transient handoff refusal once.

        The Planner used to refuse any HEAD movement between recording and
        consuming a staged handoff, and that refusal made the transition
        terminal: an unrelated commit (such as a control-plane fix) turned a
        valid handoff into an owner gate.  The Planner now accepts a fast-forward
        that leaves ``agent/`` untouched and re-validates everything else, so a
        handoff blocked with exactly that refusal may be offered to it again.
        This happens at most once per handoff: if the re-validated check refuses
        too, the block stands and stays a genuine owner gate.
        """
        with self._lock:
            ledger = self._load_ledger()
            record = ledger["executions"].get(source_request_id)
            reason = record.get("reason") if isinstance(record, dict) else None
            stale_anchor = reason == _STALE_ANCHOR_HANDOFF_REFUSAL
            transient_dirty = (
                reason == _TRANSIENT_DIRTY_HANDOFF_REFUSAL and allow_transient_dirty
            )
            already_reopened = (
                record.get("stale_anchor_reopened_at") if stale_anchor and isinstance(record, dict)
                else (
                    record.get("transient_dirty_reopened_head") == current_head
                    if isinstance(record, dict) and current_head
                    else record.get("transient_dirty_reopened_at") if isinstance(record, dict)
                    else None
                )
            )
            if (
                not isinstance(record, dict)
                or record.get("state") != "blocked"
                or record.get("outcome") != "planning_required"
                or not (stale_anchor or transient_dirty)
                or not record.get("staged_successor")
                or already_reopened
            ):
                return False
            transition = ledger.setdefault("transitions", {}).get(
                record.get("lifecycle_transition_id")
            )
            if not isinstance(transition, dict) or transition.get("state") != "waiting_recovery":
                return False
            now = utc_now_iso()
            record["state"] = "handoff"
            if stale_anchor:
                record["stale_anchor_reopened_at"] = now
                record["stale_anchor_block_reason"] = reason
                record["reason"] = "reopened after a stale-anchor planner refusal"
            else:
                record["transient_dirty_reopened_at"] = now
                record["transient_dirty_reopened_head"] = current_head
                record["transient_dirty_block_reason"] = reason
                record["reason"] = "reopened after repository became clean"
            transition["state"] = "ready"
            transition["reason"] = None
            transition["updated_at"] = now
            self._save_ledger(ledger)
            return True

    def mark_handoff_blocked(self, source_request_id: str, reason: str) -> None:
        with self._lock:
            ledger = self._load_ledger()
            record = ledger["executions"].get(source_request_id)
            if not isinstance(record, dict) or record.get("state") != "handoff":
                return
            record["state"] = "blocked"
            record["reason"] = reason
            record["handoff_blocked_at"] = utc_now_iso()
            transition = ledger.setdefault("transitions", {}).get(record.get("lifecycle_transition_id"))
            if isinstance(transition, dict):
                transition["state"] = "waiting_recovery"
                transition["reason"] = reason
                transition["updated_at"] = utc_now_iso()
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
        anchor_task_id: Optional[str] = None,
        predecessor_task_id: Optional[str] = None,
    ) -> tuple[Optional[str], str]:
        readiness = snapshot.get("readiness")
        if isinstance(readiness, dict):
            if readiness.get("code") == "READINESS_TASK_ID_MISMATCH":
                file_task = readiness.get("task_id")
                effective_anchor = anchor_task_id or expected_task_id
                effective_predecessor = predecessor_task_id or must_advance_from
                # Distinguish structured state bound to a superseded task while a reviewed
                # decision is anchored to that task:
                # 1. Exact remediation anchored to effective_anchor (the reviewed task being remediated)
                if expected_status_hash is not None and effective_anchor is not None and file_task == effective_anchor:
                    pass
                # 2. Next-task actuation: advanced from effective_predecessor (the accepted task) to successor
                elif (
                    must_advance_from is not None
                    and effective_predecessor == must_advance_from
                    and file_task == must_advance_from
                    and (
                        readiness.get("migration_candidate") == "ready_to_run"
                        or parse_task_status(snapshot.get("next_status")).is_ready_to_run()
                    )

                ):
                    pass
                else:
                    return None, f"readiness task-id mismatch: {readiness.get('reason')}"
            elif not readiness.get("valid", True) or readiness.get("stale", False) or readiness.get("state") == "invalid":
                code = readiness.get("code") or "READINESS_INVALID"
                return None, f"readiness is invalid ({code}): {readiness.get('reason') or code}"
            elif readiness.get("code") in {
                "READINESS_SCHEMA_INVALID",
                "READINESS_TOKEN_UNRESOLVABLE",
            }:
                code = readiness.get("code")
                return None, f"readiness is invalid ({code}): {readiness.get('reason') or code}"

        state = snapshot.get("state")
        if expected_status_hash is not None:
            if state not in ("READY_TO_RUN", "WAITING_REVIEW", "REVIEWING", "IDLE"):
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

    def _recovery_fresh_guard(
        self, project: dict[str, Any], snapshot: dict[str, Any], *,
        expected_branch: str, expected_head: str,
    ) -> str:
        """Check recovery truth without relabelling a reviewed task as next.md."""
        if snapshot.get("state") not in ("READY_TO_RUN", "WAITING_REVIEW", "REVIEWING", "IDLE"):
            return "project state is not eligible for exact remediation"
        if _external_worker_active(snapshot):
            return "an external task Worker is already active"
        truth = read_repository_truth(project.get("repo_path") or "")
        if not truth.valid:
            return "repository truth unavailable"
        if truth.branch != expected_branch or truth.head != expected_head:
            return "repository changed after reviewed remediation truth"
        if truth.dirty:
            return "remediation recovery requires a clean current worktree"
        return ""

    @staticmethod
    def _project_context_for_worker(project: dict[str, Any]) -> tuple[str, Any]:
        from dev_orchestrator.core.project_context import context_prompt_block
        return context_prompt_block(project, "worker")

    @staticmethod
    def _lifecycle_launch_guard(
        ledger: dict[str, Any], project_id: str, task_id: str,
        source_task_id: str | None,
    ) -> tuple[str | None, str | None]:
        lifecycle = ledger.setdefault("lifecycle", {})
        authority = lifecycle.get(project_id)
        if not isinstance(authority, dict):
            authority = new_authority(project_id, task_id, "READY_TO_RUN")
            lifecycle[project_id] = authority
        current_task = _non_blank_config(authority.get("current_task_id"))
        if current_task is not None and current_task != task_id:
            return source_task_id, (
                "authoritative lifecycle task mismatch: "
                f"current={current_task}, requested={task_id}"
            )
        if authority.get("owner_gate"):
            return source_task_id, "authoritative lifecycle is stopped at OWNER_GATE"
        authority_state = str(authority.get("lifecycle_state") or "").upper()
        if (
            authority_state in TERMINAL_LIFECYCLE_STATES
            or task_has_terminal_settlement(ledger, project_id, task_id)
        ):
            return source_task_id, "authoritative lifecycle task is terminally settled"
        if authority_state == "PENDING_DESIGN":
            return source_task_id, "PENDING_DESIGN task cannot launch a Worker"
        lineage_source = _non_blank_config(source_task_id)
        if lineage_source is None:
            prior = _non_blank_config(authority.get("source_task_id"))
            if prior is not None and prior != task_id:
                lineage_source = prior
        return lineage_source, None

    def _migrate_legacy_handoff_authority(
        self, ledger: dict[str, Any], project_id: str, target_task_id: str,
    ) -> bool:
        """Upgrade one terminal pre-P16.13 handoff at its first safe launch.

        Old handoffs did not carry source/target/generation fields.  A completed
        reviewer record plus the repository-selected target is sufficient only
        when no execution is active and exactly one unapplied legacy handoff
        exists.  The historical row is annotated, never replaced.
        """
        authority = ledger.setdefault("lifecycle", {}).get(project_id)
        if not isinstance(authority, dict):
            return False
        source_task_id = _non_blank_config(authority.get("current_task_id"))
        if source_task_id is None or source_task_id == target_task_id:
            return False
        if self._active_project(ledger, project_id):
            return False
        reviews_raw = read_json(self.runtime_root / "ai-reviewer.json", {})
        reviews = reviews_raw.get("reviews") if isinstance(reviews_raw, dict) else {}
        candidates = [
            row for row in ledger.get("executions", {}).values()
            if isinstance(row, dict)
            and row.get("project_id") == project_id
            and row.get("state") == "handoff"
            and not row.get("lifecycle_transition_id")
            and isinstance(reviews, dict)
            and isinstance(reviews.get(str(row.get("source_request_id") or "")), dict)
            and reviews[str(row.get("source_request_id") or "")].get("state") == "completed"
        ]
        if len(candidates) != 1:
            return False
        row = candidates[0]
        generation = int(authority.get("generation") or 0) + 1
        transition_id = transition_id_for(project_id, source_task_id, target_task_id, generation)
        ledger.setdefault("transitions", {})[transition_id] = {
            "transition_id": transition_id,
            "idempotency_key": transition_id,
            "project_id": project_id,
            "source_request_id": row.get("source_request_id"),
            "source_task_id": source_task_id,
            "target_task_id": target_task_id,
            "generation": generation,
            "state": "completed",
            "evidence": {"source": "legacy_terminal_handoff_migration"},
            "recorded_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        row.update({
            "legacy_lifecycle_migrated": True,
            "source_task_id": source_task_id,
            "target_task_id": target_task_id,
            "next_task_id": target_task_id,
            "lifecycle_transition_id": transition_id,
            "transition_generation": generation,
        })
        authority.update({
            "generation": generation,
            "source_task_id": source_task_id,
            "current_task_id": target_task_id,
            "active_transition_id": transition_id,
            "lifecycle_state": "READY_TO_RUN",
            "owner_gate": None,
            "updated_at": utc_now_iso(),
        })
        authority["recovery_epoch_id"] = epoch_for(authority)
        return True

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
        lineage: Optional[dict[str, Any]] = None,
    ) -> Optional[ActuationLaunch]:
        context_block, resolution = self._project_context_for_worker(project)
        ctx_decl = project.get("project_context") or {}
        if ctx_decl.get("enabled") and ctx_decl.get("require_valid", True) and resolution.state == "invalid":
            self._record_blocked(
                source_request_id, str(project["project_id"]),
                "durable project context is invalid: {0}".format(resolution.reason),
                task_id=task_id, source_kind=source_kind,
            )
            return None

        failure_environment = environment_for_project(project)
        policy_role = "remediator" if source_kind == "remediation" else "worker"
        effective_worker_prompt = inject_workflow_policy(worker_prompt, policy_role)
        if context_block:
            effective_worker_prompt += "\n\n" + context_block
        if self.failure_memory is not None:
            failure_block = self.failure_memory.prompt_block(
                failure_environment, max_chars=self.failure_memory_max_chars
            )
            if failure_block:
                effective_worker_prompt = effective_worker_prompt.rstrip() + "\n\n" + failure_block

        repo_path = project.get("repo_path")
        if repo_path and head:
            try:
                decl = load_control_plane_declaration(str(repo_path), task_id, head)
                if decl is not None and getattr(decl, "kind", None) == "declared":
                    effective_worker_prompt = inject_control_plane_contract(
                        effective_worker_prompt, policy_role, decl
                    )
            except Exception:
                pass

        if policy.get("engine") == "aibroker":
            return self._launch_aibroker(
                project, source_request_id=source_request_id, source_kind=source_kind,
                task_id=task_id, source_task_id=source_task_id, branch=branch, head=head,
                worker_prompt=effective_worker_prompt, policy=policy, resolution=resolution,
                lineage=lineage,
            )
        request = AgentRequest(
            project_id=str(project["project_id"]),
            role=AgentRole.WORKER,
            prompt=effective_worker_prompt,
            working_directory=Path(str(project["repo_path"])),
            required_capabilities=frozenset({"code", "repository"}),
            preferred_backends=tuple(policy["preferred_backends"]),
            metadata={
                "source_request_id": source_request_id,
                "task_id": task_id,
                "source_kind": source_kind,
                "failure_environment": failure_environment,
            },
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
            existing = ledger["executions"].get(source_request_id)
            project_id = str(project["project_id"])
            authority = ledger.setdefault("lifecycle", {}).get(project_id)
            replayed_from_block = None
            if existing is not None:
                if _transient_remediation_state_block(existing):
                    replayed_from_block = copy.deepcopy(existing)
                elif _is_declaration_gate_replayable(existing, authority, head):
                    replayed_from_block = copy.deepcopy(existing)
                else:
                    return None

            if not isinstance(authority, dict):
                authority = new_authority(project_id, task_id, "READY_TO_RUN")
                ledger.setdefault("lifecycle", {})[project_id] = authority

            # 1. Authoritative task-mismatch fence first
            current_task = _non_blank_config(authority.get("current_task_id"))
            if current_task is not None and current_task != task_id:
                mismatch_err = (
                    "authoritative lifecycle task mismatch: "
                    f"current={current_task}, requested={task_id}"
                )
                ledger["executions"][source_request_id] = {
                    "project_id": project_id, "source_request_id": source_request_id,
                    "source_kind": source_kind, "source_task_id": source_task_id,
                    "task_id": task_id, "state": "blocked", "reason": mismatch_err,
                    "recorded_at": utc_now_iso(),
                }
                self._save_ledger(ledger)
                return None

            # 2. Special-gate reconciliation before generic owner_gate fence
            owner_gate = authority.get("owner_gate")
            evaluated_gate = False
            if (
                isinstance(owner_gate, dict)
                and owner_gate.get("code") == "CONTROL_PLANE_DECLARATION_REQUIRED"
                and owner_gate.get("task_id") in (None, task_id)
            ):
                repo_path = project.get("repo_path") or ""
                scope, declaration, gate_eval = evaluate_launch_declaration(
                    repo_path, project_id, task_id, head,
                )
                evaluated_gate = True
                if gate_eval.allowed:
                    resolve_declaration_gate(
                        authority,
                        reason=f"repaired control-plane declaration at head {head[:12]} verified",
                        task_id=task_id,
                    )
                else:
                    open_declaration_gate(
                        authority,
                        gate_id=gate_eval.gate_id or f"cp-gate:{project_id}:{task_id}:required",
                        reason=gate_eval.reason,
                        task_id=task_id,
                        head=head,
                        declaration_hash=declaration.content_hash,
                        evidence=gate_eval.evidence,
                    )
                    ledger["executions"][source_request_id] = {
                        "project_id": project_id,
                        "source_request_id": source_request_id,
                        "source_kind": source_kind,
                        "source_task_id": source_task_id,
                        "task_id": task_id,
                        "branch": branch,
                        "head": head,
                        "state": "blocked",
                        "reason": gate_eval.reason,
                        "blocked_gate_code": "CONTROL_PLANE_DECLARATION_REQUIRED",
                        "gate_id": gate_eval.gate_id,
                        "declaration_hash": declaration.content_hash,
                        "recorded_at": utc_now_iso(),
                    }
                    self._save_ledger(ledger)
                    return None

            source_task_id, lifecycle_error = self._lifecycle_launch_guard(
                ledger, project_id, task_id, source_task_id,
            )
            if lifecycle_error:
                ledger["executions"][source_request_id] = {
                    "project_id": project_id, "source_request_id": source_request_id,
                    "source_kind": source_kind, "source_task_id": source_task_id,
                    "task_id": task_id, "state": "blocked", "reason": lifecycle_error,
                    "recorded_at": utc_now_iso(),
                }
                self._save_ledger(ledger)
                return None

            # Current declaration evaluation for tasks where gate was not already evaluated
            if not evaluated_gate:
                repo_path = project.get("repo_path") or ""
                scope, declaration, gate_eval = evaluate_launch_declaration(
                    repo_path, project_id, task_id, head,
                )
                if not gate_eval.allowed:
                    open_declaration_gate(
                        authority,
                        gate_id=gate_eval.gate_id or f"cp-gate:{project_id}:{task_id}:required",
                        reason=gate_eval.reason,
                        task_id=task_id,
                        head=head,
                        declaration_hash=declaration.content_hash,
                        evidence=gate_eval.evidence,
                    )
                    ledger["executions"][source_request_id] = {
                        "project_id": project_id,
                        "source_request_id": source_request_id,
                        "source_kind": source_kind,
                        "source_task_id": source_task_id,
                        "task_id": task_id,
                        "branch": branch,
                        "head": head,
                        "state": "blocked",
                        "reason": gate_eval.reason,
                        "blocked_gate_code": "CONTROL_PLANE_DECLARATION_REQUIRED",
                        "gate_id": gate_eval.gate_id,
                        "declaration_hash": declaration.content_hash,
                        "recorded_at": utc_now_iso(),
                    }
                    self._save_ledger(ledger)
                    return None
            barrier = self._launch_barrier_reason(project_id, source_kind)
            if barrier:
                ledger["executions"][source_request_id] = {
                    "project_id": project_id, "source_request_id": source_request_id,
                    "source_kind": source_kind, "task_id": task_id, "state": "blocked",
                    "reason": barrier, "recorded_at": utc_now_iso(),
                }
                self._save_ledger(ledger)
                return None
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
                "context_state": resolution.state,
                "context_digest": resolution.document.digest if resolution.document else None,
                **(copy.deepcopy(lineage) if lineage else {}),
                **({"replayed_from_block": replayed_from_block} if replayed_from_block is not None else {}),
            }
            authority = ledger["lifecycle"].get(project_id)
            if isinstance(authority, dict):
                authority["active_owner"] = {
                    "role": "worker", "id": source_request_id, "task_id": task_id,
                }
                authority["lifecycle_state"] = "EXECUTING"
                authority["updated_at"] = utc_now_iso()
                authority["recovery_epoch_id"] = epoch_for(authority)
            self._save_ledger(ledger)
            status_record = copy.deepcopy(ledger["executions"][source_request_id])

        # Pre-actuation obligation verification outside self._lock
        try:
            open_execution_obligation(
                self.runtime_root,
                project_id=project_id,
                task_id=task_id,
                source_request_id=source_request_id,
                branch=branch,
                head=head,
                launch_status_hash=launch_truth.status_hash,
                engine="agent",
                backend_handle=route.selected_backend_id,
                worker_identity={"started_at": started_at},
                recovery_of_lineage_key=lineage.get("recovery_of_lineage_key") if lineage else None,
            )
        except ObligationPersistError as exc:
            with self._lock:
                ledger = self._load_ledger()
                if source_request_id in ledger["executions"]:
                    ledger["executions"][source_request_id]["state"] = "blocked"
                    ledger["executions"][source_request_id]["reason"] = f"execution_obligation_unpersisted: {exc}"
                    self._save_ledger(ledger)
                    blocked_record = copy.deepcopy(ledger["executions"][source_request_id])
                else:
                    blocked_record = {"project_id": project_id, "state": "blocked", "reason": f"execution_obligation_unpersisted: {exc}"}
            write_execution_status(blocked_record, self.runtime_root)
            if self._progress_channel is not None and hasattr(self._progress_channel, "emit"):
                try:
                    self._progress_channel.emit(
                        project, "BLOCKED", task_id=task_id, occurrence_key=source_request_id,
                        details={"reason": "execution_obligation_unpersisted", "error": str(exc)},
                    )
                except Exception:
                    pass
            return None

        write_execution_status(status_record, self.runtime_root)
        try:
            from dev_orchestrator.core.execution_context import update_context
            update_context(
                self.runtime_root,
                project_id,
                task_id=task_id,
                active_role="worker",
                disposition="hold",
                git_anchor=launch_truth.head,
                next_action="execute",
            )
        except Exception:
            pass

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
        resolution: Optional[Any] = None,
        lineage: Optional[dict[str, Any]] = None,
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
            metadata={
                "source_request_id": source_request_id,
                "source_kind": source_kind,
                "failure_environment": environment_for_project(project),
                "managed_worktree": True,
            },
        )
        with self._lock:
            ledger = self._load_ledger()
            existing = ledger["executions"].get(source_request_id)
            authority = ledger.setdefault("lifecycle", {}).get(project_id)
            replayed_from_block = None
            if existing is not None:
                if _transient_remediation_state_block(existing):
                    replayed_from_block = copy.deepcopy(existing)
                elif _is_declaration_gate_replayable(existing, authority, head):
                    replayed_from_block = copy.deepcopy(existing)
                else:
                    return None

            if not isinstance(authority, dict):
                authority = new_authority(project_id, task_id, "READY_TO_RUN")
                ledger.setdefault("lifecycle", {})[project_id] = authority

            # 1. Authoritative task-mismatch fence first
            current_task = _non_blank_config(authority.get("current_task_id"))
            if current_task is not None and current_task != task_id:
                mismatch_err = (
                    "authoritative lifecycle task mismatch: "
                    f"current={current_task}, requested={task_id}"
                )
                ledger["executions"][source_request_id] = {
                    "project_id": project_id, "source_request_id": source_request_id,
                    "source_kind": source_kind, "source_task_id": source_task_id,
                    "task_id": task_id, "state": "blocked", "reason": mismatch_err,
                    "recorded_at": utc_now_iso(),
                }
                self._save_ledger(ledger)
                return None

            # 2. Special-gate reconciliation before generic owner_gate fence
            owner_gate = authority.get("owner_gate")
            evaluated_gate = False
            if (
                isinstance(owner_gate, dict)
                and owner_gate.get("code") == "CONTROL_PLANE_DECLARATION_REQUIRED"
                and owner_gate.get("task_id") in (None, task_id)
            ):
                repo_path = project.get("repo_path") or ""
                scope, declaration, gate_eval = evaluate_launch_declaration(
                    repo_path, project_id, task_id, head,
                )
                evaluated_gate = True
                if gate_eval.allowed:
                    resolve_declaration_gate(
                        authority,
                        reason=f"repaired control-plane declaration at head {head[:12]} verified",
                        task_id=task_id,
                    )
                else:
                    open_declaration_gate(
                        authority,
                        gate_id=gate_eval.gate_id or f"cp-gate:{project_id}:{task_id}:required",
                        reason=gate_eval.reason,
                        task_id=task_id,
                        head=head,
                        declaration_hash=declaration.content_hash,
                        evidence=gate_eval.evidence,
                    )
                    ledger["executions"][source_request_id] = {
                        "project_id": project_id,
                        "source_request_id": source_request_id,
                        "source_kind": source_kind,
                        "source_task_id": source_task_id,
                        "task_id": task_id,
                        "branch": branch,
                        "head": head,
                        "state": "blocked",
                        "reason": gate_eval.reason,
                        "blocked_gate_code": "CONTROL_PLANE_DECLARATION_REQUIRED",
                        "gate_id": gate_eval.gate_id,
                        "declaration_hash": declaration.content_hash,
                        "recorded_at": utc_now_iso(),
                    }
                    self._save_ledger(ledger)
                    return None

            source_task_id, lifecycle_error = self._lifecycle_launch_guard(
                ledger, project_id, task_id, source_task_id,
            )
            if lifecycle_error:
                ledger["executions"][source_request_id] = {
                    "project_id": project_id, "source_request_id": source_request_id,
                    "source_kind": source_kind, "source_task_id": source_task_id,
                    "task_id": task_id, "state": "blocked", "reason": lifecycle_error,
                    "recorded_at": utc_now_iso(),
                }
                self._save_ledger(ledger)
                return None

            # 3. Current declaration evaluation for tasks where gate was not already evaluated
            if not evaluated_gate:
                repo_path = project.get("repo_path") or ""
                scope, declaration, gate_eval = evaluate_launch_declaration(
                    repo_path, project_id, task_id, head,
                )
                if not gate_eval.allowed:
                    open_declaration_gate(
                        authority,
                        gate_id=gate_eval.gate_id or f"cp-gate:{project_id}:{task_id}:required",
                        reason=gate_eval.reason,
                        task_id=task_id,
                        head=head,
                        declaration_hash=declaration.content_hash,
                        evidence=gate_eval.evidence,
                    )
                    ledger["executions"][source_request_id] = {
                        "project_id": project_id,
                        "source_request_id": source_request_id,
                        "source_kind": source_kind,
                        "source_task_id": source_task_id,
                        "task_id": task_id,
                        "branch": branch,
                        "head": head,
                        "state": "blocked",
                        "reason": gate_eval.reason,
                        "blocked_gate_code": "CONTROL_PLANE_DECLARATION_REQUIRED",
                        "gate_id": gate_eval.gate_id,
                        "declaration_hash": declaration.content_hash,
                        "recorded_at": utc_now_iso(),
                    }
                    self._save_ledger(ledger)
                    return None
            barrier = self._launch_barrier_reason(project_id, source_kind)
            if barrier:
                ledger["executions"][source_request_id] = {
                    "project_id": project_id, "source_request_id": source_request_id,
                    "source_kind": source_kind, "task_id": task_id, "state": "blocked",
                    "reason": barrier, "recorded_at": utc_now_iso(),
                }
                self._save_ledger(ledger)
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
                "session_id": None,
                "provider_output_observed": False,
                "first_output_at": None,
                "context_state": resolution.state if resolution else None,
                "context_digest": resolution.document.digest if resolution and resolution.document else None,
                **({"replayed_from_block": replayed_from_block} if replayed_from_block is not None else {}),
                **(copy.deepcopy(lineage) if lineage else {}),
            }
            authority = ledger["lifecycle"].get(project_id)
            if isinstance(authority, dict):
                authority["active_owner"] = {
                    "role": "worker", "id": source_request_id, "task_id": task_id,
                }
                authority["lifecycle_state"] = "EXECUTING"
                authority["updated_at"] = utc_now_iso()
                authority["recovery_epoch_id"] = epoch_for(authority)
            self._save_ledger(ledger)
            status_record = copy.deepcopy(ledger["executions"][source_request_id])

        # Pre-actuation obligation verification outside self._lock
        try:
            open_execution_obligation(
                self.runtime_root,
                project_id=project_id,
                task_id=task_id,
                source_request_id=source_request_id,
                branch=branch,
                head=head,
                launch_status_hash=launch_truth.status_hash,
                engine="aibroker",
                backend_handle=broker_request_id,
                worker_identity={"started_at": status_record.get("started_at")},
                recovery_of_lineage_key=lineage.get("recovery_of_lineage_key") if lineage else None,
            )
        except ObligationPersistError as exc:
            with self._lock:
                ledger = self._load_ledger()
                if source_request_id in ledger["executions"]:
                    ledger["executions"][source_request_id]["state"] = "blocked"
                    ledger["executions"][source_request_id]["reason"] = f"execution_obligation_unpersisted: {exc}"
                    self._save_ledger(ledger)
                    blocked_record = copy.deepcopy(ledger["executions"][source_request_id])
                else:
                    blocked_record = {"project_id": project_id, "state": "blocked", "reason": f"execution_obligation_unpersisted: {exc}"}
            write_execution_status(blocked_record, self.runtime_root)
            if self._progress_channel is not None and hasattr(self._progress_channel, "emit"):
                try:
                    self._progress_channel.emit(
                        project, "BLOCKED", task_id=task_id, occurrence_key=source_request_id,
                        details={"reason": "execution_obligation_unpersisted", "error": str(exc)},
                    )
                except Exception:
                    pass
            return None

        write_execution_status(status_record, self.runtime_root)
        try:
            from dev_orchestrator.core.execution_context import update_context
            update_context(
                self.runtime_root,
                project_id,
                task_id=task_id,
                active_role="worker",
                disposition="hold",
                git_anchor=launch_truth.head,
                next_action="execute",
            )
        except Exception:
            pass
        thread = threading.Thread(
            target=self._run_broker_worker_thread,
            args=(source_request_id, request),
            name="devorch-broker-worker-" + project_id,
            daemon=True,
        )
        with self._lock:
            self._threads[source_request_id] = thread
        thread.start()
        if self._progress_channel is not None:
            self._progress_channel.emit(
                project, "WORKER_STARTED",
                task_id=task_id, occurrence_key=source_request_id,
                details={"source_request_id": source_request_id, "engine": "aibroker"},
            )
        return ActuationLaunch(project_id, source_request_id, task_id, "aibroker", "launching")

    def _run_broker_worker_thread(self, source_request_id: str, request: AIRoleRequest) -> None:
        accounting_started = time.monotonic()
        worker_role = "remediation_worker" if request.stage_run_id == "remediation" else "worker"
        failed_resource_ids: set[str] = set(request.excluded_resource_ids)
        max_resource_failovers = 2
        if isinstance(request.metadata, Mapping):
            max_failovers_meta = request.metadata.get("max_resource_failovers")
            if isinstance(max_failovers_meta, int) and 0 <= max_failovers_meta <= 5:
                max_resource_failovers = max_failovers_meta
        max_attempts = 1 + max_resource_failovers

        result = None
        current_request = request
        self._update_record(source_request_id, state="running")

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                meta = dict(request.metadata) if isinstance(request.metadata, Mapping) else {}
                meta["worker_attempt"] = attempt
                meta["worker_failover_from_resource_ids"] = sorted(failed_resource_ids)
                current_request = AIRoleRequest(
                    project_id=request.project_id,
                    task_run_id=request.task_run_id,
                    stage_run_id=request.stage_run_id,
                    role_run_id=request.role_run_id,
                    request_id=f"ai-worker:{source_request_id}:failover-{attempt - 1}",
                    role=request.role,
                    prompt=request.prompt,
                    working_directory=request.working_directory,
                    quality=request.quality,
                    independence=request.independence,
                    previous_resource_context=request.previous_resource_context,
                    excluded_resource_ids=tuple(sorted(failed_resource_ids)),
                    timeout_seconds=request.timeout_seconds,
                    metadata=meta,
                )
            self._update_record(source_request_id, broker_request_id=current_request.request_id)
            if attempt > 1:
                try:
                    from dev_orchestrator.core.execution_context import record_role_completion
                    record_role_completion(
                        self.runtime_root,
                        request.project_id,
                        "worker_failover",
                        {
                            "task_id": request.task_run_id,
                            "attempt": attempt,
                            "excluded_resources": sorted(failed_resource_ids),
                        },
                    )
                except Exception:
                    pass

            if self.accounting is not None:
                self.accounting.start_interval(
                    "ai_execution",
                    current_request.request_id,
                    project_id=request.project_id,
                    task_id=request.task_run_id,
                    role=worker_role,
                    request_id=current_request.request_id,
                    stage_run_id=request.stage_run_id,
                    role_run_id=request.role_run_id,
                    source_request_id=source_request_id,
                    attempt_id=source_request_id,
                )
            result = None
            try:
                result = self._ai_execution_port.execute(current_request) if self._ai_execution_port else None
                if result is None:
                    raise RuntimeError("AIBroker execution port became unavailable")
            except Exception as exc:
                if self.accounting is not None:
                    self.accounting.end_interval(
                        "ai_execution",
                        current_request.request_id,
                        outcome="failed",
                        project_id=request.project_id,
                        task_id=request.task_run_id,
                        role=worker_role,
                        request_id=current_request.request_id,
                        stage_run_id=request.stage_run_id,
                        role_run_id=request.role_run_id,
                        source_request_id=source_request_id,
                        attempt_id=source_request_id,
                    )
                reconciled = None
                if self._ai_execution_port is not None:
                    try:
                        reconciled = self._ai_execution_port.status(current_request.request_id)
                    except Exception:
                        reconciled = None
                classification = None
                resource = None
                if isinstance(reconciled, dict) and str(reconciled.get("status") or "") == "failed":
                    from dev_orchestrator.core.ai_planner import _classify_reconciled_dispatch_failure
                    terminal_error = _non_blank_config(reconciled.get("execution_error")) or str(exc)
                    classification = _classify_reconciled_dispatch_failure(terminal_error)
                    resource_id = _non_blank_config(reconciled.get("resource_id"))
                    if resource_id is not None:
                        resource = ResourceContext(
                            resource_id,
                            _non_blank_config(reconciled.get("provider")),
                            _non_blank_config(reconciled.get("account")),
                            _non_blank_config(reconciled.get("model")),
                        )
                can_failover = (
                    classification in ROLE_RESOURCE_FAILURES
                    and resource is not None
                    and resource.resource_id is not None
                )
                if can_failover:
                    with self._lock:
                        rec = copy.deepcopy(
                            self._load_ledger()["executions"].get(source_request_id, {})
                        )
                    truth = read_repository_truth(request.working_directory)
                    unchanged = (
                        truth.valid
                        and truth.branch == rec.get("branch")
                        and truth.head == rec.get("head")
                        and truth.status_hash == rec.get("launch_status_hash")
                    )
                    if unchanged and attempt < max_attempts:
                        failed_resource_ids.add(resource.resource_id)
                        continue

                if self.failure_memory is not None:
                    self.failure_memory.record_matching_recurrences(
                        request.metadata.get("failure_environment", {}),
                        str(exc),
                        time.monotonic() - accounting_started,
                        project_id=request.project_id,
                        task_id=request.task_run_id,
                        role=worker_role,
                        request_id=current_request.request_id,
                        source_request_id=source_request_id,
                    )
                fail_reason = (
                    f"AIBroker worker lifecycle error [{type(exc).__name__}]: {exc}"
                )
                extra_record: dict[str, Any] = {}
                if failed_resource_ids:
                    extra_record["failover_from_resource_ids"] = sorted(failed_resource_ids)
                self._update_record(
                    source_request_id, state="failed",
                    reason=fail_reason,
                    broker_status="launch_error",
                    broker_request_id=current_request.request_id,
                    session_id=None,
                    provider_output_observed=False,
                    first_output_at=None,
                    completed_at=utc_now_iso(),
                    **extra_record,
                )
                if self._progress_channel is not None:
                    self._progress_channel.emit(
                        {"project_id": request.project_id}, "WORKER_FAILED",
                        task_id=request.task_run_id, occurrence_key=source_request_id,
                        details={"error": str(exc)},
                    )
                return

            resource = getattr(result, "resource_context", None) if result else None
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

            if self.accounting is not None:
                self.accounting.end_interval(
                    "ai_execution",
                    current_request.request_id,
                    outcome={"completed": "accepted", "cancelled": "cancelled"}.get(state, "failed"),
                    project_id=request.project_id,
                    task_id=request.task_run_id,
                    role=worker_role,
                    request_id=current_request.request_id,
                    stage_run_id=request.stage_run_id,
                    role_run_id=request.role_run_id,
                    source_request_id=source_request_id,
                    attempt_id=source_request_id,
                    dispatch_id=result.dispatch_id,
                    decision_id=result.decision_id,
                    execution_id=result.execution_id,
                    session_id=result.session_id,
                    resource_id=resource.resource_id if resource else None,
                    provider=resource.provider if resource else None,
                    account=resource.account if resource else None,
                    model=resource.model if resource else None,
                )

            if state == "failed":
                classification = getattr(result, "failure_classification", None)
                can_failover = (
                    classification in ROLE_RESOURCE_FAILURES
                    and resource is not None
                    and resource.resource_id is not None
                )
                if can_failover:
                    with self._lock:
                        rec = copy.deepcopy(
                            self._load_ledger()["executions"].get(source_request_id, {})
                        )
                    truth = read_repository_truth(request.working_directory)
                    unchanged = (
                        truth.valid
                        and truth.branch == rec.get("branch")
                        and truth.head == rec.get("head")
                        and truth.status_hash == rec.get("launch_status_hash")
                    )
                    if unchanged and attempt < max_attempts:
                        failed_resource_ids.add(resource.resource_id)
                        continue

                if self.failure_memory is not None:
                    self.failure_memory.record_matching_recurrences(
                        request.metadata.get("failure_environment", {}),
                        str(result.error or ""),
                        time.monotonic() - accounting_started,
                        project_id=request.project_id,
                        task_id=request.task_run_id,
                        role=worker_role,
                        request_id=current_request.request_id,
                        source_request_id=source_request_id,
                    )
                provider_output_observed = bool(
                    _non_blank_config(result.output) is not None or result.first_output_at is not None
                )
                if can_failover and not unchanged:
                    fail_reason = (
                        f"AIBroker worker resource failover refused because repository changed after provider failure: {result.error}"
                    )
                elif can_failover:
                    fail_reason = f"AIBroker worker resource failover limit reached: {result.error}"
                else:
                    fail_reason = result.error
                extra_record = {}
                if failed_resource_ids:
                    extra_record["failover_from_resource_ids"] = sorted(failed_resource_ids)
                self._update_record(
                    source_request_id,
                    state="failed",
                    completed_at=utc_now_iso(),
                    broker_status=result.status,
                    broker_request_id=current_request.request_id,
                    dispatch_id=result.dispatch_id,
                    decision_id=result.decision_id,
                    execution_id=result.execution_id,
                    session_id=result.session_id,
                    backend_run_id=result.execution_id or result.dispatch_id,
                    resource_context=resource_payload,
                    usage_source=result.usage_source,
                    provider_output_observed=provider_output_observed,
                    first_output_at=result.first_output_at,
                    reason=fail_reason,
                    **extra_record,
                )
                if self._progress_channel is not None:
                    is_test_fail = "test" in str(result.error or "").lower()
                    self._progress_channel.emit(
                        {"project_id": request.project_id},
                        "TEST_FAILED" if is_test_fail else "WORKER_FAILED",
                        task_id=request.task_run_id, occurrence_key=source_request_id,
                        details={"error": fail_reason},
                    )
                return

            break

        provider_output_observed = bool(
            _non_blank_config(result.output) is not None or result.first_output_at is not None
        )
        extra_record = {}
        if failed_resource_ids:
            extra_record["failover_from_resource_ids"] = sorted(failed_resource_ids)
        self._update_record(
            source_request_id,
            state=state,
            completed_at=utc_now_iso(),
            broker_status=result.status,
            broker_request_id=current_request.request_id,
            dispatch_id=result.dispatch_id,
            decision_id=result.decision_id,
            execution_id=result.execution_id,
            session_id=result.session_id,
            backend_run_id=result.execution_id or result.dispatch_id,
            resource_context=resource_payload,
            usage_source=result.usage_source,
            provider_output_observed=provider_output_observed,
            first_output_at=result.first_output_at,
            reason=result.error,
            **extra_record,
        )
        if self._progress_channel is not None:
            if state == "completed":
                self._progress_channel.emit(
                    {"project_id": request.project_id}, "WORKER_DONE",
                    task_id=request.task_run_id, occurrence_key=source_request_id,
                    details={"status": result.status},
                )
            elif state == "failed":
                is_test_fail = "test" in str(result.error or "").lower()
                self._progress_channel.emit(
                    {"project_id": request.project_id},
                    "TEST_FAILED" if is_test_fail else "WORKER_FAILED",
                    task_id=request.task_run_id, occurrence_key=source_request_id,
                    details={"error": result.error},
                )
        try:
            from dev_orchestrator.core.execution_context import (
                record_role_completion,
                set_next_action,
                update_context,
            )
            record_role_completion(
                self.runtime_root,
                request.project_id,
                worker_role,
                {
                    "task_id": request.task_run_id,
                    "state": state,
                    "reason": result.error if result else "unknown",
                },
            )
            if state == "completed":
                set_next_action(self.runtime_root, request.project_id, "technical_review", disposition="advance")
            else:
                update_context(self.runtime_root, request.project_id, disposition="hold", active_role=None)
        except Exception:
            pass

    def _update_record(self, source_request_id: str, **changes: Any) -> None:
        with self._lock:
            ledger = self._load_ledger()
            record = ledger["executions"].get(source_request_id)
            if not isinstance(record, dict):
                return
            record.update(changes)
            authority = ledger.setdefault("lifecycle", {}).get(str(record.get("project_id") or ""))
            if isinstance(authority, dict):
                state = str(record.get("state") or "")
                if state in _ACTIVE_STATES:
                    authority["active_owner"] = {
                        "role": "worker", "id": source_request_id,
                        "task_id": record.get("task_id"),
                    }
                    authority["lifecycle_state"] = "EXECUTING"
                elif state in _TERMINAL_STATES:
                    owner = authority.get("active_owner")
                    if not isinstance(owner, dict) or owner.get("id") == source_request_id:
                        authority["active_owner"] = None
                    authority["lifecycle_state"] = (
                        "REVIEWING" if state == "completed" else "READY_TO_RUN"
                    )
                authority["updated_at"] = utc_now_iso()
                authority["recovery_epoch_id"] = epoch_for(authority)
            self._save_ledger(ledger)
            status_record = copy.deepcopy(record)
        write_execution_status(status_record, self.runtime_root)
        try:
            record_execution_observation(
                self.runtime_root,
                project_id=str(status_record.get("project_id") or ""),
                source_request_id=source_request_id,
                changes=changes,
            )
        except Exception:
            pass

    def reconcile_successor_handoff(
        self,
        project: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        completed_task_id: str,
        trigger: str,
    ) -> dict[str, Any]:
        """Idempotently restore one lost, unambiguous successor handoff."""
        project_id = str(project.get("project_id") or "")
        repo_path = project.get("repo_path") or ""
        truth = read_repository_truth(repo_path)
        if not truth.valid or truth.dirty:
            return {"status": "blocked", "reason": "successor recovery requires clean repository"}
        resolution = resolve_successor(repo_path, completed_task_id)
        reconciled = False
        if resolution.kind == "inconsistent":
            outcome = reconcile_roadmap_successor(repo_path, completed_task_id, resolution)
            if outcome.status not in {"applied", "already_consistent"}:
                return {"status": "blocked", "reason": outcome.reason}
            reconciled = True
            truth = read_repository_truth(repo_path)
            resolution = resolve_successor(repo_path, completed_task_id)
        if resolution.kind != "successor" or not resolution.successor_task_id:
            return {
                "status": "conflict" if resolution.kind == "ambiguous" else "blocked",
                "reason": resolution.reason or resolution.kind,
            }
        request_id = (
            f"recover-handoff:{project_id}:{completed_task_id}:"
            f"{resolution.successor_task_id}:{str(resolution.spec_sha256 or '')[:12]}"
        )
        with self._lock:
            existing = self._load_ledger()["executions"].get(request_id)
            if isinstance(existing, dict):
                return {
                    "status": "noop", "source_request_id": request_id,
                    "successor_task_id": resolution.successor_task_id,
                }
        applied = self._record_handoff(
            request_id, project_id, completed_task_id,
            str(resolution.successor_task_id),
            f"{trigger}: recovered missing successor handoff",
            staged=resolution,
            reviewed_branch=truth.branch,
            reviewed_head=truth.head,
            reviewed_ready=False,
            successor_evidence="reconciled" if reconciled else resolution.evidence,
        )
        return {
            "status": "applied" if applied else "blocked",
            "source_request_id": request_id,
            "successor_task_id": resolution.successor_task_id,
            "reason": None if applied else "source-task lifecycle ownership is not terminal",
            # Ownership that has not drained yet is a wait, not a refusal: the
            # transition is parked in waiting_source and will publish once the
            # source drains.  Callers must not gate on it, or the only code that
            # rebuilds a durable handoff would be fenced off permanently.
            "waiting": not applied,
        }

    def reconcile_execution_loss(
        self,
        source_request_id: str,
        *,
        project_id: str,
        invariant_key: str,
        command_id: str,
        expected_anchor: Optional[dict[str, Any]] = None,
        evidence: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Sole writer for stale executor reconciliation under execution-loss invariants.

        Compare-and-set the exact active row to state='explicitly_reconciled' after validating
        conclusive-death evidence, exact project/task/branch/HEAD identity, and absence of every
        other active row for the project.
        """
        evidence_dict = dict(evidence or {})
        verdict = evidence_dict.get("verdict")
        if verdict != "dead":
            return {
                "status": "unavailable",
                "reason": f"reconciliation requires conclusive death verdict; observed verdict={verdict!r}",
            }

        with self._lock:
            ledger = self._load_ledger()
            executions = ledger.get("executions", {})

            # Check absence of every other launching/running row for this project
            for other_id, other_row in executions.items():
                if not isinstance(other_row, dict):
                    continue
                if str(other_row.get("project_id") or "") != project_id:
                    continue
                if other_id != source_request_id and str(other_row.get("state") or "").lower() in _ACTIVE_STATES:
                    return {
                        "status": "conflict",
                        "reason": f"another managed execution ({other_id}) is currently active for project {project_id}",
                    }

            row = executions.get(source_request_id)
            anchor = dict(expected_anchor or {})

            # Case 1: Row disappeared from executor ledger
            if row is None:
                tombstone = {
                    "project_id": project_id,
                    "source_request_id": source_request_id,
                    "task_id": anchor.get("task_id"),
                    "branch": anchor.get("branch"),
                    "head": anchor.get("head"),
                    "launch_status_hash": anchor.get("status_hash"),
                    "state": "explicitly_reconciled",
                    "prior_state": "disappeared",
                    "started_at": anchor.get("started_at"),
                    "completed_at": utc_now_iso(),
                    "invariant_key": invariant_key,
                    "command_id": command_id,
                    "reconciled_by": command_id,
                    "evidence_hash": evidence_dict.get("evidence_hash"),
                    "reason": "watchdog_execution_loss",
                    "reconciliation_type": "tombstone",
                }
                executions[source_request_id] = tombstone
                self._save_ledger(ledger)
                write_execution_status(tombstone, self.runtime_root)
                try:
                    close_lineage_record(
                        self.runtime_root,
                        project_id,
                        source_request_id,
                        terminal_outcome="explicitly_reconciled",
                        reason="watchdog_execution_loss",
                    )
                except Exception:
                    pass
                return {
                    "status": "reconciled",
                    "row": copy.deepcopy(tombstone),
                    "reason": "created explicitly_reconciled tombstone for disappeared executor row",
                }

            # Case 2: Row already explicitly reconciled
            current_st = str(row.get("state") or "").lower()
            if current_st == "explicitly_reconciled":
                return {
                    "status": "already_reconciled",
                    "row": copy.deepcopy(row),
                    "reason": "execution row was already explicitly reconciled",
                }

            # Case 3: Row genuinely completed/failed/cancelled meanwhile
            if current_st in {"completed", "failed", "cancelled"}:
                return {
                    "status": "superseded_by_terminal",
                    "row": copy.deepcopy(row),
                    "reason": f"execution genuinely finished as {current_st} before reconciliation",
                }

            # Case 4: Row is in active state (launching or running) or non-active non-terminal (blocked, recovery_required)
            if current_st in _ACTIVE_STATES or current_st in {"blocked", "recovery_required"}:
                if anchor.get("task_id") and row.get("task_id") and row["task_id"] != anchor["task_id"]:
                    return {
                        "status": "conflict",
                        "row": copy.deepcopy(row),
                        "reason": f"task_id mismatch: row={row.get('task_id')!r}, anchor={anchor.get('task_id')!r}",
                    }
                if anchor.get("head") and row.get("head") and row["head"] != anchor["head"]:
                    return {
                        "status": "conflict",
                        "row": copy.deepcopy(row),
                        "reason": f"HEAD mismatch: row={row.get('head')!r}, anchor={anchor.get('head')!r}",
                    }

                prior_state = row.get("state")
                row["state"] = "explicitly_reconciled"
                row["prior_state"] = prior_state
                row["completed_at"] = utc_now_iso()
                row["invariant_key"] = invariant_key
                row["command_id"] = command_id
                row["reconciled_by"] = command_id
                row["evidence_hash"] = evidence_dict.get("evidence_hash")
                row["reason"] = "watchdog_execution_loss"
                self._save_ledger(ledger)
                write_execution_status(copy.deepcopy(row), self.runtime_root)
                try:
                    close_lineage_record(
                        self.runtime_root,
                        project_id,
                        source_request_id,
                        terminal_outcome="explicitly_reconciled",
                        reason="watchdog_execution_loss",
                    )
                except Exception:
                    pass
                return {
                    "status": "reconciled",
                    "row": copy.deepcopy(row),
                    "reason": f"explicitly reconciled {current_st} execution row",
                }

            return {
                "status": "unavailable",
                "row": copy.deepcopy(row),
                "reason": f"unsupported row state {current_st!r} for reconciliation",
            }

    def _run_worker_thread(
        self, source_request_id: str, request: AgentRequest, backend: AgentBackend,
        backends: dict[str, AgentBackend], fallback_ids: tuple[str, ...],
    ) -> None:
        accounting_started = time.monotonic()
        with self._lock:
            record = copy.deepcopy(
                self._load_ledger()["executions"].get(source_request_id, {})
            )
        worker_role = "remediation_worker" if record.get("source_kind") == "remediation" else "worker"
        interval_id = "legacy-worker:" + source_request_id
        if self.accounting is not None:
            self.accounting.start_interval(
                "ai_execution",
                interval_id,
                project_id=request.project_id,
                task_id=record.get("task_id"),
                role=worker_role,
                source_request_id=source_request_id,
                attempt_id=source_request_id,
            )
        try:
            asyncio.run(
                self._run_worker(source_request_id, request, backend, backends, fallback_ids)
            )
            with self._lock:
                terminal = self._load_ledger()["executions"].get(source_request_id, {})
            outcome = {
                "completed": "accepted",
                "cancelled": "cancelled",
            }.get(str(terminal.get("state") or ""), "failed")
            if self.accounting is not None:
                self.accounting.end_interval(
                    "ai_execution",
                    interval_id,
                    outcome=outcome,
                    project_id=request.project_id,
                    task_id=record.get("task_id"),
                    role=worker_role,
                    source_request_id=source_request_id,
                    attempt_id=source_request_id,
                    execution_id=terminal.get("backend_run_id"),
                    metadata={"backend_id": terminal.get("backend_id")},
                )
        except Exception as exc:  # fail closed; never auto-retry
            if self.accounting is not None:
                self.accounting.end_interval(
                    "ai_execution",
                    interval_id,
                    outcome="failed",
                    project_id=request.project_id,
                    task_id=record.get("task_id"),
                    role=worker_role,
                    source_request_id=source_request_id,
                    attempt_id=source_request_id,
                )
            if self.failure_memory is not None:
                self.failure_memory.record_matching_recurrences(
                    request.metadata.get("failure_environment", {}),
                    str(exc),
                    time.monotonic() - accounting_started,
                    project_id=request.project_id,
                    task_id=record.get("task_id"),
                    role=worker_role,
                    source_request_id=source_request_id,
                )
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
            retry_interval_id = "legacy-retry:" + source_request_id
            if self.accounting is not None:
                self.accounting.start_interval(
                    "retry",
                    retry_interval_id,
                    project_id=request.project_id,
                    task_id=record.get("task_id"),
                    role=(
                        "remediation_worker"
                        if record.get("source_kind") == "remediation"
                        else "worker"
                    ),
                    source_request_id=source_request_id,
                    attempt_id=source_request_id,
                    metadata={
                        "fallback_from": backend.backend_id,
                        "fallback_to": fallback.backend_id,
                    },
                )
            run = await fallback.start(request)
            self._update_record(
                source_request_id, state=run.state.value, backend_run_id=run.run_id,
                pid=run.pid, backend_id=run.backend_id,
            )
            result = await fallback.collect(run.run_id)
            if self.accounting is not None:
                self.accounting.end_interval(
                    "retry",
                    retry_interval_id,
                    outcome=(
                        "accepted" if result.state is AgentRunState.COMPLETED else "failed"
                    ),
                    project_id=request.project_id,
                    task_id=record.get("task_id"),
                    role=(
                        "remediation_worker"
                        if record.get("source_kind") == "remediation"
                        else "worker"
                    ),
                    source_request_id=source_request_id,
                    attempt_id=source_request_id,
                    execution_id=run.run_id,
                )

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
    def _failed_before_provider_work(record: dict[str, Any]) -> bool:
        """Recognize Broker allocation that failed at worktree safety pre-provider.

        Broker dispatch/decision/execution/resource identities establish allocation,
        not useful provider work. The recovery evidence is the explicit safety
        refusal, a terminal Broker failure, no usable provider session, and no
        positive output/work evidence.
        """
        return is_pre_provider_worktree_unsafe_failure(record)

    def _recovery_descendant_barrier(
        self, ledger: dict[str, Any], project_id: str, *, branch: str, head: str,
    ) -> str:
        """Keep a consumed remediation in its own review lifecycle.

        ``recovery_of`` is immutable lineage on the new execution, so its mere
        presence consumes the older failed row without rewriting history.  A
        completed retry remains a barrier until its AI-review descendant chain
        has reached a durable terminal lifecycle transition; only then can a
        later task use the ordinary control path.
        """
        executions = ledger["executions"]
        failed_sources = {
            _non_blank_config(record.get("source_request_id"))
            for record in executions.values()
            if isinstance(record, dict)
            and record.get("project_id") == project_id
            and record.get("engine") == "aibroker"
            and record.get("source_kind") == "remediation"
            and record.get("state") == "failed"
            and record.get("branch") == branch
            and record.get("head") == head
        }
        failed_sources.discard(None)
        if not failed_sources:
            return ""
        descendants = [
            record for record in executions.values()
            if isinstance(record, dict)
            and record.get("project_id") == project_id
            and _non_blank_config(record.get("recovery_of")) in failed_sources
        ]
        if not descendants:
            return ""
        reviews_raw = read_json(self.runtime_root / "ai-reviewer.json", {})
        reviews = reviews_raw.get("reviews") if isinstance(reviews_raw, dict) else None
        reviews = reviews if isinstance(reviews, dict) else {}
        for descendant in descendants:
            current = descendant
            seen_sources: set[str] = set()
            while True:
                state = current.get("state")
                if state in _ACTIVE_STATES:
                    return "a prior remediation recovery is still active"
                if state == "failed" and self._failed_before_provider_work(current):
                    # A review-launched remediation can fail before its provider
                    # starts just like the original remediation. Leave that exact
                    # child visible to _pre_execution_remediation_retry instead of
                    # making its consumed ancestor a permanent barrier.
                    break
                if state != "completed":
                    return "a prior remediation recovery is awaiting its normal review transition"

                source_id = _non_blank_config(current.get("source_request_id"))
                if source_id is None or source_id in seen_sources:
                    return "a prior remediation recovery is awaiting its normal review transition"
                seen_sources.add(source_id)
                review_id = "ai_review:" + source_id
                review = reviews.get(review_id)
                transition = executions.get(review_id)
                if (
                    not isinstance(review, dict)
                    or review.get("state") != "completed"
                    or not isinstance(transition, dict)
                    or transition.get("project_id") != project_id
                    or transition.get("source_request_id") != review_id
                ):
                    return "a prior remediation recovery is awaiting its normal review transition"
                transition_state = transition.get("state")
                if transition_state in {"settled", "handoff"}:
                    break
                if transition_state in _ACTIVE_STATES:
                    return "a prior remediation recovery is still active"
                if (
                    transition_state == "failed"
                    and self._failed_before_provider_work(transition)
                ):
                    # See the equivalent current-row case above. This is the
                    # review REMEDIATE descendant that owner recovery may retry.
                    break
                if (
                    transition_state != "completed"
                    or transition.get("engine") != "aibroker"
                    or transition.get("source_kind") not in {"decision", "remediation"}
                ):
                    return "a prior remediation recovery is awaiting its normal review transition"
                current = transition
        return ""

    @staticmethod
    def _status_hash(entries: tuple[str, ...]) -> str:
        digest = hashlib.sha256()
        for entry in sorted(entries):
            digest.update(entry.encode("utf-8", errors="replace"))
        return digest.hexdigest()

    @classmethod
    def _generated_only_fingerprint_evidence(
        cls, candidate: dict[str, Any], decision: dict[str, Any], review_hash: str,
    ) -> dict[str, Any] | None:
        raw_entries = candidate.get("review_dirty_entries", decision.get("review_dirty_entries"))
        if isinstance(raw_entries, (list, tuple)) and all(isinstance(entry, str) for entry in raw_entries):
            entries = tuple(raw_entries)
            if (
                entries
                and set(entries).issubset(_GENERATED_ONLY_PORCELAIN_ENTRIES)
                and cls._status_hash(entries) == review_hash
            ):
                return {
                    "kind": "generated_only_entries",
                    "review_status_hash": review_hash,
                    "entries": list(entries),
                }
            return None
        # This is the one independently verified legacy incident fingerprint.
        legacy_entries = ("?? graphify-out/",)
        if review_hash == cls._status_hash(legacy_entries):
            return {
                "kind": "legacy_generated_only_fingerprint",
                "review_status_hash": review_hash,
                "entries": list(legacy_entries),
            }
        return None

    @staticmethod
    def _review_evidence(decision: dict[str, Any]) -> str:
        """Render only durable reviewer evidence; never reconstruct findings."""
        reason = _non_blank_config(decision.get("reason")) or "(no reviewer reason recorded)"
        findings = decision.get("findings", decision.get("review_findings"))
        if findings is None:
            findings_text = "(no separate findings recorded)"
        elif isinstance(findings, str):
            findings_text = findings.strip() or "(no separate findings recorded)"
        else:
            findings_text = json.dumps(findings, sort_keys=True, ensure_ascii=False)
        return (
            "[ORIGINAL_TECHNICAL_REVIEW_EVIDENCE]\n"
            "Reviewer decision: REMEDIATE\n"
            "Reviewer reason: {0}\n"
            "Reviewer findings: {1}\n"
            "[/ORIGINAL_TECHNICAL_REVIEW_EVIDENCE]"
        ).format(reason, findings_text)

    def _blocked_reconcile_remediation_retry(
        self,
        ledger: dict[str, Any],
        decisions: dict[str, dict[str, Any]],
        *,
        project_id: str,
        task_id: str | None,
        branch: str,
        head: str,
        current_truth: Any,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, str]:
        """Resolve one exact re-anchored REMEDIATE blocked only by transient state.

        Recovery is owner-driven through a new continue command.  Historical
        blocked decision rows remain immutable; the new launch consumes the
        original pre-provider WorktreeUnsafeError via recovery_of lineage.
        """
        if task_id is None or current_truth.dirty:
            return None, None, None, ""
        reviews_raw = read_json(self.runtime_root / "ai-reviewer.json", {})
        reviews = reviews_raw.get("reviews") if isinstance(reviews_raw, dict) else None
        reviews = reviews if isinstance(reviews, dict) else {}
        consumed_sources = {
            _non_blank_config(row.get("recovery_of"))
            for row in ledger["executions"].values()
            if isinstance(row, dict)
        }
        transient_reasons = {
            "project state is not eligible for exact remediation",
            "an external task Worker is already active",
            "another managed Worker is already active",
        }
        matches: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for decision_id, decision in decisions.items():
            blocked = ledger["executions"].get(decision_id)
            review = reviews.get(decision_id)
            if not (
                isinstance(blocked, dict)
                and blocked.get("state") == "blocked"
                and blocked.get("source_kind") == "remediation"
                and blocked.get("reason") in transient_reasons
                and isinstance(review, dict)
                and review.get("state") == "completed"
                and review.get("project_id") == project_id
                and review.get("task_id") == task_id
                and review.get("branch") == branch
                and review.get("head") == head
                and decision.get("project_id") == project_id
                and decision.get("task_id") == task_id
                and decision.get("branch") == branch
                and decision.get("head") == head
                and decision.get("disposition") == "apply"
                and decision.get("decision") == "remediate"
                and decision.get("next_action") == NextAction.CONTINUE_CURRENT_STAGE.value
                and decision.get("role") == WebSolRole.REVIEWER.value
                and decision.get("event") == WebSolEvent.WORKER_DONE.value
            ):
                continue
            original_id = _non_blank_config(review.get("reconcile_of"))
            original = ledger["executions"].get(original_id or "")
            review_hash = _non_blank_config(decision.get("review_status_hash"))
            if (
                original_id is None
                or original_id in consumed_sources
                or not isinstance(original, dict)
                or not self._failed_before_provider_work(original)
                or review_hash is None
                or review.get("review_status_hash") != review_hash
                or current_truth.status_hash != review_hash
            ):
                continue
            matches.append((original, decision, review))
        if matches and self._active_project(ledger, project_id):
            return None, None, None, "another managed Worker is already active"
        if len(matches) == 1:
            return (*matches[0], "")
        if len(matches) > 1:
            return None, None, None, "multiple blocked re-anchored remediations match recovery evidence"
        return None, None, None, ""

    def _pre_execution_remediation_retry(
        self,
        ledger: dict[str, Any],
        decisions: dict[str, dict[str, Any]],
        *,
        project_id: str,
        branch: str,
        head: str,
        current_truth: Any,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, str]:
        """Find one exact, owner-resumable review remediation or fail closed.

        A plain failed Worker is deliberately invisible here.  The sole recovery
        shape is a failed AIBroker remediation whose exception proves that the
        provider was not reached because its managed worktree was unsafe.
        """
        consumed_sources = {
            _non_blank_config(row.get("recovery_of"))
            for row in ledger["executions"].values()
            if isinstance(row, dict)
        }
        # Recovery authority is anchored to the repository truth at which the
        # remediation failed.  Older task/HEAD failures remain immutable audit
        # evidence but must not globally block a later task after the repository
        # has advanced to a new anchor.  Same-anchor unresolved review recovery
        # remains eligible even when agent/next.md already advertises a later
        # task, preserving the exact reviewed-task remediation contract.
        failed_remediations = [
            row for row in ledger["executions"].values()
            if isinstance(row, dict)
            and row.get("project_id") == project_id
            and row.get("engine") == "aibroker"
            and row.get("source_kind") == "remediation"
            and row.get("state") == "failed"
            and row.get("branch") == branch
            and row.get("head") == head
            and _non_blank_config(row.get("source_request_id")) not in consumed_sources
        ]
        if not failed_remediations:
            return None, None, None, ""
        candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        errors: list[str] = []
        for candidate in failed_remediations:
            if not self._failed_before_provider_work(candidate):
                errors.append("failed remediation is not an exact no-usable-provider-work WorktreeUnsafeError recovery")
                continue
            decision_id = _non_blank_config(candidate.get("review_decision_id")) or _non_blank_config(candidate.get("source_request_id"))
            decision = decisions.get(decision_id or "")
            task_id = _non_blank_config(candidate.get("task_id"))
            if not isinstance(decision, dict) or task_id is None:
                errors.append("remediation recovery lacks its durable reviewer decision")
                continue
            if (
                decision.get("disposition") != "apply"
                or decision.get("decision") != "remediate"
                or decision.get("next_action") != NextAction.CONTINUE_CURRENT_STAGE.value
                or decision.get("project_id") != project_id
                or decision.get("task_id") != task_id
                or decision.get("branch") != branch
                or decision.get("head") != head
                or decision.get("role") != WebSolRole.REVIEWER.value
                or decision.get("event") != WebSolEvent.WORKER_DONE.value
            ):
                errors.append("remediation recovery reviewer decision no longer matches project/task/branch/HEAD")
                continue
            review_hash = _non_blank_config(decision.get("review_status_hash"))
            if review_hash is None:
                errors.append("remediation recovery decision lacks reviewed dirty fingerprint")
                continue
            if current_truth.dirty:
                errors.append("remediation recovery requires a clean current worktree")
                continue
            if current_truth.status_hash == review_hash:
                fingerprint_evidence = {
                    "kind": "exact_reviewed_fingerprint",
                    "review_status_hash": review_hash,
                }
            else:
                fingerprint_evidence = self._generated_only_fingerprint_evidence(
                    candidate, decision, review_hash,
                )
                if fingerprint_evidence is None:
                    errors.append("reviewed dirty fingerprint changed without generated-only proof")
                    continue
            candidates.append((candidate, decision, fingerprint_evidence))
        if self._active_project(ledger, project_id):
            return None, None, None, "another managed Worker is already active"
        if len(candidates) == 1:
            return (*candidates[0], "")
        if len(candidates) > 1:
            return None, None, None, "multiple failed remediations match recovery evidence"
        return None, None, None, (errors[0] if errors else "failed remediation recovery is unsafe")

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
        for request_id, record in ordered:
            project_id = _non_blank_config(record.get("project_id"))
            try:
                launch = self._actuate_decision(request_id, record, projects, snapshots)
            except Exception as exc:  # noqa: BLE001 - contain one project's actuation fault
                self.record_actuation_error(
                    project_id, exc,
                    request_id=request_id,
                    task_id=_non_blank_config(record.get("task_id")),
                    source_kind="decision",
                    phase="decision_actuation",
                )
                continue
            if launch is not None:
                launches.append(launch)
        return launches

    def _actuate_decision(
        self,
        request_id: str,
        record: dict[str, Any],
        projects: dict[str, dict[str, Any]],
        snapshots: dict[str, dict[str, Any]],
    ) -> Optional[ActuationLaunch]:
        """Actuate exactly one durable decision row.

        Raising is contained by the caller: an unexpected actuation fault must
        neither consume this decision nor abort actuation for other projects.
        """
        accepted = {
            ("next", NextAction.NEXT_TASK.value),
            ("remediate", NextAction.CONTINUE_CURRENT_STAGE.value),
        }
        with self._lock:
            ledger = self._load_ledger()
            existing = ledger["executions"].get(request_id)
            proj_id = _non_blank_config(record.get("project_id"))
            authority = ledger.get("lifecycle", {}).get(proj_id) if proj_id else None
            rec_head = _non_blank_config(record.get("head")) or ""
            if existing is not None and not (
                _legacy_not_ready_block(existing)
                or _legacy_no_next_settle(existing)
                or _legacy_not_advanced_block(existing)
                or _transient_remediation_state_block(existing)
                or _is_declaration_gate_replayable(existing, authority, rec_head)
            ):
                return None
        if record.get("disposition") != "apply":
            return None
        decision = _non_blank_config(record.get("decision"))
        next_action = _non_blank_config(record.get("next_action"))
        if (decision, next_action) not in accepted:
            return None
        project_id = _non_blank_config(record.get("project_id"))
        if project_id is None:
            return None
        if not self._automatic_decision_allowed(project_id, record.get("consumed_at")):
            return None
        task_id = _non_blank_config(record.get("task_id"))
        branch = _non_blank_config(record.get("branch"))
        head = _non_blank_config(record.get("head"))
        if task_id is None or branch is None or head is None:
            self._record_blocked(request_id, project_id, "decision identity is incomplete")
            return None
        if record.get("role") != WebSolRole.REVIEWER.value or record.get("event") != WebSolEvent.WORKER_DONE.value:
            self._record_blocked(
                request_id, project_id, "V1 actuation requires WORKER_DONE reviewer flow",
                task_id=task_id,
            )
            return None
        project = projects.get(project_id)
        snapshot = snapshots.get(project_id)
        if project is None or snapshot is None:
            self._record_blocked(
                request_id, project_id,
                "project configuration or monitor snapshot unavailable",
                task_id=task_id,
            )
            return None
        policy, error = _execution_policy(project)
        if policy is None:
            self._record_blocked(request_id, project_id, error, task_id=task_id)
            return None
        if next_action not in policy["allowed_next_actions"]:
            self._record_blocked(
                request_id, project_id,
                "next_action is not owner-authorized by project execution policy",
                task_id=task_id,
            )
            return None
        lineage: dict[str, Any] | None = None
        if decision == "remediate":
            reviewed_hash = _non_blank_config(record.get("review_status_hash"))
            if reviewed_hash is None:
                self._record_blocked(
                    request_id, project_id,
                    "remediation decision lacks reviewed dirty fingerprint",
                    task_id=task_id, source_kind="remediation",
                )
                return None
            # Exact remediation is anchored to the reviewed task identity and
            # reviewed repository truth, not to the task currently advertised
            # by agent/next.md. A completed Worker may already have advanced
            # next.md for NEXT_TASK handoff before review; remediation must
            # still repair the reviewed task without starting that later task.
            _guard_task, guard_error = self._fresh_guard(
                project, snapshot,
                expected_branch=branch, expected_head=head,
                expected_status_hash=reviewed_hash,
                anchor_task_id=task_id,
            )
            launch_task = task_id if _guard_task is not None else None
            source_kind = "remediation"
            reviewed_truth = read_repository_truth(project.get("repo_path") or "")
            if (
                launch_task is not None
                and reviewed_truth.valid
                and reviewed_truth.status_hash == reviewed_hash
            ):
                lineage = {
                    "review_decision_id": request_id,
                    "review_status_hash": reviewed_hash,
                    "review_dirty_entries": list(reviewed_truth.dirty_entries),
                }
            prompt = (
                str(policy["remediation_prompt"])
                + "\nReviewed task identity: {0}. Remediate only this reviewed task. "
                + "If agent/next.md already advertises a later task, do not implement "
                + "that later task; preserve the handoff unless the review gap itself "
                + "requires correcting it."
            ).format(task_id)
        else:
            current_task = _advertised_task_id(snapshot)
            reviewed_current_ready = (
                current_task == task_id and _next_task_ready(snapshot, predecessor_task_id=task_id)
            )
            if current_task == task_id and (
                _task_marked_complete(snapshot) or reviewed_current_ready
            ):
                repo_dir = project.get("repo_path") or ""
                successor = resolve_successor(repo_dir, task_id)
                if successor.kind in {"invalid", "ambiguous"}:
                    self._record_blocked(
                        request_id, project_id,
                        "ROADMAP_SUCCESSOR_INCONSISTENT: staged roadmap invalid: "
                        f"{successor.reason}",
                        task_id=task_id, source_kind="decision",
                    )
                    return None
                reconciled_head: str | None = None
                if successor.kind == "inconsistent":
                    if self._progress_channel is not None:
                        self._progress_channel.emit(
                            {"project_id": project_id}, "ROADMAP_SUCCESSOR_INCONSISTENT",
                            task_id=task_id, occurrence_key=f"{request_id}:successor-inconsistent",
                            details={"reason": successor.reason, "successor": successor.successor_task_id},
                        )
                    outcome = reconcile_roadmap_successor(repo_dir, task_id, successor)
                    if outcome.status not in {"applied", "already_consistent"}:
                        # Dirty-worktree and other transient failures must not
                        # terminally consume the accepted NEXT decision.
                        if self._progress_channel is not None:
                            self._progress_channel.emit(
                                {"project_id": project_id}, "BLOCKED",
                                task_id=task_id, occurrence_key=request_id,
                                details={
                                    "code": "ROADMAP_SUCCESSOR_INCONSISTENT",
                                    "reason": outcome.reason,
                                    "retryable": "clean repository" in str(outcome.reason or ""),
                                },
                            )
                        return None
                    reconciled_head = outcome.head
                    if self._progress_channel is not None:
                        self._progress_channel.emit(
                            {"project_id": project_id}, "SUCCESSOR_RECONCILED",
                            task_id=task_id, occurrence_key=f"{request_id}:successor-reconciled",
                            details={"successor": outcome.successor_task_id, "head": outcome.head},
                        )
                    successor = resolve_successor(repo_dir, task_id)
                    if successor.kind != "successor":
                        self._record_blocked(
                            request_id, project_id,
                            "ROADMAP_SUCCESSOR_INCONSISTENT: reconciliation did not converge",
                            task_id=task_id, source_kind="decision",
                        )
                        return None
                truth = read_repository_truth(repo_dir)
                reviewed_hash = _non_blank_config(record.get("review_status_hash"))
                if (
                    not truth.valid or truth.branch != branch
                    or (truth.head != head and truth.head != reconciled_head)
                    or truth.dirty or (reviewed_hash is not None and truth.status_hash != reviewed_hash)
                ):
                    self._record_blocked(
                        request_id, project_id,
                        "reviewed task repository truth changed before terminal settle",
                        task_id=task_id, source_kind="decision",
                    )
                elif successor.kind == "successor":
                    self._record_handoff(
                        request_id, project_id, task_id,
                        str(successor.successor_task_id),
                        "staged roadmap specifies successor task; planner handoff required",
                        staged=successor,
                        reviewed_branch=truth.branch,
                        reviewed_head=truth.head,
                        reviewed_ready=reviewed_current_ready,
                        successor_evidence=(
                            "reconciled" if reconciled_head else successor.evidence
                        ),
                    )
                else:
                    reason = (
                        "review accepted current READY_TO_RUN task and no next "
                        "executable task is advertised"
                        if reviewed_current_ready
                        else "reviewed task is COMPLETE and no next executable task is advertised"
                    )
                    self._record_settled(
                        request_id, project_id, reason,
                        task_id=task_id, outcome="task_complete",
                    )
                return None
            if (
                current_task is not None and current_task != task_id
                and parse_task_status(snapshot.get("next_status")).is_pending_design()
            ):

                truth = read_repository_truth(project.get("repo_path") or "")
                reviewed_hash = _non_blank_config(record.get("review_status_hash"))
                if (
                    not truth.valid or truth.branch != branch or truth.head != head
                    or truth.dirty or (reviewed_hash is not None and truth.status_hash != reviewed_hash)
                ):
                    self._record_blocked(
                        request_id, project_id,
                        "next PENDING DESIGN task repository truth changed before lifecycle handoff",
                        task_id=task_id, source_kind="decision",
                    )
                else:
                    self._record_handoff(
                        request_id, project_id, task_id, current_task,
                        "reviewed task advanced to a PENDING DESIGN task; planner handoff required",
                        successor_evidence="repository_projection",
                    )
                return None
            next_snapshot = snapshot
            if current_task is not None and current_task != task_id and _next_task_ready(snapshot, predecessor_task_id=task_id):
                next_snapshot = copy.deepcopy(snapshot)
                next_snapshot["state"] = "READY_TO_RUN"
                telemetry = next_snapshot.get("telemetry") if isinstance(next_snapshot.get("telemetry"), dict) else {}
                telemetry = copy.deepcopy(telemetry); telemetry["task_id"] = current_task
                next_snapshot["telemetry"] = telemetry
            launch_task, guard_error = self._fresh_guard(
                project, next_snapshot,
                expected_branch=branch, expected_head=head,
                must_advance_from=task_id,
                predecessor_task_id=task_id,
            )
            source_kind = "decision"
            prompt = str(policy["worker_prompt"])
        if launch_task is None:
            if "readiness" in (guard_error or "").lower():
                # A readiness-caused block must not permanently consume the decision row;
                # allow subsequent recovery/supervisor passes or fixes to actuate it.
                if self._progress_channel is not None:
                    self._progress_channel.emit(
                        {"project_id": project_id}, "BLOCKED",
                        task_id=task_id, occurrence_key=request_id,
                        details={"reason": guard_error},
                    )
                return None
            self._record_blocked(
                request_id, project_id, guard_error,
                task_id=task_id, source_kind=source_kind,
            )
            return None
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
            lineage=lineage,
        )
        return launch

    def _automatic_decision_allowed(self, project_id: str, consumed_at: Any) -> bool:
        state = self.owner_store.project_state(project_id)
        if state.get("paused"):
            return False
        paused_at = parse_utc(state.get("paused_at"))
        if paused_at is None:
            return True
        consumed = parse_utc(consumed_at)
        return consumed is not None and consumed > paused_at

    def start_control(
        self, project: dict[str, Any], snapshot: dict[str, Any], source_request_id: str,
        *, exact_remediation_only: bool = False, lineage: Optional[dict[str, Any]] = None,
        source_task_id: Optional[str] = None,
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
        task_id = _current_task_id(snapshot)
        decisions = self._load_decisions()
        with self._lock:
            ledger = self._load_ledger()
            reanchor_original, reanchor_decision, reanchor_review, reanchor_error = self._blocked_reconcile_remediation_retry(
                ledger, decisions,
                project_id=project_id, task_id=task_id, branch=truth.branch, head=truth.head,
                current_truth=truth,
            )
            failed_remediation, review_decision, fingerprint_evidence, recovery_error = self._pre_execution_remediation_retry(
                ledger, decisions,
                project_id=project_id, branch=truth.branch, head=truth.head,
                current_truth=truth,
            )
            recovery_descendant_barrier = self._recovery_descendant_barrier(
                ledger, project_id, branch=truth.branch, head=truth.head,
            )
        if reanchor_error:
            self._record_blocked(
                source_request_id, project_id, reanchor_error,
                task_id=task_id, source_kind="remediation",
            )
            return None
        if reanchor_original is not None and reanchor_decision is not None and reanchor_review is not None:
            if NextAction.CONTINUE_CURRENT_STAGE.value not in policy["allowed_next_actions"]:
                self._record_blocked(
                    source_request_id, project_id,
                    "review-driven remediation is not owner-authorized by project execution policy",
                    task_id=task_id, source_kind="remediation",
                )
                return None
            guard_error = self._recovery_fresh_guard(
                project, snapshot, expected_branch=truth.branch, expected_head=truth.head,
            )
            if guard_error:
                self._record_blocked(
                    source_request_id, project_id, guard_error,
                    task_id=task_id, source_kind="remediation",
                )
                return None
            decision_id = _non_blank_config(reanchor_decision.get("request_id"))
            original_id = _non_blank_config(reanchor_review.get("reconcile_of"))
            prompt = (
                str(policy["remediation_prompt"])
                + "\nReviewed task identity: {0}. Remediate only this reviewed task.\n\n"
                + self._review_evidence(reanchor_decision)
            ).format(task_id)
            return self._launch(
                project, source_request_id=source_request_id, source_kind="remediation",
                task_id=task_id, source_task_id=task_id,
                branch=truth.branch, head=truth.head, worker_prompt=prompt, policy=policy,
                lineage={
                    "recovery_of": original_id,
                    "review_decision_id": decision_id,
                    "reconcile_review_id": decision_id,
                    "recovery_reason": "owner continue after transient blocked re-anchored remediation",
                    "review_status_hash": reanchor_decision.get("review_status_hash"),
                },
            )

        recovery_task_id = _non_blank_config(
            failed_remediation.get("task_id") if failed_remediation else None
        )
        if recovery_descendant_barrier:
            self._record_blocked(
                source_request_id, project_id, recovery_descendant_barrier,
                task_id=recovery_task_id or task_id, source_kind="remediation",
            )
            return None
        if recovery_error:
            self._record_blocked(
                source_request_id, project_id, recovery_error,
                task_id=recovery_task_id or task_id, source_kind="remediation",
            )
            return None
        if (
            failed_remediation is not None
            and review_decision is not None
            and fingerprint_evidence is not None
            and recovery_task_id is not None
        ):
            if NextAction.CONTINUE_CURRENT_STAGE.value not in policy["allowed_next_actions"]:
                self._record_blocked(
                    source_request_id, project_id,
                    "review-driven remediation is not owner-authorized by project execution policy",
                    task_id=recovery_task_id, source_kind="remediation",
                )
                return None
            guard_error = self._recovery_fresh_guard(
                project, snapshot, expected_branch=truth.branch, expected_head=truth.head,
            )
            if guard_error:
                self._record_blocked(
                    source_request_id, project_id, guard_error,
                    task_id=recovery_task_id, source_kind="remediation",
                )
                return None
            prompt = (
                str(policy["remediation_prompt"])
                + "\nReviewed task identity: {0}. Remediate only this reviewed task.\n\n"
                + self._review_evidence(review_decision)
            ).format(recovery_task_id)
            review_decision_id = (
                _non_blank_config(review_decision.get("request_id"))
                or _non_blank_config(failed_remediation.get("review_decision_id"))
                or _non_blank_config(failed_remediation.get("source_request_id"))
            )
            lineage = {
                "recovery_of": failed_remediation.get("source_request_id"),
                "review_decision_id": review_decision_id,
                "recovery_reason": "owner continue after pre-execution worktree-safety refusal",
                "review_status_hash": review_decision.get("review_status_hash"),
                "reviewed_fingerprint_evidence": fingerprint_evidence,
            }
            launch = self._launch(
                project, source_request_id=source_request_id, source_kind="remediation",
                task_id=recovery_task_id, source_task_id=recovery_task_id,
                branch=truth.branch, head=truth.head, worker_prompt=prompt, policy=policy,
                lineage=lineage,
            )
            return launch
        if exact_remediation_only:
            return None
        if task_id is None:
            self._record_blocked(source_request_id, project_id, "current task id is unavailable", source_kind="control")
            return None
        with self._lock:
            ledger = self._load_ledger()
            broker_rows = [
                row for row in ledger["executions"].values()
                if isinstance(row, dict)
                and row.get("project_id") == project_id
                and row.get("engine") == "aibroker"
                and (not row.get("task_id") or row.get("task_id") == task_id)
                and (not row.get("head") or row.get("head") == truth.head)
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
        launch_task, guard_error = self._fresh_guard(
            project, snapshot, expected_branch=truth.branch, expected_head=truth.head,
            expected_task_id=task_id,
        )
        if launch_task is None:
            self._record_blocked(source_request_id, project_id, guard_error, task_id=task_id, source_kind="control")
            return None
        if lineage is None:
            with self._lock:
                current_ledger = self._load_ledger()
            for row in current_ledger.get("executions", {}).values():
                if isinstance(row, dict) and str(row.get("project_id") or "") == project_id and row.get("state") == "explicitly_reconciled":
                    if row.get("reconciled_by") == source_request_id or (
                        source_request_id.startswith("wd-xl-") and row.get("invariant_key") == source_request_id[len("wd-xl-"):]
                    ):
                        lost_srid = row.get("source_request_id")
                        if lost_srid:
                            from dev_orchestrator.core.execution_lifecycle import lineage_key_for
                            lineage = {
                                "recovery_of": lost_srid,
                                "recovery_of_lineage_key": lineage_key_for(project_id, lost_srid),
                                "recovery_reason": "watchdog execution loss recovery",
                            }
                        break
        with self._lock:
            current_ledger = self._load_ledger()
            if self._migrate_legacy_handoff_authority(
                current_ledger, project_id, launch_task,
            ):
                self._save_ledger(current_ledger)
        return self._launch(
            project, source_request_id=source_request_id, source_kind="control",
            task_id=launch_task, source_task_id=source_task_id,
            branch=truth.branch, head=truth.head,
            worker_prompt=str(policy["worker_prompt"]), policy=policy,
            lineage=lineage,
        )

    def resume_exact_remediation(
        self, project: dict[str, Any], snapshot: dict[str, Any], source_request_id: str,
        *, review_gate_id: str | None = None,
    ) -> Optional[ActuationLaunch]:
        """Let owner resume recover only the exact safe remediation shape.

        Normal resume remains an unpause operation.  This narrow adapter never
        turns resume into a generic Worker start when no proven recovery exists.
        """
        project_id = str(project.get("project_id") or "")
        policy, _error = _execution_policy(project)
        truth = read_repository_truth(project.get("repo_path") or "")
        if policy is None or not truth.valid:
            return None
        if review_gate_id is not None:
            return self._resume_budget_exhausted_review_gate(
                project, snapshot, source_request_id, review_gate_id,
                policy=policy, truth=truth,
            )
        decisions = self._load_decisions()
        with self._lock:
            failed, decision, fingerprint_evidence, error = self._pre_execution_remediation_retry(
                self._load_ledger(), decisions,
                project_id=project_id, branch=truth.branch, head=truth.head,
                current_truth=truth,
            )
        if failed is None or decision is None or fingerprint_evidence is None or error:
            return None
        return self.start_control(
            project, snapshot, source_request_id, exact_remediation_only=True,
        )

    def _resume_budget_exhausted_review_gate(
        self,
        project: dict[str, Any],
        snapshot: dict[str, Any],
        source_request_id: str,
        review_gate_id: str,
        *,
        policy: dict[str, Any],
        truth: Any,
    ) -> Optional[ActuationLaunch]:
        """Continue one exact localized review finding after its legacy budget gate."""
        project_id = str(project.get("project_id") or "")
        task_id = _current_task_id(snapshot)
        reviews_raw = read_json(self.runtime_root / "ai-reviewer.json", {})
        reviews = reviews_raw.get("reviews") if isinstance(reviews_raw, dict) else None
        review = reviews.get(review_gate_id) if isinstance(reviews, dict) else None
        decision = self._load_decisions().get(review_gate_id)

        error = "technical review owner gate is not an exact bounded remediation"
        blocking: list[dict[str, Any]] = []
        if isinstance(review, dict):
            findings = review.get("review_findings")
            if isinstance(findings, list):
                blocking = [
                    copy.deepcopy(row) for row in findings
                    if isinstance(row, dict)
                    and str(row.get("summary") or "").strip().upper().startswith("BLOCKING:")
                ]
        try:
            remediation_round = int(review.get("remediation_round") or 0) if isinstance(review, dict) else 0
            max_rounds = int(review.get("max_remediation_rounds") or 0) if isinstance(review, dict) else 0
        except (TypeError, ValueError):
            remediation_round = max_rounds = 0

        if not isinstance(review, dict) or not isinstance(decision, dict):
            error = "technical review owner gate evidence is unavailable"
        elif not (
            review.get("review_id") == review_gate_id
            and review.get("state") == "completed"
            and review.get("decision") == "owner_gate"
            and review.get("next_action") == "stop"
            and str(review.get("reason") or "").startswith(
                "technical review remediation budget exhausted ("
            )
            and remediation_round >= max_rounds > 0
            and review.get("remediation_extension_granted") is not True
            and len(blocking) == 1
        ):
            error = "technical review owner gate is not a single localized exhausted-budget finding"
        elif not (
            decision.get("request_id") == review_gate_id
            and decision.get("disposition") == "owner_gate"
            and decision.get("decision") == "owner_gate"
            and decision.get("next_action") == "stop"
            and decision.get("role") == WebSolRole.REVIEWER.value
            and decision.get("event") == WebSolEvent.WORKER_DONE.value
        ):
            error = "technical review owner gate decision evidence does not match"
        elif task_id is None or review.get("task_id") != task_id or decision.get("task_id") != task_id:
            error = "technical review owner gate task does not match current task"
        elif review.get("project_id") != project_id or decision.get("project_id") != project_id:
            error = "technical review owner gate project does not match"
        elif truth.dirty:
            error = "exact review remediation requires a clean current worktree"
        elif review.get("branch") != truth.branch or decision.get("branch") != truth.branch:
            error = "technical review owner gate branch does not match current branch"
        elif review.get("review_status_hash") != truth.status_hash or decision.get("review_status_hash") != truth.status_hash:
            error = "technical review owner gate dirty fingerprint does not match current worktree"
        elif NextAction.CONTINUE_CURRENT_STAGE.value not in policy["allowed_next_actions"]:
            error = "review-driven remediation is not owner-authorized by project execution policy"
        else:
            review_head = _non_blank_config(review.get("head"))
            decision_head = _non_blank_config(decision.get("head"))
            if review_head is None or review_head != decision_head:
                error = "technical review owner gate HEAD evidence is incomplete"
            elif review_head != truth.head:
                try:
                    ancestry = subprocess.run(
                        ["git", "-C", str(project.get("repo_path") or ""),
                         "merge-base", "--is-ancestor", review_head, truth.head],
                        capture_output=True, timeout=15, **hidden_subprocess_kwargs(),
                    )
                except (OSError, subprocess.SubprocessError):
                    error = "exhausted review gate ancestry is temporarily unavailable"
                else:
                    if ancestry.returncode != 0:
                        error = "current HEAD is not a descendant of the exhausted review gate"
                    else:
                        error = ""
            else:
                error = ""

        source_execution_id = _non_blank_config(review.get("source_request_id")) if isinstance(review, dict) else None
        with self._lock:
            ledger = self._load_ledger()
            source_execution = ledger.get("executions", {}).get(source_execution_id or "")
            if not error and self._active_project(ledger, project_id):
                error = "another managed Worker is already active"
            if not error and not (
                source_execution_id is not None
                and isinstance(source_execution, dict)
                and source_execution.get("project_id") == project_id
                and source_execution.get("task_id") == task_id
                and source_execution.get("source_kind") == "remediation"
                and source_execution.get("state") == "completed"
            ):
                error = "technical review owner gate source remediation evidence does not match"
            if not error:
                later = [
                    row for rid, row in (reviews or {}).items()
                    if rid != review_gate_id
                    and isinstance(row, dict)
                    and row.get("project_id") == project_id
                    and row.get("task_id") == task_id
                    and str(row.get("started_at") or "") > str(review.get("started_at") or "")
                ]
                if later:
                    error = "technical review owner gate has been superseded"

        if error:
            self._record_blocked(
                source_request_id, project_id, error,
                task_id=task_id, source_kind="remediation",
            )
            return None

        guard_error = self._recovery_fresh_guard(
            project, snapshot, expected_branch=truth.branch, expected_head=truth.head,
        )
        if guard_error:
            self._record_blocked(
                source_request_id, project_id, guard_error,
                task_id=task_id, source_kind="remediation",
            )
            return None
        bounded_evidence = copy.deepcopy(decision)
        bounded_evidence["decision"] = "remediate"
        bounded_evidence["next_action"] = NextAction.CONTINUE_CURRENT_STAGE.value
        bounded_evidence["findings"] = blocking
        prompt = (
            str(policy["remediation_prompt"])
            + f"\nOwner authorized only the single blocking finding from exhausted review gate {review_gate_id}. "
              "Do not address non-blocking follow-ups or broaden lifecycle architecture.\n\n"
            + self._review_evidence(bounded_evidence)
        )
        return self._launch(
            project, source_request_id=source_request_id, source_kind="remediation",
            task_id=str(task_id), source_task_id=str(task_id),
            branch=truth.branch, head=truth.head, worker_prompt=prompt, policy=policy,
            lineage={
                "recovery_of": source_execution_id,
                "review_decision_id": review_gate_id,
                "owner_gate_review_id": review_gate_id,
                "recovery_reason": "owner continue for exact exhausted-review remediation",
                "review_status_hash": truth.status_hash,
                "reviewed_gate_head": review.get("head"),
            },
        )

    def _advance_completed_predecessor_handoffs(
        self,
        projects: dict[str, dict[str, Any]],
        snapshots: dict[str, dict[str, Any]],
    ) -> None:
        for project_id, project in projects.items():
            try:
                self._advance_completed_predecessor_handoff(
                    project_id, project, snapshots.get(project_id),
                )
            except Exception as exc:  # noqa: BLE001 - contain one project's handoff fault
                self.record_actuation_error(
                    project_id, exc,
                    source_kind="predecessor_handoff",
                    phase="predecessor_handoff",
                )

    def _advance_completed_predecessor_handoff(
        self,
        project_id: str,
        project: dict[str, Any],
        snapshot: dict[str, Any] | None,
    ) -> None:
        """Record the handoff or settlement owed by one completed predecessor task.

        Raising is contained by the caller: one project's roadmap or successor
        repair fault must not stop the remaining projects from advancing.
        """
        if self.owner_store.is_paused(project_id):
            return
        if snapshot is None:
            return
        current_task = _advertised_task_id(snapshot)
        if not current_task:
            return
        if not _task_marked_complete(snapshot):
            return
        repo_dir = project.get("repo_path") or ""
        truth = read_repository_truth(repo_dir)
        if not truth.valid or truth.dirty:
            return
        with self._lock:
            ledger = self._load_ledger()
            if self._active_project(ledger, project_id):
                return
            successor = resolve_successor(repo_dir, current_task)
            if successor.kind == "inconsistent":
                outcome = reconcile_roadmap_successor(repo_dir, current_task, successor)
                if outcome.status not in {"applied", "already_consistent"}:
                    return
                truth = read_repository_truth(repo_dir)
                successor = resolve_successor(repo_dir, current_task)
            successor_id = str(successor.successor_task_id) if successor.kind == "successor" else None
            has_handoff_or_settled = any(
                rec.get("project_id") == project_id
                and (
                    rec.get("task_id") == current_task
                    or rec.get("source_task_id") == current_task
                    or (successor_id and (rec.get("staged_successor") == successor_id or rec.get("next_task_id") == successor_id))
                )
                and rec.get("state") in ("handoff", "settled", "blocked")
                # A prior terminal settlement was valid only while the roadmap
                # advertised no successor. If a successor is later staged, it
                # must not permanently suppress the newly valid handoff.
                and not (
                    successor_id
                    and rec.get("state") == "settled"
                    and rec.get("outcome") == "task_complete"
                )
                for rec in ledger["executions"].values()
                if isinstance(rec, dict)
            )
            if has_handoff_or_settled:
                return
            decisions = self._load_decisions()
            has_decision = any(
                dec.get("project_id") == project_id
                and dec.get("task_id") == current_task
                for dec in decisions.values()
                if isinstance(dec, dict)
            )
            if has_decision:
                return
            roles = project.get("ai_roles")
            reviewer_enabled = (
                isinstance(roles, dict)
                and isinstance(roles.get("reviewer"), dict)
                and roles["reviewer"].get("enabled") is True
            )
            if reviewer_enabled:
                # When reviewer is enabled, promotion of completed predecessor tasks
                # is strictly governed by accepted technical review decisions via
                # _advance_decisions; auto-handoff must never bypass mandatory review.
                return
        if successor.kind == "successor":
            request_id = f"auto-handoff:{current_task}:{truth.head[:12]}"
            with self._lock:
                if request_id in self._load_ledger()["executions"]:
                    return
            self._record_handoff(
                request_id,
                project_id,
                current_task,
                str(successor.successor_task_id),
                "predecessor task marked complete; planner handoff required for successor",
                staged=successor,
                reviewed_branch=truth.branch,
                reviewed_head=truth.head,
                reviewed_ready=False,
                successor_evidence=successor.evidence,
            )
        elif successor.kind == "end_of_roadmap":
            request_id = f"auto-settled:{current_task}:{truth.head[:12]}"
            with self._lock:
                if request_id in self._load_ledger()["executions"]:
                    return
            self._record_settled(
                request_id,
                project_id,
                "predecessor task marked complete and no next executable task is advertised",
                task_id=current_task,
                outcome="task_complete",
            )
        elif successor.kind == "invalid":
            request_id = f"auto-handoff:{current_task}:{truth.head[:12]}"
            with self._lock:
                if request_id in self._load_ledger()["executions"]:
                    return
            self._record_blocked(
                request_id,
                project_id,
                f"staged roadmap invalid: {successor.reason}",
                task_id=current_task,
                source_kind="decision",
            )

    def _advance_unlaunched_ready(
        self,
        projects: dict[str, dict[str, Any]],
        snapshots: dict[str, dict[str, Any]],
    ) -> list[ActuationLaunch]:
        launches: list[ActuationLaunch] = []
        for project_id, project in projects.items():
            if self.owner_store.is_paused(project_id) or self.owner_store.suppress_static_starts(project_id):
                continue
            policy, _ = _execution_policy(project)
            if policy is None:
                continue
            snapshot = snapshots.get(project_id)
            if snapshot is None:
                continue
            current_task = _advertised_task_id(snapshot)
            if not current_task:
                continue
            if snapshot.get("state") != "READY_TO_RUN" or not _next_task_ready(snapshot):
                continue
            truth = read_repository_truth(project.get("repo_path") or "")
            if not truth.valid or truth.dirty:
                continue
            with self._lock:
                ledger = self._load_ledger()
                if self._active_project(ledger, project_id):
                    continue
                if policy.get("owner_start") is not None:
                    owner_req = str(policy["owner_start"].get("request_id") or "")
                    if owner_req and owner_req not in ledger["executions"]:
                        continue
                if policy.get("bootstrap") is not None:
                    boot_req = str(policy["bootstrap"].get("request_id") or "")
                    if boot_req and boot_req not in ledger["executions"] and not self._project_has_execution_history(ledger, project_id):
                        continue
                request_id = f"auto-ready:{current_task}:{truth.head[:12]}"
                if request_id in ledger["executions"]:
                    continue
                already_run = any(
                    record.get("project_id") == project_id
                    and record.get("task_id") == current_task
                    and record.get("head") == truth.head
                    for record in ledger["executions"].values()
                    if isinstance(record, dict)
                )
                if already_run:
                    continue
            next_snapshot = copy.deepcopy(snapshot)
            next_snapshot["state"] = "READY_TO_RUN"
            telemetry = next_snapshot.get("telemetry") if isinstance(next_snapshot.get("telemetry"), dict) else {}
            telemetry = copy.deepcopy(telemetry)
            telemetry["task_id"] = current_task
            next_snapshot["telemetry"] = telemetry
            launch_task, error = self._fresh_guard(
                project,
                next_snapshot,
                expected_branch=truth.branch,
                expected_head=truth.head,
                expected_task_id=current_task,
            )
            if launch_task is None:
                self._record_blocked(
                    request_id, project_id, error,
                    task_id=current_task, source_kind="ready",
                )
                continue
            launch = self._launch(
                project,
                source_request_id=request_id,
                source_kind="ready",
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

    def _advance_owner_start(
        self, projects: dict[str, dict[str, Any]], snapshots: dict[str, dict[str, Any]],
    ) -> list[ActuationLaunch]:
        launches: list[ActuationLaunch] = []
        for project_id, project in projects.items():
            if self.owner_store.is_paused(project_id) or self.owner_store.suppress_static_starts(project_id):
                continue
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
            if self.owner_store.is_paused(project_id) or self.owner_store.suppress_static_starts(project_id):
                continue
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
        launches: list[ActuationLaunch] = []
        launches.extend(self._advance_decisions(projects, decision_snapshots))
        self._advance_completed_predecessor_handoffs(projects, raw_snapshots)
        # Each remaining phase is contained on its own: a fault in one must not
        # discard the launches already produced, nor skip the later phases.
        for phase, resolver in (
            ("unlaunched_ready", self._advance_unlaunched_ready),
            ("owner_start", self._advance_owner_start),
            ("bootstrap", self._advance_bootstrap),
        ):
            # Invoke each phase with one project at a time.  The phase helpers
            # persist launch intent before returning; isolating calls here both
            # preserves already-produced launch evidence and prevents an early
            # project failure from skipping later projects in the same phase.
            for project_id, project in projects.items():
                try:
                    launches.extend(resolver(
                        {project_id: project}, raw_snapshots,
                    ))
                except Exception as exc:  # noqa: BLE001 - per-project containment
                    self.record_actuation_error(
                        project_id, exc, source_kind=phase, phase=phase,
                    )
        return launches
