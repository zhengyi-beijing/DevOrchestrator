import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator


class FakePort:
    def __init__(self):
        self.requests = []

    def execute(self, request):
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
            return self._result(request, json.dumps(payload), resource, "plan")
        resource = ResourceContext("review-r", "p2", "a2", "m2")
        return self._result(request, json.dumps({"decision":"approve","reason":"bounded and testable"}), resource, "review")

    @staticmethod
    def _result(request, output, resource, suffix):
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=output,
            dispatch_id="dispatch-" + suffix,
            decision_id="decision-" + suffix,
            execution_id="execution-" + suffix,
            resource_context=resource,
        )


def make_repo(repo: Path) -> None:
    (repo / "agent").mkdir(parents=True)
    (repo / "agent" / "next.md").write_text(
        "# P14 Example\n\nStatus: **PENDING DESIGN**\n\nGoal: implement the bounded change.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)


class AIPlannerTests(unittest.TestCase):
    def test_plan_review_freezes_ready_to_run_commit(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            port = FakePort()
            coordinator = AIPlannerCoordinator(runtime, port)
            project = {
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"planner": {
                    "enabled": True, "quality": "high", "review_quality": "high",
                    "review_independence": "resource",
                }},
            }
            snapshot = {
                "project_id": "p1", "state": "IDLE",
                "next_status": "**PENDING DESIGN**",
                "telemetry": {"task_id": "P14"},
            }
            plan_id, reason = coordinator.start(project, snapshot, "command-1")
            self.assertEqual(plan_id, "ai_plan:command-1")
            self.assertIn("started", reason)
            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                row = coordinator.state()["plans"].get(plan_id)
                if row and row.get("state") not in {"planning", "reviewing", "applying"}:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "ready", row)
            self.assertEqual(row["planner_resource"]["resource_id"], "planner-r")
            self.assertEqual(row["review_resource"]["resource_id"], "review-r")
            self.assertEqual([request.role for request in port.requests], ["planner", "reviewer"])
            self.assertEqual(port.requests[1].independence, "resource")
            self.assertEqual(port.requests[1].previous_resource_context.resource_id, "planner-r")
            next_text = (repo / "agent" / "next.md").read_text(encoding="utf-8")
            self.assertIn("Status: **READY_TO_RUN**", next_text)
            self.assertIn("## Approved executable design", next_text)
            status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True)
            self.assertEqual(status.stdout.strip(), "")
            log = subprocess.run(["git", "-C", str(repo), "log", "-1", "--pretty=%s"], capture_output=True, text=True, check=True)
            self.assertEqual(log.stdout.strip(), "plan(P14): freeze executable design")

    def test_restart_marks_active_plan_recovery_required(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "ai-planner.json").write_text(json.dumps({"version":1,"plans":{"p":{"state":"reviewing"}}}), encoding="utf-8")
            coordinator = AIPlannerCoordinator(runtime, None)
            row = coordinator.state()["plans"]["p"]
            self.assertEqual(row["state"], "recovery_required")
            self.assertIn("automatic replay forbidden", row["reason"])


if __name__ == "__main__":
    unittest.main()
