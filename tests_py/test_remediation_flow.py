import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.core.decision import DecisionDisposition, validate_websol_response
from dev_orchestrator.core.repository import RepositoryTruth, read_repository_truth
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.websol import (
    NextAction, WebSolDecision, WebSolEvent, WebSolRequest, WebSolResponse, WebSolRole,
)
from tests_py.test_transition_executor import FakeBackend, make_repo, ready_summary, wait_terminal


def request_response():
    request = WebSolRequest(
        project_id="p1", request_id="worker_done:p1:r1", task_id="P1", stage_id=None,
        branch="master", head="abc", role=WebSolRole.REVIEWER,
        event=WebSolEvent.WORKER_DONE, nonce="nonce",
    )
    response = WebSolResponse(
        project_id=request.project_id, request_id=request.request_id,
        task_id=request.task_id, stage_id=request.stage_id, branch=request.branch,
        head=request.head, role=request.role, event=request.event, nonce=request.nonce,
        decision=WebSolDecision.REMEDIATE,
        next_action=NextAction.CONTINUE_CURRENT_STAGE,
    )
    return request, response


class KnownDirtyDecisionTests(unittest.TestCase):
    def test_matching_reviewed_dirty_fingerprint_allows_remediation(self):
        request, response = request_response()
        truth = RepositoryTruth(
            repo_path="repo", branch="master", head="abc", dirty=True,
            dirty_entries=("?? a.txt",), status_hash="fingerprint", valid=True,
        )
        verdict = validate_websol_response(
            request, response, truth, known_dirty_status_hash="fingerprint"
        )
        self.assertEqual(verdict.disposition, DecisionDisposition.APPLY)
        self.assertEqual(verdict.next_action, NextAction.CONTINUE_CURRENT_STAGE)

    def test_changed_dirty_fingerprint_fails_closed(self):
        request, response = request_response()
        truth = RepositoryTruth(
            repo_path="repo", branch="master", head="abc", dirty=True,
            dirty_entries=("?? a.txt", "?? b.txt"), status_hash="changed", valid=True,
        )
        verdict = validate_websol_response(
            request, response, truth, known_dirty_status_hash="reviewed"
        )
        self.assertEqual(verdict.disposition, DecisionDisposition.REVIEW_REQUIRED)
        self.assertIsNone(verdict.next_action)


class RemediationActuationTests(unittest.TestCase):
    def _config(self, path: Path, repo: Path):
        execution = {
            "enabled": True, "owner_authorized": True,
            "allowed_next_actions": ["continue_current_stage", "next_task"],
            "preferred_backends": ["agy"], "backends": {"agy": {}},
        }
        path.write_text(json.dumps({"projects": [{
            "project_id": "p1", "repo_path": str(repo), "execution": execution,
        }]}), encoding="utf-8")

    def _decision(self, runtime: Path, head: str, status_hash: str):
        request_id = "worker_done:p1:r1"
        record = {
            "project_id": "p1", "request_id": request_id,
            "disposition": "apply", "decision": "remediate",
            "next_action": "continue_current_stage", "task_id": "P1",
            "stage_id": None, "branch": "master", "head": head,
            "role": "reviewer", "event": "worker_done",
            "review_status_hash": status_hash,
            "consumed_at": "2026-09-05T00:00:00+00:00",
        }
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "websol-decisions.json").write_text(
            json.dumps({"version": 1, "decisions": {request_id: record}}),
            encoding="utf-8",
        )
        return request_id

    def test_reviewed_dirty_same_task_launches_remediation_once(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            head = make_repo(repo, "P1")
            (repo / "work.txt").write_text("reviewed dirty\n", encoding="utf-8")
            truth = read_repository_truth(repo)
            helper = RemediationActuationTests()
            config = base / "projects.json"; helper._config(config, repo)
            request_id = helper._decision(runtime, head, truth.status_hash)
            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            launches = executor.advance(ready_summary(repo, "P1"), config)
            self.assertEqual(len(launches), 1)
            record = wait_terminal(executor, request_id)
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["task_id"], "P1")
            self.assertEqual(record["source_kind"], "remediation")
            self.assertEqual(len(backend.started), 1)
            self.assertEqual(executor.advance(ready_summary(repo, "P1"), config), [])
            self.assertEqual(len(backend.started), 1)

    def test_dirty_change_after_review_blocks_remediation(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            head = make_repo(repo, "P1")
            (repo / "work.txt").write_text("reviewed dirty\n", encoding="utf-8")
            truth = read_repository_truth(repo)
            config = base / "projects.json"; self._config(config, repo)
            request_id = self._decision(runtime, head, truth.status_hash)
            (repo / "late.txt").write_text("unreviewed\n", encoding="utf-8")
            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            self.assertEqual(executor.advance(ready_summary(repo, "P1"), config), [])
            record = executor.state()["executions"][request_id]
            self.assertEqual(record["state"], "blocked")
            self.assertIn("fingerprint", record["reason"])
            self.assertEqual(len(backend.started), 0)


class RemediationConsumptionTests(unittest.TestCase):
    def test_dispatch_fingerprint_round_trips_into_apply_remediation(self):
        from dev_orchestrator.bridge.store import BrowserBridgeStore
        from dev_orchestrator.core.dispatcher import dispatch_worker_done_events
        from dev_orchestrator.core.response_consumer import consume_websol_responses
        from tests_py.test_response_consumer import respond, response_text
        from tests_py.test_worker_done_dispatcher import make_repo, snap

        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo)
            (repo / "reviewed.txt").write_text("dirty work\n", encoding="utf-8")
            store = BrowserBridgeStore(runtime / "bridge")
            summary = {"projects": [snap("p1", repo, "conv", "run-remediate")]}
            dispatched = dispatch_worker_done_events(summary, store, runtime)
            self.assertEqual(len(dispatched), 1)
            claim = store.claim("chatgpt_web", "conv")
            self.assertIsNotNone(claim)
            respond(store, claim, response_text(
                claim, decision="remediate", next_action="continue_current_stage"
            ))
            outcomes = consume_websol_responses(summary, store, runtime)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].disposition, DecisionDisposition.APPLY)
            self.assertEqual(outcomes[0].decision, WebSolDecision.REMEDIATE)
            self.assertTrue(outcomes[0].review_status_hash)
            ledger = json.loads((runtime / "websol-decisions.json").read_text(encoding="utf-8"))
            record = ledger["decisions"][claim.request_id]
            self.assertEqual(record["decision"], "remediate")
            self.assertEqual(record["next_action"], "continue_current_stage")
            self.assertEqual(record["review_status_hash"], outcomes[0].review_status_hash)


if __name__ == "__main__":
    unittest.main()


class RemediationWaitingReviewTests(unittest.TestCase):
    def test_waiting_review_same_task_can_enter_exact_remediation(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            head = make_repo(repo, "P1")
            truth = read_repository_truth(repo)
            helper = RemediationActuationTests()
            config = base / "projects.json"; helper._config(config, repo)
            request_id = helper._decision(runtime, head, truth.status_hash)
            backend = FakeBackend("agy")
            executor = TransitionExecutor(runtime, backend_overrides={"agy": backend})
            summary = ready_summary(repo, "P1")
            summary["projects"][0]["state"] = "WAITING_REVIEW"
            launches = executor.advance(summary, config)
            self.assertEqual(len(launches), 1)
            record = wait_terminal(executor, request_id)
            self.assertEqual(record["state"], "completed")
            self.assertEqual(len(backend.started), 1)
