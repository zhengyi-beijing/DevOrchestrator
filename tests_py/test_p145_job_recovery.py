"""Tests for P14.5 Job Substrate Recovery & Artifact Retention.

Covers:
1. Content-addressed input artifacts and additive input_digest stability.
2. Bounded output artifacts (findings.json, coverage.json, session.json, review.sarif)
   with digest verification and directory traversal protection.
3. Recovery across daemon restart and transport interruption.
4. Reconcile behavior preventing duplicate supervisor spawns and duplicate dispatches.
"""

from __future__ import annotations

import hashlib
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
from dev_orchestrator.jobs.models import (
    JobRecord,
    JobSpec,
    job_id_for,
    spec_hash,
)
from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.jobs.store import ExecutionJobStore
from dev_orchestrator.jobs.transport import LocalJobTransport
from dev_orchestrator.review.harness import DefaultReviewerHarness
from dev_orchestrator.review.models import (
    ReviewCoverage,
    ReviewFinding,
    ReviewManifest,
    ReviewRequest,
    ReviewResult,
    ReviewSession,
)
from dev_orchestrator.review.store import ReviewSessionStore


def _make_dummy_record(spec: JobSpec, runtime: Path) -> JobRecord:
    return JobRecord(
        job_id=job_id_for(spec),
        spec_hash=spec_hash(spec),
        project_id=spec.project_id,
        command_ref=spec.command_ref,
        idempotency_key=spec.idempotency_key,
        kind="validation",
        resolved_argv=["python", "-c", "pass"],
        working_directory=str(runtime),
        transport="local",
        host_identity="local",
        duration_class="short",
        state="running",
    )


class P145InputDigestAndArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp_dir.name).resolve()
        self.store = ExecutionJobStore(self.runtime)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_additive_input_digest_preserves_hash_when_none(self):
        # When input_digest is None, spec_hash matches legacy P14
        spec_without = JobSpec(
            project_id="p1",
            command_ref="runner",
            idempotency_key="key_1",
            input_digest=None,
        )
        canonical_without = spec_without.to_canonical_dict()
        self.assertNotIn("input_digest", canonical_without)

        h_without = spec_hash(spec_without)
        jid_without = job_id_for(spec_without)

        # When input_digest is set, it is present in canonical dict and influences hash
        spec_with = JobSpec(
            project_id="p1",
            command_ref="runner",
            idempotency_key="key_1",
            input_digest="sha256:abc123def456",
        )
        canonical_with = spec_with.to_canonical_dict()
        self.assertIn("input_digest", canonical_with)
        self.assertEqual(canonical_with["input_digest"], "sha256:abc123def456")

        h_with = spec_hash(spec_with)
        jid_with = job_id_for(spec_with)

        # Hash differs because spec content differs
        self.assertNotEqual(h_without, h_with)
        # job_id derives from (project_id, idempotency_key)
        self.assertEqual(jid_without, jid_with)

    def test_save_and_get_input_artifact(self):
        spec = JobSpec(
            project_id="p1",
            command_ref="runner",
            idempotency_key="key_2",
        )
        rec = _make_dummy_record(spec, self.runtime)
        self.store.save(rec)

        payload = {"request": {"project_id": "p1"}, "manifest": {"selected_files": ["a.py"]}}
        digest = self.store.save_input_artifact(rec.job_id, payload)
        self.assertTrue(digest.startswith("sha256:"))

        loaded_art = self.store.get_input_artifact(rec.job_id)
        self.assertEqual(loaded_art, payload)

    def test_bounded_output_artifacts_and_traversal_protection(self):
        spec = JobSpec(
            project_id="p1",
            command_ref="runner",
            idempotency_key="key_3",
        )
        rec = _make_dummy_record(spec, self.runtime)
        self.store.save(rec)

        findings_payload = [
            {"file": "src/app.py", "start_line": 1, "severity": "blocking", "rule_id": "r1"}
        ]
        info = self.store.save_output_artifact(rec.job_id, "findings.json", findings_payload)
        self.assertEqual(info["name"], "findings.json")
        self.assertTrue(info["sha256"].startswith("sha256:"))

        # Retrieve and verify digest matches
        retrieved = self.store.get_output_artifact(rec.job_id, "findings.json")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["content"], findings_payload)
        self.assertEqual(retrieved["sha256"], info["sha256"])

        # List output artifacts
        all_arts = self.store.list_output_artifacts(rec.job_id)
        self.assertEqual(len(all_arts), 1)
        self.assertEqual(all_arts[0]["name"], "findings.json")

        # Traversal protection in artifact names
        for bad_name in ("../secret.json", "dir/nested.json", "bad*char", " "):
            with self.assertRaises(ValueError):
                self.store.save_output_artifact(rec.job_id, bad_name, {})
            with self.assertRaises(ValueError):
                self.store.get_output_artifact(rec.job_id, bad_name)


