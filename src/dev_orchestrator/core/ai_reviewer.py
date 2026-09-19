"""Direct AIBroker reviewer path, independent of browser conversations."""
from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.ai.contracts import AIRoleRequest, ResourceContext
from dev_orchestrator.ai.execution_port import AIExecutionPort
from dev_orchestrator.accounting import ExecutionRecorder, FailureMemory, environment_for_project
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.workflow_policy import workflow_policy_prompt
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

REVIEWER_STATE_FILE = "ai-reviewer.json"
REVIEW_DECISIONS_FILE = "review-decisions.json"
_STATE_VERSION = 1
_DECISION_VERSION = 1
_ACTIVE_STATES = frozenset({"launching", "running"})
_TERMINAL_STATES = frozenset({"completed", "failed", "recovery_required"})
_ALLOWED_DECISIONS = {
    ("next", "next_task"),
    ("remediate", "continue_current_stage"),
    ("owner_gate", "stop"),
    ("stop", "stop"),
}

def _nonblank(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _review_policy(project: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    execution = project.get("execution")
    if not isinstance(execution, dict) or execution.get("engine") != "aibroker":
        return None, "direct reviewer requires execution.engine=aibroker"
    roles = project.get("ai_roles")
    if not isinstance(roles, dict):
        return None, "ai_roles missing"
    raw = roles.get("reviewer")
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None, "reviewer role disabled"
    quality = raw.get("quality", "high")
    independence = raw.get("independence", "resource")
    if quality not in ("economy", "balanced", "high"):
        return None, "reviewer quality invalid"
    if independence not in ("resource", "account", "provider"):
        return None, "reviewer independence invalid"
    timeout = raw.get("timeout_seconds", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        return None, "reviewer timeout_seconds must be positive"
    return {"quality": quality, "independence": independence, "timeout_seconds": float(timeout)}, ""


def _parse_review_output(text: str | None) -> tuple[str, str, str]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("reviewer output is empty")
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("reviewer output must be one JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != {"decision", "next_action", "reason"}:
        raise ValueError("reviewer JSON must contain exactly decision, next_action, reason")
    decision = _nonblank(payload.get("decision"))
    next_action = _nonblank(payload.get("next_action"))
    reason = _nonblank(payload.get("reason"))
    if decision is None or next_action is None or reason is None:
        raise ValueError("reviewer decision fields must be nonblank strings")
    if (decision, next_action) not in _ALLOWED_DECISIONS:
        raise ValueError("reviewer decision/next_action pair is not allowed")
    return decision, next_action, reason


class AIReviewerCoordinator:
    """Schedule one independent AIBroker reviewer per completed broker Worker."""

    def __init__(
        self, runtime_root: Path | str, port: AIExecutionPort | None,
        progress_channel: Optional[Any] = None,
        accounting: ExecutionRecorder | None = None,
        failure_memory: FailureMemory | None = None,
        failure_memory_max_chars: int = 2000,
        harness: Optional[Any] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.progress_channel = progress_channel
        self.accounting = accounting
        self.failure_memory = failure_memory
        self.failure_memory_max_chars = failure_memory_max_chars
        self.harness = harness
        self.state_path = self.runtime_root / REVIEWER_STATE_FILE
        self.decisions_path = self.runtime_root / REVIEW_DECISIONS_FILE
        self.transition_path = self.runtime_root / "transition-executor.json"
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._project_bindings: dict[str, dict[str, Any]] = {}
        self._recover_interrupted()

    def _get_harness(self) -> Any:
        if self.harness is not None:
            return self.harness
        from dev_orchestrator.review.harness import DefaultReviewerHarness
        self.harness = DefaultReviewerHarness(self.runtime_root)
        return self.harness

    def _load_state(self) -> dict[str, Any]:
        raw = read_json(self.state_path, None)
        reviews = raw.get("reviews") if isinstance(raw, dict) else None
        if not isinstance(reviews, dict):
            reviews = {}
        return {"version": _STATE_VERSION, "reviews": {
            key: value for key, value in reviews.items()
            if isinstance(key, str) and isinstance(value, dict)
        }}

    def _save_state(self, state: dict[str, Any]) -> None:
        write_json(self.state_path, state, indent=2)

    def _recover_interrupted(self) -> None:
        with self._lock:
            state = self._load_state()
            changed = False
            for record in state["reviews"].values():
                if record.get("state") in _ACTIVE_STATES:
                    record["state"] = "recovery_required"
                    record["reason"] = "daemon restarted during reviewer execution; automatic replay forbidden"
                    record["recovered_at"] = utc_now_iso()
                    changed = True
            if changed:
                self._save_state(state)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load_state())

    def enabled_project_ids(self, config_path: Path | str) -> frozenset[str]:
        config = load_projects_config(config_path)
        result = set()
        for project in config.get("projects") or []:
            h_cfg = project.get("reviewer_harness")
            if isinstance(h_cfg, dict) and h_cfg.get("enabled") is True:
                result.add(str(project["project_id"]))
                continue
            policy, _ = _review_policy(project)
            if policy is not None:
                result.add(str(project["project_id"]))
        return frozenset(result)

    def _transition_records(self) -> dict[str, dict[str, Any]]:
        raw = read_json(self.transition_path, None)
        executions = raw.get("executions") if isinstance(raw, dict) else None
        if not isinstance(executions, dict):
            return {}
        return {
            key: value for key, value in executions.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    @staticmethod
    def _project_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(project["project_id"]): project
            for project in config.get("projects") or []
            if isinstance(project, dict) and project.get("project_id")
        }

    def advance(self, config_path: Path | str) -> list[str]:
        config = load_projects_config(config_path)
        projects = self._project_map(config)
        with self._lock:
            for p_id, p in projects.items():
                b = p.get("conversation_binding")
                if isinstance(b, dict) and b.get("adapter") and b.get("binding_id"):
                    self._project_bindings[p_id] = copy.deepcopy(b)
        if self.progress_channel is not None and hasattr(self.progress_channel, "register_projects"):
            self.progress_channel.register_projects(projects.values())
        transition_records = self._transition_records()
        latest: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
        for source_request_id, worker in transition_records.items():
            if worker.get("engine") != "aibroker" or worker.get("state") != "completed":
                continue
            project_id = _nonblank(worker.get("project_id"))
            if project_id is None or project_id not in projects:
                continue
            proj_dict = projects[project_id]
            harness_cfg = proj_dict.get("reviewer_harness")
            if isinstance(harness_cfg, dict) and harness_cfg.get("enabled") is True:
                policy = {"quality": "high", "independence": "resource", "timeout_seconds": 600.0, "harness": True}
            else:
                if self.port is None:
                    continue
                policy, _ = _review_policy(proj_dict)
                if policy is None:
                    continue
            candidate_key = (str(worker.get("completed_at") or worker.get("started_at") or ""), source_request_id)
            previous = latest.get(project_id)
            previous_key = ((str(previous[1].get("completed_at") or previous[1].get("started_at") or ""), previous[0]) if previous else None)
            if previous_key is None or candidate_key > previous_key:
                latest[project_id] = (source_request_id, worker, policy)

        launched: list[str] = []
        for project_id, (source_request_id, worker, policy) in latest.items():
            review_id = "ai_review:" + source_request_id
            with self._lock:
                state = self._load_state()
                if review_id in state["reviews"]:
                    continue
            repo_path = _nonblank(worker.get("repo_path"))
            task_id = _nonblank(worker.get("task_id"))
            if repo_path is None or task_id is None:
                self._record_terminal(review_id, project_id, source_request_id, "failed", "worker identity incomplete")
                continue
            truth = read_repository_truth(repo_path)
            if not truth.valid:
                self._record_terminal(review_id, project_id, source_request_id, "failed", "repository truth unavailable")
                continue
            proj_dict = projects.get(project_id, {})
            binding = proj_dict.get("conversation_binding") or self._project_bindings.get(project_id)
            if binding is None and isinstance(worker.get("conversation_binding"), dict):
                binding = worker.get("conversation_binding")

            harness_cfg = proj_dict.get("reviewer_harness")
            if isinstance(harness_cfg, dict) and harness_cfg.get("enabled") is True:
                self._launch_harness_review(
                    review_id,
                    source_request_id,
                    project_id,
                    task_id,
                    repo_path,
                    truth,
                    harness_cfg,
                    worker=worker,
                    binding=binding,
                    policy=policy,
                )
                launched.append(review_id)
                continue

            resource = worker.get("resource_context")
            if not isinstance(resource, dict):
                self._record_terminal(review_id, project_id, source_request_id, "failed", "worker resource context missing")
                continue
            previous = ResourceContext(
                resource.get("resource_id"), resource.get("provider"),
                resource.get("account"), resource.get("model"),
            )
            from dev_orchestrator.core.project_context import context_prompt_block
            context_block, resolution = context_prompt_block(proj_dict, "reviewer")
            ctx_decl = proj_dict.get("project_context") or {}
            if ctx_decl.get("enabled") and ctx_decl.get("require_valid", True) and resolution.state == "invalid":
                self._record_terminal(
                    review_id, project_id, source_request_id, "failed",
                    "durable project context is invalid: {0}".format(resolution.reason),
                )
                continue
            binding = proj_dict.get("conversation_binding") or self._project_bindings.get(project_id)
            if binding is None and isinstance(worker.get("conversation_binding"), dict):
                binding = worker.get("conversation_binding")
            failure_memory_block = ""
            if self.failure_memory is not None:
                failure_memory_block = self.failure_memory.prompt_block(
                    environment_for_project(proj_dict), max_chars=self.failure_memory_max_chars
                )
            prompt = self._review_prompt(
                project_id,
                task_id,
                source_request_id,
                truth,
                context_block=context_block,
                failure_memory_block=failure_memory_block,
            )
            request = AIRoleRequest(
                project_id=project_id, task_run_id=task_id, stage_run_id="review",
                role_run_id="reviewer-" + source_request_id.replace(":", "-"),
                request_id=review_id, role="reviewer", prompt=prompt,
                working_directory=Path(repo_path), quality=policy["quality"],
                independence=policy["independence"], previous_resource_context=previous,
                timeout_seconds=policy["timeout_seconds"],
                metadata={
                    "worker_source_request_id": source_request_id,
                    "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                    "failure_environment": environment_for_project(proj_dict),
                },
            )
            self._launch_review(
                review_id, source_request_id, request, truth,
                conversation_binding=binding, resolution=resolution,
            )
            launched.append(review_id)
        return launched

    def reconcile(
        self, project: dict[str, Any], snapshot: dict[str, Any],
        candidate: dict[str, Any], command_id: str,
    ) -> tuple[str | None, str]:
        """Launch one explicit, current-HEAD re-review without launching a Worker.

        The control coordinator has already resolved the target, but this
        adapter resolves it again immediately before persisting anything.  That
        closes the projection-to-launch race without modifying the stale review,
        its decision, or its original Worker execution.
        """
        from dev_orchestrator.control.reconcile import resolve_reconcile_candidate
        target, reason = resolve_reconcile_candidate(snapshot, self.runtime_root, project)
        if target is None:
            return None, reason
        if target.get("target_id") != candidate.get("target_id"):
            return None, "reconcile target changed before reviewer launch"
        policy, policy_reason = _review_policy(project)
        if policy is None:
            return None, policy_reason
        if self.port is None:
            return None, "AIBroker reviewer port unavailable"
        review_id = "ai_review:reconcile:" + command_id
        with self._lock:
            existing = self._load_state()["reviews"].get(review_id)
            if isinstance(existing, dict):
                if (
                    existing.get("reconcile_of") == target["target_id"]
                    and existing.get("project_id") == project.get("project_id")
                    and existing.get("source_request_id") == target.get("source_request_id")
                    and existing.get("task_id") == target.get("task_id")
                ):
                    return review_id, "reconcile review already launched for command_id"
                return None, "conflicting reconcile review replay"
        repo_path = _nonblank(project.get("repo_path"))
        source_id = _nonblank(target.get("source_request_id"))
        task_id = _nonblank(target.get("task_id"))
        resource = target.get("resource_context")
        if repo_path is None or source_id is None or task_id is None or not isinstance(resource, dict):
            return None, "reconcile candidate source identity is incomplete"
        truth = read_repository_truth(repo_path)
        if not truth.valid or truth.dirty or truth.branch != target.get("branch") or truth.head != target.get("current_head"):
            return None, "current repository changed before reconcile reviewer launch"
        previous = ResourceContext(
            resource.get("resource_id"), resource.get("provider"),
            resource.get("account"), resource.get("model"),
        )
        from dev_orchestrator.core.project_context import context_prompt_block
        context_block, resolution = context_prompt_block(project, "reviewer")
        ctx_decl = project.get("project_context") or {}
        if ctx_decl.get("enabled") and ctx_decl.get("require_valid", True) and resolution.state == "invalid":
            self._record_terminal(
                review_id, str(project["project_id"]), source_id, "failed",
                "durable project context is invalid: {0}".format(resolution.reason),
            )
            return None, "durable project context is invalid: {0}".format(resolution.reason)
        binding = project.get("conversation_binding") or self._project_bindings.get(str(project["project_id"]))
        failure_memory_block = ""
        if self.failure_memory is not None:
            failure_memory_block = self.failure_memory.prompt_block(
                environment_for_project(project), max_chars=self.failure_memory_max_chars
            )
        prior_reason = str(target.get("prior_reason") or "(no prior reviewer reason recorded)").strip()[:2000]
        reanchor_context = (
            "[STALE_REVIEW_REANCHOR]\n"
            "A prior technical review at HEAD {0} requested REMEDIATE: {1}\n"
            "That review anchor is stale. Independently review the CURRENT clean HEAD; "
            "do not assume the prior verdict remains correct.\n"
            "[/STALE_REVIEW_REANCHOR]"
        ).format(target.get("reviewed_head"), prior_reason)
        request = AIRoleRequest(
            project_id=str(project["project_id"]), task_run_id=task_id, stage_run_id="review",
            role_run_id="reviewer-reconcile-" + command_id,
            request_id=review_id, role="reviewer",
            prompt=self._review_prompt(
                str(project["project_id"]), task_id, source_id, truth,
                context_block=context_block, failure_memory_block=failure_memory_block,
                reanchor_context=reanchor_context,
            ),
            working_directory=Path(repo_path), quality=policy["quality"],
            independence=policy["independence"], previous_resource_context=previous,
            timeout_seconds=policy["timeout_seconds"],
            metadata={
                "worker_source_request_id": source_id,
                "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                "failure_environment": environment_for_project(project),
                "reconcile_of": target["target_id"],
            },
        )
        self._launch_review(
            review_id, source_id, request, truth,
            conversation_binding=binding, resolution=resolution,
        )
        return review_id, "stale technical review re-anchored at current clean HEAD"


    def rereview_descendant(self, project, snapshot, candidate, command_id):
        """Re-review current clean descendant HEAD while preserving the stale review lineage."""
        from dev_orchestrator.control.reconcile import resolve_rereview_candidate
        target, reason = resolve_rereview_candidate(snapshot, self.runtime_root, project)
        if target is None or target.get("target_id") != candidate.get("target_id"):
            return None, reason or "re-review target changed before launch"
        policy, policy_reason = _review_policy(project)
        if policy is None or self.port is None:
            return None, policy_reason or "AIBroker reviewer port unavailable"
        review_id = "ai_review:rereview:" + command_id
        with self._lock:
            existing = self._load_state()["reviews"].get(review_id)
            if isinstance(existing, dict):
                return (review_id, "descendant re-review already launched for command_id") if existing.get("rereview_of") == target["target_id"] else (None, "conflicting descendant re-review replay")
        repo_path = _nonblank(project.get("repo_path")); source_id = _nonblank(target.get("source_request_id")); task_id = _nonblank(target.get("task_id"))
        resource = target.get("resource_context"); truth = read_repository_truth(repo_path or "")
        if not repo_path or not source_id or not task_id or not isinstance(resource, dict) or not truth.valid or truth.dirty or truth.head != target.get("current_head"):
            return None, "current repository or source identity changed before descendant re-review"
        previous = ResourceContext(resource.get("resource_id"), resource.get("provider"), resource.get("account"), resource.get("model"))
        from dev_orchestrator.core.project_context import context_prompt_block
        context_block, resolution = context_prompt_block(project, "reviewer")
        binding = project.get("conversation_binding") or self._project_bindings.get(str(project["project_id"]))
        if target.get("kind") == "next":
            reanchor = ("[ACCEPTED_NEXT_DESCENDANT_REREVIEW]\n"
                        "A prior technical review accepted NEXT (next_task) at HEAD {0}: {1}\n"
                        "That acceptance anchor is now stale because the current clean HEAD is a descendant. "
                        "Independently review the CURRENT HEAD from repository evidence; do not inherit or import the prior accepted verdict.\n"
                        "[/ACCEPTED_NEXT_DESCENDANT_REREVIEW]").format(target.get("reviewed_head"), target.get("prior_reason"))
        else:
            reanchor = ("[FAILED_REVIEW_DESCENDANT_REREVIEW]\nPrior reviewer infrastructure failed at HEAD {0}: {1}\n"
                        "The current clean HEAD is a descendant containing bounded recovery fixes. Independently review CURRENT HEAD; do not inherit a verdict.\n"
                        "[/FAILED_REVIEW_DESCENDANT_REREVIEW]").format(target.get("reviewed_head"), target.get("prior_reason"))
        request = AIRoleRequest(project_id=str(project["project_id"]), task_run_id=task_id, stage_run_id="review",
            role_run_id="reviewer-rereview-" + command_id, request_id=review_id, role="reviewer",
            prompt=self._review_prompt(str(project["project_id"]), task_id, source_id, truth, context_block=context_block, reanchor_context=reanchor),
            working_directory=Path(repo_path), quality=policy["quality"], independence=policy["independence"], previous_resource_context=previous,
            timeout_seconds=policy["timeout_seconds"], metadata={"worker_source_request_id": source_id, "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
            "failure_environment": environment_for_project(project), "rereview_of": target["target_id"]})
        self._launch_review(review_id, source_id, request, truth, conversation_binding=binding, resolution=resolution)
        if target.get("kind") == "next":
            return review_id, "stale accepted NEXT re-reviewed at current clean descendant HEAD"
        return review_id, "failed reviewer lineage re-anchored at current clean descendant HEAD"

    def retry_failed(
        self, project: dict[str, Any], snapshot: dict[str, Any],
        candidate: dict[str, Any], command_id: str,
    ) -> tuple[str | None, str]:
        """Retry one exact failed technical review at the unchanged clean HEAD."""
        from dev_orchestrator.control.reconcile import resolve_retry_candidate
        target, reason = resolve_retry_candidate(snapshot, self.runtime_root, project)
        if target is None:
            return None, reason
        if target.get("target_id") != candidate.get("target_id"):
            return None, "retry target changed before reviewer launch"
        policy, policy_reason = _review_policy(project)
        if policy is None:
            return None, policy_reason
        if self.port is None:
            return None, "AIBroker reviewer port unavailable"
        review_id = "ai_review:retry:" + command_id
        with self._lock:
            existing = self._load_state()["reviews"].get(review_id)
            if isinstance(existing, dict):
                if (
                    existing.get("retry_of") == target["target_id"]
                    and existing.get("project_id") == project.get("project_id")
                    and existing.get("source_request_id") == target.get("source_request_id")
                    and existing.get("task_id") == target.get("task_id")
                ):
                    return review_id, "failed review retry already launched for command_id"
                return None, "conflicting failed-review retry replay"
        repo_path = _nonblank(project.get("repo_path"))
        source_id = _nonblank(target.get("source_request_id"))
        task_id = _nonblank(target.get("task_id"))
        resource = target.get("resource_context")
        if repo_path is None or source_id is None or task_id is None or not isinstance(resource, dict):
            return None, "retry candidate source identity is incomplete"
        truth = read_repository_truth(repo_path)
        if not truth.valid or truth.dirty or truth.branch != target.get("branch") or truth.head != target.get("current_head"):
            return None, "current repository changed before retry reviewer launch"
        previous = ResourceContext(
            resource.get("resource_id"), resource.get("provider"),
            resource.get("account"), resource.get("model"),
        )
        from dev_orchestrator.core.project_context import context_prompt_block
        context_block, resolution = context_prompt_block(project, "reviewer")
        ctx_decl = project.get("project_context") or {}
        if ctx_decl.get("enabled") and ctx_decl.get("require_valid", True) and resolution.state == "invalid":
            self._record_terminal(
                review_id, str(project["project_id"]), source_id, "failed",
                "durable project context is invalid: {0}".format(resolution.reason),
            )
            return None, "durable project context is invalid: {0}".format(resolution.reason)
        binding = project.get("conversation_binding") or self._project_bindings.get(str(project["project_id"]))
        failure_memory_block = ""
        if self.failure_memory is not None:
            failure_memory_block = self.failure_memory.prompt_block(
                environment_for_project(project), max_chars=self.failure_memory_max_chars
            )
        retry_context = (
            "[FAILED_REVIEW_RETRY]\n"
            "A prior technical review of this SAME clean HEAD failed before producing a durable decision: {0}\n"
            "Independently review the current HEAD from repository evidence. Do not infer a verdict from the failed attempt.\n"
            "[/FAILED_REVIEW_RETRY]"
        ).format(str(target.get("prior_reason") or "review infrastructure failure")[:2000])
        request = AIRoleRequest(
            project_id=str(project["project_id"]), task_run_id=task_id, stage_run_id="review",
            role_run_id="reviewer-retry-" + command_id,
            request_id=review_id, role="reviewer",
            prompt=self._review_prompt(
                str(project["project_id"]), task_id, source_id, truth,
                context_block=context_block, failure_memory_block=failure_memory_block,
                reanchor_context=retry_context,
            ),
            working_directory=Path(repo_path), quality=policy["quality"],
            independence=policy["independence"], previous_resource_context=previous,
            timeout_seconds=policy["timeout_seconds"],
            metadata={
                "worker_source_request_id": source_id,
                "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                "failure_environment": environment_for_project(project),
                "retry_of": target["target_id"],
            },
        )
        self._launch_review(
            review_id, source_id, request, truth,
            conversation_binding=binding, resolution=resolution,
        )
        return review_id, "failed technical review retried at the same clean HEAD"

    @staticmethod
    def _review_prompt(
        project_id: str, task_id: str, source_request_id: str, truth: Any,
        context_block: str = "",
        failure_memory_block: str = "",
        reanchor_context: str = "",
    ) -> str:
        prompt = (
            "You are the independent reviewer for a completed software-development Worker. "
            "Review only; do not modify files, commit, push, or start another Worker. "
            "Inspect the repository, current diff/status, task evidence, tests, and acceptance criteria. "
            "Return exactly one JSON object and no markdown or extra text, with exactly these keys: "
            '{"decision":"next|remediate|owner_gate|stop","next_action":"next_task|continue_current_stage|stop","reason":"..."}. '
            "Allowed pairs are next/next_task, remediate/continue_current_stage, owner_gate/stop, stop/stop. "
            "Use next only when the reviewed task is actually complete and repository evidence supports advancing. "
            "Use remediate for bounded fixable gaps in this reviewed task; owner_gate only when owner input is genuinely required.\n\n"
            f"{workflow_policy_prompt('technical_reviewer')}\n\n"
            f"Project: {project_id}\nReviewed task: {task_id}\nWorker source: {source_request_id}\n"
            f"Review branch: {truth.branch}\nReview HEAD: {truth.head}\nReview dirty: {truth.dirty}\n"
        )
        if context_block:
            prompt += f"\n{context_block}\n"
        if failure_memory_block:
            prompt += f"\n{failure_memory_block}\n"
        if reanchor_context:
            prompt += f"\n{reanchor_context}\n"
        return prompt

    def _launch_harness_review(
        self,
        review_id: str,
        source_request_id: str,
        project_id: str,
        task_id: str,
        repo_path: str,
        truth: Any,
        harness_cfg: dict[str, Any],
        *,
        worker: dict[str, Any],
        binding: Optional[dict[str, Any]] = None,
        policy: dict[str, Any],
    ) -> None:
        with self._lock:
            state = self._load_state()
            if review_id in state["reviews"]:
                return
            state["reviews"][review_id] = {
                "review_id": review_id,
                "project_id": project_id,
                "source_request_id": source_request_id,
                "task_id": task_id,
                "branch": truth.branch,
                "head": truth.head,
                "review_status_hash": truth.status_hash,
                "review_dirty": bool(truth.dirty),
                "state": "launching",
                "started_at": utc_now_iso(),
                "role_run_id": "reviewer-" + source_request_id.replace(":", "-"),
                "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                "harness": True,
            }
            if binding and isinstance(binding, dict):
                self._project_bindings[project_id] = copy.deepcopy(binding)
            self._save_state(state)

        thread = threading.Thread(
            target=self._run_harness_review,
            args=(review_id, source_request_id, project_id, task_id, repo_path, truth, harness_cfg, worker, binding, policy),
            name=f"devorch-review-harness-{project_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[review_id] = thread
        thread.start()

        if self.progress_channel is not None:
            payload = {"project_id": project_id}
            if binding:
                payload["conversation_binding"] = binding
            self.progress_channel.emit(
                payload, "REVIEW_STARTED",
                task_id=task_id, occurrence_key=review_id,
                details={"review_id": review_id, "worker_source_request_id": source_request_id},
            )

    def _run_harness_review(
        self,
        review_id: str,
        source_request_id: str,
        project_id: str,
        task_id: str,
        repo_path: str,
        truth: Any,
        harness_cfg: dict[str, Any],
        worker: dict[str, Any],
        binding: Optional[dict[str, Any]],
        policy: dict[str, Any],
    ) -> None:
        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if not isinstance(record, dict):
                return
            record["state"] = "running"
            self._save_state(state)

        if self.accounting is not None:
            self.accounting.start_interval(
                "technical_review",
                review_id,
                project_id=project_id,
                task_id=task_id,
                role="reviewer",
                request_id=review_id,
                stage_run_id="review",
                role_run_id=record.get("role_run_id") or f"reviewer-{review_id}",
                source_request_id=source_request_id or None,
                attempt_id=source_request_id or None,
            )

        try:
            from dev_orchestrator.review.models import ReviewRequest
            harness = self._get_harness()

            file_limits = {
                "max_files": harness_cfg.get("max_files", 50),
                "max_bytes": harness_cfg.get("max_bytes", 500 * 1024),
                "max_packet_files": harness_cfg.get("packet_size", 10),
                "max_packet_bytes": harness_cfg.get("max_packet_bytes", 200 * 1024),
            }

            req = ReviewRequest(
                request_id=review_id,
                project_id=project_id,
                task_id=task_id,
                source_request_id=source_request_id,
                mode=harness_cfg.get("mode", "diff"),
                diff_mode=harness_cfg.get("diff_mode", "workspace"),
                scan_roots=list(harness_cfg.get("scan_roots") or []),
                rule_pack_path=harness_cfg.get("rule_pack"),
                blocking_severities=list(harness_cfg.get("blocking_severities") or ["blocking"]),
                independent_gates=list(harness_cfg.get("independent_gates") or []),
                branch=truth.branch,
                head=truth.head,
                status_hash=truth.status_hash,
                transport=harness_cfg.get("transport", "local"),
                file_limits=file_limits,
                metadata={
                    "repo_path": repo_path,
                    "worker_source_request_id": source_request_id,
                    "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                },
            )

            session = harness.submit(req)

            with self._lock:
                state = self._load_state()
                rec = state["reviews"].get(review_id)
                if rec:
                    rec["job_id"] = session.job_id
                    rec["session_id"] = session.session_id
                    self._save_state(state)

            timeout = float(policy.get("timeout_seconds", 600.0))
            poll_interval = 0.2
            start_poll = time.monotonic()

            while time.monotonic() - start_poll < timeout:
                session = harness.status(session.session_id)
                if session.state in ("completed", "failed", "cancelled"):
                    break
                time.sleep(poll_interval)

            if session.state not in ("completed", "failed", "cancelled"):
                raise TimeoutError("review session timed out waiting for completion")

            if session.state != "completed":
                err_msg = session.failure_reason or f"review session {session.state}"
                self._finish_harness_terminal(
                    review_id, project_id, task_id, source_request_id, "failed", err_msg,
                    binding=binding,
                )
                return

            review_result = harness.result(session.session_id)

        except Exception as exc:
            if self.accounting is not None:
                self.accounting.end_interval(
                    "technical_review",
                    review_id,
                    outcome="failed",
                    project_id=project_id,
                    task_id=task_id,
                    role="reviewer",
                    request_id=review_id,
                    stage_run_id="review",
                    role_run_id=record.get("role_run_id") or f"reviewer-{review_id}",
                    source_request_id=source_request_id or None,
                    attempt_id=source_request_id or None,
                )
            self._finish_harness_terminal(
                review_id, project_id, task_id, source_request_id, "failed", f"reviewer harness error: {exc}",
                binding=binding,
            )
            return

        # Post-execution verification:
        # 1. Repository truth check
        current_truth = read_repository_truth(repo_path)
        with self._lock:
            rec = self._load_state()["reviews"].get(review_id, {})
        if (
            not current_truth.valid
            or current_truth.branch != rec.get("branch")
            or current_truth.head != rec.get("head")
            or current_truth.status_hash != rec.get("review_status_hash")
        ):
            self._finish_harness_terminal(
                review_id, project_id, task_id, source_request_id, "failed", "repository changed during review",
                binding=binding,
            )
            return

        # 2. Independent gates check
        required_gates = harness_cfg.get("independent_gates") or []
        gates_ok, gate_err = self._check_independent_gates(worker, required_gates)
        if not gates_ok:
            self._finish_harness_terminal(
                review_id, project_id, task_id, source_request_id, "failed", f"independent gate failed: {gate_err}",
                binding=binding,
            )
            return

        # 3. Coverage completeness check
        if review_result.completeness != "complete":
            self._finish_harness_terminal(
                review_id, project_id, task_id, source_request_id, "failed", f"review coverage incomplete: {review_result.completeness}",
                binding=binding,
            )
            return

        # 4. Findings classification
        blocking_severities = set(harness_cfg.get("blocking_severities") or ["blocking"])
        blocking_findings = [f for f in review_result.findings if f.severity in blocking_severities]

        if blocking_findings:
            decision = "remediate"
            next_action = "continue_current_stage"
            disposition = "remediate"
            reason = f"{len(blocking_findings)} blocking finding(s) detected: " + "; ".join(
                f"{f.rule_id} at {f.file}:{f.start_line}" for f in blocking_findings[:3]
            )
        else:
            decision = "next"
            next_action = "next_task"
            disposition = "apply"
            reason = f"Review accepted clean ({len(review_result.findings)} non-blocking finding(s))"

        try:
            self._write_decision(
                review_id=review_id,
                project_id=project_id,
                task_id=task_id,
                branch=str(rec.get("branch") or ""),
                head=str(rec.get("head") or ""),
                status_hash=str(rec.get("review_status_hash") or ""),
                decision=decision,
                next_action=next_action,
                disposition=disposition,
                reason=reason,
            )
        except Exception as exc:
            self._finish_harness_terminal(
                review_id, project_id, task_id, source_request_id, "failed", f"decision persistence failed: {exc}",
                binding=binding,
            )
            return

        if self.accounting is not None:
            self.accounting.end_interval(
                "technical_review",
                review_id,
                outcome="accepted" if decision == "next" else "failed",
                project_id=project_id,
                task_id=task_id,
                role="reviewer",
                request_id=review_id,
                stage_run_id="review",
                role_run_id=record.get("role_run_id") or f"reviewer-{review_id}",
                source_request_id=source_request_id or None,
                attempt_id=source_request_id or None,
            )
            self.accounting.record_attempt_outcome(
                source_request_id,
                "accepted" if decision == "next" else "rejected",
                project_id=project_id,
                task_id=task_id,
                role="reviewer",
                request_id=review_id,
                source_request_id=source_request_id,
                metadata={"review_kind": "technical_harness", "decision": decision, "reason": reason},
            )

        self._finish_harness_terminal(
            review_id, project_id, task_id, source_request_id, "completed", reason,
            decision=decision, next_action=next_action, binding=binding,
            extra={
                "disposition": disposition,
                "completeness": review_result.completeness,
                "findings_count": len(review_result.findings),
                "blocking_count": len(blocking_findings),
            }
        )

    @staticmethod
    def _check_independent_gates(worker: dict[str, Any], required_gates: list[str]) -> tuple[bool, str]:
        if not required_gates:
            return True, ""
        gates_dict = worker.get("independent_gates") or worker.get("gates") or {}
        if not isinstance(gates_dict, dict):
            gates_dict = {}
        for gate in required_gates:
            val = gates_dict.get(gate) if gate in gates_dict else worker.get(gate)
            if isinstance(val, dict):
                st = val.get("status")
            elif isinstance(val, bool):
                st = "passed" if val else "failed"
            elif isinstance(val, str):
                st = val
            else:
                st = None
            if st not in ("passed", "success", "ok"):
                return False, f"gate {gate!r} status is {st!r}, expected 'passed'"
        return True, ""

    def _finish_harness_terminal(
        self,
        review_id: str,
        project_id: str,
        task_id: str,
        source_request_id: str,
        state_name: str,
        reason: str,
        *,
        decision: Optional[str] = None,
        next_action: Optional[str] = None,
        binding: Optional[dict[str, Any]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if isinstance(record, dict):
                record.update({
                    "state": state_name,
                    "reason": reason,
                    "completed_at": utc_now_iso(),
                })
                if decision:
                    record["decision"] = decision
                if next_action:
                    record["next_action"] = next_action
                if extra:
                    record.update(extra)
                self._save_state(state)

        if self.progress_channel is not None:
            proj_payload = {"project_id": project_id}
            if binding:
                proj_payload["conversation_binding"] = binding
            if state_name == "completed":
                if decision == "next":
                    self.progress_channel.emit(
                        proj_payload, "REVIEW_ACCEPTED",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
                elif decision == "remediate":
                    self.progress_channel.emit(
                        proj_payload, "REMEDIATE",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
            elif state_name == "failed":
                self.progress_channel.emit(
                    proj_payload, "REVIEW_FAILED",
                    task_id=task_id, occurrence_key=review_id,
                    details={"reason": reason},
                )

    def reconcile_session(self, session_id: str) -> Any:
        """Reconcile a review session via the harness."""
        harness = self._get_harness()
        return harness.reconcile(session_id)

    def get_review_session(self, session_id: str) -> Any:
        """Query a review session status via the harness."""
        harness = self._get_harness()
        return harness.status(session_id)

    def _launch_review(
        self, review_id: str, source_request_id: str, request: AIRoleRequest, truth: Any,
        conversation_binding: Optional[dict[str, Any]] = None,
        resolution: Optional[Any] = None,
    ) -> None:
        if conversation_binding is None and isinstance(request.metadata, dict):
            conversation_binding = request.metadata.get("conversation_binding")
        if conversation_binding is None:
            conversation_binding = self._project_bindings.get(request.project_id)
        with self._lock:
            state = self._load_state()
            if review_id in state["reviews"]:
                return
            state["reviews"][review_id] = {
                "review_id": review_id,
                "project_id": request.project_id,
                "source_request_id": source_request_id,
                "task_id": request.task_run_id,
                "branch": truth.branch,
                "head": truth.head,
                "review_status_hash": truth.status_hash,
                "review_dirty": bool(truth.dirty),
                "state": "launching",
                "started_at": utc_now_iso(),
                "role_run_id": request.role_run_id,
                "conversation_binding": copy.deepcopy(conversation_binding) if isinstance(conversation_binding, dict) else None,
                "context_state": resolution.state if resolution else None,
                "context_digest": resolution.document.digest if resolution and resolution.document else None,
                "reconcile_of": request.metadata.get("reconcile_of") if isinstance(request.metadata, dict) else None,
                "retry_of": request.metadata.get("retry_of") if isinstance(request.metadata, dict) else None,
                "rereview_of": request.metadata.get("rereview_of") if isinstance(request.metadata, dict) else None,
            }
            if conversation_binding and isinstance(conversation_binding, dict):
                self._project_bindings[request.project_id] = copy.deepcopy(conversation_binding)
            self._save_state(state)
        thread = threading.Thread(
            target=self._run_review,
            args=(review_id, request),
            name="devorch-review-" + request.project_id,
            daemon=True,
        )
        with self._lock:
            self._threads[review_id] = thread
        thread.start()
        if self.progress_channel is not None:
            payload = {"project_id": request.project_id}
            if conversation_binding:
                payload["conversation_binding"] = conversation_binding
            self.progress_channel.emit(
                payload, "REVIEW_STARTED",
                task_id=request.task_run_id, occurrence_key=review_id,
                details={"review_id": review_id, "worker_source_request_id": source_request_id},
            )

    def _record_terminal(self, review_id: str, project_id: str, source_request_id: str, state_name: str, reason: str) -> None:
        with self._lock:
            state = self._load_state()
            state["reviews"].setdefault(review_id, {
                "review_id": review_id, "project_id": project_id,
                "source_request_id": source_request_id, "started_at": utc_now_iso(),
            })
            state["reviews"][review_id].update({
                "state": state_name, "reason": reason, "completed_at": utc_now_iso(),
            })
            self._save_state(state)

    def _run_review(self, review_id: str, request: AIRoleRequest) -> None:
        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if not isinstance(record, dict):
                return
            record["state"] = "running"
            self._save_state(state)
        source_request_id = str(request.metadata.get("worker_source_request_id") or "")
        review_started = time.monotonic()
        if self.accounting is not None:
            self.accounting.start_interval(
                "technical_review",
                review_id,
                project_id=request.project_id,
                task_id=request.task_run_id,
                role="reviewer",
                request_id=review_id,
                stage_run_id=request.stage_run_id,
                role_run_id=request.role_run_id,
                source_request_id=source_request_id or None,
                attempt_id=source_request_id or None,
            )
        result = None
        try:
            result = self.port.execute(request) if self.port is not None else None
            if result is None:
                raise RuntimeError("AIBroker reviewer port unavailable")
        except Exception as exc:
            if self.accounting is not None:
                self.accounting.end_interval(
                    "technical_review",
                    review_id,
                    outcome="failed",
                    project_id=request.project_id,
                    task_id=request.task_run_id,
                    role="reviewer",
                    request_id=review_id,
                    stage_run_id=request.stage_run_id,
                    role_run_id=request.role_run_id,
                    source_request_id=source_request_id or None,
                    attempt_id=source_request_id or None,
                )
            if self.failure_memory is not None:
                self.failure_memory.record_matching_recurrences(
                    request.metadata.get("failure_environment", {}),
                    str(exc),
                    time.monotonic() - review_started,
                    project_id=request.project_id,
                    task_id=request.task_run_id,
                    role="reviewer",
                    request_id=review_id,
                    source_request_id=source_request_id or None,
                )
            self._record_terminal(review_id, request.project_id, source_request_id, "failed", f"reviewer lifecycle error: {exc}")
            return
        if self.accounting is not None:
            resource = result.resource_context
            self.accounting.end_interval(
                "technical_review",
                review_id,
                outcome="accepted" if result.status == "succeeded" else "failed",
                project_id=request.project_id,
                task_id=request.task_run_id,
                role="reviewer",
                request_id=review_id,
                stage_run_id=request.stage_run_id,
                role_run_id=request.role_run_id,
                source_request_id=source_request_id or None,
                attempt_id=source_request_id or None,
                dispatch_id=result.dispatch_id,
                decision_id=result.decision_id,
                execution_id=result.execution_id,
                session_id=result.session_id,
                resource_id=resource.resource_id if resource else None,
                provider=resource.provider if resource else None,
                account=resource.account if resource else None,
                model=resource.model if resource else None,
            )
        if result.status != "succeeded":
            if self.failure_memory is not None:
                self.failure_memory.record_matching_recurrences(
                    request.metadata.get("failure_environment", {}),
                    str(result.error or ""),
                    time.monotonic() - review_started,
                    project_id=request.project_id,
                    task_id=request.task_run_id,
                    role="reviewer",
                    request_id=review_id,
                    source_request_id=source_request_id or None,
                )
            self._finish_result(review_id, result, "failed", result.error or f"reviewer_{result.status}")
            return
        try:
            decision, next_action, reason = _parse_review_output(result.output)
        except ValueError as exc:
            self._finish_result(review_id, result, "failed", str(exc))
            return
        with self._lock:
            record = self._load_state()["reviews"].get(review_id, {})
        repo = read_repository_truth(request.working_directory)
        if (
            not repo.valid
            or repo.branch != record.get("branch")
            or repo.head != record.get("head")
            or repo.status_hash != record.get("review_status_hash")
        ):
            self._finish_result(review_id, result, "failed", "repository changed during review")
            return
        disposition = "apply" if decision in ("next", "remediate") else decision
        try:
            self._write_decision(
                review_id=review_id,
                project_id=request.project_id,
                task_id=request.task_run_id,
                branch=str(record.get("branch") or ""),
                head=str(record.get("head") or ""),
                status_hash=str(record.get("review_status_hash") or ""),
                decision=decision,
                next_action=next_action,
                disposition=disposition,
                reason=reason,
            )
        except Exception as exc:
            self._finish_result(review_id, result, "failed", f"decision persistence failed: {exc}")
            return
        if self.accounting is not None and decision in {"next", "remediate", "stop"}:
            self.accounting.record_attempt_outcome(
                source_request_id,
                "accepted" if decision == "next" else "rejected",
                project_id=request.project_id,
                task_id=request.task_run_id,
                role="reviewer",
                request_id=review_id,
                source_request_id=source_request_id,
                dispatch_id=result.dispatch_id,
                decision_id=result.decision_id,
                execution_id=result.execution_id,
                session_id=result.session_id,
                metadata={"review_kind": "technical", "decision": decision, "reason": reason},
            )
        if self.accounting is not None and decision == "owner_gate":
            self.accounting.open_owner_gate(
                review_id,
                project_id=request.project_id,
                task_id=request.task_run_id,
                role="owner",
                request_id=review_id,
                source_request_id=source_request_id or None,
            )
        self._finish_result(review_id, result, "completed", reason, decision=decision, next_action=next_action)

    def _finish_result(self, review_id: str, result: Any, state_name: str, reason: str, **extra: Any) -> None:
        resource = result.resource_context
        resource_payload = None
        if resource is not None:
            resource_payload = {
                "resource_id": resource.resource_id, "provider": resource.provider,
                "account": resource.account, "model": resource.model,
            }
        with self._lock:
            state = self._load_state()
            record = state["reviews"].get(review_id)
            if not isinstance(record, dict):
                return
            record.update({
                "state": state_name, "reason": reason, "completed_at": utc_now_iso(),
                "dispatch_id": result.dispatch_id, "decision_id": result.decision_id,
                "execution_id": result.execution_id, "session_id": result.session_id,
                "resource_context": resource_payload, "usage_source": result.usage_source,
                **extra,
            })
            self._save_state(state)
        if self.progress_channel is not None:
            decision = extra.get("decision")
            task_id = str(record.get("task_id") or "")
            proj_id = str(record.get("project_id") or "")
            binding = record.get("conversation_binding")
            if not binding:
                binding = self._project_bindings.get(proj_id)
            project_payload = {"project_id": proj_id}
            if binding:
                project_payload["conversation_binding"] = binding

            if state_name == "completed":
                if decision == "next":
                    self.progress_channel.emit(
                        project_payload, "REVIEW_ACCEPTED",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
                elif decision == "remediate":
                    self.progress_channel.emit(
                        project_payload, "REMEDIATE",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
                elif decision == "owner_gate":
                    self.progress_channel.emit(
                        project_payload, "OWNER_GATE",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
            elif state_name == "failed":
                self.progress_channel.emit(
                    project_payload, "REVIEW_FAILED",
                    task_id=task_id, occurrence_key=review_id,
                    details={"reason": reason},
                )

    def _write_decision(
        self,
        *,
        review_id: str,
        project_id: str,
        task_id: str,
        branch: str,
        head: str,
        status_hash: str,
        decision: str,
        next_action: str,
        disposition: str,
        reason: str,
    ) -> None:
        with self._lock:
            raw = read_json(self.decisions_path, None)
            decisions = raw.get("decisions") if isinstance(raw, dict) else None
            if not isinstance(decisions, dict):
                decisions = {}
            existing = decisions.get(review_id)
            record = {
                "project_id": project_id,
                "request_id": review_id,
                "disposition": disposition,
                "next_action": next_action,
                "decision": decision,
                "reason": reason,
                "review_status_hash": status_hash,
                "task_id": task_id,
                "stage_id": "review",
                "branch": branch,
                "head": head,
                "role": "reviewer",
                "event": "worker_done",
                "source": "aibroker",
                "consumed_at": utc_now_iso(),
            }
            if existing is not None and existing != record:
                raise RuntimeError("conflicting direct reviewer decision replay")
            decisions[review_id] = record
            write_json(
                self.decisions_path,
                {"version": _DECISION_VERSION, "decisions": decisions},
                indent=2,
            )
