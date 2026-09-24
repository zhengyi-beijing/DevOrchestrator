"""Golden-Path lifecycle test harness for DevOrchestrator.

Provides test scaffolding and simulation helpers for driving a task from
PENDING_DESIGN to DONE under owner authorization and structured readiness authority.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult, ResourceContext
from dev_orchestrator.control.surface import project_control_view, project_identity
from dev_orchestrator.core.activation_supervisor import ActivationSupervisor
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.core.execution_context import (
    get_context,
    load_execution_contexts,
    record_role_completion,
    resolve_next_action,
    update_context,
)
from dev_orchestrator.core.execution_intent import (
    get_active_intent,
    load_execution_intents,
    record_or_refresh_intent,
)
from dev_orchestrator.core.readiness import (
    resolve_readiness,
    resolve_task_state,
    write_task_execution_state,
)
from dev_orchestrator.core.task_status import (
    parse_task_status,
    render_status_line,
    require_single_status_line,
)
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json


def make_test_repo(path: Path, *, task_id: str = "P1", status: str = "PENDING DESIGN") -> Path:
    """Create a minimal Git repository initialized with agent/next.md."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)

    agent_dir = path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    next_md = agent_dir / "next.md"
    next_md.write_text(
        f"# Task {task_id}: Feature Implementation\n\n"
        f"Status: **{status}**\n\n"
        f"## Requirements\nImplement the feature cleanly.\n",
        encoding="utf-8",
    )

    tests_dir = path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_dummy.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"initial task {task_id}"], cwd=path, check=True, capture_output=True)
    return path


class HarnessFakeExecutor:
    """Fake transition executor tracking started control executions."""

    def __init__(self, launch: bool = True) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.launch = launch
        self.records: dict[str, Any] = {}

    def start_control(self, project: dict[str, Any], snapshot: dict[str, Any], command_id: str) -> Optional[Any]:
        self.calls.append((project.get("project_id", ""), snapshot.get("state", ""), command_id))
        if self.launch:
            telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
            task_id = telemetry.get("task_id") or "P1"
            rec = {
                "source_request_id": command_id,
                "project_id": project.get("project_id"),
                "task_id": task_id,
                "backend_id": "harness_worker",
                "state": "running",
                "started_at": utc_now_iso(),
            }
            self.records[command_id] = rec
            return SimpleNamespace(task_id=task_id, backend_id="harness_worker")
        self.records[command_id] = {"state": "blocked", "reason": "execution launch disabled"}
        return None

    def state(self) -> dict[str, Any]:
        return {"executions": self.records}


class HarnessFakePort:
    """Scripted AIExecutionPort fake returning valid plan and review responses."""

    def __init__(self) -> None:
        self.requests: list[AIRoleRequest] = []
        self._scripted: dict[str, list[Any]] = {}

    def script_role_response(self, role: str, result_or_exc_or_callable: Any) -> None:
        """Enqueue a scripted response, exception, or callable for a specific role."""
        self._scripted.setdefault(role, []).append(result_or_exc_or_callable)

    def script_role_fault(self, role: str, error_message: str = "quota exhausted") -> None:
        """Enqueue a resource fault response triggering failover."""
        def _fault(req: AIRoleRequest) -> AIRoleResult:
            res = req.previous_resource_context or ResourceContext("res-fault-1", "p-fault", "a-fault", "m-fault")
            return AIRoleResult(
                request_id=req.request_id,
                role_run_id=req.role_run_id,
                status="failed",
                error=error_message,
                dispatch_id="d-fault",
                decision_id="dec-fault",
                execution_id="e-fault",
                resource_context=res,
            )
        self.script_role_response(role, _fault)

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        self.requests.append(request)
        if self._scripted.get(request.role):
            item = self._scripted[request.role].pop(0)
            if callable(item):
                return item(request)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, AIRoleResult):
                return item

        if request.role == "planner":
            payload = {
                "task_id": request.task_run_id,
                "summary": "Freeze a bounded implementation plan.",
                "implementation_steps": ["Add the interface seam", "Implement the bounded backend change"],
                "interfaces": ["Keep the existing public ABI stable"],
                "validation": ["Run the focused tests", "Run the full regression"],
                "risks": ["Do not expand scope into unrelated backends"],
                "out_of_scope": ["No unrelated refactor"],
            }
            resource = ResourceContext("planner-r", "p1", "a1", "m1")
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps(payload),
                dispatch_id="dispatch-plan",
                decision_id="decision-plan",
                execution_id="execution-plan",
                resource_context=resource,
            )
        if request.stage_run_id == "plan_review":
            resource = ResourceContext("plan-reviewer-r", "p2", "a2", "m2")
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=json.dumps({"decision": "approve", "reason": "bounded and testable"}),
                dispatch_id="dispatch-plan-review",
                decision_id="decision-plan-review",
                execution_id="execution-plan-review",
                resource_context=resource,
            )
        resource = ResourceContext("review-r", "p2", "a2", "m2")
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps({
                "decision": "next",
                "next_action": "next_task",
                "reason": "bounded and testable",
            }),
            dispatch_id="dispatch-review",
            decision_id="decision-review",
            execution_id="execution-review",
            resource_context=resource,
        )