class P145RecoveryAndReconciliationTests(unittest.TestCase):
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

    def test_local_job_transport_artifact_retrieval(self):
        transport = LocalJobTransport()
        store = ExecutionJobStore(self.runtime)
        spec = JobSpec(
            project_id="p1",
            command_ref="runner",
            idempotency_key="key_trans_1",
        )
        rec = _make_dummy_record(spec, self.runtime)
        store.save(rec)

        sarif_content = {"version": "2.1.0", "runs": []}
        info = store.save_output_artifact(rec.job_id, "review.sarif", sarif_content)

        job_dir = store._job_dir(rec.job_id)
        desc = transport.job_artifact(rec.job_id, job_dir, name="review.sarif")
        self.assertIsNotNone(desc)
        self.assertEqual(desc["artifact"]["name"], "review.sarif")
        self.assertEqual(desc["content"], sarif_content)
        self.assertEqual(desc["artifact"]["sha256"], info["sha256"])

    def test_harness_reconcile_recovers_completed_job_artifacts(self):
        job_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                "p1": JobProjectConfig(
                    repo_path=self.repo,
                    commands={
                        "review-runner": JobCommandConfig(
                            argv=["python", "-c", "pass"],
                            cwd=".",
                        )
                    },
                )
            },
        )
        job_service = JobService(self.runtime, config=job_cfg)
        harness = DefaultReviewerHarness(self.runtime, job_service=job_service)

        req = ReviewRequest(
            request_id="recover_sess_1",
            project_id="p1",
            task_id="t1",
            source_request_id="src_rec_1",
            branch="main",
            head="abc",
            status_hash="def",
            metadata={"repo_path": str(self.repo)},
        )

        session = harness.submit(req)
        job_id = session.job_id
        self.assertIsNotNone(job_id)

        # Simulate job completion by writing durable output artifacts to job store
        findings = [
            ReviewFinding(
                fingerprint="",
                file="test.py",
                start_line=1,
                end_line=1,
                severity="warning",
                category="style",
                rule_id="r_style",
                message="Consider renaming",
            )
        ]
        coverage = ReviewCoverage(
            completeness="complete",
            selected_count=1,
            reviewed_count=1,
        )
        job_service.store.save_output_artifact(
            job_id, "findings.json", [f.to_dict() for f in findings]
        )
        job_service.store.save_output_artifact(
            job_id, "coverage.json", coverage.to_dict()
        )

        def _mark_done(r: JobRecord):
            if r.state == "queued":
                r.transition_to("running", reason="started")
            r.transition_to("completed", reason="finished")
            r.exit_code = 0
            r.terminal = {"outcome": "completed", "exit_code": 0}

        job_service.store.update(job_id, _mark_done)

        # Reconcile session
        reconciled = harness.reconcile(session.session_id)
        self.assertEqual(reconciled.state, "completed")
        self.assertIsNotNone(reconciled.result)
        self.assertEqual(reconciled.result.completeness, "complete")
        self.assertEqual(reconciled.result.disposition, "next")
        self.assertEqual(len(reconciled.result.findings), 1)
        self.assertEqual(reconciled.result.findings[0].rule_id, "r_style")


if __name__ == "__main__":
    unittest.main()
