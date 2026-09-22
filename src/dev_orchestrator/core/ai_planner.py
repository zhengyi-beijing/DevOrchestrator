"""AIBroker planner + independent plan-review lifecycle."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult, ResourceContext
from dev_orchestrator.ai.execution_port import AIExecutionPort
from dev_orchestrator.ai.structured_output import (
    StructuredOutputError,
    extract_unique_json_object,
    protocol_repair_prompt,
    require_exact_keys,
)
from dev_orchestrator.accounting import ExecutionRecorder, FailureMemory, environment_for_project
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.staged_roadmap import read_raw, read_successor, sha256_bytes
from dev_orchestrator.core.workflow_policy import workflow_policy_prompt
from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

PLANNER_STATE_FILE = "ai-planner.json"
_STATE_VERSION = 1
_ACTIVE_STATES = frozenset({"planning", "reviewing", "remediating", "applying"})
_ROLE_RESOURCE_FAILURES = frozenset({
    "quota_exhausted",
    "rate_limited",
    "provider_temporarily_unavailable",
    "resource_unavailable",
})


def _nonblank(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _classify_reconciled_dispatch_failure(error: str | None) -> str | None:
    text = str(error or "").lower()
    if any(token in text for token in ("session limit", "no quota", "quota exhausted")):
        return "quota_exhausted"
    if "rate limit" in text or "too many requests" in text:
        return "rate_limited"
    if any(token in text for token in ("timed out", "timeout", "temporarily unavailable", "connection reset", "connection refused")):
        return "provider_temporarily_unavailable"
    if "resource unavailable" in text or "no eligible resource" in text:
        return "resource_unavailable"
    return None


def _planner_policy(project: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    execution = project.get("execution")
    if not isinstance(execution, dict) or execution.get("engine") != "aibroker":
        return None, "planner requires execution.engine=aibroker"
    roles = project.get("ai_roles")
    if not isinstance(roles, dict):
        return None, "ai_roles missing"
    raw = roles.get("planner")
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None, "planner role disabled"
    quality = raw.get("quality", "high")
    review_quality = raw.get("review_quality", "high")
    review_independence = raw.get("review_independence", "resource")
    timeout = raw.get("timeout_seconds", 900)
    review_timeout = raw.get("review_timeout_seconds", 600)
    max_attempts = raw.get("max_attempts", 3)
    max_plan_remediation_rounds = raw.get("max_plan_remediation_rounds", 2)
    max_plan_recovery_cycles = raw.get("max_plan_recovery_cycles", 2)
    hard_total_review_rejects = raw.get("hard_total_review_rejects", 5)
    max_reviewer_resource_failovers = raw.get("max_reviewer_resource_failovers", 2)
    if quality not in {"economy", "balanced", "high"}:
        return None, "planner quality invalid"
    if review_quality not in {"economy", "balanced", "high"}:
        return None, "plan reviewer quality invalid"
    if review_independence not in {"resource", "account", "provider"}:
        return None, "plan reviewer independence invalid"
    for name, value in (("timeout_seconds", timeout), ("review_timeout_seconds", review_timeout)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return None, f"planner {name} must be positive"
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5:
        return None, "planner max_attempts must be an integer from 1 to 5"
    if (
        isinstance(max_plan_remediation_rounds, bool)
        or not isinstance(max_plan_remediation_rounds, int)
        or not 1 <= max_plan_remediation_rounds <= 5
    ):
        return None, "planner max_plan_remediation_rounds must be an integer from 1 to 5"
    if (
        isinstance(max_plan_recovery_cycles, bool)
        or not isinstance(max_plan_recovery_cycles, int)
        or not 0 <= max_plan_recovery_cycles <= 3
    ):
        return None, "planner max_plan_recovery_cycles must be an integer from 0 to 3"
    if (
        isinstance(hard_total_review_rejects, bool)
        or not isinstance(hard_total_review_rejects, int)
        or not 1 <= hard_total_review_rejects <= 12
    ):
        return None, "planner hard_total_review_rejects must be an integer from 1 to 12"
    if (
        isinstance(max_reviewer_resource_failovers, bool)
        or not isinstance(max_reviewer_resource_failovers, int)
        or not 0 <= max_reviewer_resource_failovers <= 2
    ):
        return None, "planner max_reviewer_resource_failovers must be an integer from 0 to 2"
    return {
        "quality": quality,
        "review_quality": review_quality,
        "review_independence": review_independence,
        "timeout_seconds": float(timeout),
        "review_timeout_seconds": float(review_timeout),
        "max_attempts": max_attempts,
        "max_plan_remediation_rounds": max_plan_remediation_rounds,
        "max_plan_recovery_cycles": max_plan_recovery_cycles,
        "hard_total_review_rejects": hard_total_review_rejects,
        "max_reviewer_resource_failovers": max_reviewer_resource_failovers,
    }, ""


_PLAN_REQUIRED_KEYS = {
    "task_id", "summary", "implementation_steps", "interfaces",
    "validation", "risks", "out_of_scope",
}
_PLAN_SEQUENCE_KEYS = (
    "implementation_steps", "interfaces", "validation", "risks", "out_of_scope",
)


class PlannerProtocolError(ValueError):
    """Planner protocol failure with a machine-readable pipeline stage."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def _strip_exact_json_fence(text: str) -> str:
    candidate = text.strip()
    if not candidate.startswith("```"):
        return candidate
    lines = candidate.splitlines()
    if (
        len(lines) < 3
        or lines[0].strip().casefold() not in {"```", "```json"}
        or lines[-1].strip() != "```"
    ):
        raise PlannerProtocolError("extract", "planner output must contain one JSON object")
    body = "\n".join(lines[1:-1]).strip()
    if "```" in body:
        raise PlannerProtocolError("extract", "planner output must contain one JSON object")
    return body


def _top_level_object_spans(text: str) -> list[str]:
    """Return balanced top-level JSON-object spans while respecting strings."""
    spans: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append(text[start:index + 1])
                start = None
    return spans


def _extract_plan_json_object(text: str | None) -> dict[str, Any]:
    """Raw Capture -> JSON Extract using the shared structured-output protocol."""
    try:
        return extract_unique_json_object(text, label="planner")
    except StructuredOutputError as exc:
        raise PlannerProtocolError(exc.stage, str(exc)) from exc


