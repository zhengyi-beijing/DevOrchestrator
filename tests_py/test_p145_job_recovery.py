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
    JobCorruptionError,
    JobRecord,
    JobSpec,
    job_id_for,
    spec_hash,
)
from dev_orchestrator.jobs.recovery import JobRecoveryCoordinator
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

    def test_job_service_retry_preserves_input_digest_and_copies_input_json_to_successor(self):
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
        mock_transport = MagicMock()
        mock_transport.job_start.return_value = {"status": "started", "supervisor_pid": 1234}
        job_service = JobService(self.runtime, transports={"local": mock_transport}, config=job_cfg)

        payload = {"request": {"project_id": "p1"}, "manifest": {"selected_files": ["test.py"]}}
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        expected_digest = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()

        spec = JobSpec(
            project_id="p1",
            command_ref="review-runner",
            idempotency_key="initial_review_job_key",
            input_digest=expected_digest,
        )
        initial_rec = job_service.submit(spec, input_payload=payload)
        self.assertEqual(initial_rec.input_digest, expected_digest)

        # Predecessor input.json exists on disk and in store
        pred_input_file = job_service.store._job_dir(initial_rec.job_id) / "input.json"
        self.assertTrue(pred_input_file.is_file())
        self.assertEqual(pred_input_file.read_bytes(), payload_bytes)

        # Mark predecessor terminal failed
        job_service.cancel(initial_rec.job_id, reason="reviewer timed out")
        reconciled_pred = job_service.status(initial_rec.job_id)
        self.assertEqual(reconciled_pred.state, "cancelled")

        # 1. Retry creates successor with identical input_digest and copies input.json
        succ_rec = job_service.retry(initial_rec.job_id, "retry_attempt_1")
        self.assertEqual(succ_rec.input_digest, expected_digest)
        self.assertEqual(succ_rec.retry.get("retry_of"), initial_rec.job_id)

        succ_input_file = job_service.store._job_dir(succ_rec.job_id) / "input.json"
        self.assertTrue(succ_input_file.is_file())
        self.assertEqual(succ_input_file.read_bytes(), payload_bytes)
        self.assertEqual(job_service.store.get_input_artifact(succ_rec.job_id), payload)

        # 2. Replay retry returns identical successor
        replay_succ = job_service.retry(initial_rec.job_id, "retry_attempt_1")
        self.assertEqual(replay_succ.job_id, succ_rec.job_id)
        self.assertEqual(replay_succ.input_digest, expected_digest)

        # 3. Mismatched input artifact digest raises JobCorruptionError
        corrupt_spec = JobSpec(
            project_id="p1",
            command_ref="review-runner",
            idempotency_key="corrupt_review_job_key",
            input_digest=expected_digest,
        )
        corrupt_rec = job_service.submit(corrupt_spec, input_payload=payload)
        job_service.cancel(corrupt_rec.job_id, reason="fail")
        # Tamper with input.json
        (job_service.store._job_dir(corrupt_rec.job_id) / "input.json").write_bytes(b"{\"tampered\": true}")
        with self.assertRaises(JobCorruptionError) as ctx:
            job_service.retry(corrupt_rec.job_id, "retry_tampered")
        self.assertIn("input artifact digest mismatch", str(ctx.exception))

        # 4. Missing input.json when input_digest is set raises JobCorruptionError
        missing_spec = JobSpec(
            project_id="p1",
            command_ref="review-runner",
            idempotency_key="missing_input_job_key",
            input_digest=expected_digest,
        )
        missing_rec = job_service.submit(missing_spec, input_payload=payload)
        job_service.cancel(missing_rec.job_id, reason="fail")
        # Delete input.json
        (job_service.store._job_dir(missing_rec.job_id) / "input.json").unlink()
        with self.assertRaises(JobCorruptionError) as ctx:
            job_service.retry(missing_rec.job_id, "retry_missing")
        self.assertIn("input.json is missing", str(ctx.exception))

        # 5. Stranded retry re-drive via JobRecoveryCoordinator copies input.json to successor
        stranded_spec = JobSpec(
            project_id="p1",
            command_ref="review-runner",
            idempotency_key="stranded_retry_job_key",
            input_digest=expected_digest,
        )
        stranded_rec = job_service.submit(stranded_spec, input_payload=payload)
        job_service.cancel(stranded_rec.job_id, reason="fail")
        succ_id, is_new = job_service.store.claim_retry(stranded_rec.job_id, "stranded_req_1")
        self.assertTrue(is_new)
        self.assertIsNone(job_service.store.get(succ_id))

        coord = JobRecoveryCoordinator(self.runtime, service=job_service)
        res = coord.advance()
        self.assertIn(succ_id, res["redriven"])
        redriven_succ = job_service.store.get(succ_id)
        self.assertIsNotNone(redriven_succ)
        self.assertEqual(redriven_succ.input_digest, expected_digest)
        redriven_input = job_service.store._job_dir(succ_id) / "input.json"
        self.assertTrue(redriven_input.is_file())
        self.assertEqual(redriven_input.read_bytes(), payload_bytes)


if __name__ == "__main__":
    unittest.main()
