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
from typing import Any, Optional

from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult, ResourceContext
from dev_orchestrator.control.surface import project_control_view, project_identity
from dev_orchestrator.core.activation_supervisor import ActivationSupervisor
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.core.execution_context import (
    get_context,
    load_execution_contexts,
    resolve_next_action,
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

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        self.requests.append(request)
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
        resource = ResourceContext("review-r", "p2", "a2", "m2")
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps({"decision": "approve", "reason": "bounded and testable"}),
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
        executor = HarnessFakeExecutor(launch=True)
        coordinator = ControlCommandCoordinator(runtime, planner)
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

        return {
            "project_id": self.project_id,
            "repo_path": str(self.repo_path),
            "state": "READY_TO_RUN" if state_res.is_ready_to_run() else "IDLE",
            "next_status": raw_status,
            "telemetry": {"task_id": self.task_id},
            "git": {"head": git_head, "branch": "main", "dirty": False},
            "readiness": readiness_res.to_dict(),
        }

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
        """Wait for planner to approve and freeze plan -> ready_to_run, or simulate if not active."""
        pid = plan_id or "ai_plan:cmd-start-1"
        deadline = time.time() + timeout
        if self.planner is not None:
            while time.time() < deadline:
                row = self.planner.state()["plans"].get(pid)
                if row and row.get("state") == "ready":
                    return
                if row and row.get("state") in {"failed", "rejected"}:
                    break
                time.sleep(0.02)

        next_path = self.repo_path / "agent" / "next.md"
        content = next_path.read_text(encoding="utf-8")
        line_idx, _ = require_single_status_line(content)
        lines = content.splitlines()
        lines[line_idx] = render_status_line("ready_to_run")
        lines.append("\n## Approved executable design\n1. Implementation details verified.\n")
        next_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        write_task_execution_state(
            self.repo_path,
            state="ready_to_run",
            task_id=self.task_id,
            source="planner",
        )
        subprocess.run(["git", "add", "."], cwd=self.repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "plan approved"], cwd=self.repo_path, check=True, capture_output=True)

    def complete_worker_run(self) -> None:
        """Simulate worker completing execution."""
        snap = self.current_snapshot()
        snap["state"] = "WAITING_REVIEW"
        snap["worker"] = {
            "kind": "task",
            "state": "completed",
            "process_alive": False,
            "exit_code": 0,
            "updated_at": utc_now_iso(),
        }

    def apply_review_acceptance(self) -> None:
        """Simulate reviewer accepting completion -> DONE."""
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
        subprocess.run(["git", "commit", "-m", "task completed and accepted"], cwd=self.repo_path, check=True, capture_output=True)
