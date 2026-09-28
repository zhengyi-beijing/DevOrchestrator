"""Tests for P18 canonical identity, JobSpec hash preservation, and tamper detection."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from dev_orchestrator.jobs.models import (
    JobCorruptionError,
    JobRecord,
    JobSpec,
    job_id_for,
    spec_hash,
)
from dev_orchestrator.jobs.store import ExecutionJobStore


class TestP18CanonicalIdentity(unittest.TestCase):
    """Verifies that P18 digest fields preserve byte-identical legacy spec hashes and reject tampering."""

    def test_legacy_spec_hash_stability(self):
        """A JobSpec without digest fields produces exact same hash as legacy P14."""
        spec_legacy = JobSpec(
            project_id="test_project",
            command_ref="build",
            idempotency_key="key-12345",
            kind="validation",
            transport="local",
            expected_working_directory=".",
        )
        # Expected canonical dict has no digest fields
        canon = spec_legacy.to_canonical_dict()
        self.assertNotIn("parameters_digest", canon)
        self.assertNotIn("execution_policy_digest", canon)
        self.assertNotIn("resolution_digest", canon)

        h1 = spec_hash(spec_legacy)
        self.assertIsInstance(h1, str)
        self.assertTrue(h1.startswith("sha256:"))
        self.assertEqual(len(h1), 71)

        # Re-creating an identical spec produces the exact same hash
        spec_legacy_2 = JobSpec(
            project_id="test_project",
            command_ref="build",
            idempotency_key="key-12345",
            kind="validation",
            transport="local",
            expected_working_directory=".",
            parameters_digest=None,
            execution_policy_digest=None,
            resolution_digest=None,
        )
        self.assertEqual(spec_hash(spec_legacy_2), h1)
        self.assertEqual(job_id_for(spec_legacy), job_id_for(spec_legacy_2))

    def test_digest_fields_included_only_when_present(self):
        """When digest fields are provided, they are included in canonical dict and change the hash."""
        spec_base = JobSpec(
            project_id="test_project",
            command_ref="build",
            idempotency_key="key-12345",
        )
        spec_with_digests = JobSpec(
            project_id="test_project",
            command_ref="build",
            idempotency_key="key-12345",
            parameters_digest="sha256:1111111111111111111111111111111111111111111111111111111111111111",
            execution_policy_digest="sha256:2222222222222222222222222222222222222222222222222222222222222222",
            resolution_digest="sha256:3333333333333333333333333333333333333333333333333333333333333333",
        )
        canon = spec_with_digests.to_canonical_dict()
        self.assertEqual(canon["parameters_digest"], "sha256:1111111111111111111111111111111111111111111111111111111111111111")
        self.assertEqual(canon["execution_policy_digest"], "sha256:2222222222222222222222222222222222222222222222222222222222222222")
        self.assertEqual(canon["resolution_digest"], "sha256:3333333333333333333333333333333333333333333333333333333333333333")

        self.assertNotEqual(spec_hash(spec_base), spec_hash(spec_with_digests))

    def test_job_spec_roundtrip_dict(self):
        """JobSpec deserialization from dict preserves all P18 digest fields."""
        data = {
            "project_id": "proj_a",
            "command_ref": "cmd_b",
            "idempotency_key": "k_1",
            "kind": "mutation",
            "transport": "ssh",
            "expected_working_directory": "/app",
            "parameters_digest": "p_digest_abc",
            "execution_policy_digest": "e_digest_def",
            "resolution_digest": "r_digest_ghi",
        }
        spec = JobSpec.from_dict(data)
        self.assertEqual(spec.parameters_digest, "p_digest_abc")
        self.assertEqual(spec.execution_policy_digest, "e_digest_def")
        self.assertEqual(spec.resolution_digest, "r_digest_ghi")
        canon = spec.to_canonical_dict()
        self.assertEqual(canon["parameters_digest"], "p_digest_abc")
        self.assertEqual(canon["execution_policy_digest"], "e_digest_def")
        self.assertEqual(canon["resolution_digest"], "r_digest_ghi")

    def test_job_record_digest_preservation(self):
        """JobRecord retains P18 digest fields and exports them in to_dict()."""
        rec = JobRecord(
            job_id="job-1",
            idempotency_key="k_1",
            spec_hash="hash-1",
            kind="validation",
            project_id="proj_a",
            command_ref="cmd_b",
            resolved_argv=["echo", "1"],
            working_directory=".",
            transport="local",
            host_identity="host-1",
            duration_class="short",
            parameters_digest="p_digest",
            execution_policy_digest="e_digest",
            resolution_digest="r_digest",
        )
        d = rec.to_dict()
        self.assertEqual(d["parameters_digest"], "p_digest")
        self.assertEqual(d["execution_policy_digest"], "e_digest")
        self.assertEqual(d["resolution_digest"], "r_digest")

        rec2 = JobRecord.from_dict(d)
        self.assertEqual(rec2.parameters_digest, "p_digest")
        self.assertEqual(rec2.execution_policy_digest, "e_digest")
        self.assertEqual(rec2.resolution_digest, "r_digest")

    def test_job_store_submission_spec_validation_and_corruption(self):
        """ExecutionJobStore validates submission_spec against spec_hash and raises JobCorruptionError on tamper."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = ExecutionJobStore(tmp_dir)
            spec = JobSpec(
                project_id="proj_val",
                command_ref="build",
                idempotency_key="key-canon-store-1",
                kind="validation",
                transport="local",
            )
            jid = job_id_for(spec)
            shash = spec_hash(spec)
            rec = JobRecord(
                job_id=jid,
                idempotency_key="key-canon-store-1",
                spec_hash=shash,
                kind="validation",
                project_id="proj_val",
                command_ref="build",
                resolved_argv=["python", "-m", "build"],
                working_directory=".",
                transport="local",
                host_identity="local",
                duration_class="short",
                submission_spec=spec.to_canonical_dict(),
            )
            store.save(rec)

            # Legitimate read succeeds
            read_rec = store.get(jid)
            self.assertIsNotNone(read_rec)
            self.assertEqual(read_rec.spec_hash, shash)
            self.assertEqual(read_rec.submission_spec, spec.to_canonical_dict())

            # Tampering with submission_spec on disk causes JobCorruptionError on get()
            jfile = Path(tmp_dir) / "jobs" / jid / "job.json"
            data = json.loads(jfile.read_text(encoding="utf-8"))
            data["submission_spec"]["command_ref"] = "tampered_cmd"
            jfile.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaises(JobCorruptionError):
                store.get(jid)


if __name__ == "__main__":
    unittest.main()
