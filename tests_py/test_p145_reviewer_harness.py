"""Unit tests for P14.5 Reviewer Harness & OpenCodeReview Adapter.

Tests versioned review models, SARIF 2.1.0 generator, OpenCodeReviewAdapter,
ReviewRunner, ReviewSessionStore, and DefaultReviewerHarness.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dev_orchestrator.jobs.config import (
    JobCommandConfig,
    JobProjectConfig,
    JobsConfig,
)
from dev_orchestrator.jobs.models import JobSpec
from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.jobs.store import ExecutionJobStore
from dev_orchestrator.review.harness import DefaultReviewerHarness
from dev_orchestrator.review.models import (
    ReviewCoverage,
    ReviewFinding,
    ReviewManifest,
    ReviewPacket,
    ReviewRequest,
    ReviewResult,
    ReviewSession,
    compute_finding_fingerprint,
    to_sarif,
)
from dev_orchestrator.review.ocr_adapter import OpenCodeReviewAdapter
from dev_orchestrator.review.runner import (
    ReviewRunner,
    build_packet_prompt,
    parse_packet_output,
    partition_packets,
)
from dev_orchestrator.review.store import ReviewSessionStore


class ReviewModelsAndSARIFTests(unittest.TestCase):
    def test_finding_fingerprint_deterministic(self):
        fp1 = compute_finding_fingerprint("src/driver.py", 10, 15, "rule_xray", "safety")
        fp2 = compute_finding_fingerprint("src/driver.py", 10, 15, "rule_xray", "safety")
        self.assertEqual(fp1, fp2)
        self.assertTrue(fp1.startswith("sha256:"))

        # Different line gives different fingerprint
        fp3 = compute_finding_fingerprint("src/driver.py", 11, 15, "rule_xray", "safety")
        self.assertNotEqual(fp1, fp3)

    def test_review_finding_serialization_and_validation(self):
        finding = ReviewFinding(
            fingerprint="",
            file="src/hardware.py",
            start_line=25,
            end_line=30,
            severity="blocking",
            category="safety",
            rule_id="failsafe_xray_off",
            message="Missing interlock power off check",
            evidence="power.keep_on()",
        )
        self.assertTrue(finding.fingerprint.startswith("sha256:"))
        data = finding.to_dict()
        self.assertEqual(data["file"], "src/hardware.py")
        self.assertEqual(data["severity"], "blocking")

        restored = ReviewFinding.from_dict(data)
        self.assertEqual(restored.fingerprint, finding.fingerprint)
        self.assertEqual(restored.rule_id, "failsafe_xray_off")

        # Invalid severity raises ValueError
        with self.assertRaises(ValueError):
            ReviewFinding(
                fingerprint="",
                file="src/hardware.py",
                start_line=1,
                end_line=2,
                severity="critical_danger",
                category="safety",
                rule_id="r1",
                message="msg",
            )

        # Invalid line numbers raise ValueError
        with self.assertRaises(ValueError):
            ReviewFinding(
                fingerprint="",
                file="src/hardware.py",
                start_line=10,
                end_line=5,
                severity="blocking",
                category="safety",
                rule_id="r1",
                message="msg",
            )

    def test_sarif_generator_conforms_to_sarif_210(self):
        f1 = ReviewFinding(
            fingerprint="",
            file="src/service/xray.py",
            start_line=42,
            end_line=45,
            severity="blocking",
            category="safety",
            rule_id="failsafe_xray_off",
            message="X-ray not disabled on fault condition",
            evidence="raise TimeoutError()",
        )
        f2 = ReviewFinding(
            fingerprint="",
            file="src/ui/panel.py",
            start_line=100,
            end_line=105,
            severity="warning",
            category="architecture",
            rule_id="preserve_compatibility",
            message="Public API signature modified without deprecation",
        )
        sarif = to_sarif([f1, f2])
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertEqual(sarif["$schema"], "https://json.schemastore.org/sarif-2.1.0.json")
        runs = sarif.get("runs", [])
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["tool"]["driver"]["name"], "DevOrchestrator-ReviewerHarness")
        rules = run["tool"]["driver"]["rules"]
        self.assertEqual(len(rules), 2)
        results = run["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["level"], "error")  # blocking maps to error
        self.assertEqual(results[1]["level"], "warning")
        self.assertEqual(results[0]["ruleId"], "failsafe_xray_off")
        loc = results[0]["locations"][0]["physicalLocation"]
        self.assertEqual(loc["artifactLocation"]["uri"], "src/service/xray.py")
        self.assertEqual(loc["region"]["startLine"], 42)
        self.assertEqual(loc["region"]["endLine"], 45)

    def test_review_coverage_defaults_fail_closed(self):
        cov = ReviewCoverage()
        self.assertEqual(cov.completeness, "failed")
        self.assertEqual(cov.coverage_rate, 0.0)

        # from_dict({}) must fail closed
        cov_empty = ReviewCoverage.from_dict({})
        self.assertEqual(cov_empty.completeness, "failed")
        self.assertEqual(cov_empty.coverage_rate, 0.0)

        # explicit completeness preserved
        cov_complete = ReviewCoverage.from_dict({"completeness": "complete", "coverage_rate": 1.0})
        self.assertEqual(cov_complete.completeness, "complete")
        self.assertEqual(cov_complete.coverage_rate, 1.0)


class OpenCodeReviewAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name).resolve()
        # Initialize a git repository
        subprocess.run(["git", "init"], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.repo), capture_output=True, check=True)

        # Create sample files
        (self.repo / "src").mkdir(parents=True, exist_ok=True)
        (self.repo / "src" / "driver.py").write_text("def run(): pass\n", encoding="utf-8")
        (self.repo / "src" / "safety.py").write_text("def interlock(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(self.repo), capture_output=True, check=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_probe_capabilities_fallback(self):
        adapter = OpenCodeReviewAdapter(self.repo, executable=None)
        caps = adapter.probe_capabilities()
        self.assertFalse(caps.get("available"))
        self.assertIsNone(caps.get("version"))

    def test_scan_preview_with_limits_and_containment(self):
        adapter = OpenCodeReviewAdapter(self.repo)
        selected, excluded = adapter.prepare_scan(["src"], {"max_files": 10, "max_bytes": 100000})
        paths = [s["path"] for s in selected]
        self.assertIn("src/driver.py", paths)
        self.assertIn("src/safety.py", paths)
        self.assertEqual(len(excluded), 0)

        # Respect max files limit
        selected_limited, excluded_limited = adapter.prepare_scan(["src"], {"max_files": 1, "max_bytes": 100000})
        self.assertEqual(len(selected_limited), 1)
        self.assertEqual(len(excluded_limited), 1)
        self.assertEqual(excluded_limited[0]["reason"], "exceeds_max_files_limit")

    def test_diff_preview_workspace(self):
        # Modify a file in working directory
        (self.repo / "src" / "driver.py").write_text("def run(): print('changed')\n", encoding="utf-8")
        adapter = OpenCodeReviewAdapter(self.repo)
        selected, excluded, refs = adapter.prepare_diff("workspace", {}, {"max_files": 10, "max_bytes": 100000})
        paths = [s["path"] for s in selected]
        self.assertIn("src/driver.py", paths)

    def test_diff_preview_workspace_selects_head_commit_when_untracked_files_exist(self):
        # Clean working tree with untracked directory/file (like ?? graphify-out/)
        (self.repo / "graphify-out").mkdir(parents=True, exist_ok=True)
        (self.repo / "graphify-out" / "graph.json").write_text("{}", encoding="utf-8")
        adapter = OpenCodeReviewAdapter(self.repo)
        selected, excluded, refs = adapter.prepare_diff("workspace", {}, {"max_files": 10, "max_bytes": 100000})
        paths = [s["path"] for s in selected]
        self.assertIn("src/driver.py", paths)
        self.assertIn("src/safety.py", paths)
        ex_paths = [e["path"] for e in excluded]
        self.assertTrue(any("graphify-out" in ep for ep in ex_paths))

    def test_diff_preview_workspace_selects_both_unrelated_tracked_and_head_commit(self):
        # Worker committed changes at HEAD, but left an unrelated tracked file dirty
        (self.repo / "notes.txt").write_text("initial notes\n", encoding="utf-8")
        subprocess.run(["git", "add", "notes.txt"], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "add notes"], cwd=str(self.repo), capture_output=True, check=True)
        # Now commit driver change
        (self.repo / "src" / "driver.py").write_text("def run(): print('committed worker change')\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "worker change"], cwd=str(self.repo), capture_output=True, check=True)
        # Leave unrelated tracked file dirty
        (self.repo / "notes.txt").write_text("unrelated working tree edit\n", encoding="utf-8")
        adapter = OpenCodeReviewAdapter(self.repo)
        selected, excluded, refs = adapter.prepare_diff("workspace", {}, {"max_files": 10, "max_bytes": 100000})
        paths = [s["path"] for s in selected]
        self.assertIn("notes.txt", paths)
        self.assertIn("src/driver.py", paths)

    def test_diff_preview_workspace_fails_closed_when_head_commit_contributes_no_selected_files(self):
        # Commit an empty commit
        subprocess.run(["git", "commit", "--allow-empty", "-m", "empty"], cwd=str(self.repo), capture_output=True, check=True)
        adapter = OpenCodeReviewAdapter(self.repo)
        with self.assertRaises(RuntimeError) as ctx:
            adapter.prepare_diff("workspace", {}, {"max_files": 10, "max_bytes": 100000})
        self.assertIn("contributes no selected files", str(ctx.exception))

    def test_resolve_rules_from_rule_pack(self):
        rule_pack = {
            "name": "Test Rules",
            "rules": [
                {
                    "rule_id": "test_r1",
                    "title": "Rule 1",
                    "category": "safety",
                    "severity": "blocking",
                    "description": "Must not fail",
                }
            ],
        }
        rp_path = self.repo / "rules.json"
        rp_path.write_text(json.dumps(rule_pack), encoding="utf-8")

        adapter = OpenCodeReviewAdapter(self.repo)
        rules, digest = adapter.resolve_rules("rules.json", [{"path": "src/driver.py"}])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["rule_id"], "test_r1")
        self.assertTrue(digest.startswith("sha256:"))

    def test_path_containment_rejection(self):
        adapter = OpenCodeReviewAdapter(self.repo)
        selected, excluded = adapter.prepare_scan(["../../etc"], {"max_files": 10, "max_bytes": 1000})
        self.assertEqual(len(selected), 0)
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["reason"], "escapes_repo_containment")


class ReviewRunnerAndPackagingTests(unittest.TestCase):
    def test_partition_packets_deterministic(self):
        files = [
            {"path": "a.py", "size_bytes": 100},
            {"path": "b.py", "size_bytes": 200},
            {"path": "c.py", "size_bytes": 150},
        ]
        rules = [{"rule_id": "r1"}]
        packets = partition_packets(files, rules, {"max_packet_files": 2, "max_packet_bytes": 1000}, "sess_1")
        self.assertEqual(len(packets), 2)
        self.assertEqual(len(packets[0].files), 2)
        self.assertEqual(len(packets[1].files), 1)
        self.assertEqual(packets[0].packet_id, "sess_1:packet:0")
        self.assertEqual(packets[1].packet_id, "sess_1:packet:1")

    def test_evidence_only_prompt_and_rejection_of_lifecycle_tokens(self):
        packet = ReviewPacket(
            packet_id="sess_1:packet:0",
            session_id="sess_1",
            packet_index=0,
            files=[{"path": "src/app.py", "size_bytes": 50}],
            rules=[{"rule_id": "r1", "title": "R1", "severity": "blocking", "category": "safety", "description": "desc"}],
            broker_request_id="broker_req_1",
        )
        req = ReviewRequest(
            request_id="req_1",
            project_id="p1",
            task_id="t1",
            source_request_id="src_1",
            branch="main",
            head="abc",
            status_hash="def",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            (repo_path / "src").mkdir(parents=True, exist_ok=True)
            (repo_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
            prompt = build_packet_prompt(packet, repo_path, req)
            self.assertIn("CRITICAL REQUIREMENTS", prompt)
            self.assertIn("Do NOT output any lifecycle decision", prompt)

            # Valid model output
            valid_json = json.dumps({
                "reviewed_files": ["src/app.py"],
                "skipped_files": [],
                "findings": [
                    {
                        "file": "src/app.py",
                        "start_line": 1,
                        "end_line": 1,
                        "severity": "blocking",
                        "category": "safety",
                        "rule_id": "r1",
                        "message": "Bad line",
                    }
                ],
            })
            findings, reviewed, skipped = parse_packet_output(valid_json, packet, repo_path)
            self.assertEqual(len(reviewed), 1)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].rule_id, "r1")

            # Lifecycle decision injected in model output MUST be rejected
            for forbidden_key in ("decision", "next_action", "disposition"):
                bad_json = json.dumps({
                    "reviewed_files": ["src/app.py"],
                    "skipped_files": [],
                    "findings": [],
                    forbidden_key: "next",
                })
                with self.assertRaises(ValueError):
                    parse_packet_output(bad_json, packet, repo_path)


class ReviewSessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp_dir.name).resolve()
        self.store = ReviewSessionStore(self.runtime)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_get_update_list(self):
        req = ReviewRequest(
            request_id="sess_abc_1",
            project_id="proj_1",
            task_id="task_1",
            source_request_id="src_1",
            branch="main",
            head="sha123",
            status_hash="stat456",
        )
        session = ReviewSession(
            session_id="sess_abc_1",
            request=req,
            state="running",
            job_id="job_001",
        )
        self.store.save_session(session)

        loaded = self.store.get_session("sess_abc_1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.session_id, "sess_abc_1")
        self.assertEqual(loaded.job_id, "job_001")

        # Update
        def _set_completed(s: ReviewSession):
            s.state = "completed"

        self.store.update_session("sess_abc_1", _set_completed)
        updated = self.store.get_session("sess_abc_1")
        self.assertEqual(updated.state, "completed")

        # List
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "sess_abc_1")
        self.assertEqual(sessions[0]["project_id"], "proj_1")


class DefaultReviewerHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp_dir.name).resolve()
        self.repo = self.runtime / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        (self.repo / "test.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=str(self.repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.repo), capture_output=True, check=True)

    def tearDown(self):
        try:
            from dev_orchestrator.process import terminate_pid
            store = ExecutionJobStore(self.runtime)
            for j in store.list():
                rec = store.get(j["job_id"])
                if rec and rec.supervisor.get("pid"):
                    terminate_pid(rec.supervisor["pid"])
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_harness_submit_and_status(self):
        job_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo,
                    commands={
                        "review-runner": JobCommandConfig(
                            argv=["python", "-c", "import sys; sys.exit(0)"],
                            cwd=".",
                        )
                    },
                )
            },
        )
        job_service = JobService(self.runtime, config=job_cfg)
        harness = DefaultReviewerHarness(self.runtime, job_service=job_service)

        req = ReviewRequest(
            request_id="review_sess_1",
            project_id="p1",
            task_id="t1",
            source_request_id="src_1",
            branch="main",
            head="abc",
            status_hash="def",
            mode="scan",
            scan_roots=["."],
            metadata={"repo_path": str(self.repo)},
        )

        session = harness.submit(req)
        self.assertEqual(session.session_id, "review_sess_1")
        self.assertIsNotNone(session.job_id)
        self.assertIsNotNone(session.manifest)
        self.assertTrue(session.manifest.input_digest.startswith("sha256:"))

        # Re-submitting returns existing session (idempotency)
        replay = harness.submit(req)
        self.assertEqual(replay.session_id, session.session_id)
        self.assertEqual(replay.job_id, session.job_id)

        # Status check
        st = harness.status(session.session_id)
        self.assertEqual(st.session_id, session.session_id)

    def test_harness_input_digest_consistency(self):
        job_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo,
                    commands={
                        "review-runner": JobCommandConfig(
                            argv=["python", "-c", "import sys; sys.exit(0)"],
                            cwd=".",
                        )
                    },
                )
            },
        )
        job_service = JobService(self.runtime, config=job_cfg)
        harness = DefaultReviewerHarness(self.runtime, job_service=job_service)

        req = ReviewRequest(
            request_id="review_sess_digest_test",
            project_id="p1",
            task_id="t1",
            source_request_id="src_1",
            branch="main",
            head="abc",
            status_hash="def",
            mode="scan",
            scan_roots=["."],
            metadata={"repo_path": str(self.repo)},
        )

        session = harness.submit(req)
        store = ExecutionJobStore(self.runtime)
        rec = store.get(session.job_id)
        self.assertIsNotNone(rec)
        self.assertTrue(rec.input_digest.startswith("sha256:"))
        # Verify input.json on disk matches rec.input_digest
        input_file = store._job_dir(session.job_id) / "input.json"
        import hashlib
        disk_digest = "sha256:" + hashlib.sha256(input_file.read_bytes()).hexdigest()
        self.assertEqual(rec.input_digest, disk_digest)

    def test_safe_session_id_collision_prevention(self):
        from dev_orchestrator.review.store import _safe_session_id
        id1 = _safe_session_id("review:123-abc")
        id2 = _safe_session_id("review-123-abc")
        id3 = _safe_session_id("review_123-abc")
        self.assertNotEqual(id1, id2)
        self.assertNotEqual(id1, id3)
        self.assertNotEqual(id2, id3)

    def test_ocr_adapter_fails_closed_on_error(self):
        adapter = OpenCodeReviewAdapter(self.repo, executable="fake_ocr")
        mock_sub = MagicMock()
        adapter._subprocess = mock_sub

        probe_res = MagicMock(returncode=0, stdout=json.dumps({"capabilities": ["diff_preview", "scan_preview"]}))
        fail_res = MagicMock(returncode=1, stderr="internal error")
        mock_sub.run.side_effect = [probe_res, fail_res]

        with self.assertRaises(RuntimeError) as ctx:
            adapter.prepare_diff("workspace", {}, {"max_files": 10})
        self.assertIn("OCR review-preview failed", str(ctx.exception))

    def test_workspace_diff_falls_back_to_head_when_clean(self):
        adapter = OpenCodeReviewAdapter(self.repo, executable=None)
        selected, excluded, refs = adapter.prepare_diff("workspace", {}, {"max_files": 10})
        sel_paths = [f["path"] for f in selected]
        self.assertIn("test.py", sel_paths)

    def test_review_runner_run_full_flow(self):
        from dev_orchestrator.ai.contracts import AIRoleResult, ResourceContext
        job_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo,
                    commands={"review-runner": JobCommandConfig(argv=["python", "-c", "import sys; sys.exit(0)"], cwd=".")},
                )
            },
        )
        job_service = JobService(self.runtime, config=job_cfg)
        harness = DefaultReviewerHarness(self.runtime, job_service=job_service)
        req = ReviewRequest(
            request_id="runner_sess_1",
            project_id="p1",
            task_id="t1",
            source_request_id="src_1",
            branch="main",
            head="abc",
            status_hash="def",
            mode="scan",
            scan_roots=["."],
            metadata={"repo_path": str(self.repo)},
        )
        session = harness.submit(req)
        store = ExecutionJobStore(self.runtime)
        jdir = store._job_dir(session.job_id)

        mock_port = MagicMock()
        mock_port.execute.return_value = AIRoleResult(
            request_id="ocr_review:runner_sess_1:0",
            role_run_id="reviewer",
            status="succeeded",
            output=json.dumps({
                "reviewed_files": ["test.py"],
                "skipped_files": [],
                "findings": [
                    {
                        "file": "test.py",
                        "start_line": 1,
                        "end_line": 1,
                        "severity": "warning",
                        "category": "style",
                        "rule_id": "pep8",
                        "message": "naming convention",
                    }
                ],
            }),
            resource_context=ResourceContext("res1", "mock_prov", "acc1", "model1"),
        )

        runner = ReviewRunner(jdir, port=mock_port)
        ret = runner.run()
        self.assertEqual(ret, 0)

        f_art = store.get_output_artifact(session.job_id, "findings.json")
        c_art = store.get_output_artifact(session.job_id, "coverage.json")
        s_art = store.get_output_artifact(session.job_id, "review.sarif")
        sess_art = store.get_output_artifact(session.job_id, "session.json")
        self.assertIsNotNone(f_art)
        self.assertIsNotNone(c_art)
        self.assertIsNotNone(s_art)
        self.assertIsNotNone(sess_art)

        cov_data = c_art["content"]
        self.assertEqual(cov_data["completeness"], "complete")
        self.assertEqual(cov_data["reviewed_count"], 1)

        sess_data = sess_art["content"]
        self.assertEqual(sess_data["result"]["disposition"], "next")

    def test_review_runner_run_empty_scope_fails_closed(self):
        job_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo,
                    commands={"review-runner": JobCommandConfig(argv=["python", "-c", "import sys; sys.exit(0)"], cwd=".")},
                )
            },
        )
        job_service = JobService(self.runtime, config=job_cfg)
        harness = DefaultReviewerHarness(self.runtime, job_service=job_service)
        req = ReviewRequest(
            request_id="runner_empty_sess",
            project_id="p1",
            task_id="t1",
            source_request_id="src_1",
            branch="main",
            head="abc",
            status_hash="def",
            mode="scan",
            scan_roots=["non_existent_dir"],
            metadata={"repo_path": str(self.repo)},
        )
        session = harness.submit(req)
        store = ExecutionJobStore(self.runtime)
        jdir = store._job_dir(session.job_id)

        mock_port = MagicMock()
        runner = ReviewRunner(jdir, port=mock_port)
        ret = runner.run()
        self.assertEqual(ret, 0)

        c_art = store.get_output_artifact(session.job_id, "coverage.json")
        sess_art = store.get_output_artifact(session.job_id, "session.json")
        self.assertEqual(c_art["content"]["completeness"], "failed")
        self.assertEqual(c_art["content"]["selected_count"], 0)
        self.assertEqual(sess_art["content"]["result"]["disposition"], "failed")
        self.assertIn("empty", sess_art["content"]["result"]["reason"].lower())

    def test_review_runner_rejects_lifecycle_tokens(self):
        from dev_orchestrator.ai.contracts import AIRoleResult
        job_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo,
                    commands={"review-runner": JobCommandConfig(argv=["python", "-c", "import sys; sys.exit(0)"], cwd=".")},
                )
            },
        )
        job_service = JobService(self.runtime, config=job_cfg)
        harness = DefaultReviewerHarness(self.runtime, job_service=job_service)
        req = ReviewRequest(
            request_id="runner_token_sess",
            project_id="p1",
            task_id="t1",
            source_request_id="src_1",
            branch="main",
            head="abc",
            status_hash="def",
            mode="scan",
            scan_roots=["."],
            metadata={"repo_path": str(self.repo)},
        )
        session = harness.submit(req)
        store = ExecutionJobStore(self.runtime)
        jdir = store._job_dir(session.job_id)

        mock_port = MagicMock()
        mock_port.execute.return_value = AIRoleResult(
            request_id="ocr_review:runner_token_sess:0",
            role_run_id="reviewer",
            status="succeeded",
            output=json.dumps({"decision": "next", "reviewed_files": ["test.py"]}),
        )

        runner = ReviewRunner(jdir, port=mock_port)
        ret = runner.run()
        self.assertEqual(ret, 0)

        c_art = store.get_output_artifact(session.job_id, "coverage.json")
        self.assertEqual(c_art["content"]["completeness"], "failed")
        sess_art = store.get_output_artifact(session.job_id, "session.json")
        self.assertEqual(sess_art["content"]["result"]["disposition"], "failed")

    def test_review_runner_omitted_reviewed_files_fails_completeness_and_fails_closed(self):
        from dev_orchestrator.ai.contracts import AIRoleResult
        job_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo,
                    commands={"review-runner": JobCommandConfig(argv=["python", "-c", "import sys; sys.exit(0)"], cwd=".")},
                )
            },
        )
        job_service = JobService(self.runtime, config=job_cfg)
        harness = DefaultReviewerHarness(self.runtime, job_service=job_service)
        (self.repo / "test.py").write_text("print(1)\n", encoding="utf-8")
        req = ReviewRequest(
            request_id="runner_omitted_sess",
            project_id="p1",
            task_id="t1",
            source_request_id="src_1",
            branch="main",
            head="abc",
            status_hash="def",
            mode="scan",
            scan_roots=["."],
            metadata={"repo_path": str(self.repo)},
        )
        session = harness.submit(req)
        store = ExecutionJobStore(self.runtime)
        jdir = store._job_dir(session.job_id)

        mock_port = MagicMock()
        mock_port.execute.return_value = AIRoleResult(
            request_id="ocr_review:runner_omitted_sess:0",
            role_run_id="reviewer",
            status="succeeded",
            output=json.dumps({
                "reviewed_files": [],
                "skipped_files": [],
                "findings": [],
            }),
        )

        runner = ReviewRunner(jdir, port=mock_port)
        ret = runner.run()
        self.assertEqual(ret, 0)

        c_art = store.get_output_artifact(session.job_id, "coverage.json")
        sess_art = store.get_output_artifact(session.job_id, "session.json")
        cov_data = c_art["content"]
        self.assertEqual(cov_data["completeness"], "partial")
        self.assertEqual(cov_data["reviewed_count"], 0)
        self.assertEqual(cov_data["files"]["test.py"]["status"], "unreviewed")
        self.assertEqual(sess_art["content"]["result"]["disposition"], "failed")

    def test_review_runner_skip_without_valid_reason_fails_completeness(self):
        from dev_orchestrator.ai.contracts import AIRoleResult
        job_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo,
                    commands={"review-runner": JobCommandConfig(argv=["python", "-c", "import sys; sys.exit(0)"], cwd=".")},
                )
            },
        )
        job_service = JobService(self.runtime, config=job_cfg)
        harness = DefaultReviewerHarness(self.runtime, job_service=job_service)
        (self.repo / "test.py").write_text("print(1)\n", encoding="utf-8")
        req = ReviewRequest(
            request_id="runner_invalid_skip_sess",
            project_id="p1",
            task_id="t1",
            source_request_id="src_1",
            branch="main",
            head="abc",
            status_hash="def",
            mode="scan",
            scan_roots=["."],
            metadata={"repo_path": str(self.repo)},
        )
        session = harness.submit(req)
        store = ExecutionJobStore(self.runtime)
        jdir = store._job_dir(session.job_id)

        mock_port = MagicMock()
        mock_port.execute.return_value = AIRoleResult(
            request_id="ocr_review:runner_invalid_skip_sess:0",
            role_run_id="reviewer",
            status="succeeded",
            output=json.dumps({
                "reviewed_files": [],
                "skipped_files": [{"path": "test.py", "reason": "skipped"}],
                "findings": [],
            }),
        )

        runner = ReviewRunner(jdir, port=mock_port)
        ret = runner.run()
        self.assertEqual(ret, 0)

        c_art = store.get_output_artifact(session.job_id, "coverage.json")
        sess_art = store.get_output_artifact(session.job_id, "session.json")
        cov_data = c_art["content"]
        self.assertEqual(cov_data["completeness"], "partial")
        self.assertEqual(cov_data["skipped_count"], 0)
        self.assertEqual(cov_data["files"]["test.py"]["status"], "unreviewed")
        self.assertEqual(sess_art["content"]["result"]["disposition"], "failed")

    def test_review_runner_skip_with_valid_reason_allows_complete_coverage(self):
        from dev_orchestrator.ai.contracts import AIRoleResult
        job_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo,
                    commands={"review-runner": JobCommandConfig(argv=["python", "-c", "import sys; sys.exit(0)"], cwd=".")},
                )
            },
        )
        job_service = JobService(self.runtime, config=job_cfg)
        harness = DefaultReviewerHarness(self.runtime, job_service=job_service)
        (self.repo / "test.py").write_text("print(1)\n", encoding="utf-8")
        req = ReviewRequest(
            request_id="runner_valid_skip_sess",
            project_id="p1",
            task_id="t1",
            source_request_id="src_1",
            branch="main",
            head="abc",
            status_hash="def",
            mode="scan",
            scan_roots=["."],
            metadata={"repo_path": str(self.repo)},
        )
        session = harness.submit(req)
        store = ExecutionJobStore(self.runtime)
        jdir = store._job_dir(session.job_id)

        mock_port = MagicMock()
        mock_port.execute.return_value = AIRoleResult(
            request_id="ocr_review:runner_valid_skip_sess:0",
            role_run_id="reviewer",
            status="succeeded",
            output=json.dumps({
                "reviewed_files": [],
                "skipped_files": [{"path": "test.py", "reason": "generated test fixture data"}],
                "findings": [],
            }),
        )

        runner = ReviewRunner(jdir, port=mock_port)
        ret = runner.run()
        self.assertEqual(ret, 0)

        c_art = store.get_output_artifact(session.job_id, "coverage.json")
        sess_art = store.get_output_artifact(session.job_id, "session.json")
        cov_data = c_art["content"]
        self.assertEqual(cov_data["completeness"], "complete")
        self.assertEqual(cov_data["skipped_count"], 1)
        self.assertEqual(cov_data["files"]["test.py"]["status"], "skipped")
        self.assertEqual(sess_art["content"]["result"]["disposition"], "next")

    def test_status_artifact_fallback_with_missing_or_malformed_coverage_fails_closed(self):
        job_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo,
                    commands={"review-runner": JobCommandConfig(argv=["python", "-c", "import sys; sys.exit(0)"], cwd=".")},
                )
            },
        )
        job_service = JobService(self.runtime, config=job_cfg)
        harness = DefaultReviewerHarness(self.runtime, job_service=job_service)
        req = ReviewRequest(
            request_id="fallback_cov_sess",
            project_id="p1",
            task_id="t1",
            source_request_id="src_1",
            branch="main",
            head="abc",
            status_hash="def",
            mode="scan",
            scan_roots=["."],
            metadata={"repo_path": str(self.repo)},
        )
        session = harness.submit(req)
        store = ExecutionJobStore(self.runtime)

        # Mark job completed
        def _mark_done(r: JobRecord):
            if r.state == "queued":
                r.transition_to("running", reason="started")
            r.transition_to("completed", reason="finished")
            r.exit_code = 0
            r.terminal = {"outcome": "completed", "exit_code": 0}
        store.update(session.job_id, _mark_done)

        # Case A: findings.json is empty list, coverage.json has empty dict {} (omits completeness)
        store.save_output_artifact(session.job_id, "findings.json", [])
        store.save_output_artifact(session.job_id, "coverage.json", {})

        st = harness.status(session.session_id)
        self.assertIsNotNone(st.result)
        self.assertEqual(st.result.completeness, "failed")
        self.assertEqual(st.result.disposition, "failed")
        self.assertIn("Coverage failed", st.result.reason)

        # Case B: coverage.json has malformed completeness value
        req2 = ReviewRequest(
            request_id="fallback_cov_sess_2",
            project_id="p1",
            task_id="t1",
            source_request_id="src_2",
            branch="main",
            head="abc",
            status_hash="def",
            mode="scan",
            scan_roots=["."],
            metadata={"repo_path": str(self.repo)},
        )
        session2 = harness.submit(req2)
        store.update(session2.job_id, _mark_done)
        store.save_output_artifact(session2.job_id, "findings.json", [])
        store.save_output_artifact(session2.job_id, "coverage.json", {"completeness": "bogus_status"})
        st2 = harness.status(session2.session_id)
        self.assertEqual(st2.state, "failed")
        self.assertIn("artifact loading failed", st2.failure_reason or "")


if __name__ == "__main__":
    unittest.main()
