"""Tests for P14 job transport and remote trust boundaries.

Covers:
- Local detached supervisor launch
- SSH job operations through fake subprocess module
- Only closed correlation fields cross the wire
- Rejection of request-supplied runtime roots, commands, or working directories
- Path containment, '..' traversal rejection, and symlink escape rejection
- Alternate config roots never consulted
- Unknown project_id and command_ref fail closed
- Correlation mismatch and host-identity mismatch fail closed
- Simulated ambiguous SSH transport interruption
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dev_orchestrator.ai.execution_transport import (
    ExecutionTransportError,
    SSHTransportConfig,
)
from dev_orchestrator.ai.remote_helper import handle_request
from dev_orchestrator.jobs.config import (
    JobCommandConfig,
    JobProjectConfig,
    JobsConfig,
    canonical_path,
    is_path_contained,
    resolve_remote_jobs_config_path,
    validate_and_resolve_execution,
)
from dev_orchestrator.jobs.models import JobSpec, job_id_for
from dev_orchestrator.jobs.transport import LocalJobTransport, SSHJobTransport


class P14JobTransportTests(unittest.TestCase):
    def test_local_detached_launch(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Path(td)
            job_dir = rt / "jobs" / "job-test-1"
            job_dir.mkdir(parents=True)

            spec = JobSpec(
                project_id="p1",
                command_ref="build",
                idempotency_key="key-local-1",
            )
            transport = LocalJobTransport()

            with patch("dev_orchestrator.jobs.transport.spawn_detached") as mock_spawn:
                mock_proc = MagicMock()
                mock_proc.pid = 4321
                mock_spawn.return_value = mock_proc

                res = transport.job_start(spec, job_dir)
                self.assertEqual(res["status"], "started")
                self.assertEqual(res["job_id"], job_id_for(spec))
                self.assertEqual(res["supervisor_pid"], 4321)

                # Verify spawn_detached argv invokes supervisor entrypoint
                mock_spawn.assert_called_once()
                argv = mock_spawn.call_args[0][0]
                self.assertIn("dev_orchestrator.jobs.supervisor", argv)
                self.assertIn("--job-dir", argv)
                self.assertIn(str(job_dir.resolve()), argv)

    def test_ssh_job_operations_closed_envelope(self):
        ssh_cfg = SSHTransportConfig(
            peer="10.0.0.1",
            expected_host_identity="10.0.0.1",
            user="runner",
        )
        fake_subp = MagicMock()
        transport = SSHJobTransport(ssh_cfg, subprocess_module=fake_subp)

        spec = JobSpec(
            project_id="p1",
            command_ref="test_run",
            idempotency_key="idem-key-ssh",
            expected_working_directory="/var/repo",
        )

        def make_completed_process(req_envelope: dict, payload: dict):
            resp = {
                "request_id": req_envelope["request_id"],
                "host_identity": "10.0.0.1",
                "status": "success",
                "payload": payload,
            }
            return subprocess.CompletedProcess(
                args=["ssh"],
                returncode=0,
                stdout=json.dumps(resp).encode("utf-8"),
                stderr=b"",
            )

        # 1. job_start
        def side_effect_start(argv, **kwargs):
            raw_input = kwargs.get("input", b"{}")
            env = json.loads(raw_input.decode("utf-8"))
            return make_completed_process(env, {"job_id": env["job_id"], "status": "started"})

        fake_subp.run.side_effect = side_effect_start
        res_start = transport.job_start(spec)
        self.assertEqual(res_start["status"], "started")

        # Verify wire envelope contained ONLY closed allowed fields
        last_input = json.loads(fake_subp.run.call_args[1]["input"].decode("utf-8"))
        allowed_wire_fields = {
            "operation",
            "request_id",
            "job_id",
            "project_id",
            "command_ref",
            "idempotency_key",
            "expected_working_directory",
        }
        self.assertTrue(set(last_input.keys()).issubset(allowed_wire_fields))
        self.assertEqual(last_input["operation"], "job_start")
        self.assertEqual(last_input["project_id"], "p1")
        self.assertEqual(last_input["command_ref"], "test_run")
        self.assertEqual(last_input["expected_working_directory"], "/var/repo")

        # 2. job_status
        def side_effect_status(argv, **kwargs):
            env = json.loads(kwargs.get("input", b"{}").decode("utf-8"))
            return make_completed_process(env, {"job_id": env["job_id"], "state": "running"})

        fake_subp.run.side_effect = side_effect_status
        res_status = transport.job_status("job-target-1")
        self.assertEqual(res_status["state"], "running")

        # 3. job_logs
        def side_effect_logs(argv, **kwargs):
            env = json.loads(kwargs.get("input", b"{}").decode("utf-8"))
            return make_completed_process(env, {"items": ["line 1", "line 2"], "next_cursor": 2})

        fake_subp.run.side_effect = side_effect_logs
        res_logs = transport.job_logs("job-target-1", cursor=0, limit=50)
        self.assertEqual(res_logs["next_cursor"], 2)

        # 4. job_cancel
        def side_effect_cancel(argv, **kwargs):
            env = json.loads(kwargs.get("input", b"{}").decode("utf-8"))
            return make_completed_process(env, {"status": "cancelled"})

        fake_subp.run.side_effect = side_effect_cancel
        res_cancel = transport.job_cancel("job-target-1", reason="user abort")
        self.assertEqual(res_cancel["status"], "cancelled")

    def test_remote_helper_rejects_unknown_fields_and_request_supplied_roots(self):
        # Attempt to pass illegal wire fields like runtime_root, arbitrary command, etc.
        req = {
            "operation": "job_start",
            "request_id": "req-illegal-1",
            "project_id": "p1",
            "command_ref": "test_cmd",
            "idempotency_key": "k1",
            "runtime_root": "/malicious/runtime",  # Illegal field!
            "arbitrary_argv": ["rm", "-rf", "/"],  # Illegal field!
        }
        resp = handle_request(req)
        self.assertEqual(resp["status"], "error")
        self.assertIn("unknown fields in remote job request", resp["error"])

    def test_path_containment_and_traversal_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            outside = base / "outside"
            outside.mkdir()

            cfg = JobsConfig(
                runtime_root=base / "runtime",
                enabled=True,
                projects={
                    "p1": JobProjectConfig(
                        repo_path=repo,
                        commands={
                            "valid_cmd": JobCommandConfig(argv=["echo", "1"], cwd="."),
                            "traversal_cmd": JobCommandConfig(argv=["echo", "2"], cwd="../outside"),
                            "absolute_cmd": JobCommandConfig(argv=["echo", "3"], cwd=str(outside)),
                        },
                    )
                },
            )

            # Valid command in repo
            ok, fail_kind, resolved, _ = validate_and_resolve_execution(cfg, "p1", "valid_cmd")
            self.assertTrue(ok)
            self.assertEqual(resolved, repo.resolve())

            # Traversal '../outside' escaping repo containment
            ok, fail_kind, _, _ = validate_and_resolve_execution(cfg, "p1", "traversal_cmd")
            self.assertFalse(ok)
            self.assertEqual(fail_kind, "cwd_escapes_repo_containment")

            # Absolute cwd override rejected
            ok, fail_kind, _, _ = validate_and_resolve_execution(cfg, "p1", "absolute_cmd")
            self.assertFalse(ok)
            self.assertEqual(fail_kind, "absolute_cwd_override_rejected")

            # Unknown project_id rejected
            ok, fail_kind, _, _ = validate_and_resolve_execution(cfg, "unknown_proj", "valid_cmd")
            self.assertFalse(ok)
            self.assertEqual(fail_kind, "unknown_project_id")

            # Unknown command_ref rejected
            ok, fail_kind, _, _ = validate_and_resolve_execution(cfg, "p1", "unknown_cmd")
            self.assertFalse(ok)
            self.assertEqual(fail_kind, "unknown_command_ref")

            # Working directory assertion mismatch rejected
            ok, fail_kind, _, _ = validate_and_resolve_execution(
                cfg, "p1", "valid_cmd", expected_working_directory=str(outside)
            )
            self.assertFalse(ok)
            self.assertEqual(fail_kind, "working_directory_assertion_mismatch")

    def test_symlink_escape_rejected_by_path_containment(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            outside = base / "outside"
            outside.mkdir()

            # Attempt symlink creation (may require privileges on Windows, test is_path_contained logic)
            symlink_dir = repo / "symlink_dir"
            try:
                symlink_dir.symlink_to(outside, target_is_directory=True)
                has_symlink = True
            except (OSError, NotImplementedError):
                has_symlink = False

            if has_symlink:
                self.assertFalse(is_path_contained(repo, symlink_dir))
            else:
                # If OS prevents unprivileged symlink, test canonical path containment directly
                self.assertFalse(is_path_contained(repo, outside))

    def test_alternate_config_roots_never_consulted(self):
        # Configuration resolution checks strictly DEVORCH_JOBS_CONFIG or ~/.devorch/execution-jobs.json
        with patch.dict(os.environ, {"DEVORCH_JOBS_CONFIG": "/trusted/path/execution-jobs.json"}, clear=False):
            resolved = resolve_remote_jobs_config_path()
            self.assertEqual(str(resolved), str(Path("/trusted/path/execution-jobs.json")))

        env_without = dict(os.environ)
        env_without.pop("DEVORCH_JOBS_CONFIG", None)
        with patch.dict(os.environ, env_without, clear=True):
            resolved = resolve_remote_jobs_config_path()
            expected = Path.home() / ".devorch" / "execution-jobs.json"
            self.assertEqual(resolved, expected)

    def test_ssh_correlation_mismatch_rejected(self):
        ssh_cfg = SSHTransportConfig(peer="10.0.0.1", expected_host_identity="10.0.0.1")
        fake_subp = MagicMock()
        transport = SSHJobTransport(ssh_cfg, subprocess_module=fake_subp)

        # Fake response has different request_id
        mismatched_resp = {
            "request_id": "different-request-id",
            "host_identity": "10.0.0.1",
            "status": "success",
            "payload": {"status": "started"},
        }
        fake_subp.run.return_value = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=json.dumps(mismatched_resp).encode("utf-8"),
            stderr=b"",
        )

        spec = JobSpec(project_id="p1", command_ref="c", idempotency_key="k")
        with self.assertRaises(ExecutionTransportError) as ctx:
            transport.job_start(spec)
        self.assertIn("correlation mismatch", str(ctx.exception).lower())

    def test_ssh_host_identity_mismatch_rejected(self):
        ssh_cfg = SSHTransportConfig(peer="10.0.0.1", expected_host_identity="10.0.0.1")
        fake_subp = MagicMock()
        transport = SSHJobTransport(ssh_cfg, subprocess_module=fake_subp)

        # Fake response has unexpected host_identity
        mismatched_resp = {
            "request_id": "job-start-test",
            "host_identity": "spoofed-host.evil.com",
            "status": "success",
            "payload": {"status": "started"},
        }

        def side_effect(argv, **kwargs):
            env = json.loads(kwargs.get("input", b"{}").decode("utf-8"))
            mismatched_resp["request_id"] = env["request_id"]
            return subprocess.CompletedProcess(
                args=["ssh"],
                returncode=0,
                stdout=json.dumps(mismatched_resp).encode("utf-8"),
                stderr=b"",
            )

        fake_subp.run.side_effect = side_effect
        spec = JobSpec(project_id="p1", command_ref="c", idempotency_key="k")
        with self.assertRaises(ExecutionTransportError) as ctx:
            transport.job_start(spec)
        self.assertIn("host identity mismatch", str(ctx.exception).lower())

    def test_simulated_ambiguous_ssh_transport_interruption(self):
        ssh_cfg = SSHTransportConfig(peer="10.0.0.1", expected_host_identity="10.0.0.1")
        fake_subp = MagicMock()
        transport = SSHJobTransport(ssh_cfg, subprocess_module=fake_subp)

        # SSH connection timeout / network drop (exit code 255)
        fake_subp.run.return_value = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=255,
            stdout=b"",
            stderr=b"ssh: connect to host 10.0.0.1: Connection timed out",
        )

        spec = JobSpec(project_id="p1", command_ref="c", idempotency_key="k")
        with self.assertRaises(ExecutionTransportError) as ctx:
            transport.job_start(spec)
        self.assertIn("SSH transport failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
