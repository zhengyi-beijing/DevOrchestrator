import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.reconcile import resolve_reconcile_candidate
from dev_orchestrator.control.surface import project_control_view, project_identity
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.core.repository import read_repository_truth
from tests_py.test_control_commands import FakeExecutor


class ReviewerPort:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return AIRoleResult(
            request_id=request.request_id, role_run_id=request.role_run_id, status="succeeded",
            output=json.dumps({"decision": "remediate", "next_action": "continue_current_stage", "reason": "current gap"}),
            dispatch_id="review-dispatch", decision_id="review-decision", execution_id="review-execution",
            resource_context=ResourceContext("reviewer/default", "provider", "account", "model"),
        )


class P125ReconcileTests(unittest.TestCase):
    def fixture(self, root: Path):
        repo = root / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "README.md").write_text("A launch\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "A"], cwd=repo, check=True)
        launch = read_repository_truth(repo)
        (repo / "README.md").write_text("B reviewed worker result\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "B"], cwd=repo, check=True)
        reviewed = read_repository_truth(repo)
        (repo / "README.md").write_text("C current clean head\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "C"], cwd=repo, check=True)
        current = read_repository_truth(repo)
        runtime = root / "runtime"; runtime.mkdir()
        project = {
            "project_id": "p1", "repo_path": str(repo),
            "execution": {"engine": "aibroker", "allowed_next_actions": ["continue_current_stage"]},
            "ai_roles": {"reviewer": {"enabled": True, "quality": "high", "independence": "resource"}},
        }
        config = root / "projects.json"; config.write_text(json.dumps({"projects": [project]}), encoding="utf-8")
        snapshot = {
            "project_id": "p1", "repo_path": str(repo), "state": "WAITING_REVIEW", "lifecycle_state": "WAITING_REVIEW",
            "git": {"branch": current.branch, "head": current.head, "dirty": False, "status_hash": current.status_hash},
            "telemetry": {"task_id": "P1"},
        }
        source_id = "worker-source"; old_review = "ai_review:" + source_id
        older_source_id = "older-worker-source"; older_review = "ai_review:" + older_source_id
        resource = {"resource_id": "worker/default", "provider": "provider", "account": "account", "model": "model"}
        (runtime / "transition-executor.json").write_text(json.dumps({"version": 1, "executions": {
            source_id: {"project_id": "p1", "source_request_id": source_id, "source_kind": "control", "task_id": "P1", "repo_path": str(repo), "branch": launch.branch, "head": launch.head, "engine": "aibroker", "state": "completed", "dispatch_id": "worker-dispatch", "decision_id": "worker-decision", "execution_id": "worker-execution", "resource_context": resource},
            old_review: {"project_id": "p1", "source_request_id": old_review, "source_kind": "remediation", "task_id": "P1", "repo_path": str(repo), "branch": reviewed.branch, "head": reviewed.head, "engine": "aibroker", "state": "failed", "reason": "WorktreeUnsafeError: dirty worktree requires deterministic recovery before writable reuse", "broker_status": "failed", "session_id": None, "provider_output_observed": False, "first_output_at": None, "dispatch_id": "remediation-dispatch", "decision_id": "remediation-decision", "execution_id": "remediation-execution", "resource_context": resource},
            older_source_id: {"project_id": "p1", "source_request_id": older_source_id, "source_kind": "control", "task_id": "P1", "repo_path": str(repo), "branch": launch.branch, "head": launch.head, "engine": "aibroker", "state": "completed", "dispatch_id": "older-dispatch", "decision_id": "older-decision", "execution_id": "older-execution", "resource_context": resource},
            older_review: {"project_id": "p1", "source_request_id": older_review, "source_kind": "remediation", "task_id": "P1", "repo_path": str(repo), "branch": launch.branch, "head": launch.head, "engine": "aibroker", "state": "completed", "dispatch_id": "older-remediation-dispatch", "decision_id": "older-remediation-decision", "execution_id": "older-remediation-execution", "resource_context": resource},
        }}), encoding="utf-8")
        (runtime / "ai-reviewer.json").write_text(json.dumps({"version": 1, "reviews": {
            old_review: {"review_id": old_review, "project_id": "p1", "source_request_id": source_id, "task_id": "P1", "branch": reviewed.branch, "head": reviewed.head, "review_status_hash": reviewed.status_hash, "state": "completed"},
            older_review: {"review_id": older_review, "project_id": "p1", "source_request_id": older_source_id, "task_id": "P1", "branch": launch.branch, "head": launch.head, "review_status_hash": launch.status_hash, "state": "completed"},
            "ai_review:" + older_review: {"review_id": "ai_review:" + older_review, "project_id": "p1", "source_request_id": older_review, "task_id": "P1", "branch": reviewed.branch, "head": reviewed.head, "review_status_hash": reviewed.status_hash, "state": "completed"},
        }}), encoding="utf-8")
        (runtime / "review-decisions.json").write_text(json.dumps({"version": 1, "decisions": {
            old_review: {"project_id": "p1", "request_id": old_review, "disposition": "apply", "decision": "remediate", "next_action": "continue_current_stage", "reason": "B review gap", "review_status_hash": reviewed.status_hash, "task_id": "P1", "branch": reviewed.branch, "head": reviewed.head, "role": "reviewer", "event": "worker_done"},
            older_review: {"project_id": "p1", "request_id": older_review, "disposition": "apply", "decision": "remediate", "next_action": "continue_current_stage", "reason": "older gap already remediated", "review_status_hash": launch.status_hash, "task_id": "P1", "branch": launch.branch, "head": launch.head, "role": "reviewer", "event": "worker_done"},
            "ai_review:" + older_review: {"project_id": "p1", "request_id": "ai_review:" + older_review, "disposition": "apply", "decision": "next", "next_action": "next_task", "reason": "older remediation reviewed", "review_status_hash": reviewed.status_hash, "task_id": "P1", "branch": reviewed.branch, "head": reviewed.head, "role": "reviewer", "event": "worker_done"},
        }}), encoding="utf-8")
        return repo, runtime, config, project, snapshot, old_review

    def test_surface_target_paused_launch_idempotency_and_no_worker(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, config, project, snapshot, old_review = self.fixture(Path(td))
            view = project_control_view(snapshot, runtime, project)
            capability = next(row for row in view["controls"] if row["action"] == "reconcile")
            self.assertTrue(capability["available"], capability)
            self.assertEqual(capability["target_id"], old_review)
            OwnerControlStore(runtime).set_paused("p1", True, command_id="pause", action="pause")
            paused_view = project_control_view(snapshot, runtime, project)
            port = ReviewerPort(); reviewer = AIReviewerCoordinator(runtime, port)
            command = submit_control_command(runtime, "p1", "reconcile", command_id="reanchor-one", expected=paused_view["control_identity"], target={"target_id": old_review})
            executor = FakeExecutor()
            result = ControlCommandCoordinator(runtime, reviewer=reviewer).advance(config, {"projects": [snapshot]}, executor)[0]
            self.assertEqual(result["state"], "accepted")
            self.assertEqual(result["effect"], "reconcile_stale_technical_review_no_worker_started")
            self.assertTrue(OwnerControlStore(runtime).is_paused("p1"))
            self.assertEqual(executor.calls, [])
            review_id = "ai_review:reconcile:reanchor-one"
            reviewer._threads[review_id].join(timeout=2)
            self.assertEqual(len(port.requests), 1)
            self.assertIn("[STALE_REVIEW_REANCHOR]", port.requests[0].prompt)
            self.assertEqual(port.requests[0].previous_resource_context.resource_id, "worker/default")
            decisions = json.loads((runtime / "review-decisions.json").read_text(encoding="utf-8"))["decisions"]
            self.assertEqual(decisions[review_id]["event"], "worker_done")
            self.assertEqual(decisions[review_id]["head"], snapshot["git"]["head"])
            self.assertEqual(submit_control_command(runtime, "p1", "reconcile", command_id="reanchor-one", expected=paused_view["control_identity"], target={"target_id": old_review}), result)
            self.assertEqual(ControlCommandCoordinator(runtime, reviewer=reviewer).advance(config, {"projects": [snapshot]}, executor), [])
            self.assertEqual(len(port.requests), 1)

    def test_reconcile_command_restart_recovers_persisted_review_launch(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, config, project, snapshot, old_review = self.fixture(Path(td))
            port = ReviewerPort()
            reviewer = AIReviewerCoordinator(runtime, port)
            candidate, reason = resolve_reconcile_candidate(snapshot, runtime, project)
            self.assertEqual(reason, "")
            review_id, launch_reason = reviewer.reconcile(project, snapshot, candidate, "crash-replay")
            self.assertIsNotNone(review_id, launch_reason)
            reviewer._threads[review_id].join(timeout=2)
            self.assertEqual(len(port.requests), 1)
            self.assertIsNone(resolve_reconcile_candidate(snapshot, runtime, project)[0])

            expected = project_identity(snapshot, runtime)
            submit_control_command(
                runtime, "p1", "reconcile", command_id="crash-replay",
                expected=expected, target={"target_id": old_review},
            )
            result = ControlCommandCoordinator(runtime, reviewer=reviewer).advance(
                config, {"projects": [snapshot]}, FakeExecutor()
            )[0]
            self.assertEqual(result["state"], "accepted")
            self.assertEqual(result["review_id"], review_id)
            self.assertIn("already launched", result["reason"])
            self.assertEqual(len(port.requests), 1)

    def test_stale_expected_identity_rejected_before_reconcile(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, config, project, snapshot, old_review = self.fixture(Path(td))
            stale = project_identity(snapshot, runtime); stale["head"] = "stale"
            submit_control_command(runtime, "p1", "reconcile", expected=stale, target={"target_id": old_review})
            result = ControlCommandCoordinator(runtime, reviewer=AIReviewerCoordinator(runtime, ReviewerPort())).advance(config, {"projects": [snapshot]}, FakeExecutor())[0]
            self.assertEqual(result["state"], "blocked")
            self.assertIn("stale project identity", result["reason"])

    def test_monitor_status_hash_is_optional_but_nonblank_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot, old_review = self.fixture(Path(td))
            for value in (None, "missing"):
                with self.subTest(value=value):
                    observed = {**snapshot, "git": dict(snapshot["git"])}
                    if value == "missing":
                        observed["git"].pop("status_hash")
                    else:
                        observed["git"]["status_hash"] = None
                    candidate, reason = resolve_reconcile_candidate(observed, runtime, project)
                    self.assertEqual(reason, "")
                    self.assertEqual(candidate["target_id"], old_review)
            mismatched = {**snapshot, "git": {**snapshot["git"], "status_hash": "not-current"}}
            self.assertIn("monitor repository identity is stale", resolve_reconcile_candidate(mismatched, runtime, project)[1])

    def test_wrong_target_and_review_id_conflict_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, config, project, snapshot, old_review = self.fixture(Path(td))
            expected = project_identity(snapshot, runtime)
            submit_control_command(runtime, "p1", "reconcile", command_id="wrong-target", expected=expected, target={"target_id": "other"})
            coordinator = ControlCommandCoordinator(runtime, reviewer=AIReviewerCoordinator(runtime, ReviewerPort()))
            wrong = coordinator.advance(config, {"projects": [snapshot]}, FakeExecutor())[0]
            self.assertEqual(wrong["state"], "blocked")
            self.assertIn("target_id does not match", wrong["reason"])
            state_path = runtime / "ai-reviewer.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["reviews"]["ai_review:reconcile:conflict"] = {
                "review_id": "ai_review:reconcile:conflict", "project_id": "p1",
                "source_request_id": "other-source", "task_id": "P1", "state": "completed",
                "reconcile_of": "other-review",
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            submit_control_command(runtime, "p1", "reconcile", command_id="conflict", expected=expected, target={"target_id": old_review})
            conflict = coordinator.advance(config, {"projects": [snapshot]}, FakeExecutor())[0]
            self.assertEqual(conflict["state"], "blocked")
            self.assertIn("conflicting reconcile review replay", conflict["reason"])

    def test_dirty_nonancestor_and_missing_evidence_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, _, project, snapshot, old_review = self.fixture(Path(td))
            (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
            self.assertIn("dirty", resolve_reconcile_candidate(snapshot, runtime, project)[1])
            (repo / "dirty.txt").unlink()
            ledger_path = runtime / "transition-executor.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["executions"]["worker-source"].pop("resource_context")
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            self.assertIn("resource evidence", resolve_reconcile_candidate(snapshot, runtime, project)[1])
            ledger["executions"]["worker-source"]["resource_context"] = {"resource_id": "r", "provider": "p", "account": "a", "model": "m"}
            ledger["executions"][old_review].pop("session_id")
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            self.assertIn("pre-provider", resolve_reconcile_candidate(snapshot, runtime, project)[1])
            ledger["executions"][old_review]["session_id"] = None
            ledger["executions"][old_review].pop("provider_output_observed")
            ledger["executions"][old_review].pop("first_output_at")
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            self.assertEqual(resolve_reconcile_candidate(snapshot, runtime, project)[0]["target_id"], old_review)
            ledger["executions"][old_review]["provider_output_observed"] = False
            ledger["executions"][old_review]["first_output_at"] = None
            decision_path = runtime / "review-decisions.json"; reviews_path = runtime / "ai-reviewer.json"
            decisions = json.loads(decision_path.read_text(encoding="utf-8")); reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
            # Make the old source/review a real, unrelated commit by using a side branch.
            subprocess.run(["git", "checkout", "-qb", "side", "HEAD~1"], cwd=repo, check=True)
            (repo / "side.txt").write_text("side", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True); subprocess.run(["git", "commit", "-qm", "side"], cwd=repo, check=True)
            unrelated = read_repository_truth(repo).head
            subprocess.run(["git", "checkout", "-q", snapshot["git"]["branch"]], cwd=repo, check=True)
            decisions["decisions"][old_review]["head"] = unrelated
            reviews["reviews"][old_review]["head"] = unrelated
            ledger["executions"][old_review]["head"] = unrelated
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8"); decision_path.write_text(json.dumps(decisions), encoding="utf-8"); reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
            self.assertIn("not an ancestor", resolve_reconcile_candidate(snapshot, runtime, project)[1])

    def test_multiple_unresolved_stale_remediations_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot, old_review = self.fixture(Path(td))
            ledger_path = runtime / "transition-executor.json"
            decisions_path = runtime / "review-decisions.json"
            reviews_path = runtime / "ai-reviewer.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
            reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
            second_source = "second-worker"; second_review = "ai_review:" + second_source
            source = dict(ledger["executions"]["worker-source"])
            source["source_request_id"] = second_source
            ledger["executions"][second_source] = source
            review = dict(reviews["reviews"][old_review])
            review.update({"review_id": second_review, "source_request_id": second_source})
            reviews["reviews"][second_review] = review
            decision = dict(decisions["decisions"][old_review]); decision["request_id"] = second_review
            decisions["decisions"][second_review] = decision
            remediation = dict(ledger["executions"][old_review]); remediation["source_request_id"] = second_review
            ledger["executions"][second_review] = remediation
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")
            reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
            self.assertIn("multiple", resolve_reconcile_candidate(snapshot, runtime, project)[1])

    def test_transition_retry_consumes_stale_review_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot, old_review = self.fixture(Path(td))
            ledger_path = runtime / "transition-executor.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["executions"]["retry-after-review"] = {
                "project_id": "p1", "source_request_id": "retry-after-review",
                "state": "completed", "recovery_of": old_review,
            }
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            candidate, reason = resolve_reconcile_candidate(snapshot, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("no exact", reason)

    def test_durable_reanchor_consumes_stale_review_candidate(self):
        for state_name in ("completed", "failed", "recovery_required"):
            with self.subTest(state=state_name), tempfile.TemporaryDirectory() as td:
                _, runtime, _, project, snapshot, old_review = self.fixture(Path(td))
                state_path = runtime / "ai-reviewer.json"
                reviewer_state = json.loads(state_path.read_text(encoding="utf-8"))
                reviewer_state["reviews"]["ai_review:reconcile:prior"] = {
                    "review_id": "ai_review:reconcile:prior", "project_id": "p1",
                    "source_request_id": "worker-source", "task_id": "P1",
                    "state": state_name, "reconcile_of": old_review,
                }
                state_path.write_text(json.dumps(reviewer_state), encoding="utf-8")
                candidate, _reason = resolve_reconcile_candidate(snapshot, runtime, project)
                self.assertIsNone(candidate)

    def test_active_role_and_source_launch_nonancestor_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot, _ = self.fixture(Path(td))
            active = {**snapshot, "reviewer": {"state": "running"}}
            candidate, reason = resolve_reconcile_candidate(active, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("active reviewer", reason)

        with tempfile.TemporaryDirectory() as td:
            repo, runtime, _, project, snapshot, _ = self.fixture(Path(td))
            subprocess.run(["git", "checkout", "-qb", "source-side", "HEAD~2"], cwd=repo, check=True)
            (repo / "side.txt").write_text("side", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "source side"], cwd=repo, check=True)
            unrelated_source = read_repository_truth(repo).head
            subprocess.run(["git", "checkout", "-q", snapshot["git"]["branch"]], cwd=repo, check=True)
            ledger_path = runtime / "transition-executor.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["executions"]["worker-source"]["head"] = unrelated_source
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            candidate, reason = resolve_reconcile_candidate(snapshot, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("source launch HEAD is not an ancestor", reason)


if __name__ == "__main__":
    unittest.main()