def _normalize_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize only closed, known compatibility shapes; never invent fields."""
    normalized = dict(payload)
    for key in _PLAN_SEQUENCE_KEYS:
        value = normalized.get(key)
        if not isinstance(value, dict):
            continue
        if not value or len(value) > 24:
            raise PlannerProtocolError("schema", f"planner {key} must be a non-empty bounded list")
        items: list[str] = []
        for name, item in value.items():
            name_text = str(name)
            if (
                _nonblank(name_text) is None
                or not isinstance(item, str)
                or _nonblank(item) is None
                or len(name_text) > 120
                or len(item) > 1000
            ):
                raise PlannerProtocolError("schema", f"planner {key} must be a non-empty bounded list")
            items.append(f"{name_text}: {item}")
        normalized[key] = items
    return normalized


def _validate_plan_schema(payload: dict[str, Any]) -> None:
    if set(payload) != _PLAN_REQUIRED_KEYS:
        raise PlannerProtocolError("schema", "planner JSON schema mismatch")
    if not isinstance(payload.get("task_id"), str):
        raise PlannerProtocolError("schema", "planner task_id must be a string")
    if not isinstance(payload.get("summary"), str):
        raise PlannerProtocolError("schema", "planner summary must be a string")
    for key in _PLAN_SEQUENCE_KEYS:
        value = payload.get(key)
        if not isinstance(value, list):
            raise PlannerProtocolError("schema", f"planner {key} must be a list; got {type(value).__name__}")
        if not value:
            raise PlannerProtocolError("schema", f"planner {key} must contain at least 1 item; got 0")
        if len(value) > 24:
            raise PlannerProtocolError("schema", f"planner {key} must contain at most 24 items; got {len(value)}; combine or remove {len(value) - 24} item(s)")
        if any(not isinstance(item, str) or len(item) > 1000 for item in value):
            raise PlannerProtocolError("schema", f"planner {key} entries must be bounded strings")


def _validate_plan_semantics(payload: dict[str, Any], task_id: str) -> None:
    if _nonblank(payload.get("task_id")) != task_id:
        raise PlannerProtocolError("semantic", "planner task_id mismatch")
    if _nonblank(payload.get("summary")) is None:
        raise PlannerProtocolError("semantic", "planner summary must be nonblank")
    for key in _PLAN_SEQUENCE_KEYS:
        if any(_nonblank(item) is None for item in payload[key]):
            raise PlannerProtocolError("semantic", f"planner {key} entries must be nonblank")


def _parse_plan(text: str | None, task_id: str) -> dict[str, Any]:
    """Raw Capture -> JSON Extract -> Normalize -> Schema -> Semantic validation."""
    payload = _extract_plan_json_object(text)
    payload = _normalize_plan_payload(payload)
    _validate_plan_schema(payload)
    _validate_plan_semantics(payload, task_id)
    return payload


def _parse_plan_review(text: str | None) -> tuple[str, str]:
    payload = extract_unique_json_object(text, label="plan reviewer")
    require_exact_keys(payload, {"decision", "reason"}, label="plan reviewer")
    decision = _nonblank(payload.get("decision"))
    reason = _nonblank(payload.get("reason"))
    if decision not in {"approve", "reject", "owner_gate"} or reason is None:
        raise StructuredOutputError("semantic", "invalid plan reviewer decision")
    return decision, reason


class AIPlannerCoordinator:
    """Runs one planner and one independent reviewer, then freezes the approved plan."""

    def __init__(
        self, runtime_root: Path | str, port: AIExecutionPort | None,
        progress_channel: Optional[Any] = None,
        accounting: ExecutionRecorder | None = None,
        failure_memory: FailureMemory | None = None,
        failure_memory_max_chars: int = 2000,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.progress_channel = progress_channel
        self.accounting = accounting
        self.failure_memory = failure_memory
        self.failure_memory_max_chars = failure_memory_max_chars
        self.owner_store = OwnerControlStore(self.runtime_root)
        self.state_path = self.runtime_root / PLANNER_STATE_FILE
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._project_bindings: dict[str, dict[str, Any]] = {}
        self._recover_interrupted()

    def _load_state(self) -> dict[str, Any]:
        raw = read_json(self.state_path, None)
        plans = raw.get("plans") if isinstance(raw, dict) else None
        if not isinstance(plans, dict):
            plans = {}
        return {"version": _STATE_VERSION, "plans": {
            key: value for key, value in plans.items()
            if isinstance(key, str) and isinstance(value, dict)
        }}

    def _save_state(self, state: dict[str, Any]) -> None:
        write_json(self.state_path, state, indent=2)

    def _recover_interrupted(self) -> None:
        with self._lock:
            state = self._load_state()
            changed = False
            for record in state["plans"].values():
                if record.get("state") in _ACTIVE_STATES:
                    record["state"] = "recovery_required"
                    record["reason"] = "daemon restarted during planner lifecycle; automatic replay forbidden"
                    record["recovered_at"] = utc_now_iso()
                    changed = True
            if changed:
                self._save_state(state)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load_state())

    def _wait_for_launch_barrier(self, plan_id: str, project_id: str) -> bool:
        while True:
            with self._lock:
                state = self._load_state()
                plan = state["plans"].get(plan_id)
                if isinstance(plan, dict) and plan.get("state") not in _ACTIVE_STATES:
                    return False
            owner = self.owner_store.project_state(project_id)
            if not owner.get("paused"):
                return True
            if owner.get("last_action") == "stop":
                self._finish(
                    plan_id, "failed",
                    "planner lifecycle stopped by owner before the next AI dispatch",
                )
                return False
            time.sleep(0.1)

    def _begin_lifecycle(
        self,
        project: dict[str, Any],
        policy: dict[str, Any],
        command_id: str,
        task_id: str,
        truth: Any,
        base_fields: dict[str, Any],
    ) -> tuple[str | None, str]:
        from dev_orchestrator.core.project_context import context_prompt_block
        context_block, context_resolution = context_prompt_block(project, "planner")
        ctx_decl = project.get("project_context") or {}
        if ctx_decl.get("enabled") and ctx_decl.get("require_valid", True) and context_resolution.state == "invalid":
            return None, f"durable project context is invalid: {context_resolution.reason}"
        plan_id = "ai_plan:" + command_id
        project_id = str(project.get("project_id") or "")
        repo_text = str(project.get("repo_path") or "")
        binding = project.get("conversation_binding")
        failure_memory_block = ""
        if self.failure_memory is not None:
            failure_memory_block = self.failure_memory.prompt_block(
                environment_for_project(project), max_chars=self.failure_memory_max_chars
            )
        with self._lock:
            state = self._load_state()
            if plan_id in state["plans"]:
                return plan_id, "planner lifecycle already exists"
            active = next((
                row for row in state["plans"].values()
                if isinstance(row, dict)
                and row.get("project_id") == project_id
                and row.get("state") in _ACTIVE_STATES
            ), None)
            if active is not None:
                return None, "planner lifecycle already active for project"
            record: dict[str, Any] = {
                "plan_id": plan_id,
                "command_id": command_id,
                "project_id": project_id,
                "task_id": task_id,
                "repo_path": repo_text,
                "branch": truth.branch,
                "head": truth.head,
                "status_hash": truth.status_hash,
                "state": "planning",
                "started_at": utc_now_iso(),
                "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                "context_block": context_block,
                "context_state": context_resolution.state,
                "context_digest": context_resolution.document.digest if context_resolution.document else None,
                "context_sources": copy.deepcopy(context_resolution.sources),
                "rejection_chain": [],
                "remediation_round": 0,
            }
            if failure_memory_block:
                record["failure_memory_block"] = failure_memory_block
                record["failure_environment"] = environment_for_project(project)
            record.update(base_fields)
            state["plans"][plan_id] = record
            if binding and isinstance(binding, dict):
                self._project_bindings[project_id] = copy.deepcopy(binding)
            self._save_state(state)
        if self.progress_channel is not None and hasattr(self.progress_channel, "register_project"):
            self.progress_channel.register_project(project)
        thread = threading.Thread(
            target=self._run_cycle,
            args=(plan_id, project, policy),
            name="devorch-plan-" + project_id,
            daemon=True,
        )
        with self._lock:
            self._threads[plan_id] = thread
        thread.start()
        return plan_id, "planning started"

    def start(self, project: dict[str, Any], snapshot: dict[str, Any], command_id: str) -> tuple[str | None, str]:
        if self.port is None:
            return None, "AIBroker execution port unavailable"
        policy, error = _planner_policy(project)
        if policy is None:
            return None, error
        project_id = _nonblank(project.get("project_id"))
        repo_text = _nonblank(project.get("repo_path"))
        telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
        task_id = _nonblank(telemetry.get("task_id"))
        if project_id is None or repo_text is None or task_id is None:
            return None, "planner project identity incomplete"
        if "PENDING DESIGN" not in str(snapshot.get("next_status") or "").upper():
            return None, "current task is not PENDING DESIGN"
        truth = read_repository_truth(repo_text)
        if not truth.valid or truth.dirty:
            return None, "planner requires a clean repository"
        next_path = Path(repo_text) / "agent" / "next.md"
        try:
            next_text = next_path.read_text(encoding="utf-8")
        except OSError:
            return None, "agent/next.md unavailable"
        return self._begin_lifecycle(
            project, policy, command_id, task_id, truth, {"next_text": next_text}
        )

    def start_deferred(
        self,
        project: dict[str, Any],
        snapshot: dict[str, Any],
        command_id: str,
        handoff: dict[str, Any],
    ) -> tuple[str | None, str]:
        if self.port is None:
            return None, "AIBroker execution port unavailable"
        policy, error = _planner_policy(project)
        if policy is None:
            return None, error
        project_id = _nonblank(project.get("project_id"))
        repo_text = _nonblank(project.get("repo_path"))
        if project_id is None or repo_text is None:
            return None, "planner project identity incomplete"

        staged_successor = _nonblank(handoff.get("staged_successor"))
        staged_spec_path = _nonblank(handoff.get("staged_spec_path"))
        staged_spec_sha256 = _nonblank(handoff.get("staged_spec_sha256"))
        reviewed_branch = _nonblank(handoff.get("reviewed_branch"))
        reviewed_head = _nonblank(handoff.get("reviewed_head"))
        handoff_task_id = _nonblank(handoff.get("task_id"))
        if (
            staged_successor is None
            or staged_spec_path is None
            or staged_spec_sha256 is None
            or reviewed_branch is None
            or reviewed_head is None
            or handoff_task_id is None
        ):
            return None, "staged handoff metadata incomplete"

        repo = Path(repo_text)
        truth = read_repository_truth(repo)
        if not truth.valid or truth.dirty:
            return None, "planner requires a clean repository"
        if truth.branch != reviewed_branch:
            return None, "repository moved since review"

        predecessor_bytes = read_raw(repo, "agent/next.md")
        if predecessor_bytes is None:
            return None, "agent/next.md unavailable"

        rm_res = read_successor(repo, handoff_task_id)
        if rm_res.kind != "successor":
            if rm_res.kind == "invalid":
                return None, f"staged roadmap invalid: {rm_res.reason}"
            return None, f"staged roadmap invalid: expected successor, got {rm_res.kind}"
        if (
            rm_res.successor_task_id != staged_successor
            or rm_res.spec_path != staged_spec_path
            or rm_res.spec_sha256 != staged_spec_sha256
        ):
            return None, "staged spec changed since handoff"
        successor_bytes = rm_res.spec_text.encode("utf-8")

        # If a crash landed after the activation commit but before the planner
        # state write, the staged successor is already the current task.  Do
        # not reject it as a predecessor mismatch or create another commit.
        if predecessor_bytes == successor_bytes:
            return self._begin_lifecycle(
                project, policy, command_id, staged_successor, truth,
                {
                    "predecessor_task_id": handoff_task_id,
                    "predecessor_next_sha256": sha256_bytes(predecessor_bytes),
                    "predecessor_settled_at": utc_now_iso(),
                    "activation_commit": truth.head,
                    "activation_recovered_at": utc_now_iso(),
                    "staged_spec_path": staged_spec_path,
                    "staged_spec_sha256": staged_spec_sha256,
                    "staged_spec_text": rm_res.spec_text,
                    "next_text": rm_res.spec_text,
                },
            )
        if truth.head != reviewed_head:
            return None, "repository moved since review"

        # A deferred handoff used to leave agent/next.md on the predecessor
        # until the successor's plan was approved.  That made the monitor,
        # restart recovery and reconcile all rediscover the completed task
        # while the planner was already working on its successor.  Activate
        # the immutable staged task before creating its planner lifecycle.
        # This is a lifecycle transition (with a durable commit), not a UI
        # projection or an ad-hoc edit of agent/next.md.
        try:
            activation_head = self._activate_staged_successor(
                repo, predecessor_bytes, successor_bytes,
                handoff_task_id, staged_successor,
            )
        except Exception as exc:
            return None, "staged successor activation failed: " + str(exc)
        activated_truth = read_repository_truth(repo)
        if (
            not activated_truth.valid or activated_truth.dirty
            or activated_truth.branch != reviewed_branch
            or activated_truth.head != activation_head
        ):
            return None, "staged successor activation did not produce clean repository truth"

        base_fields = {
            "predecessor_task_id": handoff_task_id,
            "predecessor_next_sha256": sha256_bytes(predecessor_bytes),
            "predecessor_settled_at": utc_now_iso(),
            "activation_commit": activation_head,
            "staged_spec_path": staged_spec_path,
            "staged_spec_sha256": staged_spec_sha256,
            "staged_spec_text": rm_res.spec_text,
        }
        return self._begin_lifecycle(
            project, policy, command_id, staged_successor, activated_truth,
            {**base_fields, "next_text": rm_res.spec_text},
        )

    def reconcile_deferred_activation(
        self, project: dict[str, Any], handoff: dict[str, Any], command_id: str,
    ) -> str:
        """Migrate a restart-recovered legacy deferred handoff once, safely.

        Older handoffs recorded a P13 planner while leaving P12.7 in
        agent/next.md.  At a restart-recovered or already terminal P13 plan,
        make the same durable activation that new handoffs perform and
        re-anchor the P13 record.  This deliberately never resumes an active
        planner or replays a P12.7 role.
        """
        plan_id = "ai_plan:" + command_id
        with self._lock:
            record = copy.deepcopy(self._load_state()["plans"].get(plan_id))
        if not isinstance(record, dict):
            return "legacy deferred handoff plan is unavailable"
        if record.get("deferred") is not True:
            return ""
        record_state = str(record.get("state") or "")
        if record_state not in {"recovery_required", "failed"}:
            return "legacy deferred handoff requires a failed or restart-recovery planner boundary"
        repo_text = _nonblank(project.get("repo_path"))
        successor = _nonblank(handoff.get("staged_successor"))
        predecessor = _nonblank(handoff.get("task_id"))
        spec_path = _nonblank(handoff.get("staged_spec_path"))
        spec_sha = _nonblank(handoff.get("staged_spec_sha256"))
        if (
            repo_text is None or successor is None or predecessor is None
            or spec_path is None or spec_sha is None or record.get("task_id") != successor
        ):
            return "legacy deferred handoff metadata is incomplete"
        repo = Path(repo_text)
        predecessor_bytes = read_raw(repo, "agent/next.md")
        successor_bytes = read_raw(repo, spec_path)
        if predecessor_bytes is None or successor_bytes is None or sha256_bytes(successor_bytes) != spec_sha:
            return "legacy deferred staged spec changed or is unavailable"
        try:
            activation_head = self._activate_staged_successor(
                repo, predecessor_bytes, successor_bytes, predecessor, successor,
            )
        except Exception as exc:
            return "legacy deferred successor activation failed: " + str(exc)
        truth = read_repository_truth(repo)
        if not truth.valid or truth.dirty or truth.head != activation_head:
            return "legacy deferred successor activation did not produce clean repository truth"
        try:
            successor_text = successor_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return "legacy deferred staged spec is not UTF-8"
        with self._lock:
            state = self._load_state()
            current = state["plans"].get(plan_id)
            if not isinstance(current, dict) or current.get("state") != record_state:
                return "legacy deferred handoff changed during reconciliation"
            current.update({
                "deferred": False,
                "next_text": successor_text,
                "branch": truth.branch,
                "head": truth.head,
                "status_hash": truth.status_hash,
                "activation_commit": activation_head,
                "predecessor_settled_at": utc_now_iso(),
                "legacy_deferred_activation_reconciled_at": utc_now_iso(),
            })
            self._save_state(state)
        return ""

    @staticmethod
    def _activate_staged_successor(
        repo: Path,
        predecessor_bytes: bytes,
        successor_bytes: bytes,
        predecessor_task_id: str,
        successor_task_id: str,
    ) -> str:
        """Commit the reviewed staged successor as the one current task.

        The caller has already verified the roadmap and repository identity.
        Recheck the exact predecessor bytes here because this is the write
        boundary.  On failure before a commit, restore the worktree and index
        to their observed clean contents without resetting unrelated paths.
        """
        next_path = repo / "agent" / "next.md"
        current = read_raw(repo, "agent/next.md")
        if current != predecessor_bytes:
            raise RuntimeError("agent/next.md changed before successor activation")
        if successor_bytes == predecessor_bytes:
            raise RuntimeError("staged successor does not advance agent/next.md")
        truth = read_repository_truth(repo)
        if not truth.valid or truth.dirty:
            raise RuntimeError("repository changed before successor activation")
        try:
            next_path.write_bytes(successor_bytes)
            add = subprocess.run(
                ["git", "-C", str(repo), "add", "--", "agent/next.md"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=20, check=False, **hidden_subprocess_kwargs(),
            )
            if add.returncode != 0:
                raise RuntimeError("git add failed: " + (add.stderr or add.stdout).strip())
            commit = subprocess.run(
                [
                    "git", "-C", str(repo), "commit", "-m",
                    f"lifecycle({predecessor_task_id}): activate staged successor {successor_task_id}",
                    "--", "agent/next.md",
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, check=False, **hidden_subprocess_kwargs(),
            )
            if commit.returncode != 0:
                raise RuntimeError("git commit failed: " + (commit.stderr or commit.stdout).strip())
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=15, check=False,
                **hidden_subprocess_kwargs(),
            )
            if head.returncode != 0 or not head.stdout.strip():
                raise RuntimeError("cannot read successor activation HEAD")
            return head.stdout.strip()
        except Exception:
            post = read_repository_truth(repo)
            if post.valid and post.head == truth.head:
                try:
                    next_path.write_bytes(predecessor_bytes)
                    subprocess.run(
                        ["git", "-C", str(repo), "add", "--", "agent/next.md"],
                        capture_output=True, timeout=15, **hidden_subprocess_kwargs(),
                    )
                except Exception:
                    pass
            raise

    def ready_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(row) for row in self._load_state()["plans"].values() if row.get("state") == "ready"]

    def resume_exhausted_technical_gate(
        self,
        project: dict[str, Any],
        snapshot: dict[str, Any],
        gate_id: str,
    ) -> tuple[bool, str]:
        """Resume only a legacy remediation-exhausted technical reject gate.

        Explicit reviewer OWNER_GATE decisions and hard-limit gates are never
        resumed here.
        """
        policy, error = _planner_policy(project)
        if policy is None:
            return False, error
        project_id = _nonblank(project.get("project_id"))
        repo_path = _nonblank(project.get("repo_path"))
        with self._lock:
            state = self._load_state()
            record = copy.deepcopy(state["plans"].get(gate_id))
        if not isinstance(record, dict) or record.get("state") != "owner_gate":
            return False, "owner gate is not pending"
        if record.get("project_id") != project_id or record.get("repo_path") != repo_path:
            return False, "owner gate project identity mismatch"
        next_path = Path(repo_path) / "agent" / "next.md"
        try:
            next_text = next_path.read_text(encoding="utf-8")
        except OSError:
            return False, "current agent/next.md is unavailable"
        if (
            "Status: **PENDING DESIGN**" not in next_text
            or str(record.get("task_id") or "") not in next_text[:1000]
        ):
            return False, "owner gate task does not match current pending-design task"
        reason = str(record.get("reason") or "")
        if not reason.startswith("plan remediation hit its bound after "):
            return False, "owner gate is not a recoverable legacy remediation-exhaustion gate"
        if record.get("review_decision") != "reject":
            return False, "owner gate does not originate from a technical reject"
        chain = record.get("rejection_chain")
        reject_count = len(chain) if isinstance(chain, list) else 0
        recovery_cycle = int(record.get("recovery_cycle") or 0)
        if reject_count >= policy["hard_total_review_rejects"]:
            return False, "plan hard reject limit already reached"
        if recovery_cycle >= policy["max_plan_recovery_cycles"]:
            return False, "plan recovery cycle limit already reached"
        truth = read_repository_truth(repo_path)
        if not truth.valid or truth.dirty or truth.branch != record.get("branch"):
            return False, "repository changed since recoverable plan gate opened"
        reanchored_from_head = None
        if truth.head != record.get("head") or truth.status_hash != record.get("status_hash"):
            original_head = _nonblank(record.get("head"))
            original_next = record.get("next_text")
            if (
                original_head is None
                or not isinstance(original_next, str)
                or original_next != next_text
            ):
                return False, "repository changed since recoverable plan gate opened"
            ancestry = subprocess.run(
                ["git", "-C", str(repo_path), "merge-base", "--is-ancestor", original_head, truth.head],
                capture_output=True, timeout=15, **hidden_subprocess_kwargs(),
            )
            if ancestry.returncode != 0:
                return False, "repository moved to a non-descendant since recoverable plan gate opened"
            reanchored_from_head = original_head
        planner_payload = record.get("planner_resource")
        excluded = []
        if isinstance(planner_payload, dict) and _nonblank(planner_payload.get("resource_id")):
            excluded = [str(planner_payload["resource_id"])]
        next_cycle = recovery_cycle + 1
        with self._lock:
            state = self._load_state()
            current = state["plans"].get(gate_id)
            if not isinstance(current, dict) or current.get("state") != "owner_gate":
                return False, "owner gate is no longer pending"
            current.update({
                "state": "remediating",
                "reason": None,
                "branch": truth.branch,
                "head": truth.head,
                "status_hash": truth.status_hash,
                "next_text": next_text,
                "recovery_cycle": next_cycle,
                "remediation_round": 0,
                "recovery_rejection": current.get("review_reason"),
                "recovery_prior_plan": copy.deepcopy(current.get("plan")),
                "recovery_prior_resource": copy.deepcopy(current.get("planner_resource")),
                "recovery_excluded_planner_resource_ids": excluded,
                "recovery_started_at": utc_now_iso(),
                "legacy_exhausted_gate_resumed_at": utc_now_iso(),
                **(
                    {
                        "reanchored_from_head": reanchored_from_head,
                        "reanchored_at": utc_now_iso(),
                    }
                    if reanchored_from_head is not None
                    else {}
                ),
            })
            self._save_state(state)
        thread = threading.Thread(
            target=self._run_cycle,
            args=(gate_id, project, policy),
            name="devorch-plan-recovery-" + str(project_id or "project"),
            daemon=True,
        )
        with self._lock:
            self._threads[gate_id] = thread
        thread.start()
        return True, f"technical plan gate resumed in recovery cycle {next_cycle}"

    def approve_owner_gate(
        self,
        project: dict[str, Any],
        snapshot: dict[str, Any],
        gate_id: str,
        command_id: str,
        binding_adapter: str | None = None,
        binding_id: str | None = None,
        *,
        approval_channel: str = "conversation",
        approving_device_id: str | None = None,
    ) -> tuple[bool, str]:
        """Approve one exact pending planner gate without applying or launching it."""
        project_id = _nonblank(project.get("project_id"))
        repo_path = _nonblank(project.get("repo_path"))
        telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
        task_id = _nonblank(telemetry.get("task_id"))
        if project_id is None or repo_path is None or task_id is None:
            return False, "owner-gate project identity incomplete"

        if approval_channel == "mobile_device":
            if not approving_device_id or not isinstance(approving_device_id, str) or not approving_device_id.strip():
                return False, "approving_device_id is required for mobile approval"
        elif approval_channel == "conversation":
            if not binding_adapter or not binding_id:
                return False, "conversation approval requires binding_adapter and binding_id"
        else:
            return False, f"unsupported approval channel: {approval_channel!r}"

        with self._lock:
            state = self._load_state()
            record = state["plans"].get(gate_id)
            if not isinstance(record, dict) or record.get("state") != "owner_gate":
                return False, "owner gate is not pending"
            if record.get("project_id") != project_id:
                return False, "owner gate belongs to a different project"
            if record.get("task_id") != task_id:
                return False, "owner gate task does not match current task"
            if record.get("repo_path") != repo_path:
                return False, "owner gate repository does not match current project"
            if approval_channel == "conversation":
                gate_binding = record.get("conversation_binding")
                if (
                    not isinstance(gate_binding, dict)
                    or gate_binding.get("adapter") != binding_adapter
                    or gate_binding.get("binding_id") != binding_id
                ):
                    return False, "owner gate does not belong to the exact bound conversation"
            plan = record.get("plan")
            if not isinstance(plan, dict):
                return False, "owner gate has no bounded plan to approve"

        truth = read_repository_truth(repo_path)
        if not truth.valid or truth.dirty:
            return False, "owner-gate approval requires a clean repository"
        if (
            truth.branch != record.get("branch")
            or truth.head != record.get("head")
            or truth.status_hash != record.get("status_hash")
        ):
            return False, "repository changed since owner gate was opened"

        with self._lock:
            state = self._load_state()
            current = state["plans"].get(gate_id)
            if not isinstance(current, dict) or current.get("state") != "owner_gate":
                return False, "owner gate is not pending"
            current.update({
                "state": "owner_approved",
                "approval_command_id": command_id,
                "approved_at": utc_now_iso(),
                "approved_via": approval_channel,
                "approving_device_id": approving_device_id.strip() if approval_channel == "mobile_device" and approving_device_id else None,
            })
            self._save_state(state)
        return True, "exact pending owner gate approved; explicit continue is required"

    def continue_owner_approved(
        self,
        project: dict[str, Any],
        snapshot: dict[str, Any],
        command_id: str,
    ) -> tuple[bool, str | None, str]:
        """Apply an owner-approved plan only in response to an explicit continue."""
        project_id = _nonblank(project.get("project_id"))
        telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
        task_id = _nonblank(telemetry.get("task_id"))
        with self._lock:
            matches = [
                copy.deepcopy(row) for row in self._load_state()["plans"].values()
                if row.get("state") == "owner_approved" and row.get("project_id") == project_id
            ]
        if not matches:
            return False, None, "no owner-approved plan is pending"
        if len(matches) != 1:
            return True, None, "multiple owner-approved plans require reconciliation"
        record = matches[0]
        plan_id = str(record.get("plan_id") or "")
        if record.get("task_id") != task_id:
            return True, None, "owner-approved plan task does not match current task"
        plan = record.get("plan")
        if not plan_id or not isinstance(plan, dict):
            return True, None, "owner-approved plan is incomplete"
        try:
            _parse_plan(json.dumps(plan), str(task_id))
        except ValueError as exc:
            return True, None, f"owner-approved plan is invalid: {exc}"
        with self._lock:
            state = self._load_state()
            current = state["plans"].get(plan_id)
            if not isinstance(current, dict) or current.get("state") != "owner_approved":
                return True, None, "owner-approved plan is no longer pending"
            current["continuation_command_id"] = command_id
            current["continuation_requested_at"] = utc_now_iso()
            self._save_state(state)
        self._apply_plan(
            plan_id,
            record,
            plan,
            "explicit owner approval after bounded plan review",
            project=project,
        )
        final = self.state()["plans"].get(plan_id, {})
        if final.get("state") != "ready":
            return True, None, str(final.get("reason") or "owner-approved plan apply failed")
        return True, plan_id, "owner-approved plan applied; Worker launch remains daemon-owned"

    def continue_failed_plan_review(
        self,
        project: dict[str, Any],
        snapshot: dict[str, Any],
        command_id: str,
    ) -> tuple[bool, str | None, str]:
        """Retry only a resource-failed plan review without rerunning the accepted planner output."""
        if self.port is None:
            return False, None, "AIBroker execution port unavailable"
        policy, error = _planner_policy(project)
        if policy is None:
            return False, None, error
        project_id = _nonblank(project.get("project_id"))
        repo_text = _nonblank(project.get("repo_path"))
        telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
        task_id = _nonblank(telemetry.get("task_id"))
        if project_id is None or repo_text is None or task_id is None:
            return False, None, "planner project identity incomplete"
        with self._lock:
            candidates = [
                copy.deepcopy(row) for row in self._load_state()["plans"].values()
                if isinstance(row, dict)
                and row.get("project_id") == project_id
                and row.get("task_id") == task_id
                and row.get("state") == "failed"
                and isinstance(row.get("plan"), dict)
                and isinstance(row.get("planner_resource"), dict)
                and str(row.get("reason") or "").startswith("plan review failed:")
            ]
        if not candidates:
            return False, None, "no failed plan review can be resumed"
        candidates.sort(key=lambda row: str(row.get("completed_at") or row.get("started_at") or ""), reverse=True)
        source = candidates[0]
        plan = source.get("plan")
        try:
            _parse_plan(json.dumps(plan), task_id)
        except ValueError as exc:
            return True, None, f"failed plan review contains an invalid plan: {exc}"
        truth = read_repository_truth(repo_text)
        if not truth.valid or truth.dirty:
            return True, None, "plan review retry requires a clean repository"
        if truth.branch != source.get("branch"):
            return True, None, "repository branch changed since the failed plan review"
        recovery_count = int(source.get("plan_review_recovery_count") or 0)
        if recovery_count >= int(policy["max_attempts"]):
            return True, None, f"failed plan review retry limit reached ({policy['max_attempts']})"
        next_path = Path(repo_text) / "agent" / "next.md"
        try:
            next_text = next_path.read_text(encoding="utf-8")
        except OSError:
            return True, None, "agent/next.md unavailable"
        if next_text != str(source.get("next_text") or ""):
            return True, None, "current task specification changed since the failed plan review"
        resource = source.get("planner_resource") or {}
        if not all(_nonblank(resource.get(name)) for name in ("resource_id", "provider", "account", "model")):
            return True, None, "failed plan review planner resource evidence is incomplete"
        base_fields = {
            "next_text": next_text,
            "seed_plan": copy.deepcopy(plan),
            "seed_planner_resource": copy.deepcopy(resource),
            "seed_planner_dispatch_id": source.get("planner_dispatch_id"),
            "seed_planner_execution_id": source.get("planner_execution_id"),
            "seed_planner_completed_at": source.get("planner_completed_at"),
            "reused_plan_from": source.get("plan_id"),
            "reused_plan_reason": source.get("reason"),
            "reused_plan_source_head": source.get("head"),
            "reused_plan_reanchored_head": truth.head,
            "plan_review_recovery_count": recovery_count + 1,
        }
        plan_id, reason = self._begin_lifecycle(
            project, policy, command_id, task_id, truth, base_fields,
        )
        if plan_id is None:
            return True, None, reason
        return True, plan_id, "failed plan review retry started from existing planner output"

    def mark_worker_launched(self, plan_id: str, source_request_id: str) -> None:
        with self._lock:
            state = self._load_state()
            record = state["plans"].get(plan_id)
            if isinstance(record, dict) and record.get("state") == "ready":
                record["state"] = "worker_launched"
                record["worker_source_request_id"] = source_request_id
                record["worker_launched_at"] = utc_now_iso()
                self._save_state(state)
    def _project_payload(self, record: dict[str, Any], project: dict[str, Any] | None) -> dict[str, Any]:
        binding = (
            record.get("conversation_binding")
            or (project.get("conversation_binding") if isinstance(project, dict) else None)
            or self._project_bindings.get(record["project_id"])
        )
        payload: dict[str, Any] = {"project_id": record["project_id"]}
        if binding:
            payload["conversation_binding"] = binding
        if isinstance(project, dict):
            if project.get("progress_channel") is not None:
                payload["progress_channel"] = project["progress_channel"]
            if project.get("progress_level") is not None:
                payload["progress_level"] = project["progress_level"]
        return payload

    def _run_planner_attempts(
        self,
        plan_id: str,
        record: dict[str, Any],
        policy: dict[str, Any],
        round_no: int,
        rejection: str | None = None,
        prior_plan: dict[str, Any] | None = None,
        prior_resource: ResourceContext | None = None,
        *,
        recovery_cycle: int = 0,
        initial_excluded_resource_ids: tuple[str, ...] = (),
        rejection_chain: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], Any] | None:
        max_attempts = policy["max_attempts"]
        # Resource availability failures do not consume the planner's semantic/protocol
        # retry budget. Keep a separate bounded failover allowance so a quota failure
        # on the last semantic attempt can still move to another eligible resource.
        max_resource_failovers = 2
        max_format_repairs = 1
        max_dispatches = max_attempts + max_resource_failovers + max_format_repairs
        semantic_failures = 0
        format_repairs = 0
        failed_resource_ids: set[str] = set()
        recovery_excluded_resource_ids = set(initial_excluded_resource_ids)
        planner_result = None
        plan = None
        previous_attempt_resource = prior_resource if (round_no > 0 or recovery_cycle > 0) else None
        failure_reason = None
        remediation_data = None
        if round_no > 0 or recovery_cycle > 0:
            remediation_data = {
                "round": round_no,
                "recovery_cycle": recovery_cycle,
                "rejection": rejection,
                "prior_plan": prior_plan,
                "prior_resource": prior_resource,
                "rejection_chain": copy.deepcopy(rejection_chain or []),
            }
        if recovery_cycle > 0:
            base_req_id = (
                f"{plan_id}:planner:recovery-{recovery_cycle}"
                if round_no == 0
                else f"{plan_id}:planner:recovery-{recovery_cycle}:remediate-{round_no}"
            )
        else:
            base_req_id = plan_id + ":planner" if round_no == 0 else f"{plan_id}:planner:remediate-{round_no}"

        for attempt in range(1, max_dispatches + 1):
            if not self._wait_for_launch_barrier(plan_id, record["project_id"]):
                return None
            retry_suffix = "" if attempt == 1 else f":retry-{attempt - 1}"
            metadata: dict[str, Any] = {
                "control_command_id": record["command_id"],
                "planner_attempt": attempt,
                "planner_max_attempts": max_attempts,
                "planner_semantic_failures": semantic_failures,
                "planner_format_repairs": format_repairs,
            }
            if failed_resource_ids:
                metadata["planner_failover_from_resource_ids"] = sorted(failed_resource_ids)
            if recovery_excluded_resource_ids:
                metadata["planner_recovery_excluded_resource_ids"] = sorted(recovery_excluded_resource_ids)
            if round_no > 0:
                metadata["remediation_round"] = round_no
            if recovery_cycle > 0:
                metadata["planner_recovery_cycle"] = recovery_cycle
            if recovery_cycle > 0:
                metadata["planner_recovery_cycle"] = recovery_cycle

            planner_request = AIRoleRequest(
                project_id=record["project_id"],
                task_run_id=record["task_id"],
                stage_run_id="plan",
                role_run_id="planner-" + record["command_id"],
                request_id=base_req_id + retry_suffix,
                role="planner",
                prompt=self._planner_prompt(
                    record,
                    retry_reason=failure_reason,
                    attempt=attempt,
                    remediation=remediation_data,
                ),
                working_directory=Path(record["repo_path"]),
                quality=policy["quality"],
                independence="none",
                previous_resource_context=previous_attempt_resource,
                excluded_resource_ids=tuple(sorted(failed_resource_ids | recovery_excluded_resource_ids)),
                timeout_seconds=policy["timeout_seconds"],
                metadata=metadata,
            )
            attempt_result = None
            attempt_reason = None
            attempt_started = time.monotonic()
            accounting_phase = (
                "retry" if attempt > 1
                else ("plan_remediation" if round_no > 0 else "planning")
            )
            if self.accounting is not None:
                self.accounting.start_interval(
                    accounting_phase,
                    planner_request.request_id,
                    project_id=record["project_id"],
                    task_id=record["task_id"],
                    role="planner",
                    request_id=planner_request.request_id,
                    stage_run_id=planner_request.stage_run_id,
                    role_run_id=planner_request.role_run_id,
                    source_request_id=record["command_id"],
                    attempt_id=f"{plan_id}:plan-round-{round_no}",
                )
            try:
                attempt_result = self.port.execute(planner_request) if self.port is not None else None
                if attempt_result is None:
                    raise RuntimeError("planner execution port unavailable")
                if attempt_result.status == "cancelled":
                    raise InterruptedError(attempt_result.error or "planner_cancelled")
                if attempt_result.status != "succeeded":
                    attempt_reason = attempt_result.error or f"planner_{attempt_result.status}"
                    classification = getattr(attempt_result, "failure_classification", None)
                    resource = attempt_result.resource_context
                    can_failover = (
                        attempt_result.status == "failed"
                        and classification in _ROLE_RESOURCE_FAILURES
                        and resource is not None
                        and resource.resource_id is not None
                    )
                    if can_failover:
                        self._record_planner_attempt(
                            plan_id, attempt, planner_request, attempt_result, attempt_reason,
                            classification=classification, round_no=round_no,
                        )
                        failed_resource_ids.add(resource.resource_id)
                        previous_attempt_resource = resource
                        failure_reason = attempt_reason
                        if len(failed_resource_ids) <= max_resource_failovers and attempt < max_dispatches:
                            continue
                        self._finish(
                            plan_id, "failed",
                            "planner resource failover limit reached: " + attempt_reason,
                        )
                        return None
                    raise RuntimeError(attempt_reason)
                # Preserve the exact provider output before parsing. Protocol failures
                # must be diagnosable without reproducing an expensive model call.
                raw_output = attempt_result.output if isinstance(attempt_result.output, str) else ""
                with self._lock:
                    state = self._load_state()
                    current = state["plans"].get(plan_id)
                    if isinstance(current, dict):
                        captures = current.setdefault("planner_raw_outputs", [])
                        captures.append({
                            "round": round_no,
                            "attempt": attempt,
                            "request_id": planner_request.request_id,
                            "sha256": hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
                            "output": raw_output,
                            "captured_at": utc_now_iso(),
                        })
                        if len(captures) > 12:
                            del captures[:-12]
                        self._save_state(state)
                plan = _parse_plan(raw_output, record["task_id"])
                if attempt_result.resource_context is None:
                    raise RuntimeError("planner resource context missing")
                planner_result = attempt_result
            except InterruptedError as exc:
                attempt_reason = str(exc)
                self._record_planner_attempt(plan_id, attempt, planner_request, attempt_result, str(exc), round_no=round_no)
                self._finish(plan_id, "failed", f"planner cancelled: {exc}")
                return None
            except Exception as exc:
                attempt_reason = str(exc)
                protocol_stage = exc.stage if isinstance(exc, PlannerProtocolError) else None
                classification = getattr(attempt_result, "failure_classification", None)
                if protocol_stage is not None:
                    classification = f"planner_{protocol_stage}_error"
                if protocol_stage in {"extract", "schema"} and format_repairs < max_format_repairs:
                    format_repairs += 1
                    self._record_planner_attempt(
                        plan_id, attempt, planner_request, attempt_result, attempt_reason,
                        classification=f"planner_{protocol_stage}_repair", round_no=round_no,
                    )
                    if attempt_result is not None and attempt_result.resource_context is not None:
                        previous_attempt_resource = attempt_result.resource_context
                    failure_reason = (
                        f"{protocol_stage} format/schema repair required: {attempt_reason}"
                    )
                    continue
                self._record_planner_attempt(
                    plan_id, attempt, planner_request, attempt_result, attempt_reason,
                    classification=classification, round_no=round_no,
                )
                if attempt_result is not None and attempt_result.resource_context is not None:
                    previous_attempt_resource = attempt_result.resource_context
                failure_reason = attempt_reason
                semantic_failures += 1
                if semantic_failures >= max_attempts:
                    self._finish(
                        plan_id,
                        "failed",
                        f"planner failed after {max_attempts} attempts: {attempt_reason}",
                    )
                    return None
                continue
            finally:
                if self.accounting is not None:
                    self.accounting.end_interval(
                        accounting_phase,
                        planner_request.request_id,
                        outcome=(
                            "accepted"
                            if attempt_result is not None
                            and attempt_result.status == "succeeded"
                            and attempt_reason is None
                            else "failed"
                        ),
                        project_id=record["project_id"],
                        task_id=record["task_id"],
                        role="planner",
                        request_id=planner_request.request_id,
                        stage_run_id=planner_request.stage_run_id,
                        role_run_id=planner_request.role_run_id,
                        source_request_id=record["command_id"],
                        attempt_id=f"{plan_id}:recovery-{recovery_cycle}:plan-round-{round_no}",
                        dispatch_id=getattr(attempt_result, "dispatch_id", None),
                        decision_id=getattr(attempt_result, "decision_id", None),
                        execution_id=getattr(attempt_result, "execution_id", None),
                        session_id=getattr(attempt_result, "session_id", None),
                        resource_id=(
                            attempt_result.resource_context.resource_id
                            if attempt_result is not None and attempt_result.resource_context else None
                        ),
                        provider=(
                            attempt_result.resource_context.provider
                            if attempt_result is not None and attempt_result.resource_context else None
                        ),
                        account=(
                            attempt_result.resource_context.account
                            if attempt_result is not None and attempt_result.resource_context else None
                        ),
                        model=(
                            attempt_result.resource_context.model
                            if attempt_result is not None and attempt_result.resource_context else None
                        ),
                    )
                if self.failure_memory is not None and attempt_reason:
                    self.failure_memory.record_matching_recurrences(
                        record.get("failure_environment", {}),
                        attempt_reason,
                        time.monotonic() - attempt_started,
                        project_id=record["project_id"],
                        task_id=record["task_id"],
                        role="planner",
                        request_id=planner_request.request_id,
                        source_request_id=record["command_id"],
                    )
            self._record_planner_attempt(plan_id, attempt, planner_request, planner_result, None, round_no=round_no)
            break

        if planner_result is None or plan is None:
            self._finish(plan_id, "failed", "planner retry loop ended without a valid plan")
            return None
        return plan, planner_result

    def _run_plan_review(
        self,
        plan_id: str,
        record: dict[str, Any],
        policy: dict[str, Any],
        plan: dict[str, Any],
        previous: ResourceContext | None,
        round_no: int,
        recovery_cycle: int = 0,
    ) -> tuple[str, str, Any] | None:
        if recovery_cycle > 0:
            base_request_id = (
                f"{plan_id}:reviewer:recovery-{recovery_cycle}"
                if round_no == 0
                else f"{plan_id}:reviewer:recovery-{recovery_cycle}:remediate-{round_no}"
            )
        else:
            base_request_id = plan_id + ":reviewer" if round_no == 0 else f"{plan_id}:reviewer:remediate-{round_no}"
        failed_resource_ids: set[str] = set()
        max_attempts = 1 + policy["max_reviewer_resource_failovers"]

        for attempt in range(1, max_attempts + 1):
            if not self._wait_for_launch_barrier(plan_id, record["project_id"]):
                return None
            request_id = base_request_id if attempt == 1 else f"{base_request_id}:failover-{attempt - 1}"
            metadata: dict[str, Any] = {
                "control_command_id": record["command_id"],
                "review_kind": "plan",
                "reviewer_attempt": attempt,
                "reviewer_max_attempts": max_attempts,
            }
            if failed_resource_ids:
                metadata["reviewer_failover_from_resource_ids"] = sorted(failed_resource_ids)
            if round_no > 0:
                metadata["remediation_round"] = round_no
            review_request = AIRoleRequest(
                project_id=record["project_id"],
                task_run_id=record["task_id"],
                stage_run_id="plan_review",
                role_run_id="plan-reviewer-" + record["command_id"],
                request_id=request_id,
                role="reviewer",
                prompt=self._review_prompt(record, plan),
                working_directory=Path(record["repo_path"]),
                quality=policy["review_quality"],
                independence=policy["review_independence"],
                previous_resource_context=previous,
                excluded_resource_ids=tuple(sorted(failed_resource_ids)),
                timeout_seconds=policy["review_timeout_seconds"],
                metadata=metadata,
            )
            review_result = None
            review_outcome = "failed"
            review_failure = None
            review_started = time.monotonic()
            attempt_id = f"{plan_id}:recovery-{recovery_cycle}:plan-round-{round_no}:reviewer-attempt-{attempt}"
            if self.accounting is not None:
                self.accounting.start_interval(
                    "plan_review", review_request.request_id,
                    project_id=record["project_id"], task_id=record["task_id"],
                    role="plan_reviewer", request_id=review_request.request_id,
                    stage_run_id=review_request.stage_run_id, role_run_id=review_request.role_run_id,
                    source_request_id=record["command_id"], attempt_id=attempt_id,
                )
            try:
                review_result = self.port.execute(review_request) if self.port is not None else None
                if review_result is None:
                    raise RuntimeError("plan reviewer execution port unavailable")
                if review_result.status != "succeeded":
                    review_failure = review_result.error or f"plan_reviewer_{review_result.status}"
                    classification = getattr(review_result, "failure_classification", None)
                    resource = review_result.resource_context
                    can_failover = (
                        review_result.status == "failed"
                        and classification in _ROLE_RESOURCE_FAILURES
                        and resource is not None
                        and resource.resource_id is not None
                    )
                    self._record_reviewer_attempt(
                        plan_id, attempt, review_request, review_result, review_failure,
                        classification=classification, round_no=round_no,
                    )
                    if can_failover and attempt < max_attempts:
                        failed_resource_ids.add(resource.resource_id)
                        continue
                    if can_failover:
                        self._finish(
                            plan_id, "failed",
                            "plan review failed: all eligible reviewer resources exhausted/unavailable: " + review_failure,
                        )
                    else:
                        self._finish(plan_id, "failed", f"plan review failed: {review_failure}")
                    return None
                raw_output = review_result.output if isinstance(review_result.output, str) else ""
                self._capture_plan_reviewer_output(plan_id, review_request, review_result)
                try:
                    decision, reason = _parse_plan_review(raw_output)
                except StructuredOutputError as protocol_exc:
                    if protocol_exc.stage not in {"extract", "schema"}:
                        raise
                    repair_request = AIRoleRequest(
                        project_id=review_request.project_id,
                        task_run_id=review_request.task_run_id,
                        stage_run_id=review_request.stage_run_id,
                        role_run_id=review_request.role_run_id + "-protocol-repair",
                        request_id=review_request.request_id + ":protocol-repair-1",
                        role=review_request.role,
                        prompt=self._plan_review_protocol_repair_prompt(raw_output, str(protocol_exc)),
                        working_directory=review_request.working_directory,
                        quality=review_request.quality,
                        independence=review_request.independence,
                        previous_resource_context=review_request.previous_resource_context,
                        excluded_resource_ids=review_request.excluded_resource_ids,
                        timeout_seconds=review_request.timeout_seconds,
                        metadata={
                            **dict(review_request.metadata),
                            "protocol_repair": True,
                            "protocol_repair_index": 1,
                            "protocol_failure_stage": protocol_exc.stage,
                        },
                    )
                    repair_result = self.port.execute(repair_request) if self.port is not None else None
                    if repair_result is not None:
                        self._capture_plan_reviewer_output(
                            plan_id, repair_request, repair_result,
                            protocol_stage=protocol_exc.stage, repair_index=1,
                        )
                    if repair_result is None or repair_result.status != "succeeded":
                        repair_reason = getattr(repair_result, "error", None) or "plan reviewer protocol repair execution failed"
                        repair_resource = getattr(repair_result, "resource_context", None)
                        self._record_reviewer_attempt(
                            plan_id, attempt, review_request, review_result,
                            f"protocol repair failed: {repair_reason}",
                            classification="reviewer_protocol_repair_failed", round_no=round_no,
                        )
                        if attempt < max_attempts and repair_resource is not None and repair_resource.resource_id is not None:
                            failed_resource_ids.add(repair_resource.resource_id)
                            continue
                        self._finish(plan_id, "failed", f"plan review failed: protocol repair failed: {repair_reason}")
                        return None
                    repaired_raw = repair_result.output if isinstance(repair_result.output, str) else ""
                    try:
                        decision, reason = _parse_plan_review(repaired_raw)
                    except StructuredOutputError as repair_exc:
                        repair_resource = repair_result.resource_context
                        self._record_reviewer_attempt(
                            plan_id, attempt, review_request, review_result,
                            f"protocol repair invalid: {repair_exc}",
                            classification="reviewer_protocol_repair_invalid", round_no=round_no,
                        )
                        if attempt < max_attempts and repair_resource is not None and repair_resource.resource_id is not None:
                            failed_resource_ids.add(repair_resource.resource_id)
                            continue
                        self._finish(plan_id, "failed", f"plan review failed: protocol repair invalid: {repair_exc}")
                        return None
                    review_result = repair_result
                review_outcome = "accepted"
                self._record_reviewer_attempt(
                    plan_id, attempt, review_request, review_result, None,
                    classification=None, round_no=round_no,
                )
                return decision, reason, review_result
            except Exception as exc:
                review_failure = str(exc)
                reconciled = None
                if self.port is not None:
                    try:
                        reconciled = self.port.status(review_request.request_id)
                    except Exception:
                        reconciled = None
                classification = None
                if isinstance(reconciled, dict) and str(reconciled.get("status") or "") == "failed":
                    resource_id = _nonblank(reconciled.get("resource_id"))
                    provider = _nonblank(reconciled.get("provider"))
                    account = _nonblank(reconciled.get("account"))
                    model = _nonblank(reconciled.get("model"))
                    terminal_error = _nonblank(reconciled.get("execution_error")) or review_failure
                    classification = _classify_reconciled_dispatch_failure(terminal_error)
                    if resource_id is not None:
                        review_result = AIRoleResult(
                            request_id=review_request.request_id,
                            role_run_id=review_request.role_run_id,
                            status="failed",
                            error=terminal_error,
                            dispatch_id=_nonblank(reconciled.get("dispatch_id")),
                            decision_id=_nonblank(reconciled.get("decision_id")),
                            execution_id=_nonblank(reconciled.get("execution_id")),
                            session_id=_nonblank(reconciled.get("execution_session_id")),
                            resource_context=ResourceContext(resource_id, provider, account, model),
                            started_at=_nonblank(reconciled.get("started_at")),
                            finished_at=_nonblank(reconciled.get("finished_at")),
                            failure_classification=classification,
                        )
                        review_failure = terminal_error
                self._record_reviewer_attempt(
                    plan_id, attempt, review_request, review_result, review_failure,
                    classification=classification, round_no=round_no,
                )
                resource = getattr(review_result, "resource_context", None)
                can_failover = (
                    classification in _ROLE_RESOURCE_FAILURES
                    and resource is not None
                    and resource.resource_id is not None
                )
                if can_failover and attempt < max_attempts:
                    failed_resource_ids.add(resource.resource_id)
                    continue
                if can_failover:
                    self._finish(
                        plan_id, "failed",
                        "plan review failed: all eligible reviewer resources exhausted/unavailable: " + review_failure,
                    )
                else:
                    self._finish(plan_id, "failed", f"plan review failed: {review_failure}")
                return None
            finally:
                if self.accounting is not None:
                    resource = getattr(review_result, "resource_context", None)
                    self.accounting.end_interval(
                        "plan_review", review_request.request_id, outcome=review_outcome,
                        project_id=record["project_id"], task_id=record["task_id"],
                        role="plan_reviewer", request_id=review_request.request_id,
                        stage_run_id=review_request.stage_run_id, role_run_id=review_request.role_run_id,
                        source_request_id=record["command_id"], attempt_id=attempt_id,
                        dispatch_id=getattr(review_result, "dispatch_id", None),
                        decision_id=getattr(review_result, "decision_id", None),
                        execution_id=getattr(review_result, "execution_id", None),
                        session_id=getattr(review_result, "session_id", None),
                        resource_id=resource.resource_id if resource else None,
                        provider=resource.provider if resource else None,
                        account=resource.account if resource else None,
                        model=resource.model if resource else None,
                    )
                if self.failure_memory is not None and review_failure:
                    self.failure_memory.record_matching_recurrences(
                        record.get("failure_environment", {}), review_failure,
                        time.monotonic() - review_started,
                        project_id=record["project_id"], task_id=record["task_id"],
                        role="plan_reviewer", request_id=review_request.request_id,
                        source_request_id=record["command_id"],
                    )
        self._finish(plan_id, "failed", "plan review failed: reviewer retry loop ended unexpectedly")
        return None

    def _run_cycle(self, plan_id: str, project: dict[str, Any], policy: dict[str, Any]) -> None:
        with self._lock:
            record = copy.deepcopy(self._load_state()["plans"].get(plan_id, {}))
        if not record:
            return
        if self.progress_channel is not None:
            self.progress_channel.emit(
                self._project_payload(record, project), "PLAN_STARTED",
                task_id=record["task_id"], occurrence_key=plan_id,
                details={"plan_id": plan_id},
            )

        max_remediation_rounds = policy["max_plan_remediation_rounds"]
        max_recovery_cycles = policy["max_plan_recovery_cycles"]
        hard_total_review_rejects = policy["hard_total_review_rejects"]
        recovery_cycle = int(record.get("recovery_cycle") or 0)
        rejection = _nonblank(record.get("recovery_rejection"))
        prior_plan = copy.deepcopy(record.get("recovery_prior_plan")) if isinstance(record.get("recovery_prior_plan"), dict) else None
        prior_resource_payload = record.get("recovery_prior_resource")
        prior_resource = (
            ResourceContext(
                prior_resource_payload.get("resource_id"),
                prior_resource_payload.get("provider"),
                prior_resource_payload.get("account"),
                prior_resource_payload.get("model"),
            )
            if isinstance(prior_resource_payload, dict) else None
        )
        recovery_excluded_resource_ids = set(
            str(item) for item in (record.get("recovery_excluded_planner_resource_ids") or [])
            if _nonblank(item) is not None
        )

        while True:
            restart_recovery_cycle = False
            for round_no in range(max_remediation_rounds + 1):
                with self._lock:
                    current_state = self._load_state()["plans"].get(plan_id, {})
                    rejection_chain = copy.deepcopy(current_state.get("rejection_chain") or [])
                seeded = recovery_cycle == 0 and round_no == 0 and isinstance(record.get("seed_plan"), dict)
                if seeded:
                    plan = copy.deepcopy(record["seed_plan"])
                    resource = record.get("seed_planner_resource") or {}
                    previous = ResourceContext(
                        resource.get("resource_id"),
                        resource.get("provider"),
                        resource.get("account"),
                        resource.get("model"),
                    )
                    planner_result = SimpleNamespace(
                        resource_context=previous,
                        dispatch_id=record.get("seed_planner_dispatch_id"),
                        execution_id=record.get("seed_planner_execution_id"),
                    )
                    planner_completed_at = str(record.get("seed_planner_completed_at") or utc_now_iso())
                else:
                    attempts_res = self._run_planner_attempts(
                        plan_id, record, policy, round_no,
                        rejection=rejection,
                        prior_plan=prior_plan,
                        prior_resource=prior_resource,
                        recovery_cycle=recovery_cycle,
                        initial_excluded_resource_ids=tuple(sorted(recovery_excluded_resource_ids if round_no == 0 else set())),
                        rejection_chain=rejection_chain,
                    )
                    if attempts_res is None:
                        return
                    plan, planner_result = attempts_res
                    previous = planner_result.resource_context
                    if previous is None:
                        self._finish(plan_id, "failed", "planner resource context missing")
                        return
                    planner_completed_at = utc_now_iso()

                with self._lock:
                    state = self._load_state()
                    current = state["plans"].get(plan_id)
                    if not isinstance(current, dict):
                        return
                    current.update({
                        "state": "reviewing",
                        "plan": plan,
                        "planner_dispatch_id": planner_result.dispatch_id,
                        "planner_execution_id": planner_result.execution_id,
                        "planner_resource": self._resource_payload(previous),
                        "planner_completed_at": planner_completed_at,
                        "recovery_cycle": recovery_cycle,
                        **({"planner_reused_from": record.get("reused_plan_from")} if seeded else {}),
                    })
                    self._save_state(state)

                review_res = self._run_plan_review(
                    plan_id, record, policy, plan, previous, round_no, recovery_cycle,
                )
                if review_res is None:
                    return
                decision, reason, review_result = review_res
                review_completed_at = utc_now_iso()
                review_resource = review_result.resource_context

                with self._lock:
                    state = self._load_state()
                    current = state["plans"].get(plan_id)
                    if not isinstance(current, dict):
                        return
                    current.update({
                        "review_decision": decision,
                        "review_reason": reason,
                        "review_dispatch_id": review_result.dispatch_id,
                        "review_execution_id": review_result.execution_id,
                        "review_resource": self._resource_payload(review_resource),
                        "review_completed_at": review_completed_at,
                    })
                    self._save_state(state)

                if decision == "owner_gate":
                    self._finish(plan_id, "owner_gate", reason)
                    if self.accounting is not None:
                        self.accounting.open_owner_gate(
                            plan_id,
                            project_id=record["project_id"],
                            task_id=record["task_id"],
                            role="owner",
                            request_id=plan_id,
                            source_request_id=record["command_id"],
                        )
                    if self.progress_channel is not None:
                        self.progress_channel.emit(
                            self._project_payload(record, project), "OWNER_GATE",
                            task_id=record["task_id"], occurrence_key=plan_id,
                            details={"plan_id": plan_id, "reason": reason},
                        )
                    return

                if decision == "approve":
                    if self.accounting is not None:
                        self.accounting.record_attempt_outcome(
                            f"{plan_id}:recovery-{recovery_cycle}:plan-round-{round_no}",
                            "accepted",
                            project_id=record["project_id"],
                            task_id=record["task_id"],
                            role="plan_reviewer",
                            request_id=review_result.request_id,
                            metadata={
                                "review_kind": "plan",
                                "decision": decision,
                                "recovery_cycle": recovery_cycle,
                                "round": round_no,
                            },
                        )
                    self._apply_plan(plan_id, record, plan, reason, project=project)
                    return

                if decision != "reject":
                    self._finish(plan_id, "failed", f"unexpected plan review decision: {decision}")
                    return

                if self.accounting is not None:
                    self.accounting.record_attempt_outcome(
                        f"{plan_id}:recovery-{recovery_cycle}:plan-round-{round_no}",
                        "rejected",
                        project_id=record["project_id"],
                        task_id=record["task_id"],
                        role="plan_reviewer",
                        request_id=review_result.request_id,
                        metadata={
                            "review_kind": "plan",
                            "decision": decision,
                            "reason": reason,
                            "recovery_cycle": recovery_cycle,
                            "round": round_no,
                        },
                    )
                rejection_entry = {
                    "recovery_cycle": recovery_cycle,
                    "round": round_no,
                    "reason": reason,
                    "prior_plan": plan,
                    "planner_dispatch_id": planner_result.dispatch_id,
                    "planner_execution_id": planner_result.execution_id,
                    "reviewer_dispatch_id": review_result.dispatch_id,
                    "reviewer_execution_id": review_result.execution_id,
                    "planner_resource": self._resource_payload(previous),
                    "reviewer_resource": self._resource_payload(review_resource),
                    "planner_completed_at": planner_completed_at,
                    "review_completed_at": review_completed_at,
                    "rejected_at": utc_now_iso(),
                }
                with self._lock:
                    state = self._load_state()
                    current = state["plans"].get(plan_id)
                    if not isinstance(current, dict):
                        return
                    chain = current.setdefault("rejection_chain", [])
                    chain.append(rejection_entry)
                    chain_len = len(chain)
                    self._save_state(state)

                if chain_len >= hard_total_review_rejects:
                    exhaust_reason = (
                        f"plan review hard reject limit reached ({hard_total_review_rejects}); "
                        f"last rejection: {reason}"
                    )
                    self._finish(plan_id, "owner_gate", exhaust_reason)
                    if self.accounting is not None:
                        self.accounting.open_owner_gate(
                            plan_id,
                            project_id=record["project_id"],
                            task_id=record["task_id"],
                            role="owner",
                            request_id=plan_id,
                            source_request_id=record["command_id"],
                        )
                    if self.progress_channel is not None:
                        self.progress_channel.emit(
                            self._project_payload(record, project), "OWNER_GATE",
                            task_id=record["task_id"], occurrence_key=f"{plan_id}:owner_gate:hard-reject-limit",
                            details={
                                "plan_id": plan_id,
                                "reason": exhaust_reason,
                                "rejection_chain_length": chain_len,
                            },
                        )
                    return

                if round_no < max_remediation_rounds:
                    with self._lock:
                        state = self._load_state()
                        current = state["plans"].get(plan_id)
                        if not isinstance(current, dict):
                            return
                        current.update({
                            "state": "remediating",
                            "remediation_round": round_no + 1,
                            "recovery_cycle": recovery_cycle,
                            "review_reason": reason,
                            "remediation_started_at": utc_now_iso(),
                        })
                        self._save_state(state)
                    if self.progress_channel is not None:
                        self.progress_channel.emit(
                            self._project_payload(record, project),
                            "REMEDIATE",
                            task_id=record["task_id"],
                            occurrence_key=f"{plan_id}:recovery-{recovery_cycle}:remediate-{round_no + 1}",
                            details={
                                "plan_id": plan_id,
                                "round": round_no + 1,
                                "recovery_cycle": recovery_cycle,
                                "reason": reason,
                            },
                        )
                    rejection = reason
                    prior_plan = plan
                    prior_resource = previous
                    continue

                if recovery_cycle < max_recovery_cycles:
                    recovery_cycle += 1
                    rejection = reason
                    prior_plan = plan
                    prior_resource = previous
                    recovery_excluded_resource_ids = {
                        previous.resource_id
                    } if previous is not None and previous.resource_id else set()
                    with self._lock:
                        state = self._load_state()
                        current = state["plans"].get(plan_id)
                        if not isinstance(current, dict):
                            return
                        current.update({
                            "state": "remediating",
                            "recovery_cycle": recovery_cycle,
                            "remediation_round": 0,
                            "review_reason": reason,
                            "recovery_rejection": reason,
                            "recovery_prior_plan": copy.deepcopy(plan),
                            "recovery_prior_resource": self._resource_payload(previous),
                            "recovery_excluded_planner_resource_ids": sorted(recovery_excluded_resource_ids),
                            "recovery_started_at": utc_now_iso(),
                        })
                        self._save_state(state)
                    if self.progress_channel is not None:
                        self.progress_channel.emit(
                            self._project_payload(record, project),
                            "PLAN_RECOVERY",
                            task_id=record["task_id"],
                            occurrence_key=f"{plan_id}:recovery-{recovery_cycle}",
                            details={
                                "plan_id": plan_id,
                                "recovery_cycle": recovery_cycle,
                                "reason": reason,
                                "rejection_chain_length": chain_len,
                                "excluded_planner_resource_ids": sorted(recovery_excluded_resource_ids),
                            },
                        )
                    restart_recovery_cycle = True
                    break

                exhaust_reason = (
                    f"plan remediation hit its bound after {max_remediation_rounds} rounds (exhausted); "
                    f"last rejection: {reason}"
                    if max_recovery_cycles == 0
                    else (
                        f"plan recovery exhausted its bound after {max_recovery_cycles} recovery cycles; "
                        f"last rejection: {reason}"
                    )
                )
                self._finish(plan_id, "owner_gate", exhaust_reason)
                if self.accounting is not None:
                    self.accounting.open_owner_gate(
                        plan_id,
                        project_id=record["project_id"],
                        task_id=record["task_id"],
                        role="owner",
                        request_id=plan_id,
                        source_request_id=record["command_id"],
                    )
                if self.progress_channel is not None:
                    self.progress_channel.emit(
                        self._project_payload(record, project),
                        "OWNER_GATE",
                        task_id=record["task_id"], occurrence_key=f"{plan_id}:owner_gate:recovery-exhausted",
                        details={
                            "plan_id": plan_id,
                            "reason": exhaust_reason,
                            "rejection_chain_length": chain_len,
                        },
                    )
                return
            if restart_recovery_cycle:
                continue
            return

    def _apply_plan(
        self, plan_id: str, record: dict[str, Any], plan: dict[str, Any],
        review_reason: str, project: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            state = self._load_state()
            current = state["plans"].get(plan_id)
            if not isinstance(current, dict):
                return
            current["state"] = "applying"
            self._save_state(state)
        repo = Path(record["repo_path"])
        truth = read_repository_truth(repo)
        if (
            not truth.valid or truth.dirty
            or truth.branch != record["branch"] or truth.head != record["head"]
            or truth.status_hash != record["status_hash"]
        ):
            self._finish(plan_id, "failed", "repository changed during planning")
            return
        if record.get("deferred"):
            self._apply_deferred_plan(plan_id, record, plan, review_reason, project)
            return
        next_path = repo / "agent" / "next.md"
        try:
            original = next_path.read_text(encoding="utf-8")
        except OSError:
            self._finish(plan_id, "failed", "agent/next.md unavailable during plan apply")
            return
        if original != record["next_text"]:
            self._finish(plan_id, "failed", "agent/next.md changed during planning")
            return
        try:
            updated = self._render_next(original, plan, review_reason)
            next_path.write_text(updated, encoding="utf-8", newline="\n")
            commit = self._commit_plan(repo, record["task_id"])
        except Exception as exc:
            try:
                next_path.write_text(original, encoding="utf-8", newline="\n")
                subprocess.run(["git", "-C", str(repo), "reset", "--", "agent/next.md"], capture_output=True, timeout=15, **hidden_subprocess_kwargs())
            except Exception:
                pass
            self._finish(plan_id, "failed", f"plan apply failed: {exc}")
            return
        self._record_ready_and_emit(plan_id, record, repo, commit, review_reason, project)

    def _apply_deferred_plan(
        self, plan_id: str, record: dict[str, Any], plan: dict[str, Any],
        review_reason: str, project: Optional[dict[str, Any]] = None,
    ) -> None:
        repo = Path(record["repo_path"])
        cur_next = read_raw(repo, "agent/next.md")
        if cur_next is None:
            self._finish(plan_id, "failed", "agent/next.md unavailable during plan apply")
            return
        if sha256_bytes(cur_next) != record.get("predecessor_next_sha256"):
            self._finish(plan_id, "failed", "agent/next.md changed during planning")
            return
        staged_spec_path = str(record.get("staged_spec_path") or "")
        cur_spec = read_raw(repo, staged_spec_path)
        if cur_spec is None or sha256_bytes(cur_spec) != record.get("staged_spec_sha256"):
            self._finish(plan_id, "failed", "staged spec changed during planning")
            return
        try:
            spec_text = cur_spec.decode("utf-8")
            updated = self._render_next(spec_text, plan, review_reason)
        except Exception as exc:
            self._finish(plan_id, "failed", f"plan apply failed: {exc}")
            return
        pre_write_truth = read_repository_truth(repo)
        if (
            not pre_write_truth.valid or pre_write_truth.dirty
            or pre_write_truth.branch != record["branch"]
            or pre_write_truth.head != record["head"]
            or pre_write_truth.status_hash != record["status_hash"]
        ):
            self._finish(plan_id, "failed", "repository changed during planning")
            return
        next_path = repo / "agent" / "next.md"
        try:
            next_path.write_bytes(updated.encode("utf-8"))
            commit = self._commit_plan(repo, record["task_id"])
        except Exception as exc:
            recovery_truth = read_repository_truth(repo)
            if recovery_truth.valid and recovery_truth.head == record["head"]:
                try:
                    next_path.write_bytes(cur_next)
                    subprocess.run(
                        ["git", "-C", str(repo), "add", "--", "agent/next.md"],
                        capture_output=True, timeout=15, **hidden_subprocess_kwargs(),
                    )
                except Exception:
                    pass
                after_restore_truth = read_repository_truth(repo)
                if (
                    not after_restore_truth.valid
                    or after_restore_truth.dirty
                    or after_restore_truth.status_hash != record["status_hash"]
                ):
                    self._finish(plan_id, "failed", f"plan apply failed: {exc}; repository not restored cleanly")
                    return
                self._finish(plan_id, "failed", f"plan apply failed: {exc}")
                return
            else:
                self._finish(plan_id, "failed", f"plan apply failed: {exc}")
                return
        self._record_ready_and_emit(plan_id, record, repo, commit, review_reason, project)

    def _record_ready_and_emit(
        self, plan_id: str, record: dict[str, Any], repo: Path, commit: str,
        review_reason: str, project: Optional[dict[str, Any]] = None,
    ) -> None:
        final_truth = read_repository_truth(repo)
        if not final_truth.valid or final_truth.dirty or final_truth.head == record["head"]:
            self._finish(plan_id, "failed", "plan commit did not produce a clean new HEAD")
            return
        with self._lock:
            state = self._load_state()
            current = state["plans"].get(plan_id)
            if not isinstance(current, dict):
                return
            current.update({
                "state": "ready",
                "reason": review_reason,
                "plan_commit": commit,
                "ready_head": final_truth.head,
                "ready_at": utc_now_iso(),
            })
            self._save_state(state)
        if self.progress_channel is not None:
            project_payload = self._project_payload(record, project)
            self.progress_channel.emit(
                project_payload, "PLAN_ACCEPTED",
                task_id=record["task_id"], occurrence_key=plan_id,
                details={"plan_id": plan_id, "reason": review_reason},
            )
            self.progress_channel.emit(
                project_payload, "NEXT_TASK",
                task_id=record["task_id"], occurrence_key=plan_id,
                details={"plan_id": plan_id, "state": "ready_to_run"},
            )

    @staticmethod
    def _render_next(original: str, plan: dict[str, Any], review_reason: str) -> str:
        marker = "## Approved executable design"
        if marker in original:
            raise RuntimeError("approved design marker already exists")
        pending = "Status: **PENDING DESIGN**"
        if pending not in original:
            raise RuntimeError("PENDING DESIGN status marker missing")
        text = original.replace(pending, "Status: **READY_TO_RUN**", 1).rstrip() + "\n\n"
        text += marker + "\n\n"
        text += str(plan["summary"]).strip() + "\n\n"
        for title, key in (("Implementation steps", "implementation_steps"), ("Interfaces / contracts", "interfaces"), ("Validation plan", "validation"), ("Risks / failure modes", "risks"), ("Out of scope", "out_of_scope")):
            text += "### " + title + "\n"
            text += "".join("- " + str(item).strip() + "\n" for item in plan[key]) + "\n"
        text += "### Independent plan review\n- Approved: " + review_reason.strip() + "\n"
        return text
    @staticmethod
    def _commit_plan(repo: Path, task_id: str) -> str:
        add = subprocess.run(
            ["git", "-C", str(repo), "add", "--", "agent/next.md"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, check=False, **hidden_subprocess_kwargs(),
        )
        if add.returncode != 0:
            raise RuntimeError("git add failed: " + (add.stderr or add.stdout).strip())
        commit = subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", f"plan({task_id}): freeze executable design", "--", "agent/next.md"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False, **hidden_subprocess_kwargs(),
        )
        if commit.returncode != 0:
            raise RuntimeError("git commit failed: " + (commit.stderr or commit.stdout).strip())
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=15, check=False,
            **hidden_subprocess_kwargs(),
        )
        if head.returncode != 0 or not head.stdout.strip():
            raise RuntimeError("cannot read plan commit HEAD")
        return head.stdout.strip()

    @staticmethod
    def _resource_payload(resource: ResourceContext | None) -> dict[str, Any] | None:
        if resource is None:
            return None
        return {"resource_id": resource.resource_id, "provider": resource.provider, "account": resource.account, "model": resource.model}
    def _record_planner_attempt(
        self, plan_id: str, attempt: int, request: AIRoleRequest, result: Any, reason: str | None,
        *, classification: str | None = None, round_no: int = 0,
    ) -> None:
        with self._lock:
            state = self._load_state()
            record = state["plans"].get(plan_id)
            if not isinstance(record, dict):
                return
            attempts = record.setdefault("planner_attempts", [])
            if not isinstance(attempts, list):
                attempts = []
                record["planner_attempts"] = attempts
            entry: dict[str, Any] = {
                "attempt": attempt,
                "request_id": request.request_id,
                "completed_at": utc_now_iso(),
                "status": getattr(result, "status", None),
                "reason": reason,
                "failure_classification": classification,
                "dispatch_id": getattr(result, "dispatch_id", None),
                "execution_id": getattr(result, "execution_id", None),
                "resource": self._resource_payload(getattr(result, "resource_context", None)),
                "excluded_resource_ids": list(request.excluded_resource_ids),
                "failover_from_resource_ids": list(
                    request.metadata.get("planner_failover_from_resource_ids", [])
                ),
            }
            if round_no > 0:
                entry["round"] = round_no
            attempts.append(entry)
            record["planner_attempt_count"] = attempt
            record["planner_retry_count"] = max(0, attempt - 1)
            if reason is not None:
                record["planner_last_failure"] = reason
                record["planner_last_failure_at"] = utc_now_iso()
            self._save_state(state)

    def _capture_plan_reviewer_output(
        self,
        plan_id: str,
        request: AIRoleRequest,
        result: Any,
        *,
        protocol_stage: str | None = None,
        repair_index: int = 0,
    ) -> None:
        raw = getattr(result, "output", None)
        raw_text = raw if isinstance(raw, str) else ""
        with self._lock:
            state = self._load_state()
            record = state["plans"].get(plan_id)
            if not isinstance(record, dict):
                return
            captures = record.setdefault("reviewer_raw_outputs", [])
            if not isinstance(captures, list):
                captures = []
                record["reviewer_raw_outputs"] = captures
            captures.append({
                "request_id": request.request_id,
                "resource": self._resource_payload(getattr(result, "resource_context", None)),
                "repair_index": repair_index,
                "protocol_stage": protocol_stage,
                "sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                "output": raw_text,
                "captured_at": utc_now_iso(),
            })
            if len(captures) > 16:
                del captures[:-16]
            self._save_state(state)

    @staticmethod
    def _plan_review_protocol_repair_prompt(raw_output: str, failure: str) -> str:
        return protocol_repair_prompt(
            raw_output=raw_output,
            failure=failure,
            schema_example='{"decision":"approve|reject|owner_gate","reason":"..."}',
            label="plan reviewer",
        )

    def _record_reviewer_attempt(
        self, plan_id: str, attempt: int, request: AIRoleRequest, result: Any,
        reason: str | None, *, classification: str | None, round_no: int,
    ) -> None:
        """Keep every reviewer dispatch visible even when a later resource succeeds."""
        with self._lock:
            state = self._load_state()
            record = state["plans"].get(plan_id)
            if not isinstance(record, dict):
                return
            attempts = record.setdefault("reviewer_attempts", [])
            if not isinstance(attempts, list):
                attempts = []
                record["reviewer_attempts"] = attempts
            entry: dict[str, Any] = {
                "attempt": attempt,
                "round": round_no,
                "request_id": request.request_id,
                "started_at": getattr(result, "started_at", None),
                "completed_at": getattr(result, "finished_at", None) or utc_now_iso(),
                "status": getattr(result, "status", None),
                "failure_classification": classification,
                "error": reason,
                "dispatch_id": getattr(result, "dispatch_id", None),
                "decision_id": getattr(result, "decision_id", None),
                "execution_id": getattr(result, "execution_id", None),
                "session_id": getattr(result, "session_id", None),
                "resource": self._resource_payload(getattr(result, "resource_context", None)),
                "excluded_resource_ids": list(request.excluded_resource_ids),
                "failover_from_resource_ids": list(
                    request.metadata.get("reviewer_failover_from_resource_ids", [])
                ),
            }
            attempts.append(entry)
            record["reviewer_attempt_count"] = attempt
            record["reviewer_failover_count"] = max(0, attempt - 1)
            if reason is not None:
                record["reviewer_last_failure"] = reason
                record["reviewer_last_failure_at"] = utc_now_iso()
            self._save_state(state)

    @staticmethod
    def _task_source(record: dict[str, Any]) -> tuple[str, str]:
        if record.get("deferred"):
            path = str(record.get("staged_spec_path") or "")
            pred_id = str(record.get("predecessor_task_id") or "")
            heading = f"Staged successor task spec ({path}; agent/next.md still advertises completed predecessor {pred_id} and must not be used as the task)"
            text = str(record.get("staged_spec_text") or "")
            return heading, text
        return "Current agent/next.md", str(record.get("next_text") or "")

    @staticmethod
    def _planner_prompt(
        record: dict[str, Any],
        retry_reason: str | None = None,
        attempt: int = 1,
        remediation: dict[str, Any] | None = None,
    ) -> str:
        prompt = (
            "You are the software-development Planner for one task. Plan only: do not modify files, commit, push, or run another agent. "
            "Inspect the repository and the supplied agent/next.md task. Convert the pending design into a bounded executable implementation plan. "
            "Return exactly one JSON object and no markdown or extra text with exactly these keys: "
            '{"task_id":"...","summary":"...","implementation_steps":["..."],"interfaces":["..."],"validation":["..."],"risks":["..."],"out_of_scope":["..."]}. '
            "Every list must contain 1-24 string items, and every list item must be at most 1000 characters. Keep the implementation bounded to the current task and preserve existing acceptance intent.\n\n"
            f"{workflow_policy_prompt('planner')}\n\n"
            f"Project: {record['project_id']}\nTask: {record['task_id']}\n"
            f"Planning branch: {record['branch']}\nPlanning HEAD: {record['head']}\n\n"
        )
        if remediation:
            rem_round = remediation.get("round")
            rejection = str(remediation.get("rejection") or "")
            prior_plan = remediation.get("prior_plan")
            prior_plan_str = json.dumps(prior_plan, ensure_ascii=False, indent=2) if isinstance(prior_plan, dict) else str(prior_plan or "")
            prior_res = remediation.get("prior_resource")
            res_payload = (
                AIPlannerCoordinator._resource_payload(prior_res)
                if isinstance(prior_res, ResourceContext)
                else (prior_res if isinstance(prior_res, dict) else None)
            )
            prompt += (
                "[PLAN_REMEDIATION]\n"
                f"Remediation round {rem_round}.\n"
                f"Task ID: {record['task_id']}\n"
                f"Planning HEAD: {record['head']}\n"
                f"Reviewer rejection reason:\n{rejection}\n\n"
                f"Prior plan JSON:\n{prior_plan_str}\n\n"
            )
            if res_payload:
                prompt += f"Prior planner resource:\n{json.dumps(res_payload, ensure_ascii=False, indent=2)}\n\n"
            prompt += (
                "Revise the plan to address the reviewer's rejection while staying bounded to the task. "
                "Return exactly one JSON object with the required schema; "
                "do not add markdown, commentary, code fences, or any text outside the JSON object.\n\n"
            )
        if retry_reason is not None:
            prompt += (
                "[PLANNER_RETRY]\n"
                f"Corrective attempt {attempt}. The previous planner attempt failed contract validation: "
                f"{retry_reason[:800]}\n"
                "Correct only that failure. Return exactly one JSON object with the required schema; "
                "do not add markdown, commentary, code fences, or any text outside the JSON object.\n\n"
            )
        if record.get("context_block"):
            prompt += f"{record['context_block']}\n\n"
        if record.get("failure_memory_block"):
            prompt += f"{record['failure_memory_block']}\n\n"
        heading, task_text = AIPlannerCoordinator._task_source(record)
        prompt += (
            f"{heading}:\n---BEGIN NEXT---\n"
            + task_text
            + "\n---END NEXT---\n"
        )
        return prompt

    @staticmethod
    def _review_prompt(record: dict[str, Any], plan: dict[str, Any]) -> str:
        prompt = (
            "You are the independent reviewer of a software implementation plan. Review only; do not modify files, commit, push, or execute the plan. "
            "Check the plan against the repository and the original pending-design task. Reject scope inflation, missing interfaces, weak validation, unsafe sequencing, or unverifiable acceptance. "
            "Return exactly one JSON object and no markdown or extra text with exactly these keys: "
            '{"decision":"approve|reject|owner_gate","reason":"..."}. '
            "Use approve only when the plan is specific enough for a Worker to execute without design guessing. Use owner_gate only for a real owner decision.\n\n"
            f"{workflow_policy_prompt('plan_reviewer')}\n\n"
            f"Project: {record['project_id']}\nTask: {record['task_id']}\n\n"
        )
        if record.get("context_block"):
            prompt += f"{record['context_block']}\n\n"
        else:
            prompt = prompt[:-1]
        if record.get("failure_memory_block"):
            if not prompt.endswith("\n\n"):
                prompt += "\n"
            prompt += f"{record['failure_memory_block']}\n\n"
        heading, task_text = AIPlannerCoordinator._task_source(record)
        task_heading = heading if record.get("deferred") else "Original task"
        prompt += (
            f"{task_heading}:\n---BEGIN NEXT---\n" + task_text + "\n---END NEXT---\n\n"
            + "Proposed plan JSON:\n" + json.dumps(plan, ensure_ascii=False, indent=2)
        )
        return prompt

    def _finish(self, plan_id: str, state_name: str, reason: str) -> None:
        with self._lock:
            state = self._load_state()
            record = state["plans"].get(plan_id)
            if not isinstance(record, dict):
                return
            record["state"] = state_name
            record["reason"] = reason
            record["completed_at"] = utc_now_iso()
            self._save_state(state)

    def mark_worker_blocked(self, plan_id: str, reason: str) -> None:
        with self._lock:
            state = self._load_state()
            record = state["plans"].get(plan_id)
            if isinstance(record, dict) and record.get("state") == "ready":
                record["state"] = "failed"
                record["reason"] = "approved plan could not launch Worker: " + reason
                record["completed_at"] = utc_now_iso()
                self._save_state(state)

    def terminal_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                copy.deepcopy(row) for row in self._load_state()["plans"].values()
                if row.get("state") in {"failed", "owner_gate", "recovery_required"}
                and row.get("control_synced") is not True
            ]

    def mark_control_synced(self, plan_id: str) -> None:
        with self._lock:
            state = self._load_state()
            record = state["plans"].get(plan_id)
            if isinstance(record, dict):
                record["control_synced"] = True
                record["control_synced_at"] = utc_now_iso()
                self._save_state(state)
