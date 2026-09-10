import unittest
from pathlib import Path

from dev_orchestrator.ai import AIRoleRequest, AIRoleResult, ResourceContext
from dev_orchestrator.core.ai_role_loop import MinimalAIRoleLoop


class FakePort:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return self.results.pop(0)


def make_result(role_run_id, request_id, *, status="succeeded", output="OK", resource="r1"):
    context = None
    if resource is not None:
        context = ResourceContext(resource, "provider", "account", "model")
    return AIRoleResult(
        request_id=request_id,
        role_run_id=role_run_id,
        status=status,
        output=output,
        dispatch_id="dispatch",
        decision_id="decision",
        execution_id="execution",
        resource_context=context,
    )


class MinimalAIRoleLoopTests(unittest.TestCase):
    def worker_request(self):
        return AIRoleRequest(
            project_id="project", task_run_id="task", stage_run_id="stage",
            role_run_id="worker-run", request_id="worker-request",
            role="worker", prompt="work", working_directory=Path("."),
            timeout_seconds=30,
        )

    def test_pass_emits_next_and_enforces_resource_independence(self):
        worker = make_result("worker-run", "worker-request", output="WORKER_OK", resource="worker-r")
        reviewer = make_result("review-run", "review-request", output="REVIEW_PASS", resource="review-r")
        port = FakePort(worker, reviewer)
        outcome = MinimalAIRoleLoop(port).run(
            self.worker_request(), reviewer_prompt="Verify worker output.",
            reviewer_request_id="review-request", reviewer_role_run_id="review-run",
        )
        self.assertEqual(outcome.state, "next")
        self.assertEqual(outcome.next_action, "NEXT")
        self.assertEqual(len(port.requests), 2)
        review_request = port.requests[1]
        self.assertEqual(review_request.role, "reviewer")
        self.assertEqual(review_request.independence, "resource")
        self.assertEqual(review_request.previous_resource_context.resource_id, "worker-r")
        self.assertIn("WORKER_OK", review_request.prompt)

    def test_worker_failure_stops_before_review(self):
        worker = make_result("worker-run", "worker-request", status="failed")
        port = FakePort(worker)
        outcome = MinimalAIRoleLoop(port).run(
            self.worker_request(), reviewer_prompt="review",
            reviewer_request_id="review-request", reviewer_role_run_id="review-run",
        )
        self.assertEqual(outcome.reason, "worker_failed")
        self.assertEqual(len(port.requests), 1)

    def test_missing_worker_resource_context_fails_closed(self):
        worker = make_result("worker-run", "worker-request", resource=None)
        port = FakePort(worker)
        outcome = MinimalAIRoleLoop(port).run(
            self.worker_request(), reviewer_prompt="review",
            reviewer_request_id="review-request", reviewer_role_run_id="review-run",
        )
        self.assertEqual(outcome.reason, "worker_resource_context_missing")
        self.assertEqual(len(port.requests), 1)

    def test_reviewer_no_candidate_and_rejection_stop(self):
        worker = make_result("worker-run", "worker-request", resource="worker-r")
        blocked = make_result(
            "review-run", "review-request", status="no_candidate", output=None, resource=None
        )
        port = FakePort(worker, blocked)
        outcome = MinimalAIRoleLoop(port).run(
            self.worker_request(), reviewer_prompt="review",
            reviewer_request_id="review-request", reviewer_role_run_id="review-run",
        )
        self.assertEqual(outcome.reason, "reviewer_no_candidate")

        worker2 = make_result("worker-run", "worker-request", resource="worker-r")
        rejected = make_result("review-run", "review-request", output="REVIEW_FAIL", resource="review-r")
        port2 = FakePort(worker2, rejected)
        outcome2 = MinimalAIRoleLoop(port2).run(
            self.worker_request(), reviewer_prompt="review",
            reviewer_request_id="review-request", reviewer_role_run_id="review-run",
        )
        self.assertEqual(outcome2.reason, "review_rejected")
        self.assertIsNone(outcome2.next_action)


if __name__ == "__main__":
    unittest.main()


class ReviewTokenParsingTests(unittest.TestCase):
    def test_final_markdown_wrapped_pass_token_is_accepted(self):
        worker = make_result("worker-run", "worker-request", resource="worker-r")
        reviewer = make_result(
            "review-run", "review-request",
            output="analysis mentions REVIEW_FAIL first\n\n**REVIEW_PASS**", resource="review-r",
        )
        outcome = MinimalAIRoleLoop(FakePort(worker, reviewer)).run(
            MinimalAIRoleLoopTests().worker_request(), reviewer_prompt="review",
            reviewer_request_id="review-request", reviewer_role_run_id="review-run",
        )
        self.assertEqual(outcome.next_action, "NEXT")

    def test_pass_token_in_body_does_not_override_final_rejection(self):
        worker = make_result("worker-run", "worker-request", resource="worker-r")
        reviewer = make_result(
            "review-run", "review-request", output="REVIEW_PASS\nREVIEW_FAIL", resource="review-r",
        )
        outcome = MinimalAIRoleLoop(FakePort(worker, reviewer)).run(
            MinimalAIRoleLoopTests().worker_request(), reviewer_prompt="review",
            reviewer_request_id="review-request", reviewer_role_run_id="review-run",
        )
        self.assertEqual(outcome.reason, "review_rejected")
