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
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
from dev_orchestrator.jobs.transport import LocalJobTransport
from dev_orchestrator.storage.json_store import write_json
from dev_orchestrator.web.server import make_server


class P14SoftwareAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
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
                                    "time.sleep(0.3); "
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
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

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
        # --- PHASE 1: Submit long job and simulate client disconnect ---
        spec_long = JobSpec(
            project_id=self.project_id,
            command_ref="long_task",
            idempotency_key="accept-long-1",
        )
        rec_long = self.service.submit(spec_long)
        expected_jid = job_id_for(spec_long)
        self.assertEqual(rec_long.job_id, expected_jid)

        # Client immediately disconnects / does not hold socket open.
        # Background detached supervisor runs independently.
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


if __name__ == "__main__":
    unittest.main()
