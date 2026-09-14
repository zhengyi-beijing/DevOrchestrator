import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.repository import read_repository_truth


def plan_payload(task_id):
    return {
        "task_id": task_id,
        "summary": "Freeze the adjudicated bounded plan.",
        "implementation_steps": ["Implement only the unresolved contract patch."],
        "interfaces": ["Keep the existing task interfaces stable."],
        "validation": ["Run focused and full regression tests."],
        "risks": ["Do not reopen already resolved review issues."],
        "out_of_scope": ["No unrelated refactor."],
    }


class AdjudicationPort:
    def __init__(self, decision="contract_patch"):
        self.decision = decision
        self.requests = []
    def execute(self, request):
        self.requests.append(request)
        if request.role != "adjudicator":
            raise AssertionError("unexpected role: " + request.role)
        if self.decision == "owner_gate":
            payload = {"decision": "owner_gate", "reason": "owner choice required", "resolved_plan": None}
        else:
            payload = {
                "decision": self.decision,
                "reason": "remaining contract ambiguity is resolved",
                "resolved_plan": plan_payload(request.task_run_id),
            }
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps(payload),
            dispatch_id="dispatch-adjudicator",
            execution_id="execution-adjudicator",
            resource_context=ResourceContext("agy-adjudicator", "agy", "agy-1", "opus-thinking"),
        )


