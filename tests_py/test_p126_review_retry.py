import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.control.reconcile import resolve_retry_candidate, resolve_rereview_candidate
from dev_orchestrator.control.surface import project_control_view
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.control_commands import ControlCommandCoordinator, submit_control_command
from dev_orchestrator.core.diagnostics import evidence_hash
from dev_orchestrator.core.watchdog import WatchdogCoordinator, compute_record_integrity_hash
from tests_py.test_control_commands import FakeExecutor
from tests_py.test_p125_reconcile import P125ReconcileTests, ReviewerPort


class P126ReviewRetryTests(unittest.TestCase):
    def fixture(self, root: Path):
        repo, runtime, config, project, snapshot, _ = P125ReconcileTests().fixture(root)
        failed_id = "ai_review:current-failed"
        reviews_path = runtime / "ai-reviewer.json"
        reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
        reviews["reviews"][failed_id] = {
            "review_id": failed_id,
            "project_id": "p1",
            "source_request_id": "worker-source",
            "task_id": "P1",
            "branch": snapshot["git"]["branch"],
            "head": snapshot["git"]["head"],
            "review_status_hash": snapshot["git"]["status_hash"],
            "review_dirty": False,
            "state": "failed",
            "reason": "reviewer lifecycle error: broker service invocation failed: timed out",
            "completed_at": "2026-09-17T03:59:24+00:00",
        }
        reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
        snapshot["state"] = "WAITING_REVIEW"
        snapshot["lifecycle_state"] = "REVIEW_FAILED"
        return repo, runtime, config, project, snapshot, failed_id

    def test_exact_failed_review_retry_launches_reviewer_only(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, config, project, snapshot, failed_id = self.fixture(Path(td))
            candidate, reason = resolve_retry_candidate(snapshot, runtime, project)
            self.assertEqual(reason, "")
            self.assertEqual(candidate["target_id"], failed_id)
            view = project_control_view(snapshot, runtime, project)
            retry = next(row for row in view["controls"] if row["action"] == "retry")
            self.assertTrue(retry["available"], retry)
            self.assertEqual(retry["target_id"], failed_id)

            port = ReviewerPort()
            reviewer = AIReviewerCoordinator(runtime, port)
            submit_control_command(
                runtime, "p1", "retry", command_id="retry-one",
                expected=view["control_identity"], target={"target_id": failed_id},
            )
            executor = FakeExecutor()
            result = ControlCommandCoordinator(runtime, reviewer=reviewer).advance(
                config, {"projects": [snapshot]}, executor
            )[0]
            self.assertEqual(result["state"], "accepted")
            self.assertEqual(result["effect"], "retry_failed_technical_review_no_worker_started")
            self.assertEqual(executor.calls, [])
            review_id = "ai_review:retry:retry-one"
            reviewer._threads[review_id].join(timeout=2)
            self.assertEqual(len(port.requests), 1)
            self.assertIn("[FAILED_REVIEW_RETRY]", port.requests[0].prompt)
            state = reviewer.state()["reviews"][review_id]
            self.assertEqual(state["retry_of"], failed_id)
            self.assertIsNone(resolve_retry_candidate(snapshot, runtime, project)[0])

    def test_retry_consumes_command_against_runtime_projected_review_failed_identity(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, config, project, snapshot, failed_id = self.fixture(Path(td))
            view = project_control_view(snapshot, runtime, project)
            submit_control_command(
                runtime, "p1", "retry", command_id="retry-runtime-projection",
                expected=view["control_identity"], target={"target_id": failed_id},
            )
            # The monitor snapshot can still be IDLE/WAITING_REVIEW before durable reviewer
            # state is projected. Control consumption must validate the same projected identity
            # that the watchdog/control surface used when the command was created.
            raw_snapshot = dict(snapshot)
            raw_snapshot["state"] = "IDLE"
            raw_snapshot["lifecycle_state"] = "IDLE"
            port = ReviewerPort()
            reviewer = AIReviewerCoordinator(runtime, port)
            executor = FakeExecutor()
            result = ControlCommandCoordinator(runtime, reviewer=reviewer).advance(
                config, {"projects": [raw_snapshot]}, executor
            )[0]
            self.assertEqual(result["state"], "accepted", result)
            self.assertEqual(result["effect"], "retry_failed_technical_review_no_worker_started")
            self.assertEqual(executor.calls, [])
            review_id = "ai_review:retry:retry-runtime-projection"
            reviewer._threads[review_id].join(timeout=2)
            self.assertEqual(len(port.requests), 1)

    def test_retry_fails_closed_on_decision_dirty_or_wrong_target(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, config, project, snapshot, failed_id = self.fixture(Path(td))
            decisions_path = runtime / "review-decisions.json"
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
            decisions["decisions"][failed_id] = {
                "project_id": "p1", "request_id": failed_id,
                "decision": "remediate", "next_action": "continue_current_stage",
            }
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")
            self.assertIsNone(resolve_retry_candidate(snapshot, runtime, project)[0])
            del decisions["decisions"][failed_id]
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")
            (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
            self.assertIn("dirty", resolve_retry_candidate(snapshot, runtime, project)[1])
            (repo / "dirty.txt").unlink()

            view = project_control_view(snapshot, runtime, project)
            submit_control_command(
                runtime, "p1", "retry", command_id="wrong-target",
                expected=view["control_identity"], target={"target_id": "other"},
            )
            result = ControlCommandCoordinator(
                runtime, reviewer=AIReviewerCoordinator(runtime, ReviewerPort())
            ).advance(config, {"projects": [snapshot]}, FakeExecutor())[0]
            self.assertEqual(result["state"], "blocked")
            self.assertIn("target_id does not match", result["reason"])


    def test_retry_requires_exact_review_status_hash_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot, failed_id = self.fixture(Path(td))
            reviews_path = runtime / "ai-reviewer.json"
            reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
            reviews["reviews"][failed_id]["review_status_hash"] = "0" * 64
            reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
            candidate, reason = resolve_retry_candidate(snapshot, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("status hash", reason)

            reviews["reviews"][failed_id].pop("review_status_hash")
            reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
            candidate, reason = resolve_retry_candidate(snapshot, runtime, project)
            self.assertIsNone(candidate)
            self.assertIn("status hash", reason)

    def test_watchdog_enqueues_exact_reviewer_retry_not_worker_continue(self):
        with tempfile.TemporaryDirectory() as td:
            _, runtime, _, project, snapshot, failed_id = self.fixture(Path(td))
            project = {
                **project,
                "watchdog": {"enabled": True, "auto_recovery": True},
            }
            evidence = {
                "process_liveness": {"process_alive": False, "pid": None},
                "ledgers": {"ai-reviewer.json": {"state": "failed", "review_id": failed_id}},
            }
            attempt = {
                "attempt_key": "review-failed-attempt",
                "run_scope_key": "review-failed-scope",
                "task_id": "P1",
                "state": "completed",
                "diagnosis": "reviewer_failed",
                "owner_gate_required": False,
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "evidence": evidence,
                "evidence_hash": evidence_hash(evidence),
            }
            attempt["record_integrity_hash"] = compute_record_integrity_hash(attempt)
            project_row = {"attempts": {attempt["attempt_key"]: attempt}}
            watchdog = WatchdogCoordinator(runtime)
            watchdog._cached_state["projects"]["p1"] = project_row
            watchdog._check_and_trigger_recovery(
                project, snapshot, project_row, attempt["attempt_key"], attempt
            )
            recovery = attempt["recovery"]
            self.assertEqual(recovery["action"], "retry")
            self.assertEqual(recovery["target"], {"target_id": failed_id})
            inbox = runtime / "control" / "inbox" / f"{recovery['command_id']}.json"
            command = json.loads(inbox.read_text(encoding="utf-8"))
            self.assertEqual(command["action"], "retry")
            self.assertEqual(command["target"], {"target_id": failed_id})


    def test_failed_review_can_rereview_clean_descendant_without_worker(self):
        with tempfile.TemporaryDirectory() as td:
            repo, runtime, config, project, snapshot, failed_id = self.fixture(Path(td))
            (repo / "bounded-fix.txt").write_text("fix", encoding="utf-8")
            import subprocess
            subprocess.run(["git", "add", "bounded-fix.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "bounded recovery fix"], cwd=repo, check=True, capture_output=True)
            from dev_orchestrator.core.repository import read_repository_truth
            truth = read_repository_truth(repo)
            snapshot["git"].update({"head": truth.head, "status_hash": truth.status_hash, "dirty": False})
            candidate, reason = resolve_rereview_candidate(snapshot, runtime, project)
            self.assertEqual(reason, "")
            self.assertEqual(candidate["target_id"], failed_id)
            view = project_control_view(snapshot, runtime, project)
            control = next(row for row in view["controls"] if row["action"] == "rereview")
            self.assertTrue(control["available"], control)
            submit_control_command(runtime, "p1", "rereview", command_id="rereview-one",
                                   expected=view["control_identity"], target={"target_id": failed_id})
            port = ReviewerPort(); reviewer = AIReviewerCoordinator(runtime, port); executor = FakeExecutor()
            result = ControlCommandCoordinator(runtime, reviewer=reviewer).advance(config, {"projects": [snapshot]}, executor)[0]
            self.assertEqual(result["state"], "accepted", result)
            self.assertEqual(result["effect"], "rereview_failed_technical_review_no_worker_started")
            self.assertEqual(executor.calls, [])
            review_id = "ai_review:rereview:rereview-one"
            reviewer._threads[review_id].join(timeout=2)
            state = reviewer.state()["reviews"][review_id]
            self.assertEqual(state["rereview_of"], failed_id)
            self.assertEqual(state["head"], truth.head)
            self.assertIn("[FAILED_REVIEW_DESCENDANT_REREVIEW]", port.requests[0].prompt)


if __name__ == "__main__":
    unittest.main()