@dataclass
class GoldenPathHarness:
    """Scaffolding environment for Golden-Path lifecycle execution."""

    root: Path
    repo_path: Path
    runtime_path: Path
    config_path: Path
    project_id: str
    task_id: str
    executor: HarnessFakeExecutor
    coordinator: ControlCommandCoordinator
    supervisor: ActivationSupervisor
    port: HarnessFakePort
    planner: AIPlannerCoordinator
    reviewer: AIReviewerCoordinator

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        project_id: str = "golden-proj",
        task_id: str = "P1",
        initial_status: str = "PENDING DESIGN",
    ) -> GoldenPathHarness:
        repo = make_test_repo(root / "repo", task_id=task_id, status=initial_status)
        runtime = root / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        config_dir = root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = config_dir / "projects.json"

        pdef = {
            "project_id": project_id,
            "repo_path": str(repo),
            "execution": {
                "enabled": True,
                "owner_authorized": True,
                "engine": "aibroker",
                "allowed_next_actions": ["continue_current_stage"],
                "preferred_backends": ["agy"],
                "backends": {"agy": {"executable": "agy.cmd"}},
            },
            "ai_roles": {
                "planner": {"enabled": True},
                "reviewer": {"enabled": True},
            },
        }
        cfg_path.write_text(json.dumps({"projects": [pdef]}, indent=2), encoding="utf-8")

        port = HarnessFakePort()
        planner = AIPlannerCoordinator(runtime, port)
        reviewer = AIReviewerCoordinator(runtime, port)
        executor = HarnessFakeExecutor(launch=True)
        coordinator = ControlCommandCoordinator(runtime, planner, reviewer=reviewer)
        supervisor = ActivationSupervisor(runtime, controls=coordinator, executor=executor)

        return cls(
            root=root,
            repo_path=repo,
            runtime_path=runtime,
            config_path=cfg_path,
            project_id=project_id,
            task_id=task_id,
            executor=executor,
            coordinator=coordinator,
            supervisor=supervisor,
            port=port,
            planner=planner,
            reviewer=reviewer,
        )

    def current_snapshot(self) -> dict[str, Any]:
        git_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_path, check=True, capture_output=True, text=True
        ).stdout.strip()
        next_text = (self.repo_path / "agent" / "next.md").read_text(encoding="utf-8")
        _, raw_status = require_single_status_line(next_text)

        readiness_res = resolve_readiness(self.repo_path, current_task_id=self.task_id, next_status=raw_status)
        state_res = resolve_task_state(
            {"project_id": self.project_id},
            {"readiness": readiness_res.to_dict(), "next_status": raw_status},
            self.repo_path,
            runtime_root=self.runtime_path,
            current_task_id=self.task_id,
        )

        base_state = "READY_TO_RUN" if state_res.is_ready_to_run() else ("DONE" if state_res.is_completed() else "IDLE")

        snap: dict[str, Any] = {
            "project_id": self.project_id,
            "repo_path": str(self.repo_path),
            "state": base_state,
            "next_status": raw_status,
            "telemetry": {"task_id": self.task_id},
            "git": {"head": git_head, "branch": "main", "dirty": False},
            "readiness": readiness_res.to_dict(),
        }

        # Overlay active or completed worker execution from records if not already completed
        for rec in reversed(list(self.executor.records.values())):
            if isinstance(rec, dict) and rec.get("project_id") == self.project_id:
                exec_state = rec.get("state")
                if exec_state == "running":
                    snap["state"] = "WORKER_RUNNING"
                elif exec_state == "completed" and not state_res.is_completed():
                    snap["state"] = "WAITING_REVIEW"
                snap["worker"] = {
                    "kind": "task",
                    "state": exec_state,
                    "process_alive": exec_state == "running",
                    "exit_code": rec.get("exit_code"),
                    "updated_at": rec.get("completed_at") or rec.get("started_at"),
                }
                break

        return snap

    def submit_continue(self, command_id: Optional[str] = None) -> dict[str, Any]:
        cid = command_id or f"cmd-cont-{self.task_id}"
        snap = self.current_snapshot()
        identity = project_identity(snap, self.runtime_path)
        submit_control_command(
            self.runtime_path,
            project_id=self.project_id,
            action="continue",
            command_id=cid,
            expected=identity,
            source="owner",
        )
        record_or_refresh_intent(
            self.runtime_path,
            self.project_id,
            task_id=self.task_id,
            command_id=cid,
            requested_action="continue",
            source="owner",
            state="active",
        )
        summary = {"projects": [snap]}
        self.coordinator.advance(self.config_path, summary, self.executor)
        return read_json(self.runtime_path / "control" / "history" / f"{cid}.json", {})

    def apply_approved_plan(self, plan_id: Optional[str] = None, timeout: float = 5.0) -> None:
        """Wait for planner to approve and freeze plan -> ready_to_run."""
        pid = plan_id or f"ai_plan:cmd-start-1"
        deadline = time.time() + timeout
        if self.planner is not None:
            while time.time() < deadline:
                plans = self.planner.state().get("plans", {})
                row = plans.get(pid)
                if row is None and len(plans) == 1:
                    row = next(iter(plans.values()))
                if row is not None:
                    if row.get("state") == "ready":
                        for th in list(self.planner._threads.values()):
                            th.join(timeout=2.0)
                        return
                    if row.get("state") in {"failed", "rejected"}:
                        for th in list(self.planner._threads.values()):
                            th.join(timeout=2.0)
                        raise RuntimeError(f"planner failed to reach ready state: {row.get('reason')}")
                time.sleep(0.02)
            for th in list(self.planner._threads.values()):
                th.join(timeout=2.0)
            raise RuntimeError(f"timed out waiting for planner to reach ready state: {self.planner.state().get('plans')}")
        raise RuntimeError("no planner configured in harness")

    def complete_worker_run(self, command_id: Optional[str] = None, exit_code: int = 0) -> None:
        """Simulate worker completing execution and record evidence in executor ledger."""
        cid = command_id
        if not cid:
            for k, v in self.executor.records.items():
                if isinstance(v, dict) and v.get("project_id") == self.project_id:
                    cid = k
                    break
        if not cid:
            cid = f"cmd-exec-{self.task_id}"

        git_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_path, check=True, capture_output=True, text=True
        ).stdout.strip()

        found = False
        for k, v in self.executor.records.items():
            if isinstance(v, dict) and v.get("project_id") == self.project_id:
                found = True
                v["state"] = "completed"
                v["completed_at"] = v.get("completed_at") or utc_now_iso()
                v["exit_code"] = exit_code
                v["engine"] = "aibroker"
                v["repo_path"] = str(self.repo_path)
                v["branch"] = "main"
                v["head"] = git_head
                v.setdefault("resource_context", {
                    "resource_id": "worker-r1",
                    "provider": "p1",
                    "account": "a1",
                    "model": "m1",
                })
        if not found:
            self.executor.records[cid] = {
                "source_request_id": cid,
                "project_id": self.project_id,
                "task_id": self.task_id,
                "repo_path": str(self.repo_path),
                "branch": "main",
                "head": git_head,
                "backend_id": "harness_worker",
                "engine": "aibroker",
                "state": "completed",
                "started_at": utc_now_iso(),
                "completed_at": utc_now_iso(),
                "exit_code": exit_code,
                "resource_context": {
                    "resource_id": "worker-r1",
                    "provider": "p1",
                    "account": "a1",
                    "model": "m1",
                },
            }
        write_json(
            self.runtime_path / "transition-executor.json",
            {"version": 1, "executions": self.executor.records},
            indent=2,
        )
        record_role_completion(
            self.runtime_path,
            self.project_id,
            "worker",
            {"task_id": self.task_id, "git_anchor": git_head, "exit_code": exit_code, "command_id": cid},
        )
        update_context(
            self.runtime_path,
            self.project_id,
            task_id=self.task_id,
            git_anchor=git_head,
            disposition="hold",
            next_action="technical_review",
            idle_ticks=0,
        )

    def apply_review_acceptance(self, timeout: float = 5.0) -> None:
        """Trigger reviewer coordinator, wait for reviewer to approve (next), and finalize task."""
        if self.reviewer is None:
            raise RuntimeError("no reviewer configured in harness")

        self.reviewer.advance(self.config_path)

        deadline = time.time() + timeout
        decision_record = None
        while time.time() < deadline:
            decisions_file = self.runtime_path / "review-decisions.json"
            if decisions_file.is_file():
                data = read_json(decisions_file, {})
                decs = data.get("decisions", {})
                proj_decs = [
                    d for d in decs.values()
                    if isinstance(d, dict) and d.get("project_id") == self.project_id
                ]
                if proj_decs:
                    latest = proj_decs[-1]
                    if latest.get("decision") == "next":
                        decision_record = latest
                        break
            if decision_record is not None:
                break
            time.sleep(0.02)

        if decision_record is None:
            raise RuntimeError("timed out waiting for reviewer approval decision in review-decisions.json")

        decision = decision_record.get("decision")
        if decision != "next":
            raise RuntimeError(f"reviewer did not approve task: decision={decision}, reason={decision_record.get('reason')}")

        next_path = self.repo_path / "agent" / "next.md"
        content = next_path.read_text(encoding="utf-8")
        line_idx, _ = require_single_status_line(content)
        lines = content.splitlines()
        lines[line_idx] = render_status_line("completed")
        next_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        write_task_execution_state(
            self.repo_path,
            state="completed",
            task_id=self.task_id,
            source="reviewer",
        )
        subprocess.run(["git", "add", "."], cwd=self.repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"task({self.task_id}): completed and accepted by review"],
            cwd=self.repo_path,
            check=True,
            capture_output=True,
        )

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo_path, check=True, capture_output=True, text=True
        ).stdout.strip()
        update_context(
            self.runtime_path,
            self.project_id,
            task_id=self.task_id,
            git_anchor=head,
            disposition="hold",
            next_action="none",
            idle_ticks=0,
        )
        for th in list(self.reviewer._threads.values()):
            th.join(timeout=2.0)

    def close(self) -> None:
        """Join all active background threads to ensure clean directory removal."""
        if self.planner is not None:
            for th in list(self.planner._threads.values()):
                th.join(timeout=2.0)
        if self.reviewer is not None:
            for th in list(self.reviewer._threads.values()):
                th.join(timeout=2.0)

    def run_ticks(self, n: int = 1) -> list[Any]:
        """Advance the full control plane by n ticks."""
        outcomes: list[Any] = []
        for _ in range(n):
            snap = self.current_snapshot()
            self.coordinator.advance(self.config_path, {"projects": [snap]}, self.executor)
            if self.reviewer is not None:
                self.reviewer.advance(self.config_path)
            res = self.supervisor.advance(self.config_path, {"projects": [snap]}, executor=self.executor)
            outcomes.extend(res)
        return outcomes

    def restart_daemon(self) -> None:
        """Simulate daemon restart by joining active threads and reloading coordinators from disk."""
        if self.planner is not None:
            for th in list(self.planner._threads.values()):
                th.join(timeout=1.0)
        if self.reviewer is not None:
            for th in list(self.reviewer._threads.values()):
                th.join(timeout=1.0)

        self.planner = AIPlannerCoordinator(self.runtime_path, self.port)
        self.reviewer = AIReviewerCoordinator(self.runtime_path, self.port)
        self.coordinator = ControlCommandCoordinator(self.runtime_path, self.planner, reviewer=self.reviewer)
        self.supervisor = ActivationSupervisor(self.runtime_path, controls=self.coordinator, executor=self.executor)
