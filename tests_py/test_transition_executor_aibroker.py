import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
from dev_orchestrator.ai.execution_port import MANAGED_INTERRUPT_REASON
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



class RecoveryPort(FakePort):
    def __init__(self, fact):
        super().__init__(); self.fact = fact; self.status_requests = []
    def status(self, request_id):
        self.status_requests.append(request_id); return dict(self.fact) if self.fact else None


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


    def _write_active_broker_record(self, runtime: Path, repo: Path, truth, *, request_id="worker-1"):
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "transition-executor.json").write_text(json.dumps({"version": 1, "executions": {
            request_id: {"project_id": "p1", "source_request_id": request_id, "engine": "aibroker",
                         "broker_request_id": "ai-worker:" + request_id, "state": "running",
                         "branch": truth.branch, "head": truth.head, "launch_status_hash": truth.status_hash,
                         "repo_path": str(repo), "started_at": "2026-09-10T01:00:00+00:00",
                         "review_state": "pending"}
        }}), encoding="utf-8")

    def test_restart_recovers_succeeded_broker_as_completed_with_resource_facts(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); repo=self.make_repo(root); runtime=root/"runtime"; truth=read_repository_truth(repo)
            self._write_active_broker_record(runtime, repo, truth)
            port=RecoveryPort({"status":"succeeded", "dispatch_id":"d1", "decision_id":"q1",
                               "execution_id":"e1", "resource_id":"r1", "provider":"deepseek",
                               "account":"a", "model":"m", "finished_at":"2026-09-10T01:02:00+00:00"})
            record=TransitionExecutor(runtime, ai_execution_port=port).state()["executions"]["worker-1"]
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["execution_id"], "e1")
            self.assertEqual(record["resource_context"]["resource_id"], "r1")

    def test_restart_managed_interrupt_is_safe_retry_only_when_repo_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); repo=self.make_repo(root); runtime=root/"runtime"; truth=read_repository_truth(repo)
            self._write_active_broker_record(runtime, repo, truth)
            fact={"status":"failed", "execution_error":MANAGED_INTERRUPT_REASON, "resource_id":"r1"}
            record=TransitionExecutor(runtime, ai_execution_port=RecoveryPort(fact)).state()["executions"]["worker-1"]
            self.assertEqual(record["state"], "recovery_required")
            self.assertTrue(record["recovery_safe_retry"])

    def test_restart_running_or_changed_repo_never_auto_replays(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); repo=self.make_repo(root); runtime=root/"runtime"; truth=read_repository_truth(repo)
            self._write_active_broker_record(runtime, repo, truth)
            (repo/"README.md").write_text("changed\n", encoding="utf-8")
            fact={"status":"failed", "execution_error":MANAGED_INTERRUPT_REASON, "resource_id":"r1"}
            record=TransitionExecutor(runtime, ai_execution_port=RecoveryPort(fact)).state()["executions"]["worker-1"]
            self.assertFalse(record["recovery_safe_retry"])
            runtime2=root/"runtime2"; truth2=read_repository_truth(repo)
            self._write_active_broker_record(runtime2, repo, truth2, request_id="worker-2")
            running=TransitionExecutor(runtime2, ai_execution_port=RecoveryPort({"status":"running", "resource_id":"r1"})).state()["executions"]["worker-2"]
            self.assertEqual(running["state"], "recovery_required")
            self.assertFalse(running["recovery_safe_retry"])

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
