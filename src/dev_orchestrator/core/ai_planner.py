"""AIBroker planner + independent plan-review lifecycle."""
from __future__ import annotations

import copy
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from dev_orchestrator.ai.contracts import AIRoleRequest, ResourceContext
from dev_orchestrator.ai.execution_port import AIExecutionPort
from dev_orchestrator.accounting import ExecutionRecorder, FailureMemory, environment_for_project
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.staged_roadmap import read_raw, read_successor, sha256_bytes
from dev_orchestrator.core.workflow_policy import workflow_policy_prompt
from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

PLANNER_STATE_FILE = "ai-planner.json"
_STATE_VERSION = 1
_ACTIVE_STATES = frozenset({"planning", "reviewing", "remediating", "applying"})


def _nonblank(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


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
    return {
        "quality": quality,
        "review_quality": review_quality,
        "review_independence": review_independence,
        "timeout_seconds": float(timeout),
        "review_timeout_seconds": float(review_timeout),
        "max_attempts": max_attempts,
        "max_plan_remediation_rounds": max_plan_remediation_rounds,
    }, ""


def _parse_plan(text: str | None, task_id: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("planner output is empty")
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("planner output must be one JSON object") from exc
    required = {"task_id", "summary", "implementation_steps", "interfaces", "validation", "risks", "out_of_scope"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("planner JSON schema mismatch")
    if _nonblank(payload.get("task_id")) != task_id:
        raise ValueError("planner task_id mismatch")
    if _nonblank(payload.get("summary")) is None:
        raise ValueError("planner summary must be nonblank")
    for key in ("implementation_steps", "interfaces", "validation", "risks", "out_of_scope"):
        value = payload.get(key)
        if not isinstance(value, list) or not value or len(value) > 24:
            raise ValueError(f"planner {key} must be a non-empty bounded list")
        if any(_nonblank(item) is None or len(str(item)) > 1000 for item in value):
            raise ValueError(f"planner {key} entries must be bounded strings")
    return payload


def _parse_plan_review(text: str | None) -> tuple[str, str]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("plan reviewer output is empty")
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("plan reviewer output must be one JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != {"decision", "reason"}:
        raise ValueError("plan reviewer JSON must contain exactly decision and reason")
    decision = _nonblank(payload.get("decision"))
    reason = _nonblank(payload.get("reason"))
    if decision not in {"approve", "reject", "owner_gate"} or reason is None:
        raise ValueError("invalid plan reviewer decision")
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
        if truth.branch != reviewed_branch or truth.head != reviewed_head:
            return None, "repository moved since review"

        predecessor_bytes = read_raw(repo, "agent/next.md")
        if predecessor_bytes is None:
            return None, "agent/next.md unavailable"
        try:
            predecessor_text = predecessor_bytes.decode("utf-8")
        except UnicodeDecodeError:
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

        base_fields = {
            "deferred": True,
            "predecessor_task_id": handoff_task_id,
            "predecessor_next_sha256": sha256_bytes(predecessor_bytes),
            "next_text": predecessor_text,
            "staged_spec_path": staged_spec_path,
            "staged_spec_sha256": staged_spec_sha256,
            "staged_spec_text": rm_res.spec_text,
        }
        return self._begin_lifecycle(
            project, policy, command_id, staged_successor, truth, base_fields
        )

    def ready_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(row) for row in self._load_state()["plans"].values() if row.get("state") == "ready"]

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
    ) -> tuple[dict[str, Any], Any] | None:
        max_attempts = policy["max_attempts"]
        planner_result = None
        plan = None
        previous_attempt_resource = prior_resource if round_no > 0 else None
        failure_reason = None
        remediation_data = None
        if round_no > 0:
            remediation_data = {
                "round": round_no,
                "rejection": rejection,
                "prior_plan": prior_plan,
                "prior_resource": prior_resource,
            }
        base_req_id = plan_id + ":planner" if round_no == 0 else f"{plan_id}:planner:remediate-{round_no}"

        for attempt in range(1, max_attempts + 1):
            retry_suffix = "" if attempt == 1 else f":retry-{attempt - 1}"
            metadata: dict[str, Any] = {
                "control_command_id": record["command_id"],
                "planner_attempt": attempt,
                "planner_max_attempts": max_attempts,
            }
            if round_no > 0:
                metadata["remediation_round"] = round_no

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
                    raise RuntimeError(attempt_result.error or f"planner_{attempt_result.status}")
                plan = _parse_plan(attempt_result.output, record["task_id"])
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
                self._record_planner_attempt(plan_id, attempt, planner_request, attempt_result, attempt_reason, round_no=round_no)
                if attempt_result is not None and attempt_result.resource_context is not None:
                    previous_attempt_resource = attempt_result.resource_context
                failure_reason = attempt_reason
                if attempt >= max_attempts:
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
                        attempt_id=f"{plan_id}:plan-round-{round_no}",
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
    ) -> tuple[str, str, Any] | None:
        request_id = plan_id + ":reviewer" if round_no == 0 else f"{plan_id}:reviewer:remediate-{round_no}"
        metadata: dict[str, Any] = {"control_command_id": record["command_id"], "review_kind": "plan"}
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
            timeout_seconds=policy["review_timeout_seconds"],
            metadata=metadata,
        )
        review_result = None
        review_outcome = "failed"
        review_failure = None
        review_started = time.monotonic()
        if self.accounting is not None:
            self.accounting.start_interval(
                "plan_review",
                review_request.request_id,
                project_id=record["project_id"],
                task_id=record["task_id"],
                role="plan_reviewer",
                request_id=review_request.request_id,
                stage_run_id=review_request.stage_run_id,
                role_run_id=review_request.role_run_id,
                source_request_id=record["command_id"],
                attempt_id=f"{plan_id}:plan-round-{round_no}",
            )
        try:
            review_result = self.port.execute(review_request) if self.port is not None else None
            if review_result is None:
                raise RuntimeError("plan reviewer execution port unavailable")
            if review_result.status != "succeeded":
                raise RuntimeError(review_result.error or f"plan_reviewer_{review_result.status}")
            decision, reason = _parse_plan_review(review_result.output)
            review_outcome = "accepted"
        except Exception as exc:
            review_failure = str(exc)
            self._finish(plan_id, "failed", f"plan review failed: {exc}")
            return None
        finally:
            if self.accounting is not None:
                self.accounting.end_interval(
                    "plan_review",
                    review_request.request_id,
                    outcome=review_outcome,
                    project_id=record["project_id"],
                    task_id=record["task_id"],
                    role="plan_reviewer",
                    request_id=review_request.request_id,
                    stage_run_id=review_request.stage_run_id,
                    role_run_id=review_request.role_run_id,
                    source_request_id=record["command_id"],
                    attempt_id=f"{plan_id}:plan-round-{round_no}",
                    dispatch_id=getattr(review_result, "dispatch_id", None),
                    decision_id=getattr(review_result, "decision_id", None),
                    execution_id=getattr(review_result, "execution_id", None),
                    session_id=getattr(review_result, "session_id", None),
                    resource_id=(
                        review_result.resource_context.resource_id
                        if review_result is not None and review_result.resource_context else None
                    ),
                    provider=(
                        review_result.resource_context.provider
                        if review_result is not None and review_result.resource_context else None
                    ),
                    account=(
                        review_result.resource_context.account
                        if review_result is not None and review_result.resource_context else None
                    ),
                    model=(
                        review_result.resource_context.model
                        if review_result is not None and review_result.resource_context else None
                    ),
                )
            if self.failure_memory is not None and review_failure:
                self.failure_memory.record_matching_recurrences(
                    record.get("failure_environment", {}),
                    review_failure,
                    time.monotonic() - review_started,
                    project_id=record["project_id"],
                    task_id=record["task_id"],
                    role="plan_reviewer",
                    request_id=review_request.request_id,
                    source_request_id=record["command_id"],
                )
        return decision, reason, review_result

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
        rejection: str | None = None
        prior_plan: dict[str, Any] | None = None
        prior_resource: ResourceContext | None = None

        for round_no in range(max_remediation_rounds + 1):
            attempts_res = self._run_planner_attempts(
                plan_id, record, policy, round_no,
                rejection=rejection,
                prior_plan=prior_plan,
                prior_resource=prior_resource,
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
                })
                self._save_state(state)

            review_res = self._run_plan_review(
                plan_id, record, policy, plan, previous, round_no,
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
                        f"{plan_id}:plan-round-{round_no}",
                        "accepted",
                        project_id=record["project_id"],
                        task_id=record["task_id"],
                        role="plan_reviewer",
                        request_id=(
                            f"{plan_id}:reviewer"
                            if round_no == 0
                            else f"{plan_id}:reviewer:remediate-{round_no}"
                        ),
                        metadata={"review_kind": "plan", "decision": decision},
                    )
                self._apply_plan(plan_id, record, plan, reason, project=project)
                return

            if decision != "reject":
                self._finish(plan_id, "failed", f"unexpected plan review decision: {decision}")
                return

            # Review decision is 'reject'
            if self.accounting is not None:
                self.accounting.record_attempt_outcome(
                    f"{plan_id}:plan-round-{round_no}",
                    "rejected",
                    project_id=record["project_id"],
                    task_id=record["task_id"],
                    role="plan_reviewer",
                    request_id=(
                        f"{plan_id}:reviewer"
                        if round_no == 0
                        else f"{plan_id}:reviewer:remediate-{round_no}"
                    ),
                    metadata={"review_kind": "plan", "decision": decision, "reason": reason},
                )
            rejection_entry = {
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

            if round_no < max_remediation_rounds:
                with self._lock:
                    state = self._load_state()
                    current = state["plans"].get(plan_id)
                    if not isinstance(current, dict):
                        return
                    chain = current.setdefault("rejection_chain", [])
                    chain.append(rejection_entry)
                    current.update({
                        "state": "remediating",
                        "remediation_round": round_no + 1,
                        "review_reason": reason,
                        "remediation_started_at": utc_now_iso(),
                    })
                    self._save_state(state)

                if self.progress_channel is not None:
                    self.progress_channel.emit(
                        self._project_payload(record, project),
                        "REMEDIATE",
                        task_id=record["task_id"],
                        occurrence_key=f"{plan_id}:remediate-{round_no + 1}",
                        details={
                            "plan_id": plan_id,
                            "round": round_no + 1,
                            "reason": reason,
                        },
                    )

                rejection = reason
                prior_plan = plan
                prior_resource = previous
                continue

            # round_no == max_remediation_rounds (bound exhausted)
            with self._lock:
                state = self._load_state()
                current = state["plans"].get(plan_id)
                if not isinstance(current, dict):
                    return
                chain = current.setdefault("rejection_chain", [])
                chain.append(rejection_entry)
                chain_len = len(chain)
                self._save_state(state)

            exhaust_reason = f"plan remediation hit its bound after {max_remediation_rounds} rounds (exhausted); last rejection: {reason}"
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
                    task_id=record["task_id"],
                    occurrence_key=f"{plan_id}:owner_gate:exhausted",
                    details={
                        "plan_id": plan_id,
                        "reason": exhaust_reason,
                        "rejection_chain_length": chain_len,
                    },
                )
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
        round_no: int = 0,
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
                "dispatch_id": getattr(result, "dispatch_id", None),
                "execution_id": getattr(result, "execution_id", None),
                "resource": self._resource_payload(getattr(result, "resource_context", None)),
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
            "Every list must be non-empty. Keep the implementation bounded to the current task and preserve existing acceptance intent.\n\n"
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
