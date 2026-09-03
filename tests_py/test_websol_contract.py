import unittest

from dev_orchestrator.core.decision import DecisionDisposition, validate_websol_response
from dev_orchestrator.core.repository import RepositoryTruth
from dev_orchestrator.core.websol import (
    NextAction,
    WebSolDecision,
    WebSolEvent,
    WebSolRequest,
    WebSolResponse,
    WebSolRole,
    should_send_to_web_sol,
)


def request(**changes):
    values = dict(
        project_id="labdemo", request_id="req-1", task_id="P4.3.3",
        stage_id="P4.3", branch="service-control", head="abc123",
        role=WebSolRole.REVIEWER, event=WebSolEvent.REVIEW_REQUIRED, nonce="n-1",
    )
    values.update(changes)
    return WebSolRequest(**values)


def response(**changes):
    values = dict(
        project_id="labdemo", request_id="req-1", task_id="P4.3.3",
        stage_id="P4.3", branch="service-control", head="abc123",
        role=WebSolRole.REVIEWER, event=WebSolEvent.REVIEW_REQUIRED, nonce="n-1",
        decision=WebSolDecision.NEXT, next_action=NextAction.NEXT_TASK,
    )
    values.update(changes)
    return WebSolResponse(**values)
class WebSolContractTests(unittest.TestCase):
    def setUp(self):
        self.truth = RepositoryTruth(
            repo_path="D:/repo", branch="service-control", head="abc123",
            dirty=False, dirty_entries=(), status_hash="clean", valid=True, error="",
        )

    def test_only_reasoning_events_are_sent_to_web_sol(self):
        reasoning = {
            WebSolEvent.REVIEW_REQUIRED, WebSolEvent.WORKER_DONE,
            WebSolEvent.TEST_FAILED, WebSolEvent.OWNER_GATE,
            WebSolEvent.RECOVERY_REQUIRED,
        }
        for event in WebSolEvent:
            self.assertEqual(should_send_to_web_sol(event), event in reasoning)

    def test_valid_decision_preserves_explicit_transition_scope(self):
        verdict = validate_websol_response(request(), response(), self.truth)
        self.assertEqual(verdict.disposition, DecisionDisposition.APPLY)
        self.assertEqual(verdict.next_action, NextAction.NEXT_TASK)
        stage = validate_websol_response(
            request(), response(next_action=NextAction.NEXT_STAGE), self.truth
        )
        self.assertEqual(stage.disposition, DecisionDisposition.APPLY)
        self.assertEqual(stage.next_action, NextAction.NEXT_STAGE)
    def test_identity_head_action_dirty_and_owner_gates_fail_closed(self):
        self.assertEqual(
            validate_websol_response(request(), response(request_id="other"), self.truth).disposition,
            DecisionDisposition.IGNORE,
        )
        self.assertEqual(
            validate_websol_response(request(), response(nonce="other"), self.truth).disposition,
            DecisionDisposition.IGNORE,
        )
        stale = RepositoryTruth(
            repo_path="D:/repo", branch="service-control", head="def456",
            dirty=False, dirty_entries=(), status_hash="clean", valid=True, error="",
        )
        self.assertEqual(
            validate_websol_response(request(), response(), stale).disposition,
            DecisionDisposition.STALE,
        )
        self.assertEqual(
            validate_websol_response(request(), response(next_action=None), self.truth).disposition,
            DecisionDisposition.STOP,
        )
        dirty = RepositoryTruth(
            repo_path="D:/repo", branch="service-control", head="abc123",
            dirty=True, dirty_entries=(" M file.txt",), status_hash="dirty", valid=True, error="",
        )
        self.assertEqual(
            validate_websol_response(request(), response(), dirty).disposition,
            DecisionDisposition.REVIEW_REQUIRED,
        )
        owner = response(decision=WebSolDecision.OWNER_GATE, next_action=NextAction.STOP)
        self.assertEqual(
            validate_websol_response(request(), owner, self.truth).disposition,
            DecisionDisposition.OWNER_GATE,
        )
        invalid = response(decision=WebSolDecision.REMEDIATE, next_action=NextAction.NEXT_STAGE)
        self.assertEqual(
            validate_websol_response(request(), invalid, self.truth).disposition,
            DecisionDisposition.STOP,
        )

    def test_request_requires_task_or_stage_identity(self):
        with self.assertRaises(ValueError):
            request(task_id=None, stage_id=None)


if __name__ == "__main__":
    unittest.main()
