import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.transition_executor import TransitionExecutor, _execution_policy


class FakePort:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output="WORKER_OK",
            dispatch_id="dispatch-1",
            decision_id="decision-1",
            execution_id="execution-1",
            resource_context=ResourceContext(
                "dsh/default/model", "deepseek", "default", "model"
            ),
        )


def broker_project(repo: Path):
    return {
        "project_id": "p1",
        "repo_path": str(repo),
        "execution": {
            "enabled": True,
            "owner_authorized": True,
            "engine": "aibroker",
            "worker_quality": "balanced",
            "allowed_next_actions": ["next_task"],
        },
    }
class AIBrokerTransitionTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=repo, check=True, capture_output=True)
        return repo

    def test_policy_accepts_aibroker_without_legacy_backends(self):
        with tempfile.TemporaryDirectory() as td:
            policy, error = _execution_policy(broker_project(Path(td)))
        self.assertEqual(error, "")
        self.assertEqual(policy["engine"], "aibroker")
        self.assertEqual(policy["preferred_backends"], ())
        self.assertEqual(policy["worker_quality"], "balanced")

    def test_aibroker_worker_records_exact_dispatch_resource_facts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = self.make_repo(root)
            project = broker_project(repo)
            policy, error = _execution_policy(project)
            self.assertFalse(error)
            truth = read_repository_truth(repo)
            port = FakePort()
            executor = TransitionExecutor(root / "runtime", ai_execution_port=port)
            launch = executor._launch(
                project, source_request_id="owner-p1", source_kind="owner_start",
                task_id="P1", source_task_id=None, branch=truth.branch, head=truth.head,
                worker_prompt="Return exactly WORKER_OK", policy=policy,
            )
            self.assertIsNotNone(launch)
            deadline = time.time() + 3
            record = {}
            while time.time() < deadline:
                record = executor.state()["executions"]["owner-p1"]
                if record.get("state") == "completed":
                    break
                time.sleep(0.02)
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["engine"], "aibroker")
            self.assertEqual(record["dispatch_id"], "dispatch-1")
            self.assertEqual(record["execution_id"], "execution-1")
            executor._threads["owner-p1"].join(timeout=2)
            self.assertFalse(executor._threads["owner-p1"].is_alive())
            self.assertEqual(record["resource_context"]["provider"], "deepseek")
            self.assertEqual(port.requests[0].role, "worker")
            self.assertEqual(port.requests[0].timeout_seconds, 14400.0)
