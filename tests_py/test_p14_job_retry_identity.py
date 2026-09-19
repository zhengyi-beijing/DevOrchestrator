"""Tests for P14 durable jobs retry identity and one-successor-per-predecessor semantics.

Covers deterministic successor mapping, replay of the same retry_request_id returning
identical successor without second spawn, conflict on a different retry_request_id once
a successor exists, refusal to retry a non-terminal non-recovery-safe job, and
crash-between-intent-and-spawn recovery re-driving the same successor.
"""

from __future__ import annotations

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
    JobConflictError,
    JobRecord,
    JobSpec,
    job_id_for,
    retry_successor_id,
)
from dev_orchestrator.jobs.recovery import JobRecoveryCoordinator
from dev_orchestrator.jobs.service import JobService
from dev_orchestrator.jobs.store import ExecutionJobStore
from dev_orchestrator.storage.json_store import write_json


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


class P14JobRetryIdentityTests(unittest.TestCase):
    def test_deterministic_successor_mapping(self):
        pred_id = "job-abc12345"
        req_id = "retry-req-001"
        succ1 = retry_successor_id(pred_id, req_id)
        succ2 = retry_successor_id(pred_id, req_id)

        self.assertEqual(succ1, succ2)
        self.assertTrue(succ1.startswith("job-"))

        # Different retry_request_id yields different successor
        succ3 = retry_successor_id(pred_id, "retry-req-002")
        self.assertNotEqual(succ1, succ3)

        # Different predecessor yields different successor
        succ4 = retry_successor_id("job-other9999", req_id)
        self.assertNotEqual(succ1, succ4)

    def test_replay_same_retry_request_id_returns_identical_successor_with_no_second_spawn(self):
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
                idempotency_key="initial-job-1",
            )
            initial_rec = service.submit(spec)
            self.assertEqual(mock_transport.job_start.call_count, 1)

            # Mark initial job failed (terminal)
            service.cancel(initial_rec.job_id, reason="test failure")
            rec_cancelled = service.status(initial_rec.job_id)
            self.assertEqual(rec_cancelled.state, "cancelled")

            # First retry
            retry_req_id = "retry-attempt-1"
            succ_rec1 = service.retry(initial_rec.job_id, retry_req_id)
            expected_succ_id = retry_successor_id(initial_rec.job_id, retry_req_id)
            self.assertEqual(succ_rec1.job_id, expected_succ_id)
            self.assertEqual(succ_rec1.retry.get("retry_of"), initial_rec.job_id)
            self.assertEqual(succ_rec1.retry.get("attempt"), 2)
            # Spawn count should now be 2 (initial + successor)
            self.assertEqual(mock_transport.job_start.call_count, 2)

            # Replay of exact same retry_request_id
            succ_rec2 = service.retry(initial_rec.job_id, retry_req_id)
            self.assertEqual(succ_rec1.job_id, succ_rec2.job_id)
            # Crucial: NO second spawn on replay!
            self.assertEqual(mock_transport.job_start.call_count, 2)

    def test_conflict_on_different_retry_request_id_after_successor_exists(self):
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
                idempotency_key="initial-job-conflict",
            )
            initial_rec = service.submit(spec)
            service.cancel(initial_rec.job_id, reason="cancelled")

            # First retry with req-A
            service.retry(initial_rec.job_id, "retry-req-A")

            # Attempting second retry with different retry_request_id must raise JobConflictError
            with self.assertRaises(JobConflictError):
                service.retry(initial_rec.job_id, "retry-req-B")

    def test_refusal_to_retry_non_terminal_non_recovery_safe_job(self):
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
                idempotency_key="non-terminal-key",
            )
            rec = service.submit(spec)
            # Job is running/queued and supervisor PID is simulated alive
            service.store.update(rec.job_id, lambda r: r.supervisor.update({"pid": 99999, "start_token": "tok1"}))

            with patch("dev_orchestrator.jobs.service.is_pid_alive", return_value=True):
                # Predecessor is non-terminal and live; retry must be refused
                with self.assertRaises(ValueError) as ctx:
                    service.retry(rec.job_id, "retry-req-refused")
                self.assertIn("neither terminal nor marked recovery_safe_retry", str(ctx.exception))

    def test_crash_between_intent_and_spawn_service_redrive(self):
        """When retry intent is claimed in store but supervisor was never spawned (crash),

        calling retry() again re-drives the exact same recorded successor.
        """
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
                idempotency_key="crash-test-key",
            )
            initial_rec = service.submit(spec)
            service.cancel(initial_rec.job_id, reason="failed")
            self.assertEqual(mock_transport.job_start.call_count, 1)

            retry_req_id = "crash-req-001"
            # Simulate crash: claim retry directly in store, recording successor_job_id,
            # but do NOT spawn supervisor or write successor record.
            succ_id, is_new = service.store.claim_retry(initial_rec.job_id, retry_req_id)
            self.assertTrue(is_new)
            pred_rec = service.store.get(initial_rec.job_id)
            self.assertEqual(pred_rec.retry.get("successor_job_id"), succ_id)
            self.assertIsNone(pred_rec.retry.get("successor_spawned_at"))
            self.assertIsNone(service.store.get(succ_id))

            # Service retry is called (e.g. client retransmits or coordinator handles it)
            succ_rec = service.retry(initial_rec.job_id, retry_req_id)
            self.assertEqual(succ_rec.job_id, succ_id)
            # Transport job_start was called for the stranded successor
            self.assertEqual(mock_transport.job_start.call_count, 2)

            # Updated predecessor now has successor_spawned_at
            pred_after = service.store.get(initial_rec.job_id)
            self.assertIsNotNone(pred_after.retry.get("successor_spawned_at"))

    def test_crash_between_intent_and_spawn_coordinator_redrive(self):
        """JobRecoveryCoordinator automatically detects stranded retry intent and re-drives it."""
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
                idempotency_key="coordinator-redrive-key",
            )
            initial_rec = service.submit(spec)
            service.cancel(initial_rec.job_id, reason="failed")
            self.assertEqual(mock_transport.job_start.call_count, 1)

            retry_req_id = "coordinator-redrive-req"
            succ_id, is_new = service.store.claim_retry(initial_rec.job_id, retry_req_id)
            self.assertTrue(is_new)
            self.assertIsNone(service.store.get(succ_id))

            # Run coordinator startup recovery
            coordinator = JobRecoveryCoordinator(rt, service=service, config=cfg)
            res = coordinator.recover()
            self.assertIn(succ_id, res.get("redriven", []))

            # Successor record was created and spawned
            succ_rec = service.store.get(succ_id)
            self.assertIsNotNone(succ_rec)
            self.assertEqual(succ_rec.job_id, succ_id)
            self.assertEqual(mock_transport.job_start.call_count, 2)

    def test_ssh_retry_remote_job_correlation(self):
        """SSH retry claims successor under retry_successor_id and sends exact successor id over wire."""
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            repo = rt / "repo"
            repo.mkdir()
            cfg = make_test_config(rt, repo)

            mock_transport = MagicMock()
            mock_transport.job_start.return_value = {"status": "started", "supervisor_pid": 8888}

            service = JobService(rt, transports={"ssh": mock_transport}, config=cfg)
            spec = JobSpec(
                project_id="p1",
                command_ref="test_cmd",
                idempotency_key="ssh-retry-idem-1",
                transport="ssh",
            )
            initial_rec = service.submit(spec)
            self.assertEqual(mock_transport.job_start.call_count, 1)
            # Fail initial job
            service.cancel(initial_rec.job_id, reason="build failed")

            # Retry with stable retry_request_id
            retry_req_id = "retry-ssh-req-456"
            succ_rec = service.retry(initial_rec.job_id, retry_req_id)
            self.assertEqual(mock_transport.job_start.call_count, 2)

            # Check that transport was invoked with the exact successor id, NOT job_id_for(succ_spec)
            second_call_spec, second_call_jdir = mock_transport.job_start.call_args_list[1][0]
            self.assertEqual(second_call_jdir.name, succ_rec.job_id)
            self.assertEqual(succ_rec.job_id, retry_successor_id(initial_rec.job_id, retry_req_id))

            # Now verify SSHJobTransport packaging preserves that successor id
            from dev_orchestrator.jobs.transport import SSHJobTransport
            from dev_orchestrator.ai.execution_transport import SSHTransportConfig
            fake_subp = MagicMock()
            ssh_t = SSHJobTransport(SSHTransportConfig(peer="remote.host"), subprocess_module=fake_subp)

            # Mock _execute_op to inspect envelope sent over wire
            with patch.object(ssh_t, "_execute_op") as mock_exec:
                mock_exec.return_value = {"job_id": succ_rec.job_id, "status": "started"}
                res = ssh_t.job_start(second_call_spec, second_call_jdir)
                sent_env = mock_exec.call_args[0][0]
                # Over-the-wire job_id MUST be the successor ID!
                self.assertEqual(sent_env["job_id"], succ_rec.job_id)

            # Verify remote_helper handles this target_job_id without stranding
            from dev_orchestrator.ai.remote_helper import execute_request
            remote_rt = rt / "remote_rt"
            remote_rt.mkdir()
            remote_repo = rt / "remote_repo"
            remote_repo.mkdir()
            remote_cfg_file = rt / "remote_jobs.json"
            write_json(remote_cfg_file, {
                "runtime_root": str(remote_rt),
                "enabled": True,
                "projects": {
                    "p1": {
                        "repo_path": str(remote_repo),
                        "commands": {
                            "test_cmd": {
                                "argv": ["python", "-c", "print('remote ok')"],
                                "cwd": ".",
                            }
                        }
                    }
                }
            })
            with patch("dev_orchestrator.jobs.config.resolve_remote_jobs_config_path", return_value=remote_cfg_file), \
                 patch("dev_orchestrator.jobs.transport.spawn_detached") as mock_remote_spawn:
                mock_remote_proc = MagicMock()
                mock_remote_proc.pid = 9999
                mock_remote_spawn.return_value = mock_remote_proc

                start_res = execute_request({
                    "operation": "job_start",
                    "request_id": "req-rem-1",
                    "job_id": succ_rec.job_id,
                    "project_id": "p1",
                    "command_ref": "test_cmd",
                    "idempotency_key": second_call_spec.idempotency_key,
                })
                self.assertEqual(start_res["job_id"], succ_rec.job_id)
                self.assertEqual(start_res["supervisor_pid"], 9999)

                # Remote store now has the job in the successor directory!
                remote_store = ExecutionJobStore(remote_rt)
                rem_rec = remote_store.get(succ_rec.job_id)
                self.assertIsNotNone(rem_rec)
                self.assertEqual(rem_rec.job_id, succ_rec.job_id)
                self.assertEqual(rem_rec.supervisor["pid"], 9999)

                # Remote status query by successor ID succeeds
                status_res = execute_request({
                    "operation": "job_status",
                    "request_id": "req-rem-2",
                    "job_id": succ_rec.job_id,
                })
                self.assertEqual(status_res["job_id"], succ_rec.job_id)
                self.assertIsNotNone(status_res.get("job"))


if __name__ == "__main__":
    unittest.main()
