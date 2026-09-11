"""Direct AIBroker reviewer path, independent of browser conversations."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from dev_orchestrator.ai.contracts import AIRoleRequest, ResourceContext
from dev_orchestrator.ai.execution_port import AIExecutionPort
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.core.repository import read_repository_truth
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
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.progress_channel = progress_channel
        self.state_path = self.runtime_root / REVIEWER_STATE_FILE
        self.decisions_path = self.runtime_root / REVIEW_DECISIONS_FILE
        self.transition_path = self.runtime_root / "transition-executor.json"
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._recover_interrupted()

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
        if self.port is None:
            return []
        config = load_projects_config(config_path)
        projects = self._project_map(config)
        transition_records = self._transition_records()
        latest: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
        for source_request_id, worker in transition_records.items():
            if worker.get("engine") != "aibroker" or worker.get("state") != "completed":
                continue
            project_id = _nonblank(worker.get("project_id"))
            if project_id is None or project_id not in projects:
                continue
            policy, _ = _review_policy(projects[project_id])
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
            resource = worker.get("resource_context")
            if not isinstance(resource, dict):
                self._record_terminal(review_id, project_id, source_request_id, "failed", "worker resource context missing")
                continue
            previous = ResourceContext(
                resource.get("resource_id"), resource.get("provider"),
                resource.get("account"), resource.get("model"),
            )
            repo_path = _nonblank(worker.get("repo_path"))
            task_id = _nonblank(worker.get("task_id"))
            if repo_path is None or task_id is None:
                self._record_terminal(review_id, project_id, source_request_id, "failed", "worker identity incomplete")
                continue
            truth = read_repository_truth(repo_path)
            if not truth.valid:
                self._record_terminal(review_id, project_id, source_request_id, "failed", "repository truth unavailable")
                continue
            prompt = self._review_prompt(project_id, task_id, source_request_id, truth)
            request = AIRoleRequest(
                project_id=project_id, task_run_id=task_id, stage_run_id="review",
                role_run_id="reviewer-" + source_request_id.replace(":", "-"),
                request_id=review_id, role="reviewer", prompt=prompt,
                working_directory=Path(repo_path), quality=policy["quality"],
                independence=policy["independence"], previous_resource_context=previous,
                timeout_seconds=policy["timeout_seconds"],
                metadata={"worker_source_request_id": source_request_id},
            )
            self._launch_review(review_id, source_request_id, request, truth)
            launched.append(review_id)
        return launched

    @staticmethod
    def _review_prompt(project_id: str, task_id: str, source_request_id: str, truth: Any) -> str:
        return (
            "You are the independent reviewer for a completed software-development Worker. "
            "Review only; do not modify files, commit, push, or start another Worker. "
            "Inspect the repository, current diff/status, task evidence, tests, and acceptance criteria. "
            "Return exactly one JSON object and no markdown or extra text, with exactly these keys: "
            '{"decision":"next|remediate|owner_gate|stop","next_action":"next_task|continue_current_stage|stop","reason":"..."}. '
            "Allowed pairs are next/next_task, remediate/continue_current_stage, owner_gate/stop, stop/stop. "
            "Use next only when the reviewed task is actually complete and repository evidence supports advancing. "
            "Use remediate for bounded fixable gaps in this reviewed task; owner_gate only when owner input is genuinely required.\n\n"
            f"Project: {project_id}\nReviewed task: {task_id}\nWorker source: {source_request_id}\n"
            f"Review branch: {truth.branch}\nReview HEAD: {truth.head}\nReview dirty: {truth.dirty}\n"
        )

    def _launch_review(self, review_id: str, source_request_id: str, request: AIRoleRequest, truth: Any) -> None:
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
            }
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
            self.progress_channel.emit(
                {"project_id": request.project_id}, "REVIEW_STARTED",
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
        try:
            result = self.port.execute(request) if self.port is not None else None
            if result is None:
                raise RuntimeError("AIBroker reviewer port unavailable")
        except Exception as exc:
            self._record_terminal(review_id, request.project_id, str(request.metadata.get("worker_source_request_id") or ""), "failed", f"reviewer lifecycle error: {exc}")
            return
        if result.status != "succeeded":
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
            if state_name == "completed":
                if decision == "next":
                    self.progress_channel.emit(
                        {"project_id": proj_id}, "REVIEW_ACCEPTED",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
                elif decision == "remediate":
                    self.progress_channel.emit(
                        {"project_id": proj_id}, "REMEDIATE",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
                elif decision == "owner_gate":
                    self.progress_channel.emit(
                        {"project_id": proj_id}, "OWNER_GATE",
                        task_id=task_id, occurrence_key=review_id,
                        details={"decision": decision, "reason": reason},
                    )
            elif state_name == "failed":
                self.progress_channel.emit(
                    {"project_id": proj_id}, "REVIEW_FAILED",
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
