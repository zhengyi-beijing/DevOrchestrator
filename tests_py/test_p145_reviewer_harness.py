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
        self.temp_dir.cleanup()

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


if __name__ == "__main__":
    unittest.main()
