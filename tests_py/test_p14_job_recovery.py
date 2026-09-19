"""Tests for P14 durable jobs recovery, heartbeat sequence, and ambiguous outcome handling.

Covers:
- Supervisor kill mid-run (reconciles to unknown_recovery)
- Daemon restart recovery sweep via JobRecoveryCoordinator
- Stale versus advancing heartbeat_sequence
- PID reuse defeated by start_token mismatch
- Ambiguous evidence yielding unknown_recovery with no successor
- recovery_safe_retry only for never-started work
- Watchdog clock-free progress signals integration
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dev_orchestrator.core.watchdog import (
    FINGERPRINT_FIELDS,
    FINGERPRINT_FORBIDDEN,
    build_progress_fingerprint,
    canonical_path,
    collect_progress_signals,
)
from dev_orchestrator.jobs.config import (
    JobCommandConfig,
    JobProjectConfig,
    JobsConfig,
)
from dev_orchestrator.jobs.models import (
    JobRecord,
    JobSpec,
    job_id_for,
)
from dev_orchestrator.jobs.recovery import JobRecoveryCoordinator
from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json


def make_test_config(temp_dir: Path, repo_path: Path) -> JobsConfig:
    return JobsConfig(
        runtime_root=temp_dir,
        enabled=True,
        projects={
            "p1": JobProjectConfig(
                repo_path=repo_path,
                commands={
                    "build_cmd": JobCommandConfig(
                        argv=["python", "-c", "import time; time.sleep(10)"],
                        cwd=".",
                        duration_class="long",
                        heartbeat_interval_seconds=1.0,
                    )
                },
            )
        },
    )


class P14JobRecoveryTests(unittest.TestCase):
    def test_supervisor_kill_mid_run_reconciles_to_unknown_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            cfg = make_test_config(rt, repo)

            mock_transport = MagicMock()
            service = JobService(rt, transports={"local": mock_transport}, config=cfg)
            spec = JobSpec(project_id="p1", command_ref="build_cmd", idempotency_key="kill-mid-run")

            rec = service.submit(spec)
            # Simulate supervisor running: pid recorded, started_at recorded, state=running
            now = utc_now_iso()
            service.store.update(rec.job_id, lambda r: (
                r.transition_to("running", timestamp=now),
                r.supervisor.update({"pid": 11111, "start_token": "tok-1", "started_at": now}),
                r.timestamps.update({"started_at": now}),
            ))

            # Simulate supervisor killed: is_pid_alive returns False, no result.json
            with patch("dev_orchestrator.jobs.service.is_pid_alive", return_value=False):
                reconciled = service.reconcile(rec.job_id)
                self.assertEqual(reconciled.state, "unknown_recovery")
                self.assertEqual(reconciled.failure_kind, "supervisor_died_without_result")
                self.assertFalse(reconciled.recovery.get("recovery_safe_retry", True))

    def test_daemon_restart_recovery_sweep(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            cfg = make_test_config(rt, repo)

            mock_transport = MagicMock()
            service = JobService(rt, transports={"local": mock_transport}, config=cfg)

            # Job 1: finished normally with result.json
            spec1 = JobSpec(project_id="p1", command_ref="build_cmd", idempotency_key="job-done")
            rec1 = service.submit(spec1)
            service.store.update(rec1.job_id, lambda r: r.transition_to("running"))
            write_json(service.store._job_dir(rec1.job_id) / "result.json", {
                "job_id": rec1.job_id,
                "exit_code": 0,
                "outcome": "success",
                "finished_at": utc_now_iso(),
            })

            # Job 2: killed mid-run (running, dead PID, no result.json)
            spec2 = JobSpec(project_id="p1", command_ref="build_cmd", idempotency_key="job-crashed")
            rec2 = service.submit(spec2)
            now = utc_now_iso()
            service.store.update(rec2.job_id, lambda r: (
                r.transition_to("running", timestamp=now),
                r.supervisor.update({"pid": 22222, "start_token": "tok-2", "started_at": now}),
                r.timestamps.update({"started_at": now}),
            ))

            # Job 3: never started (queued, no pid, no started_at)
            spec3 = JobSpec(project_id="p1", command_ref="build_cmd", idempotency_key="job-never-started")
            rec3 = service.submit(spec3)

            # Re-initialize coordinator simulating daemon startup
            coordinator = JobRecoveryCoordinator(rt, service=service, config=cfg)
            with patch("dev_orchestrator.jobs.service.is_pid_alive", return_value=False):
                res = coordinator.recover()

            # Job 1 promoted to completed
            rec1_after = service.status(rec1.job_id)
            self.assertEqual(rec1_after.state, "completed")

            # Job 2 promoted to unknown_recovery
            rec2_after = service.status(rec2.job_id)
            self.assertEqual(rec2_after.state, "unknown_recovery")
            self.assertFalse(rec2_after.recovery.get("recovery_safe_retry", True))

            # Job 3 failed with recovery_safe_retry=True
            rec3_after = service.status(rec3.job_id)
            self.assertEqual(rec3_after.state, "failed")
            self.assertEqual(rec3_after.failure_kind, "never_started")
            self.assertTrue(rec3_after.recovery.get("recovery_safe_retry", False))

    def test_stale_vs_advancing_heartbeat_sequence(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            cfg = make_test_config(rt, repo)

            mock_transport = MagicMock()
            service = JobService(rt, transports={"local": mock_transport}, config=cfg)
            spec = JobSpec(project_id="p1", command_ref="build_cmd", idempotency_key="hb-seq")
            rec = service.submit(spec)

            now = utc_now_iso()
            service.store.update(rec.job_id, lambda r: (
                r.transition_to("running", timestamp=now),
                r.supervisor.update({"pid": 33333, "start_token": "tok-3", "started_at": now}),
                r.timestamps.update({"started_at": now}),
            ))

            # Advancing heartbeat: sequence 1 -> 2
            write_json(service.store._job_dir(rec.job_id) / "heartbeat.json", {
                "job_id": rec.job_id,
                "pid": 33333,
                "start_token": "tok-3",
                "heartbeat_sequence": 1,
                "reported_at": now,
            })
            with patch("dev_orchestrator.jobs.service.is_pid_alive", return_value=True):
                rec_check1 = service.reconcile(rec.job_id)
                self.assertEqual(rec_check1.state, "running")
                self.assertEqual(rec_check1.heartbeat.get("heartbeat_sequence"), 1)
                self.assertIsNotNone(rec_check1.heartbeat.get("observed_at"))

            write_json(service.store._job_dir(rec.job_id) / "heartbeat.json", {
                "job_id": rec.job_id,
                "pid": 33333,
                "start_token": "tok-3",
                "heartbeat_sequence": 2,
                "reported_at": utc_now_iso(),
            })
            with patch("dev_orchestrator.jobs.service.is_pid_alive", return_value=True):
                rec_check2 = service.reconcile(rec.job_id)
                self.assertEqual(rec_check2.state, "running")
                self.assertEqual(rec_check2.heartbeat.get("heartbeat_sequence"), 2)

            # Stale heartbeat & process dead: reconcile transitions to unknown_recovery
            with patch("dev_orchestrator.jobs.service.is_pid_alive", return_value=False):
                rec_dead = service.reconcile(rec.job_id)
                self.assertEqual(rec_dead.state, "unknown_recovery")

    def test_stalled_heartbeat_detected_when_process_alive_but_sequence_frozen(self):
        from datetime import datetime, timezone, timedelta

        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            cfg = make_test_config(rt, repo)

            mock_transport = MagicMock()
            service = JobService(rt, transports={"local": mock_transport}, config=cfg)
            spec = JobSpec(project_id="p1", command_ref="build_cmd", idempotency_key="hb-stalled")
            rec = service.submit(spec)

            past_time = (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()
            service.store.update(rec.job_id, lambda r: (
                r.transition_to("running", timestamp=past_time),
                r.supervisor.update({"pid": 55555, "start_token": "tok-stalled", "started_at": past_time}),
                r.heartbeat.update({
                    "sequence": 10,
                    "heartbeat_sequence": 10,
                    "reported_at": past_time,
                    "observed_at": past_time,
                }),
                r.timestamps.update({"started_at": past_time}),
            ))
            # Write heartbeat with same sequence 10 (not advancing)
            write_json(service.store._job_dir(rec.job_id) / "heartbeat.json", {
                "job_id": rec.job_id,
                "pid": 55555,
                "start_token": "tok-stalled",
                "sequence": 10,
                "heartbeat_sequence": 10,
                "reported_at": past_time,
            })

            # Process is still reported alive by OS, but heartbeat has not advanced past timeout
            with patch("dev_orchestrator.jobs.service.is_pid_alive", return_value=True):
                reconciled = service.reconcile(rec.job_id)
                self.assertEqual(reconciled.state, "unknown_recovery")
                self.assertEqual(reconciled.failure_kind, "heartbeat_stalled")
                self.assertFalse(reconciled.recovery.get("recovery_safe_retry", True))

    def test_pid_reuse_defeated_by_start_token_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            cfg = make_test_config(rt, repo)

            mock_transport = MagicMock()
            service = JobService(rt, transports={"local": mock_transport}, config=cfg)
            spec = JobSpec(project_id="p1", command_ref="build_cmd", idempotency_key="pid-reuse")
            rec = service.submit(spec)

            now = utc_now_iso()
            service.store.update(rec.job_id, lambda r: (
                r.transition_to("running", timestamp=now),
                r.supervisor.update({"pid": 44444, "start_token": "tok-original", "started_at": now}),
                r.timestamps.update({"started_at": now}),
            ))

            # Simulate OS reused PID 44444, but heartbeat has a DIFFERENT start_token
            write_json(service.store._job_dir(rec.job_id) / "heartbeat.json", {
                "job_id": rec.job_id,
                "pid": 44444,
                "start_token": "tok-different-unrelated-process",
                "heartbeat_sequence": 99,
                "reported_at": now,
            })

            # is_pid_alive returns True because PID 44444 exists in OS
            with patch("dev_orchestrator.jobs.service.is_pid_alive", return_value=True):
                # Start token mismatch MUST defeat PID reuse and fail closed to unknown_recovery!
                reconciled = service.reconcile(rec.job_id)
                self.assertEqual(reconciled.state, "unknown_recovery")

    def test_ambiguous_evidence_yielding_unknown_recovery_refuses_retry(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            cfg = make_test_config(rt, repo)

            mock_transport = MagicMock()
            service = JobService(rt, transports={"local": mock_transport}, config=cfg)
            spec = JobSpec(project_id="p1", command_ref="build_cmd", idempotency_key="ambig-test")
            rec = service.submit(spec)

            # Move to running, then unknown_recovery
            now = utc_now_iso()
            service.store.update(rec.job_id, lambda r: (
                r.transition_to("running", timestamp=now),
                r.transition_to("unknown_recovery", reason="ambiguous process crash", timestamp=now),
                r.recovery.update({"recovery_safe_retry": False}),
            ))

            with patch("dev_orchestrator.jobs.service.is_pid_alive", return_value=False):
                with self.assertRaises(ValueError) as ctx:
                    service.retry(rec.job_id, retry_request_id="retry-after-ambig")
                self.assertIn("neither terminal nor marked recovery_safe_retry", str(ctx.exception))

            # Store must have NO successor
            rec_after = service.status(rec.job_id)
            self.assertIsNone(rec_after.retry.get("successor_job_id"))
            self.assertEqual(mock_transport.job_start.call_count, 1)  # Only original submit

    def test_recovery_safe_retry_only_for_never_started_work(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            cfg = make_test_config(rt, repo)

            mock_transport = MagicMock()
            mock_transport.job_start.return_value = {"status": "started"}
            service = JobService(rt, transports={"local": mock_transport}, config=cfg)
            spec = JobSpec(project_id="p1", command_ref="build_cmd", idempotency_key="never-started")

            rec = service.submit(spec)
            # Reconcile never-started job
            with patch("dev_orchestrator.jobs.service.is_pid_alive", return_value=False):
                rec_recon = service.reconcile(rec.job_id)
                self.assertEqual(rec_recon.state, "failed")
                self.assertEqual(rec_recon.failure_kind, "never_started")
                self.assertTrue(rec_recon.recovery.get("recovery_safe_retry"))

                # Now retry is allowed for never-started work!
                succ_rec = service.retry(rec.job_id, retry_request_id="retry-never-started")
                self.assertIsNotNone(succ_rec)
                self.assertEqual(succ_rec.retry.get("attempt"), 2)
                self.assertEqual(succ_rec.retry.get("retry_of"), rec.job_id)

    def test_watchdog_progress_signals_integration(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            jobs_dir = rt / "jobs"
            jobs_dir.mkdir(parents=True)

            # Write jobs index with project job and watchdog-excluded job
            now = utc_now_iso()
            jobs_index = {
                "jobs": {
                    "job-test-1": {
                        "job_id": "job-test-1",
                        "project_id": "p1",
                        "state": "running",
                        "command_ref": "build_cmd",
                        "started_at": now,
                    },
                    "wd-job-internal": {
                        "job_id": "wd-job-internal",
                        "project_id": "p1",
                        "state": "running",
                        "source": "watchdog",
                        "started_at": now,
                    },
                }
            }
            write_json(jobs_dir / "index.json", jobs_index)

            repo_fp = hashlib.sha256(canonical_path(repo).encode("utf-8")).hexdigest()[:16]
            snapshot = {
                "project_id": "p1",
                "repo_path": str(repo),
                "git": {"head": "abc1234"},
                "activity": {
                    "watchdog_safe": {
                        "repo_scope": "canonical",
                        "repo_root_fingerprint": repo_fp,
                        "last_activity_at": now,
                    }
                },
            }

            last_prog, fp, sig = collect_progress_signals(snapshot, rt)
            self.assertIn("fingerprint_inputs", sig)
            self.assertIn("job_records", sig["fingerprint_inputs"])
            job_sig = sig["fingerprint_inputs"]["job_records"]
            self.assertIsNotNone(job_sig)
            self.assertEqual(job_sig["id"], "job-test-1")
            self.assertEqual(job_sig["timestamp"], now)

            # Verify build_progress_fingerprint includes job_records and stays clock-free
            payload = {
                "git_head": "abc1234",
                "last_activity_at": now,
                "newest_kind": "job",
                "newest_path": "jobs/job-test-1",
                "sources": {},
                "changed_entries_considered": 1,
                "role_records": {},
                "progress_entry": None,
                "job_records": job_sig,
            }
            fp1 = build_progress_fingerprint(payload)
            fp2 = build_progress_fingerprint(payload)
            self.assertEqual(fp1, fp2)
            self.assertEqual(len(fp1), 16)

            # Clock-free fingerprint purity: forbidden keys raise ValueError
            for forbidden in FINGERPRINT_FORBIDDEN:
                bad_payload = dict(payload)
                bad_payload[forbidden] = "value"
                with self.assertRaises(ValueError):
                    build_progress_fingerprint(bad_payload)


if __name__ == "__main__":
    unittest.main()
