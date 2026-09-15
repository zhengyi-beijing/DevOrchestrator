import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult, ResourceContext
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.surface import project_control_view, project_identity
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.storage.json_store import write_json
from tests_py.test_control_commands import FakeExecutor, FakePlanner, write_config
from tests_py.test_p12_control_actions import FakeInterruptPort


def make_test_repo(repo: Path, task_id: str = "P1") -> None:
    (repo / "agent").mkdir(parents=True, exist_ok=True)
    (repo / "agent" / "next.md").write_text(
        f"# {task_id}\n\nStatus: **PENDING DESIGN**\n\nGoal: test task.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)


def valid_plan_payload(task_id: str = "P1") -> str:
    return json.dumps({
        "task_id": task_id,
        "summary": "Plan for task",
        "implementation_steps": ["Step 1"],
        "interfaces": ["Interface 1"],
        "validation": ["Validation 1"],
        "risks": ["Risk 1"],
        "out_of_scope": ["Scope 1"],
    })


def valid_review_payload(decision: str = "approve", reason: str = "Plan verified") -> str:
    return json.dumps({"decision": decision, "reason": reason})


class ControllablePort:
    def __init__(self, plan_handler=None, review_handler=None):
        self.requests: list[AIRoleRequest] = []
        self.plan_handler = plan_handler
        self.review_handler = review_handler

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        self.requests.append(request)
        if request.role == "planner":
            if callable(self.plan_handler):
                return self.plan_handler(request, len(self.requests))
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status="succeeded",
                output=valid_plan_payload(request.task_run_id),
                dispatch_id="dispatch-plan",
                decision_id="decision-plan",
                execution_id="execution-plan",
                resource_context=ResourceContext("planner-res", "provider-a", "acc-1", "mod-1"),
            )
        if callable(self.review_handler):
            return self.review_handler(request, len(self.requests))
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=valid_review_payload(),
            dispatch_id="dispatch-review",
            decision_id="decision-review",
            execution_id="execution-review",
            resource_context=ResourceContext("reviewer-res", "provider-b", "acc-2", "mod-2"),
        )


class P12PlannerSingleFlightTests(unittest.TestCase):
    def test_control_surface_allows_pending_design_only_from_idle(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td) / "runtime"
            project = {
                "project_id": "p1",
                "execution": {"engine": "aibroker", "enabled": True, "owner_authorized": True},
                "ai_roles": {"planner": {"enabled": True}},
            }
            idle = {
                "project_id": "p1", "state": "IDLE", "lifecycle_state": "IDLE",
                "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P1"},
            }
            view = project_control_view(idle, runtime, project_config=project)
            capability = next(row for row in view["controls"] if row["action"] == "continue")
            self.assertTrue(capability["available"])

            active = dict(idle)
            active["state"] = "REVIEWING_PLAN"
            active["lifecycle_state"] = "REVIEWING_PLAN"
            view = project_control_view(active, runtime, project_config=project)
            capability = next(row for row in view["controls"] if row["action"] == "continue")
            self.assertFalse(capability["available"])

    def test_control_command_rejects_pending_design_from_active_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            runtime = base / "runtime"
            config = base / "projects.json"
            write_config(config, repo)
            snapshot = {
                "project_id": "p1", "state": "REVIEWING_PLAN",
                "lifecycle_state": "REVIEWING_PLAN",
                "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P1"},
            }
            planner = FakePlanner()
            command = submit_control_command(
                runtime, "p1", "continue", expected=project_identity(snapshot, runtime)
            )
            result = ControlCommandCoordinator(runtime, planner).advance(
                config, {"projects": [snapshot]}, FakeExecutor()
            )[0]
            self.assertEqual(result["command_id"], command["command_id"])
            self.assertEqual(result["state"], "blocked")
            self.assertIn("requires IDLE lifecycle", result["reason"])
            self.assertEqual(planner.calls, [])

    def test_planner_rejects_second_active_plan_for_same_project(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            (repo / "agent").mkdir(parents=True)
            (repo / "agent" / "next.md").write_text(
                "# P1\n\nStatus: **PENDING DESIGN**\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            runtime = base / "runtime"
            coordinator = AIPlannerCoordinator(runtime, object())
            state = {
                "version": 1,
                "plans": {
                    "ai_plan:first": {
                        "plan_id": "ai_plan:first", "command_id": "first",
                        "project_id": "p1", "task_id": "P1", "state": "reviewing",
                    }
                },
            }
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "ai-planner.json").write_text(json.dumps(state), encoding="utf-8")
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True}},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P1"},
            }
            plan_id, reason = coordinator.start(project, snapshot, "second")
            self.assertIsNone(plan_id)
            self.assertEqual(reason, "planner lifecycle already active for project")
            persisted = json.loads((runtime / "ai-planner.json").read_text(encoding="utf-8"))
            self.assertEqual(set(persisted["plans"]), {"ai_plan:first"})

    def test_control_command_allows_pending_design_from_idle(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            runtime = base / "runtime"
            config = base / "projects.json"
            write_config(config, repo)
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "lifecycle_state": "IDLE",
                "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P1"},
            }
            planner = FakePlanner()
            command = submit_control_command(
                runtime, "p1", "continue", expected=project_identity(snapshot, runtime)
            )
            result = ControlCommandCoordinator(runtime, planner).advance(
                config, {"projects": [snapshot]}, FakeExecutor()
            )[0]
            self.assertEqual(result["command_id"], command["command_id"])
            self.assertEqual(result["state"], "accepted")
            self.assertEqual(result["lifecycle_action"], "plan")
            self.assertEqual(len(planner.calls), 1)


