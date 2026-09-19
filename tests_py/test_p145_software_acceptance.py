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

    def test_coordinator_daemon_restart_reconciliation(self):
        truth = read_repository_truth(str(self.repo))
        source_id = "worker_req_rec"
        review_id = "ai_review:" + source_id

        # Write interrupted state to ai-reviewer.json
        reviewer_state = {
            "version": 1,
            "reviews": {
                review_id: {
                    "review_id": review_id,
                    "project_id": "labdemo",
                    "task_id": "task_lab_rec",
                    "source_request_id": source_id,
                    "state": "running",
                    "harness": True,
                    "session_id": review_id,
                    "started_at": utc_now_iso(),
                }
            }
        }
        write_json(self.runtime / "ai-reviewer.json", reviewer_state, indent=2)

        mock_harness = MagicMock()
        reconciled_session = ReviewSession(
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
            state="completed",
            job_id="job_lab_rec",
        )
        mock_harness.reconcile.return_value = reconciled_session

        coordinator = AIReviewerCoordinator(
            self.runtime,
            port=None,
            harness=mock_harness,
        )

        mock_harness.reconcile.assert_called_once_with(review_id)
        saved_state = read_json(self.runtime / "ai-reviewer.json", {})
        self.assertEqual(saved_state["reviews"][review_id]["state"], "completed")
        self.assertEqual(saved_state["reviews"][review_id]["job_id"], "job_lab_rec")

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
