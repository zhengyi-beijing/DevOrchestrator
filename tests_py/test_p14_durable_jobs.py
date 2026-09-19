"""Tests for P14 durable execution jobs foundation.

Covers identity determinism, idempotent submit, spec conflict, corruption quarantine,
index rebuild, log truncation/redaction, and state machine transitions.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from dev_orchestrator.jobs.config import (
    JobCommandConfig,
    JobProjectConfig,
    JobsConfig,
)
from dev_orchestrator.jobs.logs import BoundedNDJSONLog
from dev_orchestrator.jobs.models import (
    JOB_STATES,
    TERMINAL_JOB_STATES,
    JobConflictError,
    JobCorruptionError,
    JobRecord,
    JobSpec,
    JobTransitionError,
    job_id_for,
    spec_hash,
)
from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.jobs.store import ExecutionJobStore


def make_test_config(temp_dir: Path, repo_path: Path) -> JobsConfig:
    return JobsConfig(
        runtime_root=temp_dir,
        enabled=True,
        projects={
            "p1": JobProjectConfig(
                repo_path=repo_path,
                commands={
                    "test_cmd": JobCommandConfig(
                        argv=["python", "-c", "print('hello')"],
                        cwd=".",
                        duration_class="short",
                    )
                },
            )
        },
    )


class P14DurableJobsFoundationTests(unittest.TestCase):
    def test_identity_determinism_and_canonical_spec_hash(self):
        spec1 = JobSpec(
            project_id="p1",
            command_ref="test_cmd",
            idempotency_key="key-abc-123",
            kind="validation",
        )
        spec2 = JobSpec(
            project_id="p1",
            command_ref="test_cmd",
            idempotency_key="key-abc-123",
            kind="validation",
        )
        self.assertEqual(job_id_for(spec1), job_id_for(spec2))
        self.assertTrue(job_id_for(spec1).startswith("job-"))
        self.assertEqual(spec_hash(spec1), spec_hash(spec2))
        self.assertTrue(spec_hash(spec1).startswith("sha256:"))

        # Differing idempotency key produces differing job_id
        spec3 = JobSpec(
            project_id="p1",
            command_ref="test_cmd",
            idempotency_key="key-different",
            kind="validation",
        )
        self.assertNotEqual(job_id_for(spec1), job_id_for(spec3))

    def test_idempotent_submit_with_single_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            cfg = make_test_config(rt, repo)

            mock_transport = MagicMock()
            mock_transport.job_start.return_value = {"status": "started"}

            service = JobService(rt, transports={"local": mock_transport}, config=cfg)
            spec = JobSpec(
                project_id="p1",
                command_ref="test_cmd",
                idempotency_key="test-key-1",
            )

            # First submit
            rec1 = service.submit(spec)
            self.assertEqual(rec1.job_id, job_id_for(spec))
            self.assertEqual(mock_transport.job_start.call_count, 1)

            # Replay of exact same submit
            rec2 = service.submit(spec)
            self.assertEqual(rec1.job_id, rec2.job_id)
            # No duplicate spawn!
            self.assertEqual(mock_transport.job_start.call_count, 1)

    def test_spec_conflict_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            cfg = JobsConfig(
                runtime_root=rt,
                enabled=True,
                projects={
                    "p1": JobProjectConfig(
                        repo_path=repo,
                        commands={
                            "cmd_a": JobCommandConfig(argv=["echo", "a"]),
                            "cmd_b": JobCommandConfig(argv=["echo", "b"]),
                        },
                    )
                },
            )
            mock_transport = MagicMock()
            mock_transport.job_start.return_value = {"status": "started"}
            service = JobService(rt, transports={"local": mock_transport}, config=cfg)

            spec_a = JobSpec(project_id="p1", command_ref="cmd_a", idempotency_key="same-key")
            service.submit(spec_a)

            # Same idempotency key but different command_ref -> conflict!
            spec_b = JobSpec(project_id="p1", command_ref="cmd_b", idempotency_key="same-key")
            with self.assertRaises(JobConflictError):
                service.submit(spec_b)

    def test_corruption_quarantine_and_health(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            store = ExecutionJobStore(rt)

            spec = JobSpec(project_id="p1", command_ref="c", idempotency_key="k1")
            rec, _ = store.claim_or_get(spec, lambda jid, shash: JobRecord(
                job_id=jid,
                idempotency_key=spec.idempotency_key,
                spec_hash=shash,
                kind=spec.kind,
                project_id=spec.project_id,
                command_ref=spec.command_ref,
                resolved_argv=["echo"],
                working_directory=str(rt),
                transport="local",
                host_identity="host",
                duration_class="short",
            ))

            # Corrupt job.json file with non-JSON bytes
            jfile = store._job_dir(rec.job_id) / "job.json"
            jfile.write_text("CORRUPTED-NOT-JSON{{{{", encoding="utf-8")

            # get() raises JobCorruptionError
            with self.assertRaises(JobCorruptionError):
                store.get(rec.job_id)

            # repair_corruption() moves it to quarantine and marks health degraded
            quarantined = store.repair_corruption()
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0]["job_id"], rec.job_id)

            health = store.health()
            self.assertTrue(health["degraded"])
            self.assertIn(quarantined[0]["quarantined_path"], health["quarantined"])

    def test_index_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            store = ExecutionJobStore(rt)

            spec1 = JobSpec(project_id="p1", command_ref="c", idempotency_key="k1")
            spec2 = JobSpec(project_id="p1", command_ref="c", idempotency_key="k2")

            store.claim_or_get(spec1, lambda jid, shash: JobRecord(
                job_id=jid, idempotency_key=spec1.idempotency_key, spec_hash=shash,
                kind=spec1.kind, project_id="p1", command_ref="c", resolved_argv=["echo"],
                working_directory=str(rt), transport="local", host_identity="host", duration_class="short",
            ))
            store.claim_or_get(spec2, lambda jid, shash: JobRecord(
                job_id=jid, idempotency_key=spec2.idempotency_key, spec_hash=shash,
                kind=spec2.kind, project_id="p1", command_ref="c", resolved_argv=["echo"],
                working_directory=str(rt), transport="local", host_identity="host", duration_class="short",
            ))

            self.assertEqual(len(store.list()), 2)

            # Delete index.json
            store.index_path.unlink()

            # Rebuild index reconstructs the records
            rebuilt = store.rebuild_index()
            self.assertEqual(len(rebuilt["jobs"]), 2)
            self.assertEqual(len(store.list()), 2)

    def test_log_truncation_and_redaction(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "log.ndjson"
            # Small caps for testing compaction
            logger = BoundedNDJSONLog(
                log_path,
                max_line_bytes=100,
                max_job_bytes=1000,
                head_lines=5,
                tail_lines=5,
            )

            # Test per-line capping
            long_line = "A" * 200
            entry = logger.append(long_line)
            self.assertEqual(len(entry["text"]), 100)
            self.assertEqual(entry["truncated_bytes"], 100)

            # Append secrets to test redaction
            logger.append("Bearer secret_api_token_123456")
            logger.append("regular non-sensitive line")

            # Append many lines to trigger file compaction
            for i in range(50):
                logger.append(f"line number {i} padding data to grow file")

            paginated = logger.read_paginated(cursor=0, limit=50)
            self.assertTrue(paginated["truncated_lines"] > 0)

            # Verify secret redaction applied
            all_text = " ".join(str(l.get("text")) for l in paginated["lines"])
            self.assertNotIn("secret_api_token_123456", all_text)
            self.assertIn("[REDACTED]", all_text)

    def test_legal_and_illegal_transitions(self):
        record = JobRecord(
            job_id="job-1",
            idempotency_key="k",
            spec_hash="h",
            kind="validation",
            project_id="p1",
            command_ref="c",
            resolved_argv=["echo"],
            working_directory=".",
            transport="local",
            host_identity="host",
            duration_class="short",
            state="queued",
        )

        # Legal: queued -> running
        record.transition_to("running")
        self.assertEqual(record.state, "running")

        # Legal: running -> completed
        record.transition_to("completed")
        self.assertEqual(record.state, "completed")

        # Illegal: completed -> running (terminal states are write-once)
        with self.assertRaises(JobTransitionError):
            record.transition_to("running")

        # Illegal: completed -> failed
        with self.assertRaises(JobTransitionError):
            record.transition_to("failed")

        # Check unknown_recovery legal transitions
        rec2 = JobRecord(
            job_id="job-2",
            idempotency_key="k2",
            spec_hash="h2",
            kind="validation",
            project_id="p1",
            command_ref="c",
            resolved_argv=["echo"],
            working_directory=".",
            transport="local",
            host_identity="host",
            duration_class="short",
            state="running",
        )
        rec2.transition_to("unknown_recovery")
        self.assertEqual(rec2.state, "unknown_recovery")
        # unknown_recovery can transition to terminal when reconciled
        rec2.transition_to("failed", failure_kind="reconciled_fail")
        self.assertEqual(rec2.state, "failed")


if __name__ == "__main__":
    unittest.main()
