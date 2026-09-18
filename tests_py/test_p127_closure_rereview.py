"""Focused P12.7 closure re-review tests for a stale accepted NEXT decision.

The current re-review surface already re-anchors a failed reviewer lineage at a
clean descendant HEAD.  These tests pin the extended semantics for a *stale
accepted NEXT*: a durable next/next_task decision recorded at an ancestor HEAD
for the same completed current task must be able to launch exactly one
independent AIBroker reviewer at the current clean descendant HEAD, without
launching a Worker or importing the prior verdict, and remain replay-idempotent.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.control.reconcile import resolve_rereview_candidate
from dev_orchestrator.control.surface import project_control_view
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.core.repository import read_repository_truth
from tests_py.test_control_commands import FakeExecutor
from tests_py.test_p125_reconcile import P125ReconcileTests, ReviewerPort


ACCEPTED_NEXT_REVIEW = "ai_review:ai_review:older-worker-source"


class P127ClosureRereviewTests(unittest.TestCase):
    def fixture(self, root: Path):
        repo, runtime, config, project, snapshot, _ = P125ReconcileTests().fixture(root)
        # The P125 fixture records an accepted NEXT decision at an ancestor HEAD,
        # but omits the explicit clean-worktree flag that real launches persist.
        reviews_path = runtime / "ai-reviewer.json"
        reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
        self.assertIn(ACCEPTED_NEXT_REVIEW, reviews["reviews"])
        reviews["reviews"][ACCEPTED_NEXT_REVIEW]["review_dirty"] = False
        reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
        return repo, runtime, config, project, snapshot

    def test_accepted_stale_next_rereviews_clean_descendant_without_worker(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, config, project, snapshot = self.fixture(Path(td))
            candidate, reason = resolve_rereview_candidate(snapshot, runtime, project)
            self.assertEqual(reason, "")
            self.assertEqual(candidate["target_id"], ACCEPTED_NEXT_REVIEW)
            self.assertEqual(candidate["kind"], "next")

            view = project_control_view(snapshot, runtime, project)
            control = next(row for row in view["controls"] if row["action"] == "rereview")
            self.assertTrue(control["available"], control)
            self.assertEqual(control["target_id"], ACCEPTED_NEXT_REVIEW)

            submit_control_command(
                runtime, "p1", "rereview", command_id="rereview-next",
                expected=view["control_identity"], target={"target_id": ACCEPTED_NEXT_REVIEW},
            )
            port = ReviewerPort()
            reviewer = AIReviewerCoordinator(runtime, port)
            executor = FakeExecutor()
            result = ControlCommandCoordinator(runtime, reviewer=reviewer).advance(
                config, {"projects": [snapshot]}, executor
            )[0]
            self.assertEqual(result["state"], "accepted", result)
            self.assertEqual(result["effect"], "rereview_technical_review_no_worker_started")
            self.assertEqual(executor.calls, [])

            review_id = "ai_review:rereview:rereview-next"
            reviewer._threads[review_id].join(timeout=2)
            self.assertEqual(len(port.requests), 1)
            prompt = port.requests[0].prompt
            self.assertIn("[ACCEPTED_NEXT_DESCENDANT_REREVIEW]", prompt)
            self.assertIn("do not inherit or import the prior accepted verdict", prompt)
            self.assertNotIn("[FAILED_REVIEW_DESCENDANT_REREVIEW]", prompt)
            state = reviewer.state()["reviews"][review_id]
            self.assertEqual(state["rereview_of"], ACCEPTED_NEXT_REVIEW)
            self.assertEqual(state["head"], snapshot["git"]["head"])

    def test_dirty_rejects_accepted_next_rereview(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, _, project, snapshot = self.fixture(Path(td))
            (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
            candidate, reason = resolve_rereview_candidate(snapshot, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("dirty", reason)

    def test_non_descendant_rejects_accepted_next_rereview(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, _, project, snapshot = self.fixture(Path(td))
            subprocess.run(["git", "checkout", "-qb", "side", "HEAD~1"], cwd=repo, check=True)
            (repo / "side.txt").write_text("side", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "side"], cwd=repo, check=True)
            unrelated = read_repository_truth(repo).head
            subprocess.run(["git", "checkout", "-q", snapshot["git"]["branch"]], cwd=repo, check=True)

            reviews_path = runtime / "ai-reviewer.json"
            reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
            reviews["reviews"][ACCEPTED_NEXT_REVIEW]["head"] = unrelated
            reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
            decisions_path = runtime / "review-decisions.json"
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
            decisions["decisions"][ACCEPTED_NEXT_REVIEW]["head"] = unrelated
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")

            candidate, reason = resolve_rereview_candidate(snapshot, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("no safe stale-review", reason)

    def test_task_mismatch_rejects_accepted_next_rereview(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot = self.fixture(Path(td))
            reviews_path = runtime / "ai-reviewer.json"
            reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
            reviews["reviews"][ACCEPTED_NEXT_REVIEW]["task_id"] = "P2"
            reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
            decisions_path = runtime / "review-decisions.json"
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
            decisions["decisions"][ACCEPTED_NEXT_REVIEW]["task_id"] = "P2"
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")

            candidate, reason = resolve_rereview_candidate(snapshot, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("no safe stale-review", reason)

    def test_active_role_rejects_accepted_next_rereview(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot = self.fixture(Path(td))
            active = {**snapshot, "reviewer": {"state": "running"}}
            candidate, reason = resolve_rereview_candidate(active, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("active reviewer", reason)

    def test_accepted_next_rereview_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, config, project, snapshot = self.fixture(Path(td))
            port = ReviewerPort()
            reviewer = AIReviewerCoordinator(runtime, port)
            candidate, reason = resolve_rereview_candidate(snapshot, runtime, project)
            self.assertEqual(reason, "")
            review_id, launch_reason = reviewer.rereview_descendant(
                project, snapshot, candidate, "rereview-replay"
            )
            self.assertIsNotNone(review_id, launch_reason)
            reviewer._threads[review_id].join(timeout=2)
            self.assertEqual(len(port.requests), 1)
            # The persisted rereview_of row consumes the stale candidate.
            self.assertIsNone(resolve_rereview_candidate(snapshot, runtime, project)[0])

            view = project_control_view(snapshot, runtime, project)
            submit_control_command(
                runtime, "p1", "rereview", command_id="rereview-replay",
                expected=view["control_identity"], target={"target_id": ACCEPTED_NEXT_REVIEW},
            )
            result = ControlCommandCoordinator(runtime, reviewer=reviewer).advance(
                config, {"projects": [snapshot]}, FakeExecutor()
            )[0]
            self.assertEqual(result["state"], "accepted", result)
            self.assertEqual(result["review_id"], review_id)
            self.assertIn("already launched", result["reason"])
            self.assertEqual(len(port.requests), 1)


if __name__ == "__main__":
    unittest.main()
