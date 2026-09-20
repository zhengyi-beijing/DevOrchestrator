"""End-to-end software acceptance tests for P14.5 Reviewer Harness.

Verifies:
1. Representative LabDemo diff inspection and rule pack evaluation.
2. AIReviewerCoordinator lifecycle integration with reviewer_harness opt-in.
3. Decision derivation: blocking findings -> REMEDIATE; clean/non-blocking -> REVIEW_ACCEPTED.
4. Independent gates enforcement (compiler/tests/build gates cannot be bypassed).
5. Coverage completeness enforcement (partial coverage fails closed).
6. Non-opted-in projects continue using direct reviewer path.
7. CLI subcommands (review-submit, review-status, review-reconcile, review-findings).
8. Web Control API GET endpoints with authentication.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dev_orchestrator.cli import (
    cmd_review_findings,
    cmd_review_reconcile,
    cmd_review_status,
    cmd_review_submit,
)
from dev_orchestrator.config import load_projects_config
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.jobs.config import (
    JobCommandConfig,
    JobProjectConfig,
    JobsConfig,
)
from dev_orchestrator.jobs.models import JobCorruptionError, JobSpec
from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.review.harness import DefaultReviewerHarness
from dev_orchestrator.review.models import (
    ReviewCoverage,
    ReviewFinding,
    ReviewRequest,
    ReviewResult,
    ReviewSession,
)
from dev_orchestrator.review.store import ReviewSessionStore
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json


class P145SoftwareAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp_dir.name).resolve()
        self.repo = self.runtime / "labdemo_repo"
        self.repo.mkdir(parents=True, exist_ok=True)

        # Initialize git repo with LabDemo code
        subprocess.run(["git", "init"], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.repo), capture_output=True, check=True)

        (self.repo / "src").mkdir(parents=True, exist_ok=True)
        (self.repo / "src" / "service.py").write_text("# Hardware service\ndef power_on(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.repo), capture_output=True, check=True)

        # Copy LabDemo rule pack
        self.rules_file = self.repo / "labdemo_rules.json"
        labdemo_rules_src = Path("examples/labdemo_rules.json").resolve()
        if labdemo_rules_src.is_file():
            self.rules_file.write_text(labdemo_rules_src.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            self.rules_file.write_text(json.dumps({
                "rules": [
                    {"rule_id": "service_hardware_authority", "severity": "blocking", "category": "architecture"},
                    {"rule_id": "failsafe_xray_off", "severity": "blocking", "category": "safety"},
                    {"rule_id": "preserve_compatibility", "severity": "warning", "category": "compatibility"},
                ]
            }), encoding="utf-8")

        # Create valid projects.json
        self.config_path = self.runtime / "projects.json"
        self.config_data = {
            "version": 1,
            "projects": [
                {
                    "project_id": "labdemo",
                    "repo_path": str(self.repo),
                    "watch_mode": "branch_head",
                    "branch": "master",
                    "execution": {"engine": "aibroker"},
                    "ai_roles": {"reviewer": {"enabled": True, "quality": "high", "independence": "resource"}},
                    "reviewer_harness": {
                        "enabled": True,
                        "adapter": "opencode_review",
                        "mode": "diff",
                        "diff_mode": "workspace",
                        "rule_pack": "labdemo_rules.json",
                        "blocking_severities": ["blocking"],
                        "independent_gates": ["tests"],
                    },
                },
                {
                    "project_id": "legacy_project",
                    "repo_path": str(self.repo),
                    "watch_mode": "branch_head",
                    "branch": "master",
                    "execution": {"engine": "aibroker"},
                    "ai_roles": {"reviewer": {"enabled": True, "quality": "high", "independence": "resource"}},
                }
            ],
        }
        write_json(self.config_path, self.config_data, indent=2)

        jobs_config = {
            "version": 1,
            "enabled": True,
            "projects": {
                "labdemo": {
                    "repo_path": str(self.repo),
                    "commands": {
                        "review-runner": {
                            "argv": ["python", "-c", "import sys; sys.exit(0)"],
                            "cwd": ".",
                        }
                    },
                }
            },
        }
        write_json(self.runtime / "execution-jobs.json", jobs_config, indent=2)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_project_configuration_validation(self):
        # Valid config passes
        loaded = load_projects_config(self.config_path)
        self.assertEqual(len(loaded["projects"]), 2)
        labdemo = loaded["projects"][0]
        self.assertTrue(labdemo["orchestration_ready"])
        self.assertEqual(labdemo["reviewer_harness"]["mode"], "diff")
        self.assertEqual(labdemo["reviewer_harness"]["backend"], "opencode_review")
        self.assertEqual(labdemo["reviewer_harness"]["timeout_seconds"], 600.0)
        self.assertEqual(labdemo["reviewer_harness"]["poll_interval_seconds"], 0.2)

        # Config with custom timeouts normalizes properly
        custom_config = dict(self.config_data)
        custom_config["projects"] = [
            dict(
                self.config_data["projects"][0],
                reviewer_harness=dict(
                    self.config_data["projects"][0]["reviewer_harness"],
                    timeout_seconds=120,
                    poll_interval_seconds=1.5,
                ),
            )
        ]
        custom_path = self.runtime / "custom_projects.json"
        write_json(custom_path, custom_config, indent=2)
        loaded_custom = load_projects_config(custom_path)
        self.assertEqual(loaded_custom["projects"][0]["reviewer_harness"]["timeout_seconds"], 120.0)
        self.assertEqual(loaded_custom["projects"][0]["reviewer_harness"]["poll_interval_seconds"], 1.5)

        # Invalid reviewer_harness mode fails validation
        bad_config = dict(self.config_data)
        bad_config["projects"] = [
            dict(self.config_data["projects"][0], reviewer_harness={"enabled": True, "mode": "invalid_mode"})
        ]
        bad_path = self.runtime / "bad_projects.json"
        write_json(bad_path, bad_config, indent=2)
        with self.assertRaises(ValueError) as ctx:
            load_projects_config(bad_path)
        self.assertIn("reviewer_harness", str(ctx.exception))

        # Unsupported reviewer_harness backend fails validation
        bad_backend_config = dict(self.config_data)
        bad_backend_config["projects"] = [
            dict(self.config_data["projects"][0], reviewer_harness={"enabled": True, "backend": "mock"})
        ]
        bad_backend_path = self.runtime / "bad_backend_projects.json"
        write_json(bad_backend_path, bad_backend_config, indent=2)
        with self.assertRaises(ValueError) as ctx:
            load_projects_config(bad_backend_path)
        self.assertIn("backend", str(ctx.exception))

    def test_coordinator_blocking_finding_triggers_remediate(self):
        truth = read_repository_truth(str(self.repo))
        # Record completed worker in transition-executor.json with passed independent gate
        source_id = "worker_req_1"
        worker_record = {
            "engine": "aibroker",
            "state": "completed",
            "project_id": "labdemo",
            "task_id": "task_lab_1",
            "repo_path": str(self.repo),
            "completed_at": utc_now_iso(),
            "independent_gates": {"tests": "passed"},
            "resource_context": {"resource_id": "res_1", "provider": "p", "account": "a", "model": "m"},
        }
        texec_path = self.runtime / "transition-executor.json"
        write_json(texec_path, {"version": 1, "executions": {source_id: worker_record}}, indent=2)

        # Mock harness returning a blocking finding
        mock_harness = MagicMock()
        mock_session = ReviewSession(
            session_id="ai_review:" + source_id,
            request=ReviewRequest(
                request_id="ai_review:" + source_id,
                project_id="labdemo",
                task_id="task_lab_1",
                source_request_id=source_id,
                branch=truth.branch,
                head=truth.head,
                status_hash=truth.status_hash,
            ),
            state="completed",
            job_id="job_lab_1",
        )
        mock_harness.submit.return_value = mock_session
        mock_harness.status.return_value = mock_session

        blocking_finding = ReviewFinding(
            fingerprint="",
            file="src/service.py",
            start_line=2,
            end_line=2,
            severity="blocking",
            category="safety",
            rule_id="failsafe_xray_off",
            message="X-ray interlock missing off handler",
        )
        mock_result = ReviewResult(
            session_id=mock_session.session_id,
            job_id="job_lab_1",
            disposition="remediate",
            completeness="complete",
            findings=[blocking_finding],
            coverage=ReviewCoverage(completeness="complete", selected_count=1, reviewed_count=1),
            reason="1 blocking finding",
        )
        mock_harness.result.return_value = mock_result

        progress_mock = MagicMock()
        coordinator = AIReviewerCoordinator(
            self.runtime,
            port=None,
            progress_channel=progress_mock,
            harness=mock_harness,
        )

        launched = coordinator.advance(self.config_path)
        self.assertEqual(launched, ["ai_review:" + source_id])

        # Wait for thread to finish
        for t in list(coordinator._threads.values()):
            t.join(timeout=3)

        # Verify review-decisions.json contains REMEDIATE
        decisions_file = self.runtime / "review-decisions.json"
        self.assertTrue(decisions_file.is_file())
        decisions_data = read_json(decisions_file, {})
        d_record = decisions_data["decisions"].get("ai_review:" + source_id)
        self.assertIsNotNone(d_record)
        self.assertEqual(d_record["decision"], "remediate")
        self.assertEqual(d_record["next_action"], "continue_current_stage")
        self.assertEqual(d_record["disposition"], "remediate")

        # Verify progress event REMEDIATE emitted
        calls = [c[0] for c in progress_mock.emit.call_args_list]
        event_names = [c[1] for c in calls]
        self.assertIn("REVIEW_STARTED", event_names)
        self.assertIn("REMEDIATE", event_names)

    def test_coordinator_clean_review_triggers_review_accepted(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_2"
        worker_record = {
            "engine": "aibroker",
            "state": "completed",
            "project_id": "labdemo",
            "task_id": "task_lab_2",
            "repo_path": str(self.repo),
            "completed_at": utc_now_iso(),
            "independent_gates": {"tests": "passed"},
            "resource_context": {"resource_id": "res_1", "provider": "p", "account": "a", "model": "m"},
        }
        texec_path = self.runtime / "transition-executor.json"
        write_json(texec_path, {"version": 1, "executions": {source_id: worker_record}}, indent=2)

        mock_harness = MagicMock()
        mock_session = ReviewSession(
            session_id="ai_review:" + source_id,
            request=ReviewRequest(
                request_id="ai_review:" + source_id,
                project_id="labdemo",
                task_id="task_lab_2",
                source_request_id=source_id,
                branch=truth.branch,
                head=truth.head,
                status_hash=truth.status_hash,
            ),
            state="completed",
            job_id="job_lab_2",
        )
        mock_harness.submit.return_value = mock_session
        mock_harness.status.return_value = mock_session

        warning_finding = ReviewFinding(
            fingerprint="",
            file="src/service.py",
            start_line=1,
            end_line=1,
            severity="warning",
            category="compatibility",
            rule_id="preserve_compatibility",
            message="Non-breaking telemetry adjustment",
        )
        mock_result = ReviewResult(
            session_id=mock_session.session_id,
            job_id="job_lab_2",
            disposition="next",
            completeness="complete",
            findings=[warning_finding],
            coverage=ReviewCoverage(completeness="complete", selected_count=1, reviewed_count=1),
            reason="Clean review",
        )
        mock_harness.result.return_value = mock_result

        progress_mock = MagicMock()
        coordinator = AIReviewerCoordinator(
            self.runtime,
            port=None,
            progress_channel=progress_mock,
            harness=mock_harness,
        )

        launched = coordinator.advance(self.config_path)
        self.assertEqual(launched, ["ai_review:" + source_id])

        for t in list(coordinator._threads.values()):
            t.join(timeout=3)

        decisions_file = self.runtime / "review-decisions.json"
        decisions_data = read_json(decisions_file, {})
        d_record = decisions_data["decisions"].get("ai_review:" + source_id)
        self.assertIsNotNone(d_record)
        self.assertEqual(d_record["decision"], "next")
        self.assertEqual(d_record["next_action"], "next_task")
        self.assertEqual(d_record["disposition"], "apply")

        calls = [c[0] for c in progress_mock.emit.call_args_list]
        event_names = [c[1] for c in calls]
        self.assertIn("REVIEW_ACCEPTED", event_names)

    def test_independent_gate_failure_blocks_advancement(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_3"
        # Worker tests gate reports failed!
        worker_record = {
            "engine": "aibroker",
            "state": "completed",
            "project_id": "labdemo",
            "task_id": "task_lab_3",
            "repo_path": str(self.repo),
            "completed_at": utc_now_iso(),
            "independent_gates": {"tests": "failed"},
            "resource_context": {"resource_id": "res_1", "provider": "p", "account": "a", "model": "m"},
        }
        texec_path = self.runtime / "transition-executor.json"
        write_json(texec_path, {"version": 1, "executions": {source_id: worker_record}}, indent=2)

        mock_harness = MagicMock()
        mock_session = ReviewSession(
            session_id="ai_review:" + source_id,
            request=ReviewRequest(
                request_id="ai_review:" + source_id,
                project_id="labdemo",
                task_id="task_lab_3",
                source_request_id=source_id,
                branch=truth.branch,
                head=truth.head,
                status_hash=truth.status_hash,
            ),
            state="completed",
            job_id="job_lab_3",
        )
        mock_harness.submit.return_value = mock_session
        mock_harness.status.return_value = mock_session
        mock_result = ReviewResult(
            session_id=mock_session.session_id,
            job_id="job_lab_3",
            disposition="next",
            completeness="complete",
            findings=[],
            coverage=ReviewCoverage(completeness="complete", selected_count=1, reviewed_count=1),
            reason="Clean review",
        )
        mock_harness.result.return_value = mock_result

        progress_mock = MagicMock()
        coordinator = AIReviewerCoordinator(
            self.runtime,
            port=None,
            progress_channel=progress_mock,
            harness=mock_harness,
        )

        coordinator.advance(self.config_path)
        for t in list(coordinator._threads.values()):
            t.join(timeout=3)

        # No decision written to review-decisions.json
        decisions_file = self.runtime / "review-decisions.json"
        decisions_data = read_json(decisions_file, {}) if decisions_file.is_file() else {}
        self.assertNotIn("ai_review:" + source_id, decisions_data.get("decisions", {}))

        # REVIEW_FAILED emitted
        calls = [c[0] for c in progress_mock.emit.call_args_list]
        event_names = [c[1] for c in calls]
        self.assertIn("REVIEW_FAILED", event_names)

    def test_cli_subcommands(self):
        # Create a session in store
        store = ReviewSessionStore(self.runtime)
        req = ReviewRequest(
            request_id="cli_sess_1",
            project_id="labdemo",
            task_id="task_cli",
            source_request_id="src_cli",
            branch="master",
            head="head123",
            status_hash="stat123",
        )
        finding = ReviewFinding(
            fingerprint="",
            file="src/service.py",
            start_line=1,
            end_line=2,
            severity="blocking",
            category="safety",
            rule_id="failsafe_xray_off",
            message="Missing power cut",
        )
        sess = ReviewSession(
            session_id="cli_sess_1",
            request=req,
            state="completed",
            result=ReviewResult(
                session_id="cli_sess_1",
                job_id="job_cli",
                disposition="remediate",
                completeness="complete",
                findings=[finding],
                coverage=ReviewCoverage(completeness="complete", selected_count=1, reviewed_count=1),
            ),
        )
        store.save_session(sess)

        # Test review-status CLI
        status_args = argparse.Namespace(
            session_id="cli_sess_1",
            runtime_root=str(self.runtime),
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            ret = cmd_review_status(status_args)
            self.assertEqual(ret, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["session_id"], "cli_sess_1")
            self.assertEqual(data["state"], "completed")

        # Test review-findings CLI (json format)
        findings_args = argparse.Namespace(
            session_id="cli_sess_1",
            severity=None,
            rule_id=None,
            format="json",
            runtime_root=str(self.runtime),
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            ret = cmd_review_findings(findings_args)
            self.assertEqual(ret, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["rule_id"], "failsafe_xray_off")

        # Test review-submit CLI
        submit_args = argparse.Namespace(
            project_id="labdemo",
            task_id="task_cli_submit",
            source_request_id="src_cli_submit",
            request_id="cli_submit_sess",
            mode="diff",
            diff_mode="workspace",
            scan_roots=None,
            rule_pack=None,
            base=None,
            head=None,
            transport="local",
            repo_path=str(self.repo),
            config=str(self.config_path),
            runtime_root=str(self.runtime),
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            ret = cmd_review_submit(submit_args)
            self.assertEqual(ret, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["session_id"], "cli_submit_sess")
            self.assertEqual(data["request"]["source_request_id"], "src_cli_submit")

        # Test review-findings CLI (sarif format)
        sarif_args = argparse.Namespace(
            session_id="cli_sess_1",
            severity=None,
            rule_id=None,
            format="sarif",
            runtime_root=str(self.runtime),
        )
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            ret = cmd_review_findings(sarif_args)
            self.assertEqual(ret, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["version"], "2.1.0")
            self.assertEqual(len(data["runs"][0]["results"]), 1)

    def test_coordinator_partial_coverage_fails_closed(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_partial"
        worker_record = {
            "engine": "aibroker",
            "state": "completed",
            "project_id": "labdemo",
            "task_id": "task_lab_partial",
            "repo_path": str(self.repo),
            "completed_at": utc_now_iso(),
            "independent_gates": {"tests": "passed"},
            "resource_context": {"resource_id": "res_1", "provider": "p", "account": "a", "model": "m"},
        }
        texec_path = self.runtime / "transition-executor.json"
        write_json(texec_path, {"version": 1, "executions": {source_id: worker_record}}, indent=2)

        mock_harness = MagicMock()
        mock_session = ReviewSession(
            session_id="ai_review:" + source_id,
            request=ReviewRequest(
                request_id="ai_review:" + source_id,
                project_id="labdemo",
                task_id="task_lab_partial",
                source_request_id=source_id,
                branch=truth.branch,
                head=truth.head,
                status_hash=truth.status_hash,
            ),
            state="completed",
            job_id="job_lab_partial",
        )
        mock_harness.submit.return_value = mock_session
        mock_harness.status.return_value = mock_session
        mock_result = ReviewResult(
            session_id=mock_session.session_id,
            job_id="job_lab_partial",
            disposition="failed",
            completeness="partial",
            findings=[],
            coverage=ReviewCoverage(completeness="partial", selected_count=2, reviewed_count=1),
            reason="Partial coverage",
        )
        mock_harness.result.return_value = mock_result

        progress_mock = MagicMock()
        coordinator = AIReviewerCoordinator(
            self.runtime,
            port=None,
            progress_channel=progress_mock,
            harness=mock_harness,
        )

        coordinator.advance(self.config_path)
        for t in list(coordinator._threads.values()):
            t.join(timeout=3)

        decisions_file = self.runtime / "review-decisions.json"
        decisions_data = read_json(decisions_file, {}) if decisions_file.is_file() else {}
        self.assertNotIn("ai_review:" + source_id, decisions_data.get("decisions", {}))

        calls = [c[0] for c in progress_mock.emit.call_args_list]
        event_names = [c[1] for c in calls]
        self.assertIn("REVIEW_FAILED", event_names)

    def test_coordinator_propagates_worker_resource_context_and_independence_to_harness_request(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_prop"
        worker_record = {
            "engine": "aibroker",
            "state": "completed",
            "project_id": "labdemo",
            "task_id": "task_lab_prop",
            "repo_path": str(self.repo),
            "completed_at": utc_now_iso(),
            "independent_gates": {"tests": "passed"},
            "resource_context": {
                "resource_id": "worker_res_test",
                "provider": "anthropic",
                "account": "default",
                "model": "claude-3-opus",
            },
        }
        texec_path = self.runtime / "transition-executor.json"
        write_json(texec_path, {"version": 1, "executions": {source_id: worker_record}}, indent=2)

        mock_harness = MagicMock()
        mock_session = ReviewSession(
            session_id="ai_review:" + source_id,
            request=ReviewRequest(
                request_id="ai_review:" + source_id,
                project_id="labdemo",
                task_id="task_lab_prop",
                source_request_id=source_id,
                branch=truth.branch,
                head=truth.head,
                status_hash=truth.status_hash,
            ),
            state="completed",
            job_id="job_lab_prop",
        )
        mock_harness.submit.return_value = mock_session
        mock_harness.status.return_value = mock_session
        mock_result = ReviewResult(
            session_id=mock_session.session_id,
            job_id="job_lab_prop",
            disposition="next",
            completeness="complete",
            findings=[],
            coverage=ReviewCoverage(completeness="complete", selected_count=1, reviewed_count=1),
            reason="Clean review",
        )
        mock_harness.result.return_value = mock_result

        coordinator = AIReviewerCoordinator(
            self.runtime,
            port=None,
            harness=mock_harness,
        )
        launched = coordinator.advance(self.config_path)
        self.assertEqual(launched, ["ai_review:" + source_id])
        for t in list(coordinator._threads.values()):
            t.join(timeout=3)

        self.assertEqual(mock_harness.submit.call_count, 1)
        submitted_req: ReviewRequest = mock_harness.submit.call_args[0][0]
        self.assertEqual(submitted_req.metadata.get("independence"), "resource")
        self.assertEqual(
            submitted_req.metadata.get("worker_resource_context"),
            {"resource_id": "worker_res_test", "provider": "anthropic", "account": "default", "model": "claude-3-opus"},
        )
        self.assertEqual(
            submitted_req.metadata.get("previous_resource_context"),
            {"resource_id": "worker_res_test", "provider": "anthropic", "account": "default", "model": "claude-3-opus"},
        )

    def test_coordinator_missing_worker_resource_context_fails_closed_without_harness_launch(self):
        source_id = "worker_req_no_res"
        worker_record = {
            "engine": "aibroker",
            "state": "completed",
            "project_id": "labdemo",
            "task_id": "task_lab_no_res",
            "repo_path": str(self.repo),
            "completed_at": utc_now_iso(),
            "independent_gates": {"tests": "passed"},
            # resource_context missing!
        }
        texec_path = self.runtime / "transition-executor.json"
        write_json(texec_path, {"version": 1, "executions": {source_id: worker_record}}, indent=2)

        mock_harness = MagicMock()
        coordinator = AIReviewerCoordinator(
            self.runtime,
            port=None,
            harness=mock_harness,
        )
        launched = coordinator.advance(self.config_path)
        self.assertEqual(launched, [])
        mock_harness.submit.assert_not_called()

        rec = coordinator.state()["reviews"].get("ai_review:" + source_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["state"], "failed")
        self.assertIn("worker resource context missing", rec["reason"])

    def test_coordinator_repository_truth_drift_fails_closed(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_drift_head"
        worker_record = {
            "engine": "aibroker",
            "state": "completed",
            "project_id": "labdemo",
            "task_id": "task_lab_drift_head",
            "repo_path": str(self.repo),
            "completed_at": utc_now_iso(),
            "independent_gates": {"tests": "passed"},
            "resource_context": {"resource_id": "res_1", "provider": "p", "account": "a", "model": "m"},
        }
        texec_path = self.runtime / "transition-executor.json"
        write_json(texec_path, {"version": 1, "executions": {source_id: worker_record}}, indent=2)

        mock_harness = MagicMock()
        mock_session = ReviewSession(
            session_id="ai_review:" + source_id,
            request=ReviewRequest(
                request_id="ai_review:" + source_id,
                project_id="labdemo",
                task_id="task_lab_drift_head",
                source_request_id=source_id,
                branch=truth.branch,
                head=truth.head,
                status_hash=truth.status_hash,
            ),
            state="running",
            job_id="job_lab_drift_head",
        )
        mock_harness.submit.return_value = mock_session

        completed_session = ReviewSession(
            session_id="ai_review:" + source_id,
            request=mock_session.request,
            state="completed",
            job_id="job_lab_drift_head",
        )

        def fake_status_drift_head(sess_id):
            # Mutate repo by committing a new file, advancing HEAD
            (self.repo / "src" / "drift_head.py").write_text("# drift head commit\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(self.repo), capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "drift commit"], cwd=str(self.repo), capture_output=True, check=True)
            return completed_session

        mock_harness.status.side_effect = fake_status_drift_head
        mock_result = ReviewResult(
            session_id=mock_session.session_id,
            job_id="job_lab_drift_head",
            disposition="next",
            completeness="complete",
            findings=[],
            coverage=ReviewCoverage(completeness="complete", selected_count=1, reviewed_count=1),
            reason="Clean review",
        )
        mock_harness.result.return_value = mock_result

        progress_mock = MagicMock()
        coordinator = AIReviewerCoordinator(
            self.runtime,
            port=None,
            progress_channel=progress_mock,
            harness=mock_harness,
        )

        launched = coordinator.advance(self.config_path)
        self.assertEqual(launched, ["ai_review:" + source_id])

        for t in list(coordinator._threads.values()):
            t.join(timeout=3)

        # Assert no decision in review-decisions.json
        decisions_file = self.runtime / "review-decisions.json"
        decisions_data = read_json(decisions_file, {}) if decisions_file.is_file() else {}
        self.assertNotIn("ai_review:" + source_id, decisions_data.get("decisions", {}))

        # Assert review record is failed with "repository changed during review"
        rec = coordinator.state()["reviews"]["ai_review:" + source_id]
        self.assertEqual(rec["state"], "failed")
        self.assertIn("repository changed during review", rec["reason"])

        # Assert REVIEW_FAILED event emitted
        calls = [c[0] for c in progress_mock.emit.call_args_list]
        event_names = [c[1] for c in calls]
        self.assertIn("REVIEW_FAILED", event_names)

    def test_coordinator_repository_status_hash_drift_fails_closed(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_drift_stat"
        worker_record = {
            "engine": "aibroker",
            "state": "completed",
            "project_id": "labdemo",
            "task_id": "task_lab_drift_stat",
            "repo_path": str(self.repo),
            "completed_at": utc_now_iso(),
            "independent_gates": {"tests": "passed"},
            "resource_context": {"resource_id": "res_1", "provider": "p", "account": "a", "model": "m"},
        }
        texec_path = self.runtime / "transition-executor.json"
        write_json(texec_path, {"version": 1, "executions": {source_id: worker_record}}, indent=2)

        mock_harness = MagicMock()
        mock_session = ReviewSession(
            session_id="ai_review:" + source_id,
            request=ReviewRequest(
                request_id="ai_review:" + source_id,
                project_id="labdemo",
                task_id="task_lab_drift_stat",
                source_request_id=source_id,
                branch=truth.branch,
                head=truth.head,
                status_hash=truth.status_hash,
            ),
            state="running",
            job_id="job_lab_drift_stat",
        )
        mock_harness.submit.return_value = mock_session

        completed_session = ReviewSession(
            session_id="ai_review:" + source_id,
            request=mock_session.request,
            state="completed",
            job_id="job_lab_drift_stat",
        )

        def fake_status_drift_dirty(sess_id):
            # Write uncommitted dirty file to drift status_hash without changing HEAD
            (self.repo / "dirty_uncommitted_file.txt").write_text("dirty content\n", encoding="utf-8")
            return completed_session

        mock_harness.status.side_effect = fake_status_drift_dirty
        mock_result = ReviewResult(
            session_id=mock_session.session_id,
            job_id="job_lab_drift_stat",
            disposition="next",
            completeness="complete",
            findings=[],
            coverage=ReviewCoverage(completeness="complete", selected_count=1, reviewed_count=1),
            reason="Clean review",
        )
        mock_harness.result.return_value = mock_result

        progress_mock = MagicMock()
        coordinator = AIReviewerCoordinator(
            self.runtime,
            port=None,
            progress_channel=progress_mock,
            harness=mock_harness,
        )

        launched = coordinator.advance(self.config_path)
        self.assertEqual(launched, ["ai_review:" + source_id])

        for t in list(coordinator._threads.values()):
            t.join(timeout=3)

        # Assert no decision in review-decisions.json
        decisions_file = self.runtime / "review-decisions.json"
        decisions_data = read_json(decisions_file, {}) if decisions_file.is_file() else {}
        self.assertNotIn("ai_review:" + source_id, decisions_data.get("decisions", {}))

        # Assert review record is failed with "repository changed during review"
        rec = coordinator.state()["reviews"]["ai_review:" + source_id]
        self.assertEqual(rec["state"], "failed")
        self.assertIn("repository changed during review", rec["reason"])

        # Assert REVIEW_FAILED event emitted
        calls = [c[0] for c in progress_mock.emit.call_args_list]
        event_names = [c[1] for c in calls]
        self.assertIn("REVIEW_FAILED", event_names)

    def _restart_reviewer_state(self, review_id, source_id, truth, **overrides):
        record = {
            "review_id": review_id,
            "project_id": "labdemo",
            "task_id": "task_lab_rec",
            "source_request_id": source_id,
            "state": "running",
            "harness": True,
            "session_id": review_id,
            "started_at": utc_now_iso(),
            "branch": truth.branch,
            "head": truth.head,
            "review_status_hash": truth.status_hash,
            "review_dirty": False,
            "repo_path": str(self.repo),
            "harness_cfg": {"blocking_severities": ["blocking"]},
            "worker": {"resource_context": {"resource_id": "w/res/1", "provider": "w", "account": "a"}},
            "policy": {"independence": "resource", "quality": "high", "timeout_seconds": 600.0},
        }
        record.update(overrides)
        write_json(self.runtime / "ai-reviewer.json", {"version": 1, "reviews": {review_id: record}}, indent=2)
        return record

    def _reconciled_session(self, review_id, source_id, truth, state="completed", job_id="job_lab_rec"):
        return ReviewSession(
            session_id=review_id,
            request=ReviewRequest(
                request_id=review_id,
                project_id="labdemo",
                task_id="task_lab_rec",
                source_request_id=source_id,
                branch=truth.branch,
                head=truth.head,
                status_hash=truth.status_hash,
            ),
            state=state,
            job_id=job_id,
        )

    @staticmethod
    def _review_result(review_id, findings=None, completeness="complete"):
        return ReviewResult(
            session_id=review_id,
            job_id="job_lab_rec",
            disposition="evidence",
            completeness=completeness,
            findings=list(findings or []),
            coverage=ReviewCoverage(),
        )

    def test_coordinator_daemon_restart_reconciliation_emits_clean_next_decision(self):
        # A session completed while the daemon was down must yield a durable
        # decision. Recovering harness/job state alone leaves the review
        # complete-but-undecided, which neither advances nor retries.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_rec"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth)

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)
        progress = MagicMock()

        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness, progress_channel=progress)

        mock_harness.reconcile.assert_called_once_with(review_id)
        mock_harness.result.assert_called_once_with(review_id)

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "completed")
        self.assertEqual(saved["job_id"], "job_lab_rec")
        self.assertEqual(saved["decision"], "next")
        self.assertEqual(saved["next_action"], "next_task")

        decisions = read_json(self.runtime / "review-decisions.json", {}).get("decisions", {})
        self.assertIn(review_id, decisions)
        self.assertEqual(decisions[review_id]["decision"], "next")
        self.assertEqual(decisions[review_id]["head"], truth.head)
        self.assertEqual(decisions[review_id]["review_status_hash"], truth.status_hash)

        events = [c[0][1] for c in progress.emit.call_args_list]
        self.assertIn("REVIEW_ACCEPTED", events)

    def test_coordinator_daemon_restart_reconciliation_emits_blocking_remediate(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_rec_block"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth)

        finding = ReviewFinding(
            file="src/x.py", start_line=1, end_line=2, severity="blocking",
            category="correctness", rule_id="R1", message="boom",
        )
        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id, findings=[finding])
        progress = MagicMock()

        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness, progress_channel=progress)

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["decision"], "remediate")
        decisions = read_json(self.runtime / "review-decisions.json", {}).get("decisions", {})
        self.assertEqual(decisions[review_id]["decision"], "remediate")
        self.assertEqual(decisions[review_id]["next_action"], "continue_current_stage")
        self.assertIn("REMEDIATE", [c[0][1] for c in progress.emit.call_args_list])

    def test_coordinator_daemon_restart_fails_closed_on_repository_drift(self):
        # Recovery must re-verify the reviewed anchor, not trust stale evidence.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_rec_drift"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth, head="0" * 40)

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)
        progress = MagicMock()

        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness, progress_channel=progress)

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "failed")
        self.assertIn("repository changed during review", saved["reason"])
        decisions = read_json(self.runtime / "review-decisions.json", {}).get("decisions", {})
        self.assertNotIn(review_id, decisions)
        self.assertIn("REVIEW_FAILED", [c[0][1] for c in progress.emit.call_args_list])

    def test_coordinator_daemon_restart_fails_closed_without_anchor_evidence(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_rec_noanchor"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(
            review_id, source_id, truth, branch=None, head=None, review_status_hash=None,
        )

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)

        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness)

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "failed")
        self.assertIn("anchor evidence missing", saved["reason"])
        decisions = read_json(self.runtime / "review-decisions.json", {}).get("decisions", {})
        self.assertNotIn(review_id, decisions)

    def test_coordinator_daemon_restart_recovery_is_idempotent(self):
        # A second recovery pass must not write a duplicate decision or event.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_rec_idem"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth)

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)

        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness)
        first = read_json(self.runtime / "review-decisions.json", {})["decisions"][review_id]

        # Force the record active again, as a crash mid-recovery would leave it.
        state = read_json(self.runtime / "ai-reviewer.json", {})
        state["reviews"][review_id]["state"] = "running"
        write_json(self.runtime / "ai-reviewer.json", state, indent=2)

        progress = MagicMock()
        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness, progress_channel=progress)

        second = read_json(self.runtime / "review-decisions.json", {})["decisions"][review_id]
        self.assertEqual(first, second)
        self.assertEqual(second["consumed_at"], first["consumed_at"])
        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "completed")
        self.assertEqual(saved["decision"], "next")
        # The replay re-offers the terminal event rather than dropping it; the
        # progress channel is responsible for exactly-once delivery (asserted
        # against the real channel in the dedup test below).
        self.assertNotIn("REVIEW_FAILED", [c[0][1] for c in progress.emit.call_args_list])

    def test_recovery_replay_delivers_lifecycle_event_exactly_once(self):
        # Replay is only safe because the progress channel deduplicates on
        # occurrence key and persists that ledger across processes. Assert the
        # guarantee against the real channel, not a mock.
        from dev_orchestrator.core.progress import ProgressChannel

        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_exactly_once"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth)

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)

        def emitted_accepted():
            # Count delivered notifications, not dedup-map keys: the dedup map
            # assigns to a single key, so counting keys stays 1 even if dedup
            # were removed entirely and would not test the guarantee at all.
            raw = read_json(self.runtime / "progress-channel.json", {})
            return [
                item for item in (raw.get("history") or [])
                if item.get("milestone") == "REVIEW_ACCEPTED"
                and (item.get("details") or {}).get("review_id", review_id) == review_id
            ]

        AIReviewerCoordinator(
            self.runtime, port=None, harness=mock_harness,
            progress_channel=ProgressChannel(self.runtime),
        )
        self.assertEqual(len(emitted_accepted()), 1)
        original = read_json(self.runtime / "review-decisions.json", {})["decisions"][review_id]

        # Replay the crash window repeatedly; delivery must stay exactly once.
        for _ in range(3):
            state = read_json(self.runtime / "ai-reviewer.json", {})
            rec = state["reviews"][review_id]
            rec["state"] = "running"
            for key in ("decision", "next_action", "completed_at", "lifecycle_event_pending"):
                rec.pop(key, None)
            write_json(self.runtime / "ai-reviewer.json", state, indent=2)
            AIReviewerCoordinator(
                self.runtime, port=None, harness=mock_harness,
                progress_channel=ProgressChannel(self.runtime),
            )

        self.assertEqual(len(emitted_accepted()), 1)
        final = read_json(self.runtime / "review-decisions.json", {})["decisions"][review_id]
        self.assertEqual(final, original)

    def test_recovery_converges_after_crash_between_decision_and_state(self):
        # The decision ledger is written before the reviewer-state terminal
        # write. A replay landing between them must converge on the durable
        # decision, not re-derive one: re-deriving raised a replay conflict and
        # settled the review as failed while a real NEXT decision existed.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_crash_decision"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth)

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)

        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness)
        first = read_json(self.runtime / "review-decisions.json", {})["decisions"][review_id]

        # Simulate the crash window: ledger durable, reviewer state unfinished.
        state = read_json(self.runtime / "ai-reviewer.json", {})
        rec = state["reviews"][review_id]
        rec["state"] = "running"
        for key in ("decision", "next_action", "completed_at", "lifecycle_event_pending"):
            rec.pop(key, None)
        write_json(self.runtime / "ai-reviewer.json", state, indent=2)

        progress = MagicMock()
        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness, progress_channel=progress)

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        second = read_json(self.runtime / "review-decisions.json", {})["decisions"][review_id]
        self.assertEqual(saved["state"], "completed")
        self.assertEqual(saved["decision"], "next")
        self.assertEqual(second["decision"], "next")
        # The original ordering key must survive the replay.
        self.assertEqual(second["consumed_at"], first["consumed_at"])
        self.assertEqual(second, first)
        events = [c[0][1] for c in progress.emit.call_args_list]
        self.assertIn("REVIEW_ACCEPTED", events)
        self.assertNotIn("REVIEW_FAILED", events)

    def test_recovery_replays_lifecycle_event_owed_from_previous_process(self):
        # A crash between the terminal state write and the event emit must not
        # lose the event: terminal records are not otherwise replayed.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_crash_event"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth)

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)
        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness)

        # Simulate the crash window: terminal state written, event never sent.
        state = read_json(self.runtime / "ai-reviewer.json", {})
        state["reviews"][review_id]["lifecycle_event_pending"] = "REVIEW_ACCEPTED"
        write_json(self.runtime / "ai-reviewer.json", state, indent=2)

        progress = MagicMock()
        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness, progress_channel=progress)

        events = [c[0][1] for c in progress.emit.call_args_list]
        self.assertIn("REVIEW_ACCEPTED", events)
        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertNotIn("lifecycle_event_pending", saved)

    def test_terminal_write_marks_then_clears_pending_lifecycle_event(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_pending_marker"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth)

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)

        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness, progress_channel=MagicMock())

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "completed")
        self.assertNotIn("lifecycle_event_pending", saved)

    def test_recovered_completed_session_without_repo_path_fails_closed(self):
        # Legacy records predate repo_path. read_repository_truth("") resolves
        # to the daemon cwd, so an unresolved path must never reach it.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_legacy_completed"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth, repo_path=None, worker={})

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)

        cwd = os.getcwd()
        os.chdir(str(self.repo))
        try:
            AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness)
        finally:
            os.chdir(cwd)

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "failed")
        self.assertIn("repository path evidence", saved["reason"])
        decisions = read_json(self.runtime / "review-decisions.json", {}).get("decisions", {})
        self.assertNotIn(review_id, decisions)

    def test_recovered_in_flight_session_without_repo_path_fails_closed(self):
        # Same guard on the resumed path, where it was previously missing: the
        # daemon cwd deliberately matches the stored Git anchors here, so an
        # unguarded run would produce a NEXT decision against the wrong source.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_legacy_inflight"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth, repo_path=None, worker={})

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(
            review_id, source_id, truth, state="running",
        )
        mock_harness.status.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)

        cwd = os.getcwd()
        os.chdir(str(self.repo))
        try:
            coordinator = AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness)
            thread = coordinator._threads.get(review_id)
            if thread is not None:
                thread.join(timeout=30)
        finally:
            os.chdir(cwd)

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "failed")
        self.assertIn("repository path evidence", saved["reason"])
        decisions = read_json(self.runtime / "review-decisions.json", {}).get("decisions", {})
        self.assertNotIn(review_id, decisions)

    def test_legacy_repo_path_recovered_only_from_matching_source_record(self):
        # A legacy record may borrow repo_path from its exact source transition
        # record, but only when project/task/source/branch/HEAD all agree.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_legacy_borrow"
        review_id = "ai_review:" + source_id

        def run_with_source(source_record):
            write_json(
                self.runtime / "transition-executor.json",
                {"version": 1, "executions": {source_id: source_record}},
                indent=2,
            )
            self._restart_reviewer_state(review_id, source_id, truth, repo_path=None, worker={})
            for path in ("review-decisions.json",):
                if (self.runtime / path).exists():
                    (self.runtime / path).unlink()
            mock_harness = MagicMock()
            mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
            mock_harness.result.return_value = self._review_result(review_id)
            AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness)
            return read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]

        matching = {
            "project_id": "labdemo", "task_id": "task_lab_rec",
            "source_request_id": source_id, "branch": truth.branch,
            "head": truth.head, "repo_path": str(self.repo),
            "resource_context": {"resource_id": "w/res/1"},
        }
        self.assertEqual(run_with_source(matching)["state"], "completed")

        for field, bad in (
            ("head", "0" * 40),
            ("branch", "other-branch"),
            ("task_id", "different_task"),
            ("project_id", "other_project"),
        ):
            with self.subTest(mismatched=field):
                mismatched = dict(matching)
                mismatched[field] = bad
                saved = run_with_source(mismatched)
                self.assertEqual(saved["state"], "failed")
                self.assertIn("repository path evidence", saved["reason"])

    def _valid_ledger_record(self, review_id, truth, **overrides):
        record = {
            "project_id": "labdemo",
            "request_id": review_id,
            "disposition": "apply",
            "next_action": "next_task",
            "decision": "next",
            "reason": "Review accepted clean (0 non-blocking finding(s))",
            "review_status_hash": truth.status_hash,
            "task_id": "task_lab_rec",
            "stage_id": "review",
            "branch": truth.branch,
            "head": truth.head,
            "role": "reviewer",
            "event": "worker_done",
            "source": "aibroker",
            "consumed_at": utc_now_iso(),
        }
        record.update(overrides)
        return record

    def _run_with_ledger(self, review_id, source_id, truth, ledger_record):
        self._restart_reviewer_state(review_id, source_id, truth)
        write_json(
            self.runtime / "review-decisions.json",
            {"version": 1, "decisions": {review_id: ledger_record}},
            indent=2,
        )
        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(review_id, source_id, truth)
        mock_harness.result.return_value = self._review_result(review_id)
        progress = MagicMock()
        AIReviewerCoordinator(
            self.runtime, port=None, harness=mock_harness, progress_channel=progress,
        )
        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        ledger = read_json(self.runtime / "review-decisions.json", {})
        events = [c[0][1] for c in progress.emit.call_args_list]
        return saved, ledger, events

    def test_malformed_durable_decision_is_quarantined_not_projected(self):
        # A structurally invalid ledger entry must never settle a review as
        # completed. Atomic writes prevent torn bytes, not invalid structure,
        # stale schemas, or a competing writer's entry under this review id.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_bad_ledger"
        review_id = "ai_review:" + source_id

        saved, ledger, events = self._run_with_ledger(
            review_id, source_id, truth, {"decision": "next"},
        )

        self.assertEqual(saved["state"], "failed")
        self.assertIsNone(saved.get("decision"))
        self.assertIn("durable decision rejected", saved["reason"])
        self.assertIn("REVIEW_FAILED", events)
        self.assertNotIn("REVIEW_ACCEPTED", events)
        # Quarantined out of the decided set so retry remains possible.
        self.assertNotIn(review_id, ledger.get("decisions", {}))
        self.assertIn(review_id, ledger.get("quarantined_decisions", {}))

    def test_durable_decision_mismatching_reviewer_record_is_rejected(self):
        truth = read_repository_truth(str(self.repo))
        for field, bad in (
            ("head", "0" * 40),
            ("branch", "other-branch"),
            ("task_id", "another_task"),
            ("project_id", "another_project"),
            ("review_status_hash", "deadbeef"),
        ):
            with self.subTest(mismatched=field):
                source_id = f"worker_req_mismatch_{field}"
                review_id = "ai_review:" + source_id
                saved, ledger, events = self._run_with_ledger(
                    review_id, source_id, truth,
                    self._valid_ledger_record(review_id, truth, **{field: bad}),
                )
                self.assertEqual(saved["state"], "failed")
                self.assertIsNone(saved.get("decision"))
                self.assertNotIn("REVIEW_ACCEPTED", events)
                self.assertNotIn(review_id, ledger.get("decisions", {}))
                self.assertIn(review_id, ledger.get("quarantined_decisions", {}))

    def test_durable_decision_with_disallowed_pair_or_identity_is_rejected(self):
        truth = read_repository_truth(str(self.repo))
        cases = {
            "bad_pair": {"decision": "next", "next_action": "continue_current_stage"},
            "wrong_role": {"role": "planner"},
            "wrong_event": {"event": "plan_done"},
            "wrong_stage": {"stage_id": "plan"},
            "wrong_request_id": {"request_id": "ai_review:someone_else"},
            "blank_reason": {"reason": "   "},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                source_id = f"worker_req_bad_{label}"
                review_id = "ai_review:" + source_id
                saved, ledger, events = self._run_with_ledger(
                    review_id, source_id, truth,
                    self._valid_ledger_record(review_id, truth, **override),
                )
                self.assertEqual(saved["state"], "failed")
                self.assertNotIn("REVIEW_ACCEPTED", events)
                self.assertIn(review_id, ledger.get("quarantined_decisions", {}))

    def test_valid_durable_decision_is_still_projected(self):
        # Guard against the validation being so strict that legitimate
        # convergence stops working.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_good_ledger"
        review_id = "ai_review:" + source_id
        record = self._valid_ledger_record(review_id, truth)

        saved, ledger, events = self._run_with_ledger(review_id, source_id, truth, record)

        self.assertEqual(saved["state"], "completed")
        self.assertEqual(saved["decision"], "next")
        self.assertIn("REVIEW_ACCEPTED", events)
        self.assertEqual(ledger["decisions"][review_id]["consumed_at"], record["consumed_at"])
        self.assertNotIn(review_id, ledger.get("quarantined_decisions", {}))

    def test_recovery_failure_event_survives_emit_failure_and_is_replayed(self):
        # Recovery failure outcomes must use the same crash-recoverable outbox
        # as the normal terminal path: terminal state and pending marker are
        # persisted together, and the event is replayed until delivered.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_emit_fault"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth)

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(
            review_id, source_id, truth, state="failed",
        )

        failing = MagicMock()
        failing.emit.side_effect = RuntimeError("emit crashed")
        # A transport fault must not escape coordinator construction.
        AIReviewerCoordinator(
            self.runtime, port=None, harness=mock_harness, progress_channel=failing,
        )

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["lifecycle_event_pending"], "REVIEW_FAILED")

        healthy = MagicMock()
        AIReviewerCoordinator(
            self.runtime, port=None, harness=mock_harness, progress_channel=healthy,
        )
        self.assertIn("REVIEW_FAILED", [c[0][1] for c in healthy.emit.call_args_list])
        replayed = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertNotIn("lifecycle_event_pending", replayed)

    def test_non_harness_recovery_failure_uses_the_lifecycle_outbox(self):
        # The non-harness interruption path wrote terminal state inline and
        # bypassed the outbox entirely; assert it no longer does.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_nonharness_fault"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth, harness=False, session_id=None)

        failing = MagicMock()
        failing.emit.side_effect = RuntimeError("emit crashed")
        AIReviewerCoordinator(self.runtime, port=None, progress_channel=failing)

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["lifecycle_event_pending"], "REVIEW_FAILED")

    def test_legacy_in_flight_guard_rejects_before_starting_a_poller(self):
        # Route-specific: the resume entry point must refuse, rather than
        # relying on the finalizer's defence-in-depth guard after a poll.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_no_poller"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth, repo_path=None, worker={})

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(
            review_id, source_id, truth, state="running",
        )

        coordinator = AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness)

        self.assertIsNone(coordinator._threads.get(review_id))
        mock_harness.status.assert_not_called()
        mock_harness.result.assert_not_called()
        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "failed")

    def test_coordinator_daemon_restart_failed_session_is_retry_eligible(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_rec_failed"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth)

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(
            review_id, source_id, truth, state="failed",
        )
        progress = MagicMock()

        AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness, progress_channel=progress)

        saved = read_json(self.runtime / "ai-reviewer.json", {})["reviews"][review_id]
        self.assertEqual(saved["state"], "failed")
        self.assertIn("REVIEW_FAILED", [c[0][1] for c in progress.emit.call_args_list])

    def test_coordinator_daemon_restart_resumes_in_flight_session(self):
        # A session still running after restart must be resumed, not abandoned.
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_rec_inflight"
        review_id = "ai_review:" + source_id
        self._restart_reviewer_state(review_id, source_id, truth)

        mock_harness = MagicMock()
        mock_harness.reconcile.return_value = self._reconciled_session(
            review_id, source_id, truth, state="running",
        )
        mock_harness.status.return_value = self._reconciled_session(
            review_id, source_id, truth, state="completed",
        )
        mock_harness.result.return_value = self._review_result(review_id)

        coordinator = AIReviewerCoordinator(self.runtime, port=None, harness=mock_harness)
        thread = coordinator._threads.get(review_id)
        self.assertIsNotNone(thread)
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())

        decisions = read_json(self.runtime / "review-decisions.json", {}).get("decisions", {})
        self.assertIn(review_id, decisions)
        self.assertEqual(decisions[review_id]["decision"], "next")

    def test_control_api_review_endpoints(self):
        import http.client
        import threading
        from dev_orchestrator.control.security import ControlSecurity
        from dev_orchestrator.jobs.store import ExecutionJobStore
        from dev_orchestrator.web.server import make_server

        # 1. Populate a review session with findings and coverage in the store
        store = ReviewSessionStore(self.runtime)
        job_service = JobService(self.runtime)
        job_rec = job_service.submit(
            JobSpec(
                project_id="labdemo",
                command_ref="review-runner",
                idempotency_key="idemp_api",
            )
        )
        jstore = ExecutionJobStore(self.runtime)
        jstore.save_output_artifact(
            job_rec.job_id,
            "findings.json",
            [{"rule_id": "failsafe_xray_off", "secret_key": "supersecretpassword123"}],
        )

        req = ReviewRequest(
            request_id="api_sess_1",
            project_id="labdemo",
            task_id="task_api",
            source_request_id="src_api",
            branch="master",
            head="head123",
            status_hash="stat123",
        )
        finding = ReviewFinding(
            fingerprint="",
            file="src/service.py",
            start_line=1,
            end_line=2,
            severity="blocking",
            category="safety",
            rule_id="failsafe_xray_off",
            message="Missing power cut with Bearer supersecrettoken123",
        )
        coverage = ReviewCoverage(completeness="complete", selected_count=1, reviewed_count=1)
        sess = ReviewSession(
            session_id="api_sess_1",
            request=req,
            state="completed",
            job_id=job_rec.job_id,
            result=ReviewResult(
                session_id="api_sess_1",
                job_id=job_rec.job_id,
                disposition="remediate",
                completeness="complete",
                findings=[finding],
                coverage=coverage,
            ),
        )
        store.save_session(sess)

        # Start web server
        server = make_server(
            "127.0.0.1",
            0,
            self.runtime,
            self.repo,
            enable_control=True,
            config_path=self.config_path,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]

        security = ControlSecurity(self.runtime)
        token = security.token()

        try:
            def do_get(path, auth=True):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                headers = {"Authorization": f"Bearer {token}"} if auth else {}
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                body = resp.read()
                conn.close()
                return status, body

            # Unauthorized request returns 401
            st, _ = do_get("/api/v1/control/reviews", auth=False)
            self.assertEqual(st, 401)

            # List reviews
            st, body = do_get("/api/v1/control/reviews")
            self.assertEqual(st, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertTrue(len(data["data"]) >= 1)

            # Session detail
            st, body = do_get("/api/v1/control/reviews/api_sess_1")
            self.assertEqual(st, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(data["data"]["session_id"], "api_sess_1")

            # Findings
            st, body = do_get("/api/v1/control/reviews/api_sess_1/findings")
            self.assertEqual(st, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(len(data["data"]), 1)
            self.assertEqual(data["data"][0]["rule_id"], "failsafe_xray_off")
            # Secret should be redacted
            self.assertNotIn("supersecrettoken123", body.decode("utf-8"))
            self.assertIn("Bearer [REDACTED]", body.decode("utf-8"))

            # Findings with filter
            st, body = do_get("/api/v1/control/reviews/api_sess_1/findings?severity=warning")
            self.assertEqual(st, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(len(data["data"]), 0)

            # Coverage
            st, body = do_get("/api/v1/control/reviews/api_sess_1/coverage")
            self.assertEqual(st, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(data["data"]["completeness"], "complete")

            # Artifact GET endpoint (verifies raw_bytes excluded and secret redacted)
            st, body = do_get("/api/v1/control/reviews/api_sess_1/artifacts/findings.json")
            self.assertEqual(st, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertNotIn("raw_bytes", data["data"])
            self.assertIn("content", data["data"])
            self.assertNotIn("supersecretpassword123", body.decode("utf-8"))
            self.assertEqual(data["data"]["content"][0]["secret_key"], "[REDACTED]")

            # Non-existent session returns 404
            st, _ = do_get("/api/v1/control/reviews/non_existent_session")
            self.assertEqual(st, 404)

            # Session with dots returns 404 (rejected by route regex, not 500)
            st, _ = do_get("/api/v1/control/reviews/session.with.dots")
            self.assertEqual(st, 404)
            st, _ = do_get("/api/v1/control/reviews/session.with.dots/findings")
            self.assertEqual(st, 404)

            # Artifact traversal ('..' returns 400 from global path check, '.' and non-existent return 404)
            st, _ = do_get("/api/v1/control/reviews/api_sess_1/artifacts/..")
            self.assertEqual(st, 400)
            st, _ = do_get("/api/v1/control/reviews/api_sess_1/artifacts/.")
            self.assertEqual(st, 404)
            st, _ = do_get("/api/v1/control/reviews/api_sess_1/artifacts/non_existent.json")
            self.assertEqual(st, 404)

        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_job_artifact_digest_mismatch_raises_corruption_error(self):
        import hashlib
        from dev_orchestrator.ai.execution_transport import SSHTransportConfig
        from dev_orchestrator.jobs.models import JobCorruptionError, JobRecord
        from dev_orchestrator.jobs.transport import SSHJobTransport

        # 1. Test SSHJobTransport re-verification of remote artifact digest
        ssh_cfg = SSHTransportConfig(peer="10.0.0.1", expected_host_identity="10.0.0.1")
        fake_subp = MagicMock()
        transport = SSHJobTransport(ssh_cfg, subprocess_module=fake_subp)

        tampered_artifact_payload = {
            "status": "success",
            "artifact": {
                "name": "findings.json",
                "sha256": "sha256:" + hashlib.sha256(b"original content").hexdigest(),
                "size_bytes": 16,
                "content_type": "application/json",
            },
            "raw_text": "tampered content",
        }
        resp = {
            "request_id": "test-req-id",
            "host_identity": "10.0.0.1",
            "status": "success",
            "payload": tampered_artifact_payload,
        }

        def side_effect(argv, **kwargs):
            env = json.loads(kwargs.get("input", b"{}").decode("utf-8"))
            resp["request_id"] = env["request_id"]
            return subprocess.CompletedProcess(
                args=["ssh"],
                returncode=0,
                stdout=json.dumps(resp).encode("utf-8"),
                stderr=b"",
            )

        fake_subp.run.side_effect = side_effect

        with self.assertRaises(JobCorruptionError) as ctx:
            transport.job_artifact("job-123", name="findings.json")
        self.assertIn("digest verification failed", str(ctx.exception))

        # 2. Test JobService.get_artifact detects local tampering against JobRecord descriptor
        service = JobService(self.runtime)
        job_rec = service.submit(
            JobSpec(
                project_id="labdemo",
                command_ref="review-runner",
                idempotency_key="tamper-key",
            )
        )
        service.store.save_output_artifact(
            job_rec.job_id,
            "findings.json",
            [{"rule_id": "failsafe_xray_off"}],
        )
        # Tamper with the artifact file on disk
        art_path = self.runtime / "jobs" / job_rec.job_id / "artifacts" / "findings.json"
        art_path.write_text("tampered file content", encoding="utf-8")

        with self.assertRaises(JobCorruptionError) as ctx:
            service.get_artifact(job_rec.job_id, "findings.json")
        self.assertIn("artifact digest mismatch", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
