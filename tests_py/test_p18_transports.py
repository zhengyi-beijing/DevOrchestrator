"""Tests for P18 native execution transports: Local, SSH, and RDC fallback."""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from dev_orchestrator.jobs.config import (
    JobCommandConfig,
    JobProjectConfig,
    JobsConfig,
)
from dev_orchestrator.transport.contracts import (
    FileReadRequest,
    FileWriteRequest,
    MachineOperation,
    TransportAmbiguousError,
    TransportRejectedError,
    TransportUnavailableError,
    WriteContentUpload,
)
from dev_orchestrator.transport.hosts import TransportHostProfile
from dev_orchestrator.transport.local import LocalMachineTransport
from dev_orchestrator.transport.rdc import RDCTransport
from dev_orchestrator.transport.ssh import SSHMachineTransport


class TestP18Transports(unittest.TestCase):
    """Verifies LocalMachineTransport, SSHMachineTransport, and RDCTransport behavior."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.repo_dir = self.root / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src" / "sample.txt").write_bytes(b"sample content\n")

        # Configure local jobs
        self.read_only_cmd = JobCommandConfig(
            argv=["python", "-c", "import sys; sys.stdout.write('read_only_success')"],
            cwd=".",
            effect_class="read_only",
        )
        self.effectful_cmd = JobCommandConfig(
            argv=["python", "-c", "import sys; sys.stdout.write('effectful_success')"],
            cwd=".",
            effect_class="effectful",
        )
        self.hardware_cmd = JobCommandConfig(
            argv=["python", "-c", "print('hardware')"],
            cwd=".",
            effect_class="hardware",
        )

        self.proj = JobProjectConfig(
            repo_path=self.repo_dir,
            commands={
                "ro": self.read_only_cmd,
                "mut": self.effectful_cmd,
                "hw": self.hardware_cmd,
            },
        )
        self.cfg = JobsConfig(runtime_root=self.root, projects={"p1": self.proj})
        self.local_transport = LocalMachineTransport(jobs_config=self.cfg)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_local_exec_read_only_success(self):
        """LocalMachineTransport.exec runs read_only command synchronously."""
        op = MachineOperation(
            project_id="p1",
            command_ref="ro",
            idempotency_key="exec-1",
        )
        res = self.local_transport.exec(op)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.stdout, "read_only_success")
        self.assertTrue(res.execution_policy_digest.startswith("sha256:"))
        self.assertTrue(res.resolution_digest.startswith("sha256:"))

    def test_local_exec_rejects_effectful_and_hardware_commands(self):
        """LocalMachineTransport.exec rejects effectful and hardware commands."""
        op_mut = MachineOperation(
            project_id="p1",
            command_ref="mut",
            idempotency_key="exec-2",
        )
        with self.assertRaises(TransportRejectedError) as ctx:
            self.local_transport.exec(op_mut)
        self.assertIn("only supports read_only", str(ctx.exception))

        op_hw = MachineOperation(
            project_id="p1",
            command_ref="hw",
            idempotency_key="exec-3",
        )
        with self.assertRaises(TransportRejectedError) as ctx:
            self.local_transport.exec(op_hw)
        self.assertIn("hardware_execution_not_supported_in_p18", str(ctx.exception))

    def test_local_spawn_poll_cancel_lifecycle(self):
        """LocalMachineTransport.spawn durable job, poll, and cancel."""
        op = MachineOperation(
            project_id="p1",
            command_ref="mut",
            idempotency_key="spawn-1",
        )
        spawn_res = self.local_transport.spawn(op)
        self.assertIn(spawn_res.status, ("ok", "queued", "running"))
        self.assertIsNotNone(spawn_res.job_id)

        poll_res = self.local_transport.poll(spawn_res.job_id)
        self.assertEqual(poll_res.job_id, spawn_res.job_id)

        cancel_res = self.local_transport.cancel(spawn_res.job_id)
        self.assertEqual(cancel_res.job_id, spawn_res.job_id)

    def test_local_file_read_and_stat(self):
        """LocalMachineTransport.read_file reads contained file; stat returns filesystem metadata."""
        read_req = FileReadRequest(
            project_id="p1",
            path="src/sample.txt",
        )
        read_res = self.local_transport.read_file(read_req)
        self.assertEqual(read_res.status, "ok")
        self.assertEqual(read_res.content_bytes, b"sample content\n")

        # Escaping path is rejected
        read_bad = FileReadRequest(
            project_id="p1",
            path="../../escaped.txt",
        )
        with self.assertRaises(TransportRejectedError):
            self.local_transport.read_file(read_bad)

        # Stat existing file
        st_file = self.local_transport.stat("src/sample.txt", project_id="p1")
        self.assertEqual(st_file.status, "ok")
        self.assertTrue(st_file.exists)
        self.assertTrue(st_file.is_file)

        # Stat absent file
        st_absent = self.local_transport.stat("src/absent.txt", project_id="p1")
        self.assertEqual(st_absent.status, "ok")
        self.assertFalse(st_absent.exists)

    def test_local_capabilities(self):
        """LocalMachineTransport.capabilities returns discovered host profile."""
        caps = self.local_transport.capabilities()
        self.assertEqual(caps.host_id, "local")
        self.assertTrue(caps.jobs_config_valid)
        self.assertIn("exec", caps.supported_operations)
        self.assertIn("spawn", caps.supported_operations)
        self.assertIn("write_file", caps.supported_operations)

    def test_ssh_machine_transport_exec_mock(self):
        """SSHMachineTransport delegates to remote helper envelope over OpenSSH."""
        profile = TransportHostProfile(
            host_id="remote-node",
            candidate_order=["ssh"],
            ssh={"peer": "192.168.1.100", "user": "agent", "port": 2222},
        )
        mock_subproc = MagicMock()

        # Mock successful exec response envelope
        def mock_run(argv, input, capture_output, timeout, check):
            req = json.loads(input.decode("utf-8"))
            resp = {
                "request_id": req.get("request_id"),
                "status": "ok",
                "host_identity": "192.168.1.100",
                "payload": {
                    "status": "ok",
                    "exit_code": 0,
                    "stdout": "remote_exec_success\n",
                    "stderr": "",
                    "parameters_digest": "sha256:111",
                    "execution_policy_digest": "sha256:222",
                    "resolution_digest": "sha256:333",
                },
            }
            res = MagicMock()
            res.returncode = 0
            res.stdout = json.dumps(resp).encode("utf-8")
            res.stderr = b""
            return res

        mock_subproc.run = mock_run
        ssh_transport = SSHMachineTransport(profile, subprocess_module=mock_subproc)

        op = MachineOperation(
            project_id="p1",
            command_ref="ro",
            idempotency_key="ssh-exec-1",
        )
        res = ssh_transport.exec(op)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.stdout, "remote_exec_success\n")
        self.assertEqual(res.parameters_digest, "sha256:111")

    def test_ssh_machine_transport_exit_255_unavailable(self):
        """SSH returncode 255 raises TransportUnavailableError."""
        profile = TransportHostProfile(
            host_id="remote-node",
            candidate_order=["ssh"],
            ssh={"peer": "192.168.1.100", "user": "agent"},
        )
        mock_subproc = MagicMock()
        mock_res = MagicMock()
        mock_res.returncode = 255
        mock_res.stdout = b""
        mock_res.stderr = b"ssh: connect to host 192.168.1.100 port 22: Connection refused"
        mock_subproc.run.return_value = mock_res

        ssh_transport = SSHMachineTransport(profile, subprocess_module=mock_subproc)
        op = MachineOperation(project_id="p1", command_ref="ro", idempotency_key="ssh-fail-1")
        with self.assertRaises(TransportUnavailableError) as ctx:
            ssh_transport.exec(op)
        self.assertIn("Connection refused", str(ctx.exception))

    def test_ssh_machine_transport_timeout_ambiguous(self):
        """SSH TimeoutExpired raises TransportAmbiguousError."""
        profile = TransportHostProfile(
            host_id="remote-node",
            candidate_order=["ssh"],
            ssh={"peer": "192.168.1.100"},
        )
        mock_subproc = MagicMock()
        mock_subproc.run.side_effect = subprocess.TimeoutExpired(cmd=["ssh"], timeout=30.0)

        ssh_transport = SSHMachineTransport(profile, subprocess_module=mock_subproc)
        op = MachineOperation(project_id="p1", command_ref="ro", idempotency_key="ssh-timeout-1")
        with self.assertRaises(TransportAmbiguousError) as ctx:
            ssh_transport.exec(op)
        self.assertIn("post-dispatch ambiguous", str(ctx.exception))

    def test_rdc_transport_fallback_descriptor(self):
        """RDCTransport returns status='rdc_fallback_required' or raises TransportRejectedError."""
        rdc = RDCTransport(host_id="rdc-fallback")
        op = MachineOperation(project_id="p1", command_ref="ro", idempotency_key="rdc-1")
        res_exec = rdc.exec(op)
        self.assertEqual(res_exec.status, "rdc_fallback_required")
        self.assertIn("requires interactive escalation", str(res_exec.error))

        res_spawn = rdc.spawn(op)
        self.assertEqual(res_spawn.status, "rdc_fallback_required")

        res_poll = rdc.poll("op-123")
        self.assertEqual(res_poll.status, "rdc_fallback_required")

        res_cancel = rdc.cancel("op-123")
        self.assertEqual(res_cancel.status, "rdc_fallback_required")

        res_read = rdc.read_file(FileReadRequest("p1", "file.txt"))
        self.assertEqual(res_read.status, "rdc_fallback_required")

        with self.assertRaises(TransportRejectedError):
            rdc.stage_write_content(WriteContentUpload("p1", "rdc", "aGVsbG8=", 5, "sha256:123"))


if __name__ == "__main__":
    unittest.main()
