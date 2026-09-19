"""Acceptance test suite for P14 Remote Execution Resilience & Recoverable Jobs.

Modeled on test_p13_software_acceptance.py.
Validates:
- Real detached execution appending to a sentinel file
- Client disconnect / detach without terminating healthy job
- Recovering identical job_id, accumulated bounded logs, and terminal result
- Daemon kill / restart recovery sweep
- Submit and retry replay across simulated lost response
- Exactly one sentinel entry per intended attempt (no duplicate side effects)
- Read-only control API endpoints (/api/v1/control/jobs*) with authorization and secret redaction
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from dev_orchestrator.ai.execution_transport import ExecutionTransportError
from dev_orchestrator.ai.remote_helper import handle_request

from dev_orchestrator.control.security import ControlSecurity
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
from dev_orchestrator.jobs.transport import LocalJobTransport, SSHJobTransport
from dev_orchestrator.platform.process import is_pid_alive, terminate_process_tree
from dev_orchestrator.storage.json_store import write_json
from dev_orchestrator.web.server import make_server


class P14SoftwareAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.runtime = Path(self.temp.name) / "runtime"
        self.runtime.mkdir(parents=True)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir(parents=True)
        self.sentinel = self.repo / "sentinel.txt"

        (self.runtime / "projects").mkdir()
        (self.runtime / "history").mkdir()
        self.config_path = self.runtime / "projects.json"

        self.project_id = "p14-accept"
        project_data = {
            "project_id": self.project_id,
            "id": self.project_id,
            "name": "P14 Acceptance Project",
            "repo_path": str(self.repo),
            "state": "READY_TO_RUN",
            "lifecycle_state": "READY_TO_RUN",
            "git": {"branch": "main", "head": "def5678", "dirty": False},
        }
        self.config_path.write_text(json.dumps({"projects": [project_data]}), encoding="utf-8")
        (self.runtime / "projects" / f"{self.project_id}.json").write_text(
            json.dumps(project_data), encoding="utf-8"
        )
        (self.runtime / "summary.json").write_text(
            json.dumps({
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "projects": [project_data],
            }),
            encoding="utf-8",
        )

        # Host-local execution-jobs.json
        self.jobs_cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            projects={
                self.project_id: JobProjectConfig(
                    repo_path=self.repo,
                    commands={
                        "long_task": JobCommandConfig(
                            argv=[
                                sys.executable,
                                "-c",
                                (
                                    "import time, sys; "
                                    "sys.stdout.write('BEGIN_LONG_JOB Bearer secret-super-token-12345\\n'); sys.stdout.flush(); "
                                    "time.sleep(0.6); "
                                    "open('sentinel.txt', 'a', encoding='utf-8').write('success_attempt\\n'); "
                                    "sys.stdout.write('FINISH_LONG_JOB\\n'); sys.stdout.flush()"
                                ),
                            ],
                            cwd=".",
                            duration_class="short",
                            heartbeat_interval_seconds=0.2,
                            max_runtime_seconds=30.0,
                        ),
                        "fail_task": JobCommandConfig(
                            argv=[
                                sys.executable,
                                "-c",
                                (
                                    "import sys; "
                                    "open('sentinel.txt', 'a', encoding='utf-8').write('fail_attempt\\n'); "
                                    "sys.stderr.write('Intentional failure\\n'); "
                                    "sys.exit(1)"
                                ),
                            ],
                            cwd=".",
                            duration_class="short",
                            heartbeat_interval_seconds=0.2,
                            max_runtime_seconds=30.0,
                        ),
                        "interrupt_task": JobCommandConfig(
                            argv=[
                                sys.executable,
                                "-c",
                                (
                                    "import time, sys; "
                                    "sys.stdout.write('INTERRUPT_START\\n'); sys.stdout.flush(); "
                                    "open('sentinel_interrupt.txt', 'a', encoding='utf-8').write('start\\n'); "
                                    "time.sleep(15); "
                                    "open('sentinel_interrupt.txt', 'a', encoding='utf-8').write('end\\n'); "
                                ),
                            ],
                            cwd=".",
                            duration_class="long",
                            heartbeat_interval_seconds=0.2,
                            max_runtime_seconds=60.0,
                        ),
                    },
                )
            },
        )

        write_json(self.runtime / "execution-jobs.json", {
            "runtime_root": str(self.runtime),
            "enabled": True,
            "projects": {
                self.project_id: {
                    "repo_path": str(self.repo),
                    "commands": {
                        "long_task": {
                            "argv": self.jobs_cfg.projects[self.project_id].commands["long_task"].argv,
                            "cwd": ".",
                            "duration_class": "short",
                        },
                        "fail_task": {
                            "argv": self.jobs_cfg.projects[self.project_id].commands["fail_task"].argv,
                            "cwd": ".",
                            "duration_class": "short",
                        },
                        "interrupt_task": {
                            "argv": self.jobs_cfg.projects[self.project_id].commands["interrupt_task"].argv,
                            "cwd": ".",
                            "duration_class": "long",
                        },
                    },
                }
            },
        })

        # Start control web server
        self.server = make_server(
            "127.0.0.1",
            0,
            self.runtime,
            self.repo,
            enable_control=True,
            config_path=self.config_path,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

        self.security = ControlSecurity(self.runtime)
        self.master_token = self.security.token()

        self.service = JobService(
            self.runtime,
            transports={"local": LocalJobTransport()},
            config=self.jobs_cfg,
        )

    def tearDown(self):
        if hasattr(self, "service") and self.service:
            try:
                for job in self.service.store.list():
                    rec = self.service.store.get(job["job_id"])
                    if rec and rec.supervisor.get("pid"):
                        terminate_process_tree(rec.supervisor["pid"])
            except Exception:
                pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        try:
            self.temp.cleanup()
        except Exception:
            pass

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = headers or {}
        conn.request("GET", path, headers=h)
        resp = conn.getresponse()
        resp_data = resp.read()
        conn.close()
        return resp.status, resp.headers, resp_data

    def _wait_for_terminal(self, job_id: str, timeout_seconds: float = 10.0) -> JobRecord:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            rec = self.service.reconcile(job_id)
            if rec.state in ("completed", "failed", "cancelled"):
                return rec
            time.sleep(0.1)
        return self.service.reconcile(job_id)

    def test_end_to_end_detached_execution_recovery_and_retry_replay(self):
        """End-to-end acceptance:

        1. Detached long execution survives client disconnect and completes
        2. Replay of submit creates NO duplicate side effect or spawn
        3. Recovery sweep reconciles terminal result
        4. Retry of failed job with identical retry_request_id replays without duplicate side effect
        5. Different retry_request_id is rejected as conflict
        6. Control API surfaces status, list and secret-redacted logs
        """
        # --- PHASE 1: Submit long job and simulate client disconnect & daemon reboot ---
        spec_long = JobSpec(
            project_id=self.project_id,
            command_ref="long_task",
            idempotency_key="accept-long-1",
        )
        rec_long = self.service.submit(spec_long)
        expected_jid = job_id_for(spec_long)
        self.assertEqual(rec_long.job_id, expected_jid)

        # Simulate client disconnect & daemon reboot mid-execution:
        # discard in-memory service reference while supervisor process runs in the OS background.
        del self.service
        self.service = JobService(
            self.runtime,
            transports={"local": LocalJobTransport()},
            config=self.jobs_cfg,
        )

        terminal_rec = self._wait_for_terminal(rec_long.job_id)
        self.assertEqual(terminal_rec.state, "completed")
        self.assertEqual(terminal_rec.exit_code, 0)

        # Assert sentinel file was written exactly ONCE
        self.assertTrue(self.sentinel.is_file())
        lines = [line.strip() for line in self.sentinel.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(lines, ["success_attempt"])

        # Replay submit of the exact same spec across a simulated lost response
        rec_replay = self.service.submit(spec_long)
        self.assertEqual(rec_replay.job_id, expected_jid)
        # Sentinel file STILL has exactly one entry (no duplicate supervisor execution!)
        lines_replay = [line.strip() for line in self.sentinel.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(lines_replay, ["success_attempt"])

        # --- PHASE 2: Check logs and secret redaction ---
        logs_page = self.service.logs(rec_long.job_id)
        self.assertTrue(len(logs_page.get("lines", [])) >= 2)
        raw_logs = json.dumps(logs_page)
        self.assertIn("BEGIN_LONG_JOB", raw_logs)
        self.assertIn("FINISH_LONG_JOB", raw_logs)
        # Token in logs must be redacted
        self.assertNotIn("secret-super-token-12345", raw_logs)
        self.assertIn("[REDACTED]", raw_logs)

        # --- PHASE 3: Failed job, retry replay, and one-successor-per-predecessor ---
        spec_fail = JobSpec(
            project_id=self.project_id,
            command_ref="fail_task",
            idempotency_key="accept-fail-1",
        )
        rec_fail = self.service.submit(spec_fail)
        rec_fail_term = self._wait_for_terminal(rec_fail.job_id)
        self.assertEqual(rec_fail_term.state, "failed")
        self.assertEqual(rec_fail_term.exit_code, 1)

        # Sentinel file now has: ["success_attempt", "fail_attempt"]
        lines_fail = [line.strip() for line in self.sentinel.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(lines_fail, ["success_attempt", "fail_attempt"])

        # Claim and execute retry attempt 1
        retry_req_id = "accept-retry-attempt-001"
        succ_rec1 = self.service.retry(rec_fail.job_id, retry_req_id)
        expected_succ_id = retry_successor_id(rec_fail.job_id, retry_req_id)
        self.assertEqual(succ_rec1.job_id, expected_succ_id)
        self.assertEqual(succ_rec1.retry.get("attempt"), 2)

        # Wait for successor to complete
        succ_term = self._wait_for_terminal(succ_rec1.job_id)
        self.assertEqual(succ_term.state, "failed")

        # Sentinel now has 2 fail_attempts (1 initial + 1 successor)
        lines_after_retry = [line.strip() for line in self.sentinel.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(lines_after_retry, ["success_attempt", "fail_attempt", "fail_attempt"])

        # Simulate lost retry response: client replays exact same retry_request_id!
        succ_rec2 = self.service.retry(rec_fail.job_id, retry_req_id)
        self.assertEqual(succ_rec1.job_id, succ_rec2.job_id)

        # Crucial: NO second successor spawned, sentinel file count did NOT increase!
        lines_after_replay = [line.strip() for line in self.sentinel.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(lines_after_replay, ["success_attempt", "fail_attempt", "fail_attempt"])

        # Attempting a different retry_request_id against the same predecessor raises JobConflictError
        with self.assertRaises(JobConflictError):
            self.service.retry(rec_fail.job_id, "different-conflicting-retry-request")

        # --- PHASE 4: Daemon restart recovery coordinator sweep ---
        mock_accounting = []

        class DummyAccounting:
            def start_interval(self, **kwargs):
                mock_accounting.append(("start", kwargs))

            def end_interval(self, **kwargs):
                mock_accounting.append(("end", kwargs))

        coordinator = JobRecoveryCoordinator(
            self.runtime,
            service=self.service,
            config=self.jobs_cfg,
            accounting=DummyAccounting(),
        )
        recovery_summary = coordinator.recover()
        self.assertIn("reconciled", recovery_summary)

        # Accounting emitted for completed jobs
        self.assertTrue(len(mock_accounting) >= 1)
        first_acct = mock_accounting[0][1]
        self.assertIn("interval_id", first_acct)
        self.assertIn("project_id", first_acct)

        # --- PHASE 5: Control API verification ---
        # 1. GET /api/v1/control/jobs
        status_code, _, body = self.get(
            f"/api/v1/control/jobs?project_id={self.project_id}",
            headers={"Authorization": f"Bearer {self.master_token}"},
        )
        self.assertEqual(status_code, 200)
        jobs_env = json.loads(body.decode("utf-8"))
        self.assertEqual(jobs_env["schema_version"], 1)
        self.assertTrue(len(jobs_env["data"]) >= 2)

        # 2. GET /api/v1/control/jobs/{job_id}
        status_code2, _, body2 = self.get(
            f"/api/v1/control/jobs/{rec_long.job_id}",
            headers={"Authorization": f"Bearer {self.master_token}"},
        )
        self.assertEqual(status_code2, 200)
        job_env2 = json.loads(body2.decode("utf-8"))
        self.assertEqual(job_env2["data"]["job_id"], rec_long.job_id)
        self.assertEqual(job_env2["data"]["state"], "completed")
        # Assert secret redaction on job record: start_token must be redacted
        self.assertEqual(job_env2["data"]["supervisor"].get("start_token"), "[REDACTED]")
        self.assertNotIn("secret-super-token-12345", body2.decode("utf-8"))

        # 3. GET /api/v1/control/jobs/{job_id}/logs
        status_code3, _, body3 = self.get(
            f"/api/v1/control/jobs/{rec_long.job_id}/logs",
            headers={"Authorization": f"Bearer {self.master_token}"},
        )
        self.assertEqual(status_code3, 200)
        logs_env3 = json.loads(body3.decode("utf-8"))
        self.assertIn("lines", logs_env3["data"])
        logs_text = body3.decode("utf-8")
        self.assertNotIn("secret-super-token-12345", logs_text)
        self.assertIn("[REDACTED]", logs_text)

    def test_real_detached_supervisor_interrupted_mid_run_and_recovered(self):
        """Simulate real supervisor process interruption mid-run.

        Spawns a long-running supervisor process, verifies it is actively executing,
        then terminates the supervisor process tree mid-run.
        Reconciliation must detect supervisor disappearance without result.json,
        transition the job to unknown_recovery, set recovery_safe_retry=False,
        preserve accumulated logs, and refuse automatic retry.
        """
        spec_interrupt = JobSpec(
            project_id=self.project_id,
            command_ref="interrupt_task",
            idempotency_key="accept-interrupt-1",
        )
        rec = self.service.submit(spec_interrupt)
        self.assertIn(rec.state, ("queued", "running"))

        # Wait until supervisor is actively running and sentinel_interrupt.txt has started
        sentinel_interrupt = self.repo / "sentinel_interrupt.txt"
        deadline = time.monotonic() + 5.0
        while not sentinel_interrupt.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(sentinel_interrupt.is_file())

        rec = self.service.reconcile(rec.job_id)
        self.assertEqual(rec.state, "running")
        pid = rec.supervisor.get("pid")
        self.assertIsNotNone(pid)
        self.assertTrue(is_pid_alive(pid))

        # Abruptly terminate the live supervisor process tree (simulating kill/crash)
        terminate_process_tree(pid)
        self.assertFalse(is_pid_alive(pid))

        # Daemon restart / recovery sweep
        del self.service
        self.service = JobService(
            self.runtime,
            transports={"local": LocalJobTransport()},
            config=self.jobs_cfg,
        )
        reconciled = self.service.reconcile(rec.job_id)
        self.assertEqual(reconciled.state, "unknown_recovery")
        self.assertEqual(reconciled.failure_kind, "supervisor_died_without_result")
        self.assertFalse(reconciled.recovery.get("recovery_safe_retry"))

        # Logs accumulated before termination must be preserved
        logs_page = self.service.logs(rec.job_id)
        raw_logs = json.dumps(logs_page)
        self.assertIn("INTERRUPT_START", raw_logs)

        # Automatic retry must be rejected because state is unknown_recovery
        with self.assertRaises(ValueError):
            self.service.retry(rec.job_id, "retry-interrupted-attempt")

        # Sentinel file has 'start' but never 'end'
        content = sentinel_interrupt.read_text(encoding="utf-8").strip()
        self.assertIn("start", content)
        self.assertNotIn("end", content)

    def test_control_jobs_api_corruption_handling_and_read_only_store(self):
        """Verify GET /api/v1/control/jobs error handling and read-only behavior."""
        # 1. On an isolated runtime without a jobs directory, GET must not create the directory
        empty_runtime = Path(self.temp.name) / "empty_runtime"
        empty_runtime.mkdir(parents=True)
        store = ExecutionJobStore(empty_runtime, read_only=True)
        self.assertEqual(store.list(), [])
        self.assertFalse((empty_runtime / "jobs").exists())

        # 2. Corrupt job.json handling on GET /api/v1/control/jobs/{job_id}
        corrupt_job_dir = self.runtime / "jobs" / "corrupt-job-001"
        corrupt_job_dir.mkdir(parents=True, exist_ok=True)
        (corrupt_job_dir / "job.json").write_text("{corrupt json", encoding="utf-8")

        status_code2, _, body2 = self.get(
            "/api/v1/control/jobs/corrupt-job-001",
            headers={"Authorization": f"Bearer {self.master_token}"},
        )
        self.assertEqual(status_code2, 500)
        resp2 = json.loads(body2.decode("utf-8"))
        self.assertIn("error", resp2)

        # 3. Corrupt index.json handling on GET /api/v1/control/jobs
        (self.runtime / "jobs" / "index.json").write_text("{corrupt index json", encoding="utf-8")
        status_code, _, body = self.get(
            f"/api/v1/control/jobs?project_id={self.project_id}",
            headers={"Authorization": f"Bearer {self.master_token}"},
        )
        self.assertEqual(status_code, 500)
        resp = json.loads(body.decode("utf-8"))
        self.assertIn("error", resp)

    def test_ssh_job_submit_configuration_wiring(self):
        """Verify SSHJobTransport auto-wiring from JobsConfig ssh settings."""
        cfg = JobsConfig(
            runtime_root=self.runtime,
            enabled=True,
            ssh={"host": "remote-host.example.com", "user": "dev", "port": 2222},
        )
        service = JobService(
            self.runtime,
            transports={"local": LocalJobTransport()},
            config=cfg,
        )
        self.assertIn("ssh", service.transports)
        self.assertIsInstance(service.transports["ssh"], SSHJobTransport)

    def test_ssh_job_submit_reconcile_and_liveness_recovery(self):
        """Submit a transport='ssh' job through JobService and verify reconcile/liveness semantics."""
        mock_ssh = MagicMock()
        service = JobService(
            self.runtime,
            transports={"local": LocalJobTransport(), "ssh": mock_ssh},
            config=self.jobs_cfg,
        )

        # 1. Submit SSH job
        spec1 = JobSpec(
            project_id=self.project_id,
            command_ref="long_task",
            idempotency_key="ssh-job-reconcile-1",
            transport="ssh",
        )
        mock_ssh.job_start.return_value = {
            "job_id": job_id_for(spec1),
            "status": "started",
            "supervisor_pid": 88888,
            "host_identity": "remote-worker-node",
        }
        rec1 = service.submit(spec1)
        self.assertEqual(rec1.transport, "ssh")
        self.assertEqual(rec1.state, "queued")
        self.assertEqual(rec1.supervisor["pid"], 88888)

        # 2. First reconcile while remote job is actively running
        now_iso = datetime.now(timezone.utc).isoformat()
        mock_ssh.job_status.return_value = {
            "job_id": rec1.job_id,
            "host_identity": "remote-worker-node",
            "job": {
                "state": "running",
                "supervisor": {
                    "pid": 88888,
                    "start_token": "token-remote-xyz",
                    "started_at": now_iso,
                },
                "timestamps": {"started_at": now_iso},
            },
            "heartbeat": {
                "sequence": 1,
                "heartbeat_sequence": 1,
                "start_token": "token-remote-xyz",
                "pid": 88888,
                "child_pid": 88889,
                "reported_at": now_iso,
            },
            "result": None,
            "supervisor_alive": True,
        }

        reconciled1 = service.reconcile(rec1.job_id)
        # Must sync remote state to running without checking local is_pid_alive against remote PID 88888
        self.assertEqual(reconciled1.state, "running")
        self.assertEqual(reconciled1.supervisor["start_token"], "token-remote-xyz")
        self.assertEqual(reconciled1.heartbeat["heartbeat_sequence"], 1)
        self.assertIsNotNone(reconciled1.heartbeat.get("observed_at"))
        self.assertFalse(reconciled1.recovery.get("recovery_safe_retry", False))

        # 3. Advancing heartbeat sequence on remote is consumed
        mock_ssh.job_status.return_value["heartbeat"]["sequence"] = 2
        mock_ssh.job_status.return_value["heartbeat"]["heartbeat_sequence"] = 2
        reconciled2 = service.reconcile(rec1.job_id)
        self.assertEqual(reconciled2.state, "running")
        self.assertEqual(reconciled2.heartbeat["heartbeat_sequence"], 2)

        # 4. Stalled heartbeat on remote triggers unknown_recovery without safe retry
        def _backdate(r: JobRecord) -> None:
            r.heartbeat["observed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()
        service.store.update(rec1.job_id, _backdate)

        reconciled_stalled = service.reconcile(rec1.job_id)
        self.assertEqual(reconciled_stalled.state, "unknown_recovery")
        self.assertEqual(reconciled_stalled.failure_kind, "heartbeat_stalled")
        self.assertFalse(reconciled_stalled.recovery["recovery_safe_retry"])
        # Automatic retry must be refused!
        with self.assertRaises(ValueError):
            service.retry(rec1.job_id, "retry-on-stalled-should-fail")

        # 5. Remote supervisor mid-run death without terminal result -> unknown_recovery
        spec2 = JobSpec(
            project_id=self.project_id,
            command_ref="long_task",
            idempotency_key="ssh-job-reconcile-2",
            transport="ssh",
        )
        mock_ssh.job_start.return_value = {
            "job_id": job_id_for(spec2),
            "status": "started",
            "supervisor_pid": 77777,
            "host_identity": "remote-worker-node",
        }
        rec2 = service.submit(spec2)

        # Remote supervisor died mid-run: supervisor_alive=False, result=None, but start evidence exists
        mock_ssh.job_status.return_value = {
            "job_id": rec2.job_id,
            "host_identity": "remote-worker-node",
            "job": {
                "state": "running",
                "supervisor": {"pid": 77777, "start_token": "token-mid-run", "started_at": now_iso},
                "timestamps": {"started_at": now_iso},
            },
            "heartbeat": {"sequence": 1, "heartbeat_sequence": 1, "start_token": "token-mid-run", "pid": 77777},
            "result": None,
            "supervisor_alive": False,
        }
        reconciled_died = service.reconcile(rec2.job_id)
        self.assertEqual(reconciled_died.state, "unknown_recovery")
        self.assertEqual(reconciled_died.failure_kind, "supervisor_died_without_result")
        self.assertFalse(reconciled_died.recovery["recovery_safe_retry"])
        with self.assertRaises(ValueError):
            service.retry(rec2.job_id, "retry-on-midrun-death-refused")

        # 6. Positive evidence required for never_started
        # 6a. Positive confirmation from remote that supervisor never started
        spec3 = JobSpec(
            project_id=self.project_id,
            command_ref="long_task",
            idempotency_key="ssh-job-reconcile-3",
            transport="ssh",
        )
        mock_ssh.job_start.return_value = {
            "job_id": job_id_for(spec3),
            "status": "started",
            "supervisor_pid": None,
            "host_identity": "remote-worker-node",
        }
        rec3 = service.submit(spec3)
        mock_ssh.job_status.return_value = {
            "job_id": rec3.job_id,
            "host_identity": "remote-worker-node",
            "job": None,
            "heartbeat": None,
            "result": None,
            "supervisor_alive": False,
        }
        reconciled_never = service.reconcile(rec3.job_id)
        self.assertEqual(reconciled_never.state, "failed")
        self.assertEqual(reconciled_never.failure_kind, "never_started")
        self.assertTrue(reconciled_never.recovery["recovery_safe_retry"])

        # 6b. Remote unreachable (poll exception): must transition to unknown_recovery without safe retry
        spec4 = JobSpec(
            project_id=self.project_id,
            command_ref="long_task",
            idempotency_key="ssh-job-reconcile-4",
            transport="ssh",
        )
        mock_ssh.job_start.return_value = {
            "job_id": job_id_for(spec4),
            "status": "started",
            "supervisor_pid": None,
            "host_identity": "remote-worker-node",
        }
        rec4 = service.submit(spec4)
        mock_ssh.job_status.side_effect = ExecutionTransportError("SSH connection timeout")
        reconciled_unreachable = service.reconcile(rec4.job_id)
        self.assertEqual(reconciled_unreachable.state, "unknown_recovery")
        self.assertEqual(reconciled_unreachable.failure_kind, "transport_unreachable")
        self.assertFalse(reconciled_unreachable.recovery["recovery_safe_retry"])

        # Retry must be refused after transport_unreachable
        with self.assertRaises(ValueError):
            service.retry(rec4.job_id, "retry-on-unreachable-refused")

        # Later successful poll reconciles the job on reconnect
        mock_ssh.job_status.side_effect = None
        mock_ssh.job_status.return_value = {
            "job_id": rec4.job_id,
            "host_identity": "remote-worker-node",
            "job": {
                "state": "running",
                "supervisor": {"pid": 88888, "start_token": "tok-rec4", "started_at": now_iso},
                "timestamps": {"started_at": now_iso},
            },
            "heartbeat": {"sequence": 1, "heartbeat_sequence": 1, "start_token": "tok-rec4", "pid": 88888},
            "result": None,
            "supervisor_alive": True,
        }
        reconciled_reconnected = service.reconcile(rec4.job_id)
        self.assertEqual(reconciled_reconnected.state, "running")
        self.assertEqual(reconciled_reconnected.supervisor["pid"], 88888)

        # And if it finishes on remote, reconcile promotes to completed
        mock_ssh.job_status.return_value["result"] = {
            "job_id": rec4.job_id,
            "exit_code": 0,
            "outcome": "success",
            "finished_at": now_iso,
        }
        reconciled_done = service.reconcile(rec4.job_id)
        self.assertEqual(reconciled_done.state, "completed")
        self.assertEqual(reconciled_done.exit_code, 0)

        # 6c. Remote unreachable with supervisor_pid recorded at submit: must transition to unknown_recovery
        spec4b = JobSpec(
            project_id=self.project_id,
            command_ref="long_task",
            idempotency_key="ssh-job-reconcile-4b",
            transport="ssh",
        )
        mock_ssh.job_start.return_value = {
            "job_id": job_id_for(spec4b),
            "status": "started",
            "supervisor_pid": 99999,
            "host_identity": "remote-worker-node",
        }
        rec4b = service.submit(spec4b)
        self.assertEqual(rec4b.state, "queued")
        self.assertEqual(rec4b.supervisor["pid"], 99999)
        mock_ssh.job_status.side_effect = ExecutionTransportError("SSH connection dropped")
        reconciled_unreachable_pid = service.reconcile(rec4b.job_id)
        self.assertEqual(reconciled_unreachable_pid.state, "unknown_recovery")
        self.assertEqual(reconciled_unreachable_pid.failure_kind, "transport_unreachable")
        self.assertFalse(reconciled_unreachable_pid.recovery["recovery_safe_retry"])

        # Retry must be refused
        with self.assertRaises(ValueError):
            service.retry(rec4b.job_id, "retry-on-pid-unreachable-refused")

        # Later successful poll reconciles
        mock_ssh.job_status.side_effect = None
        mock_ssh.job_status.return_value = {
            "job_id": rec4b.job_id,
            "host_identity": "remote-worker-node",
            "job": {
                "state": "running",
                "supervisor": {"pid": 99999, "start_token": "tok-rec4b", "started_at": now_iso},
                "timestamps": {"started_at": now_iso},
            },
            "heartbeat": {"sequence": 1, "heartbeat_sequence": 1, "start_token": "tok-rec4b", "pid": 99999},
            "result": None,
            "supervisor_alive": True,
        }
        reconciled_reconnected_pid = service.reconcile(rec4b.job_id)
        self.assertEqual(reconciled_reconnected_pid.state, "running")

        # 7. Remote completion
        spec5 = JobSpec(
            project_id=self.project_id,
            command_ref="long_task",
            idempotency_key="ssh-job-reconcile-5",
            transport="ssh",
        )
        mock_ssh.job_start.return_value = {
            "job_id": job_id_for(spec5),
            "status": "started",
            "supervisor_pid": 66666,
            "host_identity": "remote-worker-node",
        }
        rec5 = service.submit(spec5)
        mock_ssh.job_status.return_value = {
            "job_id": rec5.job_id,
            "host_identity": "remote-worker-node",
            "job": {"state": "completed"},
            "heartbeat": {"sequence": 3, "heartbeat_sequence": 3},
            "result": {"job_id": rec5.job_id, "exit_code": 0, "outcome": "success", "finished_at": now_iso},
            "supervisor_alive": False,
        }
        reconciled_comp = service.reconcile(rec5.job_id)
        self.assertEqual(reconciled_comp.state, "completed")
        self.assertEqual(reconciled_comp.exit_code, 0)

    def test_supervisor_and_submit_concurrent_lock_synchronization(self):
        """Verify supervisor.py and submit() update job.json under InterProcessFileLock without lost updates."""
        service = JobService(self.runtime, transports={"local": LocalJobTransport()}, config=self.jobs_cfg)
        spec = JobSpec(
            project_id=self.project_id,
            command_ref="long_task",
            idempotency_key="lock-sync-test-1",
        )
        record = service.submit(spec)

        # Simulate supervisor updating to running with start_token
        def _sup_start(r: JobRecord) -> None:
            r.transition_to("running", timestamp=datetime.now(timezone.utc).isoformat())
            r.supervisor["pid"] = 55555
            r.supervisor["start_token"] = "sup-token-12345"
            r.supervisor["started_at"] = datetime.now(timezone.utc).isoformat()
            r.timestamps["started_at"] = r.supervisor["started_at"]
        service.store.update(record.job_id, _sup_start)

        # Simulate submit() _record_pid running under lock
        def _record_pid(r: JobRecord) -> None:
            r.supervisor["pid"] = 55555
        updated = service.store.update(record.job_id, _record_pid)

        # Verify state is NOT reset to queued and start_token is preserved
        self.assertEqual(updated.state, "running")
        self.assertEqual(updated.supervisor["start_token"], "sup-token-12345")
        self.assertEqual(updated.supervisor["pid"], 55555)

    def test_remote_helper_job_start_idempotency_no_respawn(self):
        """Verify remote_helper job_start does not re-spawn supervisor for already-claimed jobs."""
        from unittest.mock import patch
        cfg_file = self.runtime / "execution-jobs.json"
        with patch.object(os.environ, "get", side_effect=lambda k, d=None: str(cfg_file) if k == "DEVORCH_JOBS_CONFIG" else d):
            req = {
                "operation": "job_start",
                "request_id": "req-idemp-1",
                "project_id": self.project_id,
                "command_ref": "long_task",
                "idempotency_key": "idem-remote-no-respawn",
            }
            resp1 = handle_request(req)
            self.assertEqual(resp1.get("status"), "success")
            payload1 = resp1.get("payload", {})
            self.assertEqual(payload1.get("status"), "started")
            jid = payload1.get("job_id")

            # Second call with same idempotency key
            resp2 = handle_request(req)
            self.assertEqual(resp2.get("status"), "success")
            payload2 = resp2.get("payload", {})
            self.assertEqual(payload2.get("job_id"), jid)
            self.assertTrue(payload2.get("already_exists"))


if __name__ == "__main__":
    unittest.main()
