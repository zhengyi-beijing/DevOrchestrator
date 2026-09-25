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

from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
from dev_orchestrator.control.owner_store import OwnerControlStore
from dev_orchestrator.control.reconcile import resolve_rereview_candidate
from dev_orchestrator.control.surface import project_control_view
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.transition_executor import TransitionExecutor
from tests_py.test_control_commands import FakeExecutor, FakePlanner
from tests_py.test_p125_reconcile import P125ReconcileTests, ReviewerPort


ACCEPTED_NEXT_REVIEW = "ai_review:ai_review:older-worker-source"


class NextReviewerPort:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return AIRoleResult(
            request_id=request.request_id,
            role_run_id=request.role_run_id,
            status="succeeded",
            output=json.dumps({
                "decision": "next",
                "next_action": "next_task",
                "reason": "current descendant remediation is accepted",
            }),
            dispatch_id="review-dispatch-next",
            decision_id="review-decision-next",
            execution_id="review-execution-next",
            resource_context=ResourceContext(
                "reviewer/independent", "review-provider", "review-account", "review-model"
            ),
        )


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

    def predecessor_fixture(self, root: Path):
        repo, runtime, config, project, snapshot, remediate_review = P125ReconcileTests().fixture(root)

        # Keep one completed REMEDIATE lineage backed by its completed worker.
        reviews_path = runtime / "ai-reviewer.json"
        reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
        reviews["reviews"] = {remediate_review: reviews["reviews"][remediate_review]}
        reviews["reviews"][remediate_review]["review_dirty"] = False
        reviews_path.write_text(json.dumps(reviews), encoding="utf-8")

        decisions_path = runtime / "review-decisions.json"
        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
        decisions["decisions"] = {remediate_review: decisions["decisions"][remediate_review]}
        decisions_path.write_text(json.dumps(decisions), encoding="utf-8")

        transitions_path = runtime / "transition-executor.json"
        transitions = json.loads(transitions_path.read_text(encoding="utf-8"))
        transitions["executions"] = {
            key: value for key, value in transitions["executions"].items()
            if key in {"worker-source", remediate_review}
        }
        transitions_path.write_text(json.dumps(transitions), encoding="utf-8")

        staged = repo / "agent" / "staged"
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "P2.md").write_text(
            "# P2 Pending Successor\n\nStatus: **PENDING DESIGN**\n",
            encoding="utf-8",
            newline="\n",
        )
        (staged / "roadmap.json").write_text(
            json.dumps({
                "schema_version": 1,
                "tasks": [
                    {
                        "task_id": "P1",
                        "successor": "P2",
                        "successor_spec_path": "agent/staged/P2.md",
                    },
                    {
                        "task_id": "P2",
                        "successor": None,
                        "successor_spec_path": None,
                    },
                ],
            }),
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "stage P2 successor"],
            cwd=repo,
            check=True,
        )
        current = read_repository_truth(repo)

        snapshot = {
            **snapshot,
            "state": "IDLE",
            "lifecycle_state": "IDLE",
            "next_status": "**PENDING DESIGN**",
            "telemetry": {"task_id": "P2"},
            "git": {
                "branch": current.branch,
                "head": current.head,
                "dirty": False,
                "status_hash": current.status_hash,
            },
        }
        project["execution"] = {
            "enabled": True,
            "owner_authorized": True,
            "engine": "aibroker",
            "allowed_next_actions": ["continue_current_stage", "next_task"],
            "worker_prompt": "worker prompt",
            "remediation_prompt": "remediation prompt",
        }
        config.write_text(json.dumps({"projects": [project]}), encoding="utf-8")
        return repo, runtime, config, project, snapshot, remediate_review

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

    def test_done_task_hides_stale_descendant_rereview_as_audit_history(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot = self.fixture(Path(td))
            terminal = {**snapshot, "state": "IDLE", "lifecycle_state": "IDLE", "next_status": "**DONE**"}
            candidate, reason = resolve_rereview_candidate(terminal, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("terminal", reason)
            view = project_control_view(terminal, runtime, project)
            control = next(row for row in view["controls"] if row["action"] == "rereview")
            self.assertFalse(control["available"], control)

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

    def test_remediate_predecessor_matches_exact_pending_successor(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot, remediate_review = self.predecessor_fixture(Path(td))
            candidate, reason = resolve_rereview_candidate(snapshot, runtime, project)
            self.assertEqual(reason, "")
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["target_id"], remediate_review)
            self.assertEqual(candidate["kind"], "remediate")
            self.assertEqual(candidate["task_id"], "P1")

            view = project_control_view(snapshot, runtime, project)
            control = next(row for row in view["controls"] if row["action"] == "rereview")
            self.assertTrue(control["available"], control)
            self.assertEqual(control["target_id"], remediate_review)

    def test_owner_gate_clean_descendant_has_one_rereview_and_consumes_gate(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot, review_id = self.predecessor_fixture(Path(td))
            decisions_path = runtime / "review-decisions.json"
            payload = json.loads(decisions_path.read_text(encoding="utf-8"))
            decision = payload["decisions"][review_id]
            decision["decision"] = "owner_gate"
            decision["next_action"] = "stop"
            decision["disposition"] = "owner_gate"
            decisions_path.write_text(json.dumps(payload), encoding="utf-8")

            candidate, reason = resolve_rereview_candidate(snapshot, runtime, project)
            self.assertEqual(reason, "")
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["target_id"], review_id)
            self.assertEqual(candidate["kind"], "remediate")

            view = project_control_view(snapshot, runtime, project)
            control = next(row for row in view["controls"] if row["action"] == "rereview")
            self.assertTrue(control["available"], control)
            self.assertEqual(control["target_id"], review_id)

            reviewer = AIReviewerCoordinator(runtime, ReviewerPort())
            new_review_id, launch_reason = reviewer.rereview_descendant(
                project, snapshot, candidate, "owner-gate-descendant-rereview"
            )
            self.assertIsNotNone(new_review_id, launch_reason)
            rereview = reviewer.state()["reviews"][new_review_id]
            self.assertEqual(rereview["rereview_of"], review_id)
            consumed = project_control_view(snapshot, runtime, project)
            self.assertIsNone(consumed["gate"])

    def test_cross_task_rereview_rejects_non_successor_or_non_pending_current_task(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot, _ = self.predecessor_fixture(Path(td))

            unrelated = {
                **snapshot,
                "telemetry": {"task_id": "P3"},
            }
            candidate, reason = resolve_rereview_candidate(unrelated, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("no safe stale-review", reason)

            executable = {
                **snapshot,
                "next_status": "**READY_TO_RUN**",
            }
            candidate, reason = resolve_rereview_candidate(executable, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("no safe stale-review", reason)

    def test_remediate_predecessor_rereview_flows_to_handoff_and_planner(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, config, project, snapshot, remediate_review = self.predecessor_fixture(Path(td))
            candidate, reason = resolve_rereview_candidate(snapshot, runtime, project)
            self.assertEqual(reason, "")
            self.assertEqual(candidate["target_id"], remediate_review)

            port = NextReviewerPort()
            reviewer = AIReviewerCoordinator(runtime, port)
            review_id, launch_reason = reviewer.rereview_descendant(
                project, snapshot, candidate, "p15-style-rereview"
            )
            self.assertIsNotNone(review_id, launch_reason)
            reviewer._threads[review_id].join(timeout=3)
            self.assertEqual(len(port.requests), 1)
            self.assertIn("[REMEDIATED_DESCENDANT_REREVIEW]", port.requests[0].prompt)
            self.assertEqual(port.requests[0].task_run_id, "P1")

            decisions = json.loads(
                (runtime / "review-decisions.json").read_text(encoding="utf-8")
            )["decisions"]
            accepted = decisions[review_id]
            self.assertEqual(accepted["decision"], "next")
            self.assertEqual(accepted["next_action"], "next_task")
            self.assertEqual(accepted["task_id"], "P1")
            self.assertEqual(accepted["head"], snapshot["git"]["head"])

            # Legacy static-start suppression must not gate a durable review
            # decision or the subsequent PENDING DESIGN planner handoff.
            owner = OwnerControlStore(runtime)
            owner.set_paused(
                "p1", False, command_id="resume-after-stop", action="resume"
            )
            self.assertFalse(owner.is_paused("p1"))
            self.assertTrue(owner.suppress_static_starts("p1"))

            executor = TransitionExecutor(runtime)
            executor.advance({"projects": [snapshot]}, config)
            handoff = executor.state()["executions"][review_id]
            self.assertEqual(handoff["state"], "handoff")
            self.assertEqual(handoff["outcome"], "planning_required")
            self.assertEqual(handoff["task_id"], "P1")
            self.assertEqual(handoff["next_task_id"], "P2")

            planner = FakePlanner()
            outcomes = ControlCommandCoordinator(runtime, planner=planner).advance(
                config, {"projects": [snapshot]}, executor
            )
            auto = [
                row for row in outcomes
                if row.get("source") == "automatic_review_handoff"
            ]
            self.assertEqual(len(auto), 1, outcomes)
            self.assertEqual(auto[0]["state"], "accepted", auto[0])
            self.assertEqual(auto[0]["lifecycle_action"], "plan")
            self.assertEqual(len(planner.calls), 1)
            self.assertEqual(planner.calls[0][0], "p1")
            self.assertTrue(
                executor.state()["executions"][review_id].get("handoff_consumed")
            )


if __name__ == "__main__":
    unittest.main()
