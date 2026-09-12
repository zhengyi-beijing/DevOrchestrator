"""AIBroker planner + independent plan-review lifecycle."""
from __future__ import annotations

import copy
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

from dev_orchestrator.ai.contracts import AIRoleRequest, ResourceContext
from dev_orchestrator.ai.execution_port import AIExecutionPort
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

PLANNER_STATE_FILE = "ai-planner.json"
_STATE_VERSION = 1
_ACTIVE_STATES = frozenset({"planning", "reviewing", "applying"})


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
    if quality not in {"economy", "balanced", "high"}:
        return None, "planner quality invalid"
    if review_quality not in {"economy", "balanced", "high"}:
        return None, "plan reviewer quality invalid"
    if review_independence not in {"resource", "account", "provider"}:
        return None, "plan reviewer independence invalid"
    for name, value in (("timeout_seconds", timeout), ("review_timeout_seconds", review_timeout)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return None, f"planner {name} must be positive"
    return {
        "quality": quality,
        "review_quality": review_quality,
        "review_independence": review_independence,
        "timeout_seconds": float(timeout),
        "review_timeout_seconds": float(review_timeout),
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
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.progress_channel = progress_channel
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
        from dev_orchestrator.core.project_context import context_prompt_block
        context_block, context_resolution = context_prompt_block(project, "planner")
        ctx_decl = project.get("project_context") or {}
        if ctx_decl.get("enabled") and ctx_decl.get("require_valid", True) and context_resolution.state == "invalid":
            return None, f"durable project context is invalid: {context_resolution.reason}"
        plan_id = "ai_plan:" + command_id
        binding = project.get("conversation_binding")
        with self._lock:
            state = self._load_state()
            if plan_id in state["plans"]:
                return plan_id, "planner lifecycle already exists"
            state["plans"][plan_id] = {
                "plan_id": plan_id,
                "command_id": command_id,
                "project_id": project_id,
                "task_id": task_id,
                "repo_path": repo_text,
                "branch": truth.branch,
                "head": truth.head,
                "status_hash": truth.status_hash,
                "next_text": next_text,
                "state": "planning",
                "started_at": utc_now_iso(),
                "conversation_binding": copy.deepcopy(binding) if isinstance(binding, dict) else None,
                "context_block": context_block,
                "context_state": context_resolution.state,
                "context_digest": context_resolution.document.digest if context_resolution.document else None,
                "context_sources": copy.deepcopy(context_resolution.sources),
            }
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
    def _run_cycle(self, plan_id: str, project: dict[str, Any], policy: dict[str, Any]) -> None:
        with self._lock:
            record = copy.deepcopy(self._load_state()["plans"].get(plan_id, {}))
        if not record:
            return
        if self.progress_channel is not None:
            self.progress_channel.emit(
                project, "PLAN_STARTED",
                task_id=record["task_id"], occurrence_key=plan_id,
                details={"plan_id": plan_id},
            )
        planner_request = AIRoleRequest(
            project_id=record["project_id"],
            task_run_id=record["task_id"],
            stage_run_id="plan",
            role_run_id="planner-" + record["command_id"],
            request_id=plan_id + ":planner",
            role="planner",
            prompt=self._planner_prompt(record),
            working_directory=Path(record["repo_path"]),
            quality=policy["quality"],
            independence="none",
            timeout_seconds=policy["timeout_seconds"],
            metadata={"control_command_id": record["command_id"]},
        )
        try:
            planner_result = self.port.execute(planner_request) if self.port is not None else None
            if planner_result is None:
                raise RuntimeError("planner execution port unavailable")
            if planner_result.status != "succeeded":
                raise RuntimeError(planner_result.error or f"planner_{planner_result.status}")
            plan = _parse_plan(planner_result.output, record["task_id"])
        except Exception as exc:
            self._finish(plan_id, "failed", f"planner failed: {exc}")
            return
        previous = planner_result.resource_context
        if previous is None:
            self._finish(plan_id, "failed", "planner resource context missing")
            return
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
                "planner_completed_at": utc_now_iso(),
            })
            self._save_state(state)
        review_request = AIRoleRequest(
            project_id=record["project_id"],
            task_run_id=record["task_id"],
            stage_run_id="plan_review",
            role_run_id="plan-reviewer-" + record["command_id"],
            request_id=plan_id + ":reviewer",
            role="reviewer",
            prompt=self._review_prompt(record, plan),
            working_directory=Path(record["repo_path"]),
            quality=policy["review_quality"],
            independence=policy["review_independence"],
            previous_resource_context=previous,
            timeout_seconds=policy["review_timeout_seconds"],
            metadata={"control_command_id": record["command_id"], "review_kind": "plan"},
        )
        try:
            review_result = self.port.execute(review_request) if self.port is not None else None
            if review_result is None:
                raise RuntimeError("plan reviewer execution port unavailable")
            if review_result.status != "succeeded":
                raise RuntimeError(review_result.error or f"plan_reviewer_{review_result.status}")
            decision, reason = _parse_plan_review(review_result.output)
        except Exception as exc:
            self._finish(plan_id, "failed", f"plan review failed: {exc}")
            return
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
                "review_completed_at": utc_now_iso(),
            })
            self._save_state(state)
        if decision == "owner_gate":
            self._finish(plan_id, "owner_gate", reason)
            if self.progress_channel is not None:
                self.progress_channel.emit(
                    project, "OWNER_GATE",
                    task_id=record["task_id"], occurrence_key=plan_id,
                    details={"plan_id": plan_id, "reason": reason},
                )
            return
        if decision != "approve":
            self._finish(plan_id, "failed", "plan rejected: " + reason)
            return
        self._apply_plan(plan_id, record, plan, reason, project=project)

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
            binding = (
                record.get("conversation_binding")
                or (project.get("conversation_binding") if isinstance(project, dict) else None)
                or self._project_bindings.get(record["project_id"])
            )
            project_payload = {"project_id": record["project_id"]}
            if binding:
                project_payload["conversation_binding"] = binding
            if isinstance(project, dict):
                if project.get("progress_channel") is not None:
                    project_payload["progress_channel"] = project["progress_channel"]
                if project.get("progress_level") is not None:
                    project_payload["progress_level"] = project["progress_level"]
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
    @staticmethod
    def _planner_prompt(record: dict[str, Any]) -> str:
        prompt = (
            "You are the software-development Planner for one task. Plan only: do not modify files, commit, push, or run another agent. "
            "Inspect the repository and the supplied agent/next.md task. Convert the pending design into a bounded executable implementation plan. "
            "Return exactly one JSON object and no markdown or extra text with exactly these keys: "
            '{"task_id":"...","summary":"...","implementation_steps":["..."],"interfaces":["..."],"validation":["..."],"risks":["..."],"out_of_scope":["..."]}. '
            "Every list must be non-empty. Keep the implementation bounded to the current task and preserve existing acceptance intent.\n\n"
            f"Project: {record['project_id']}\nTask: {record['task_id']}\n"
            f"Planning branch: {record['branch']}\nPlanning HEAD: {record['head']}\n\n"
        )
        if record.get("context_block"):
            prompt += f"{record['context_block']}\n\n"
        prompt += (
            "Current agent/next.md:\n---BEGIN NEXT---\n"
            + record["next_text"]
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
            f"Project: {record['project_id']}\nTask: {record['task_id']}\n\n"
        )
        if record.get("context_block"):
            prompt += f"{record['context_block']}\n\n"
        else:
            prompt = prompt[:-1]
        prompt += (
            "Original task:\n---BEGIN NEXT---\n" + record["next_text"] + "\n---END NEXT---\n\n"
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
