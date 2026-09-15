from __future__ import annotations

import json
from pathlib import Path

from dev_orchestrator.accounting import ExecutionEventStore, ExecutionRecorder
from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult, ResourceContext
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.control.surface import project_identity
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.core.dispatcher import dispatch_worker_done_events
from dev_orchestrator.core.response_consumer import consume_websol_responses
from dev_orchestrator.core.transition_executor import TransitionExecutor
from tests_py.test_response_consumer import response_text, respond
from tests_py.test_worker_done_dispatcher import make_repo, snap


class ResultPort:
    def __init__(self, result: AIRoleResult) -> None:
        self.result = result

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        return self.result


def planner_payload() -> str:
    return json.dumps(
        {
            "task_id": "P11b",
            "summary": "implement accounting",
            "implementation_steps": ["implement"],
            "interfaces": ["events"],
            "validation": ["tests"],
            "risks": ["corruption"],
            "out_of_scope": ["dashboard"],
        }
    )


def test_planner_attempt_records_correlations_and_resource_identity(tmp_path: Path) -> None:
    recorder = ExecutionRecorder(ExecutionEventStore(tmp_path))
    resource = ResourceContext("resource-1", "provider-1", "account-1", "model-1")
    result = AIRoleResult(
        request_id="ignored-by-fake",
        role_run_id="planner-cmd",
        status="succeeded",
        output=planner_payload(),
        dispatch_id="dispatch-1",
        decision_id="decision-1",
        execution_id="execution-1",
        session_id="session-1",
        resource_context=resource,
    )
    coordinator = AIPlannerCoordinator(tmp_path, ResultPort(result), accounting=recorder)
    record = {
        "project_id": "project",
        "task_id": "P11b",
        "command_id": "cmd",
        "repo_path": str(tmp_path),
        "branch": "feature/test",
        "head": "abc",
        "next_text": "# P11b",
    }
    policy = {"max_attempts": 1, "quality": "high", "timeout_seconds": 10.0}
    assert coordinator._run_planner_attempts("ai_plan:cmd", record, policy, 0) is not None
    events = recorder.store.read().events
    assert [event["event_type"] for event in events] == ["interval_started", "interval_ended"]
    ended = events[-1]
    assert ended["phase"] == "planning"
    assert ended["project_id"] == "project"
    assert ended["task_id"] == "P11b"
    assert ended["dispatch_id"] == "dispatch-1"
    assert ended["execution_id"] == "execution-1"
    assert ended["resource_id"] == "resource-1"


def test_broker_worker_records_ai_execution_for_later_review_outcome(tmp_path: Path) -> None:
    recorder = ExecutionRecorder(ExecutionEventStore(tmp_path))
    result = AIRoleResult(
        request_id="ai-worker:source",
        role_run_id="worker-source",
        status="succeeded",
        dispatch_id="dispatch-worker",
        execution_id="execution-worker",
        resource_context=ResourceContext("r", "p", "a", "m"),
    )
    executor = TransitionExecutor(
        tmp_path,
        ai_execution_port=ResultPort(result),
        accounting=recorder,
    )
    executor._save_ledger(
        {
            "version": 1,
            "executions": {
                "source": {
                    "project_id": "project",
                    "source_request_id": "source",
                    "task_id": "P11b",
                    "state": "launching",
                }
            },
        }
    )
    request = AIRoleRequest(
        project_id="project",
        task_run_id="P11b",
        stage_run_id="remediation",
        role_run_id="worker-source",
        request_id="ai-worker:source",
        role="worker",
        prompt="work",
        working_directory=tmp_path,
    )
    executor._run_broker_worker_thread("source", request)
    events = recorder.store.read().events
    assert events[0]["role"] == "remediation_worker"
    assert events[0]["attempt_id"] == "source"
    assert events[1]["execution_id"] == "execution-worker"
    assert events[1]["resource_id"] == "r"


def test_control_command_closes_only_an_explicitly_correlated_owner_gate(tmp_path: Path) -> None:
    recorder = ExecutionRecorder(ExecutionEventStore(tmp_path))
    requested_at = recorder.now()
    recorder.open_owner_gate(
        "gate-1", occurred_at=requested_at, project_id="project", task_id="P11b", role="owner"
    )
    record = submit_control_command(
        tmp_path,
        "project",
        "continue",
        command_id="continue-1",
        gate_id="gate-1",
        expected=project_identity(
            {
                "project_id": "project", "next_status": "READY_TO_RUN",
                "telemetry": {"task_id": "P11b"},
            },
            tmp_path,
        ),
    )
    coordinator = ControlCommandCoordinator(tmp_path, accounting=recorder)
    outcome = coordinator._consume_one(
        record,
        {"project": {"project_id": "project"}},
        {
            "project": {
                "project_id": "project",
                "next_status": "READY_TO_RUN",
                "telemetry": {"task_id": "P11b"},
            }
        },
        type("Executor", (), {"start_control": lambda *args: None, "state": lambda self: {"executions": {}}})(),
    )
    assert outcome["state"] == "blocked"
    events = recorder.store.read().events
    assert events[-1]["event_type"] == "owner_gate_closed"
    assert events[-1]["gate_id"] == "gate-1"


def test_browser_review_outcome_keeps_worker_attempt_identity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    runtime = tmp_path / "runtime"
    recorder = ExecutionRecorder(ExecutionEventStore(runtime))
    store = BrowserBridgeStore(runtime / "bridge")
    summary = {"projects": [snap("project", repo, "conversation", "worker-run-1")]}

    dispatched = dispatch_worker_done_events(
        summary, store, runtime, accounting=recorder
    )
    assert len(dispatched) == 1
    claim = store.claim("chatgpt_web", "conversation")
    assert claim is not None
    respond(store, claim, response_text(claim))
    outcomes = consume_websol_responses(summary, store, runtime, accounting=recorder)

    assert outcomes[0].source_request_id == "worker-run-1"
    events = recorder.store.read().events
    outcome = next(event for event in events if event["event_type"] == "attempt_outcome")
    assert outcome["attempt_id"] == "worker-run-1"
    assert outcome["source_request_id"] == "worker-run-1"
    technical = [event for event in events if event.get("phase") == "technical_review"]
    assert {event.get("attempt_id") for event in technical} == {"worker-run-1"}