def make_repo(repo):
    (repo / "agent").mkdir(parents=True)
    (repo / "agent" / "next.md").write_text(
        "# P14 Example\n\nStatus: **PENDING DESIGN**\n\nGoal: bounded change.\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)


def write_config(path, repo):
    path.write_text(json.dumps({"projects": [{
        "project_id": "p1",
        "repo_path": str(repo),
        "adapter": "agent_files",
        "execution": {"engine": "aibroker"},
        "ai_roles": {"planner": {
            "enabled": True,
            "adjudication_enabled": True,
            "adjudication_min_rejections": 3,
        }},
    }]}), encoding="utf-8")


def seed_failed_plan(runtime, repo):
    truth = read_repository_truth(repo)
    plan_id = "ai_plan:cmd"
    record = {
        "plan_id": plan_id,
        "command_id": "cmd",
        "project_id": "p1",
        "task_id": "P14",
        "repo_path": str(repo),
        "branch": truth.branch,
        "head": truth.head,
        "status_hash": truth.status_hash,
        "next_text": (repo / "agent" / "next.md").read_text(encoding="utf-8"),
        "state": "failed",
        "plan": plan_payload("P14"),
        "review_reason": "third bounded rejection",
        "reason": "planner output exhausted after bounded remediation",
        "rejection_chain": [{"round": i, "reason": f"reject-{i}"} for i in range(3)],
        "remediation_round": 3,
    }
    runtime.mkdir(parents=True, exist_ok=True)
    seeder = AIPlannerCoordinator(runtime, None)
    seeder._save_state({"version": 1, "plans": {plan_id: record}})
    return plan_id


def wait_terminal(coordinator, plan_id):
    deadline = time.time() + 5
    while time.time() < deadline:
        row = coordinator.state()["plans"][plan_id]
        if row.get("state") not in {"adjudicating", "applying"}:
            return row
        time.sleep(0.02)
    return coordinator.state()["plans"][plan_id]


class AdjudicationRecoveryTests(unittest.TestCase):
    def test_failed_plan_is_adjudicated_once_and_becomes_ready(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            plan_id = seed_failed_plan(runtime, repo)
            config = base / "projects.json"; write_config(config, repo)
            old_head = read_repository_truth(repo).head
            port = AdjudicationPort("contract_patch")
            coordinator = AIPlannerCoordinator(runtime, port)
            self.assertEqual(coordinator.advance_adjudications(config), [plan_id])
            row = wait_terminal(coordinator, plan_id)
            self.assertEqual(row["state"], "ready")
            self.assertEqual(row["adjudication_decision"], "contract_patch")
            self.assertEqual([r.role for r in port.requests], ["adjudicator"])
            self.assertNotEqual(read_repository_truth(repo).head, old_head)
            self.assertIn("Status: **READY_TO_RUN**", (repo / "agent" / "next.md").read_text(encoding="utf-8"))
            self.assertEqual(coordinator.advance_adjudications(config), [])
            self.assertEqual(len(port.requests), 1)
    def test_gate_decision_does_not_apply_plan(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            plan_id = seed_failed_plan(runtime, repo)
            config = base / "projects.json"; write_config(config, repo)
            old_head = read_repository_truth(repo).head
            old_next = (repo / "agent" / "next.md").read_bytes()
            port = AdjudicationPort("owner_gate")
            coordinator = AIPlannerCoordinator(runtime, port)
            self.assertEqual(coordinator.advance_adjudications(config), [plan_id])
            row = wait_terminal(coordinator, plan_id)
            self.assertEqual(row["state"], "owner_gate")
            self.assertEqual(read_repository_truth(repo).head, old_head)
            self.assertEqual((repo / "agent" / "next.md").read_bytes(), old_next)
            self.assertEqual([r.role for r in port.requests], ["adjudicator"])


def test_adjudication_accepts_json_markdown_fence_only():
    from dev_orchestrator.core.ai_planner import _parse_adjudication
    payload = {
        "decision": "contract_patch",
        "reason": "resolved",
        "resolved_plan": plan_payload("P14"),
    }
    decision, reason, plan = _parse_adjudication(
        "```json\n" + json.dumps(payload) + "\n```", "P14"
    )
    assert decision == "contract_patch"
    assert reason == "resolved"
    assert plan["task_id"] == "P14"


class InterruptedAdjudicationPort(AdjudicationPort):
    def __init__(self, execution_error):
        super().__init__("contract_patch")
        self.execution_error = execution_error

    def status(self, request_id):
        return {
            "request_id": request_id,
            "dispatch_id": "dispatch-interrupted",
            "status": "failed",
            "execution_error": self.execution_error,
        }


def _mark_recovery_required(runtime, plan_id, attempts=5):
    coordinator = AIPlannerCoordinator(runtime, None)
    state = coordinator.state()
    row = state["plans"][plan_id]
    row["state"] = "recovery_required"
    row["adjudication_attempt_count"] = attempts
    row["reason"] = "daemon restarted during planner lifecycle; automatic replay forbidden"
    coordinator._save_state(state)


def test_daemon_interrupted_adjudication_reuses_same_attempt():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td); repo = base / "repo"; runtime = base / "runtime"
        make_repo(repo)
        plan_id = seed_failed_plan(runtime, repo)
        _mark_recovery_required(runtime, plan_id, attempts=5)
        config = base / "projects.json"; write_config(config, repo)
        data = json.loads(config.read_text(encoding="utf-8"))
        data["projects"][0]["ai_roles"]["planner"]["adjudication_max_attempts"] = 5
        config.write_text(json.dumps(data), encoding="utf-8")
        reason = "DevOrchestrator daemon restart interrupted adjudication; process tree confirmed stopped"
        port = InterruptedAdjudicationPort(reason)
        coordinator = AIPlannerCoordinator(runtime, port)
        assert coordinator.advance_adjudications(config) == [plan_id]
        row = wait_terminal(coordinator, plan_id)
        assert row["state"] == "ready"
        assert row["adjudication_attempt_count"] == 5
        assert row["adjudication_recovered_dispatch_id"] == "dispatch-interrupted"
        assert len(port.requests) == 1


def test_non_interrupt_recovery_required_is_not_replayed():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td); repo = base / "repo"; runtime = base / "runtime"
        make_repo(repo)
        plan_id = seed_failed_plan(runtime, repo)
        _mark_recovery_required(runtime, plan_id, attempts=5)
        config = base / "projects.json"; write_config(config, repo)
        data = json.loads(config.read_text(encoding="utf-8"))
        data["projects"][0]["ai_roles"]["planner"]["adjudication_max_attempts"] = 5
        config.write_text(json.dumps(data), encoding="utf-8")
        port = InterruptedAdjudicationPort("provider schema failure")
        coordinator = AIPlannerCoordinator(runtime, port)
        assert coordinator.advance_adjudications(config) == []
        row = coordinator.state()["plans"][plan_id]
        assert row["state"] == "recovery_required"
        assert row["adjudication_attempt_count"] == 5
        assert port.requests == []