class P12PlannerPauseBarrierTests(unittest.TestCase):
    def test_pause_barrier_blocks_new_planner_retry_dispatch(self):
        """When attempt 1 fails and project is paused, no retry-1 dispatch occurs until resumed."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_test_repo(repo, "P1")
            owner_store = OwnerControlStore(runtime)

            attempt_counter = 0
            def plan_handler(request, count):
                nonlocal attempt_counter
                attempt_counter += 1
                if attempt_counter == 1:
                    owner_store.set_paused("p1", True, command_id="pause-after-att1", action="pause")
                    return AIRoleResult(
                        request_id=request.request_id,
                        role_run_id=request.role_run_id,
                        status="failed",
                        error="simulated timeout",
                        dispatch_id="disp-att1",
                    )
                return AIRoleResult(
                    request_id=request.request_id,
                    role_run_id=request.role_run_id,
                    status="succeeded",
                    output=valid_plan_payload("P1"),
                    dispatch_id=f"disp-att{attempt_counter}",
                    decision_id="dec-1",
                    execution_id="exec-1",
                    resource_context=ResourceContext("planner-res", "prov", "acc", "mod"),
                )

            port = ControllablePort(plan_handler=plan_handler)
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True, "max_attempts": 3}},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P1"},
            }

            plan_id, reason = coordinator.start(project, snapshot, "cmd-retry-pause")
            self.assertIsNotNone(plan_id)

            deadline = time.time() + 3
            while time.time() < deadline and attempt_counter < 1:
                time.sleep(0.02)
            self.assertEqual(attempt_counter, 1)

            time.sleep(0.3)
            self.assertEqual(len(port.requests), 1, "retry must not dispatch while paused")

            owner_store.set_paused("p1", False, command_id="resume-retry", action="resume")

            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "applying"}:
                    break
                time.sleep(0.02)

            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "ready")
            self.assertGreaterEqual(len(port.requests), 2)
            self.assertIn(":retry-1", port.requests[1].request_id)

    def test_pause_barrier_blocks_new_plan_reviewer_dispatch(self):
        """When planner succeeds and project is paused, plan reviewer is not dispatched until resumed."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_test_repo(repo, "P1")
            owner_store = OwnerControlStore(runtime)

            def plan_handler(request, count):
                owner_store.set_paused("p1", True, command_id="pause-before-review", action="pause")
                return AIRoleResult(
                    request_id=request.request_id,
                    role_run_id=request.role_run_id,
                    status="succeeded",
                    output=valid_plan_payload("P1"),
                    dispatch_id="disp-plan",
                    decision_id="dec-plan",
                    execution_id="exec-plan",
                    resource_context=ResourceContext("planner-res", "prov", "acc", "mod"),
                )

            port = ControllablePort(plan_handler=plan_handler)
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True}},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P1"},
            }

            plan_id, reason = coordinator.start(project, snapshot, "cmd-review-pause")
            self.assertIsNotNone(plan_id)

            deadline = time.time() + 3
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("planner_completed_at"):
                    break
                time.sleep(0.02)

            time.sleep(0.3)
            self.assertEqual([r.role for r in port.requests], ["planner"])

            owner_store.set_paused("p1", False, command_id="resume-review", action="resume")

            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "applying"}:
                    break
                time.sleep(0.02)

            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "ready")
            self.assertEqual([r.role for r in port.requests], ["planner", "reviewer"])

    def test_stop_action_terminates_lifecycle_at_barrier(self):
        """When stop action is recorded in owner store, barrier finishes plan as failed and aborts next dispatch."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_test_repo(repo, "P1")
            owner_store = OwnerControlStore(runtime)

            def plan_handler(request, count):
                owner_store.set_paused("p1", True, command_id="stop-cmd", action="stop", reason="explicit stop")
                return AIRoleResult(
                    request_id=request.request_id,
                    role_run_id=request.role_run_id,
                    status="succeeded",
                    output=valid_plan_payload("P1"),
                    dispatch_id="disp-plan",
                    decision_id="dec-plan",
                    execution_id="exec-plan",
                    resource_context=ResourceContext("planner-res", "prov", "acc", "mod"),
                )

            port = ControllablePort(plan_handler=plan_handler)
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {"enabled": True}},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**", "telemetry": {"task_id": "P1"},
            }

            plan_id, reason = coordinator.start(project, snapshot, "cmd-stop-barrier")
            self.assertIsNotNone(plan_id)

            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") in {"failed", "ready"}:
                    break
                time.sleep(0.02)

            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "failed")
            self.assertIn("stopped by owner", row["reason"])
            self.assertEqual([r.role for r in port.requests], ["planner"])


class P12StopSemanticsTests(unittest.TestCase):
    def test_stop_not_advertised_in_planner_and_reviewer_lifecycle_states(self):
        """Stop capability must not be advertised as available during un-interruptible AI roles."""
        unsupported_states = ["PLANNING", "REVIEWING_PLAN", "REMEDIATING_PLAN", "APPLYING_PLAN", "REVIEWING"]
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td) / "runtime"
            project = {
                "project_id": "p1",
                "execution": {"engine": "aibroker", "enabled": True, "owner_authorized": True},
                "ai_roles": {"planner": {"enabled": True}},
            }
            for state in unsupported_states:
                snapshot = {
                    "project_id": "p1", "state": state, "lifecycle_state": state,
                    "next_status": "**IN PROGRESS**", "telemetry": {"task_id": "P1"},
                }
                view = project_control_view(snapshot, runtime, project_config=project)
                stop_ctrl = next(row for row in view["controls"] if row["action"] == "stop")
                self.assertFalse(
                    stop_ctrl["available"],
                    f"stop must not be available in lifecycle {state}",
                )
                self.assertEqual(
                    stop_ctrl["reason"],
                    "active AI role does not support managed interruption; use pause",
                    f"incorrect stop reason in {state}",
                )

    def test_stop_command_rejected_server_side_in_planner_and_reviewer_states(self):
        """Direct server-side stop command must be blocked when active AI roles are running."""
        unsupported_states = ["PLANNING", "REVIEWING_PLAN", "REMEDIATING_PLAN", "APPLYING_PLAN", "REVIEWING"]
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"; config = base / "projects.json"
            repo.mkdir()
            write_config(config, repo)

            for state in unsupported_states:
                snapshot = {
                    "project_id": "p1", "state": state, "lifecycle_state": state,
                    "next_status": "**IN PROGRESS**", "telemetry": {"task_id": "P1"},
                }
                command = submit_control_command(
                    runtime, "p1", "stop", expected=project_identity(snapshot, runtime)
                )
                coordinator = ControlCommandCoordinator(runtime, FakePlanner())
                result = coordinator.advance(config, {"projects": [snapshot]}, FakeExecutor())[0]
                self.assertEqual(result["command_id"], command["command_id"])
                self.assertEqual(result["state"], "blocked")
                self.assertIn("does not support managed interruption; use pause", result["reason"])
                self.assertFalse(OwnerControlStore(runtime).is_paused("p1"))

    def test_stop_functional_for_supported_stoppable_executor_state(self):
        """Stop remains available and functional when execution is a stoppable aibroker worker."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"; config = base / "projects.json"
            repo.mkdir()
            write_config(config, repo)

            snapshot = {
                "project_id": "p1", "state": "WORKER_RUNNING", "lifecycle_state": "EXECUTING",
                "next_status": "**EXECUTING**", "telemetry": {"task_id": "P1"},
            }

            write_json(runtime / "transition-executor.json", {
                "version": 1,
                "executions": {
                    "exec-1": {
                        "source_request_id": "exec-1", "project_id": "p1", "state": "running",
                        "engine": "aibroker", "broker_request_id": "broker-req-999",
                        "started_at": "2026-09-15T00:00:00Z",
                    }
                },
            })

            view = project_control_view(snapshot, runtime, project_config={"project_id": "p1"})
            stop_ctrl = next(row for row in view["controls"] if row["action"] == "stop")
            self.assertTrue(stop_ctrl["available"])
            self.assertEqual(stop_ctrl["reason"], "pause and interrupt exact supported execution")

            port = FakeInterruptPort()
            executor = FakeExecutor()
            executor._ai_execution_port = port
            executor.records = {
                "exec-1": {
                    "source_request_id": "exec-1", "project_id": "p1", "state": "running",
                    "engine": "aibroker", "broker_request_id": "broker-req-999",
                    "started_at": "2026-09-15T00:00:00Z",
                }
            }

            command = submit_control_command(
                runtime, "p1", "stop", expected=project_identity(snapshot, runtime)
            )
            result = ControlCommandCoordinator(runtime).advance(
                config, {"projects": [snapshot]}, executor
            )[0]

            self.assertEqual(result["command_id"], command["command_id"])
            self.assertEqual(result["state"], "accepted")
            self.assertEqual(result["effect"], "pause_and_interrupt")
            self.assertEqual(port.calls, [("broker-req-999", "explicit P12 owner stop")])
            self.assertTrue(OwnerControlStore(runtime).is_paused("p1"))


if __name__ == "__main__":
    unittest.main()
