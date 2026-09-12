import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.core.transition_executor import TransitionExecutor


class ReviewerPort:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return AIRoleResult(
            request_id=request.request_id, role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps({
                "decision": "next", "next_action": "next_task", "reason": "accepted"
            }),
            dispatch_id="review-dispatch", decision_id="review-resource-decision",
            execution_id="review-execution",
            resource_context=ResourceContext(
                "copilot/default/reviewer", "github-copilot", "default", "reviewer"
            ),
        )


def init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=repo, check=True, capture_output=True)
    return repo


class DirectReviewerTests(unittest.TestCase):
    def test_unbound_project_reviews_through_aibroker_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = init_repo(root)
            runtime = root / "runtime"
            runtime.mkdir()
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": [{
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"reviewer": {"enabled": True, "quality": "high", "independence": "resource"}},
            }]}), encoding="utf-8")
            truth = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=repo, text=True).strip()
            (runtime / "transition-executor.json").write_text(json.dumps({
                "version": 1, "executions": {"worker-source": {
                    "project_id": "p1", "source_request_id": "worker-source",
                    "task_id": "P1", "repo_path": str(repo), "branch": branch, "head": truth,
                    "engine": "aibroker", "state": "completed", "completed_at": "2026-09-10T02:00:00+00:00",
                    "resource_context": {"resource_id": "dsh/default/worker", "provider": "deepseek", "account": "default", "model": "worker"},
                }, "older-worker": {
                    "project_id": "p1", "source_request_id": "older-worker", "task_id": "P0",
                    "repo_path": str(repo), "branch": branch, "head": truth, "engine": "aibroker",
                    "state": "completed", "completed_at": "2026-09-10T01:00:00+00:00",
                    "resource_context": {"resource_id": "older/resource", "provider": "deepseek", "account": "default", "model": "old"},
                }}
            }), encoding="utf-8")
            normalized = load_projects_config(config)["projects"][0]
            self.assertTrue(normalized["orchestration_ready"])
            self.assertNotIn("conversation_binding", normalized)
            port = ReviewerPort()
            reviewer = AIReviewerCoordinator(runtime, port)
            launched = reviewer.advance(config)
            self.assertEqual(launched, ["ai_review:worker-source"])
            reviewer._threads[launched[0]].join(timeout=2)
            self.assertFalse(reviewer._threads[launched[0]].is_alive())
            self.assertEqual(reviewer.advance(config), [])
            self.assertEqual(len(port.requests), 1)
            self.assertEqual(port.requests[0].role, "reviewer")
            self.assertEqual(port.requests[0].independence, "resource")
            self.assertEqual(port.requests[0].previous_resource_context.resource_id, "dsh/default/worker")
            decisions = json.loads((runtime / "review-decisions.json").read_text(encoding="utf-8"))["decisions"]
            record = decisions["ai_review:worker-source"]
            self.assertEqual(record["disposition"], "apply")
            self.assertEqual(record["decision"], "next")
            self.assertEqual(record["next_action"], "next_task")
            self.assertEqual(record["source"], "aibroker")
            self.assertIn("ai_review:worker-source", TransitionExecutor(runtime)._load_decisions())


    def test_reviewer_flag_without_broker_worker_engine_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = init_repo(root)
            config = root / "legacy.json"
            config.write_text(json.dumps({"projects": [{
                "project_id": "legacy", "repo_path": str(repo),
                "ai_roles": {"reviewer": {"enabled": True}}
            }]}), encoding="utf-8")
            normalized = load_projects_config(config)["projects"][0]
            self.assertFalse(normalized["orchestration_ready"])
            self.assertNotIn("legacy", AIReviewerCoordinator(root / "runtime", ReviewerPort()).enabled_project_ids(config))

    def test_restart_marks_active_review_recovery_required(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            (runtime / "ai-reviewer.json").write_text(json.dumps({
                "version": 1, "reviews": {"r1": {
                    "review_id": "r1", "project_id": "p1", "source_request_id": "w1",
                    "state": "running"
                }}
            }), encoding="utf-8")
            reviewer = AIReviewerCoordinator(runtime, ReviewerPort())
            record = reviewer.state()["reviews"]["r1"]
            self.assertEqual(record["state"], "recovery_required")
            self.assertIn("automatic replay forbidden", record["reason"])

    def test_project_context_injected_into_review_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = init_repo(root)
            ctx_payload = {
                "schema_version": 1,
                "project_id": "p1",
                "goals": ["Ship review context"],
                "architecture": ["Review module"],
                "protected_scope": ["Production infra"],
                "safety_constraints": ["Fail closed"],
                "validation_commands": ["python -m unittest"],
                "runtime_assumptions": ["Python 3.11"],
                "key_decisions": ["D1 schema"],
            }
            (repo / "agent").mkdir(parents=True, exist_ok=True)
            (repo / "agent" / "project-context.json").write_text(json.dumps(ctx_payload), encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "ctx"], check=True)

            runtime = root / "runtime"; runtime.mkdir()
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": [{
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"reviewer": {"enabled": True, "quality": "high", "independence": "resource"}},
                "project_context": {
                    "enabled": True,
                    "document_path": "agent/project-context.json",
                    "require_valid": True,
                },
            }]}), encoding="utf-8")
            truth = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=repo, text=True).strip()
            (runtime / "transition-executor.json").write_text(json.dumps({
                "version": 1, "executions": {"worker-source": {
                    "project_id": "p1", "source_request_id": "worker-source",
                    "task_id": "P1", "repo_path": str(repo), "branch": branch, "head": truth,
                    "engine": "aibroker", "state": "completed", "completed_at": "2026-09-10T02:00:00+00:00",
                    "resource_context": {"resource_id": "dsh/default/worker", "provider": "deepseek", "account": "default", "model": "worker"},
                }}
            }), encoding="utf-8")
            port = ReviewerPort()
            reviewer = AIReviewerCoordinator(runtime, port)
            launched = reviewer.advance(config)
            self.assertEqual(launched, ["ai_review:worker-source"])
            reviewer._threads[launched[0]].join(timeout=2)
            self.assertFalse(reviewer._threads[launched[0]].is_alive())

            prompt = port.requests[0].prompt
            self.assertIn("[PROJECT_CONTEXT_BEGIN]", prompt)
            self.assertIn("## goals\n- Ship review context", prompt)
            self.assertIn("[PROJECT_CONTEXT_END]", prompt)

            rec = reviewer.state()["reviews"]["ai_review:worker-source"]
            self.assertEqual(rec["context_state"], "ready")
            self.assertIsNotNone(rec["context_digest"])

    def test_invalid_context_records_terminal_failure_without_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = init_repo(root)
            (repo / "agent").mkdir(parents=True, exist_ok=True)
            (repo / "agent" / "project-context.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "bad-ctx"], check=True)

            runtime = root / "runtime"; runtime.mkdir()
            config = root / "projects.json"
            config.write_text(json.dumps({"projects": [{
                "project_id": "p1", "repo_path": str(repo),
                "execution": {"engine": "aibroker"},
                "ai_roles": {"reviewer": {"enabled": True, "quality": "high", "independence": "resource"}},
                "project_context": {
                    "enabled": True,
                    "document_path": "agent/project-context.json",
                    "require_valid": True,
                },
            }]}), encoding="utf-8")
            truth = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=repo, text=True).strip()
            (runtime / "transition-executor.json").write_text(json.dumps({
                "version": 1, "executions": {"worker-source": {
                    "project_id": "p1", "source_request_id": "worker-source",
                    "task_id": "P1", "repo_path": str(repo), "branch": branch, "head": truth,
                    "engine": "aibroker", "state": "completed", "completed_at": "2026-09-10T02:00:00+00:00",
                    "resource_context": {"resource_id": "dsh/default/worker", "provider": "deepseek", "account": "default", "model": "worker"},
                }}
            }), encoding="utf-8")
            port = ReviewerPort()
            reviewer = AIReviewerCoordinator(runtime, port)
            launched = reviewer.advance(config)
            self.assertEqual(launched, [])
            self.assertEqual(len(port.requests), 0)

            rec = reviewer.state()["reviews"]["ai_review:worker-source"]
            self.assertEqual(rec["state"], "failed")
            self.assertIn("durable project context is invalid", rec["reason"])


if __name__ == "__main__":
    unittest.main()
