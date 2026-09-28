"""Tests for P18 native execution transports: Local, SSH, and RDC fallback."""

from __future__ import annotations

import base64
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
    JobSpec,
    job_id_for,
    retry_successor_id,
    spec_hash,
)
from dev_orchestrator.jobs.transport import SSHJobTransport
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
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

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

    def test_local_stage_write_and_cas_write_end_to_end(self):
        """LocalMachineTransport stages binary content and executes CAS write intent."""
        from dev_orchestrator.transport.contracts import canonical_sha256
        import base64

        data = b"Hello from local CAS write!\n"
        b64_data = base64.b64encode(data).decode("ascii")
        sha_data = canonical_sha256(data)

        # 1. Stage content
        upload = WriteContentUpload(
            project_id="p1",
            host_id="local",
            content_base64=b64_data,
            decoded_size_bytes=len(data),
            content_sha256=sha_data,
        )
        staged = self.local_transport.stage_write_content(upload)
        self.assertTrue(staged.content_ref.startswith("stage:p1:sha256:"))
        self.assertEqual(staged.content_sha256, sha_data)

        # 2. Write file if_absent
        target_rel = "src/cas_file.txt"
        write_req = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=target_rel,
            idempotency_key="write-cas-1",
            content_ref=staged.content_ref,
            content_sha256=sha_data,
            decoded_size_bytes=len(data),
            if_absent=True,
        )
        res = self.local_transport.write_file(write_req)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.content_sha256, sha_data)
        self.assertTrue((self.repo_dir / target_rel).is_file())
        self.assertEqual((self.repo_dir / target_rel).read_bytes(), data)

        # 3. Overwrite with expected_sha256 precondition
        new_data = b"Updated content!\n"
        new_b64 = base64.b64encode(new_data).decode("ascii")
        new_sha = canonical_sha256(new_data)
        staged_new = self.local_transport.stage_write_content(
            WriteContentUpload(
                project_id="p1",
                host_id="local",
                content_base64=new_b64,
                decoded_size_bytes=len(new_data),
                content_sha256=new_sha,
            )
        )
        write_update = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=target_rel,
            idempotency_key="write-cas-2",
            content_ref=staged_new.content_ref,
            content_sha256=new_sha,
            decoded_size_bytes=len(new_data),
            expected_sha256=sha_data,
        )
        res_update = self.local_transport.write_file(write_update)
        self.assertEqual(res_update.status, "ok")
        self.assertEqual((self.repo_dir / target_rel).read_bytes(), new_data)

        # 4. Conflicting expected_sha256 fails precondition
        write_bad = FileWriteRequest(
            project_id="p1",
            host_id="local",
            target_path=target_rel,
            idempotency_key="write-cas-3",
            content_ref=staged.content_ref,
            content_sha256=sha_data,
            decoded_size_bytes=len(data),
            expected_sha256="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        )
        res_bad = self.local_transport.write_file(write_bad)
        self.assertEqual(res_bad.status, "failed")

    def test_remote_helper_stage_write_and_write_file_operations(self):
        """remote_helper execute_request op_stage_write and op_write_file work end-to-end."""
        import os
        from dev_orchestrator.ai.remote_helper import execute_request
        from dev_orchestrator.storage.json_store import write_json

        # Write jobs config for remote_helper
        cfg_file = self.root / "projects.json"
        write_json(
            cfg_file,
            {
                "runtime_root": str(self.root),
                "projects": {
                    "p1": {
                        "repo_path": str(self.repo_dir),
                        "file_roots": [str(self.repo_dir)],
                        "commands": {
                            "ro": {"argv": ["python", "-c", "print('ok')"], "cwd": ".", "effect_class": "read_only"}
                        },
                    }
                },
            },
        )
        old_env = os.environ.get("DEVORCH_JOBS_CONFIG")
        os.environ["DEVORCH_JOBS_CONFIG"] = str(cfg_file)
        try:
            from dev_orchestrator.transport.contracts import canonical_sha256
            raw_bytes = b"Hello from remote_helper wire!\n"
            b64_str = base64.b64encode(raw_bytes).decode("ascii")
            sha_val = canonical_sha256(raw_bytes)

            stage_req = {
                "operation": "op_stage_write",
                "request_id": "req-stage-1",
                "project_id": "p1",
                "content_base64": b64_str,
                "decoded_size_bytes": len(raw_bytes),
                "content_sha256": sha_val,
            }
            stage_res = execute_request(stage_req)
            self.assertEqual(stage_res["status"], "ok")
            c_ref = stage_res["content_ref"]
            self.assertTrue(c_ref.startswith("stage:p1:sha256:"))

            write_req = {
                "operation": "op_write_file",
                "request_id": "req-write-1",
                "project_id": "p1",
                "target_path": "src/remote_written.txt",
                "idempotency_key": "write-remote-1",
                "content_ref": c_ref,
                "content_sha256": sha_val,
                "decoded_size_bytes": len(raw_bytes),
                "if_absent": True,
            }
            write_res = execute_request(write_req)
            self.assertEqual(write_res["status"], "ok")
            self.assertEqual(write_res["content_sha256"], sha_val)
            self.assertNotIn("content_bytes", write_res)
            self.assertTrue((self.repo_dir / "src" / "remote_written.txt").is_file())
            self.assertEqual((self.repo_dir / "src" / "remote_written.txt").read_bytes(), raw_bytes)
        finally:
            if old_env is None:
                os.environ.pop("DEVORCH_JOBS_CONFIG", None)
            else:
                os.environ["DEVORCH_JOBS_CONFIG"] = old_env

    def test_remote_helper_op_poll_returns_record_state(self):
        """remote_helper op_poll returns record state using ExecutionJobStore.get."""
        import os
        from dev_orchestrator.ai.remote_helper import execute_request
        from dev_orchestrator.storage.json_store import write_json

        cfg_file = self.root / "projects.json"
        write_json(
            cfg_file,
            {
                "runtime_root": str(self.root),
                "projects": {
                    "p1": {
                        "repo_path": str(self.repo_dir),
                        "commands": {
                            "ro": {"argv": ["python", "-c", "print('ok')"], "cwd": ".", "effect_class": "read_only"}
                        },
                    }
                },
            },
        )
        old_env = os.environ.get("DEVORCH_JOBS_CONFIG")
        os.environ["DEVORCH_JOBS_CONFIG"] = str(cfg_file)
        try:
            spawn_req = {
                "operation": "op_spawn",
                "request_id": "req-spawn-1",
                "project_id": "p1",
                "command_ref": "ro",
                "idempotency_key": "poll-test-1",
            }
            spawn_res = execute_request(spawn_req)
            self.assertEqual(spawn_res["status"], "ok")
            job_id = spawn_res["job_id"]

            poll_req = {
                "operation": "op_poll",
                "request_id": "req-poll-1",
                "job_id": job_id,
            }
            poll_res = execute_request(poll_req)
            self.assertEqual(poll_res["status"], "ok")
            self.assertEqual(poll_res["job_id"], job_id)
            self.assertIn(poll_res["state"], ("queued", "running", "completed"))
            self.assertIsNotNone(poll_res["execution_policy_digest"])
            self.assertIsNotNone(poll_res["resolution_digest"])
        finally:
            if old_env is None:
                os.environ.pop("DEVORCH_JOBS_CONFIG", None)
            else:
                os.environ["DEVORCH_JOBS_CONFIG"] = old_env

    def test_remote_helper_spawn_validates_canonical_spec_and_controller_hash(self):
        """remote_helper op_spawn rejects wire-supplied job_id mismatch and controller_spec_hash mismatch."""
        import os
        from dev_orchestrator.ai.remote_helper import execute_request
        from dev_orchestrator.jobs.store import ExecutionJobStore
        from dev_orchestrator.storage.json_store import write_json

        cfg_file = self.root / "projects_spawn.json"
        write_json(
            cfg_file,
            {
                "runtime_root": str(self.root),
                "projects": {
                    "p1": {
                        "repo_path": str(self.repo_dir),
                        "commands": {
                            "ro": {"argv": ["python", "-c", "print('ok')"], "cwd": ".", "effect_class": "read_only"}
                        },
                    }
                },
            },
        )
        old_env = os.environ.get("DEVORCH_JOBS_CONFIG")
        os.environ["DEVORCH_JOBS_CONFIG"] = str(cfg_file)
        try:
            spec = JobSpec(
                project_id="p1",
                command_ref="ro",
                idempotency_key="canon-spawn-key-1",
                kind="validation",
                transport="local",
            )
            canon_spec = spec.to_canonical_dict()
            correct_shash = spec_hash(spec)
            correct_jid = job_id_for(spec)

            # 1. Reject wire-supplied job_id mismatch
            with self.assertRaises(ValueError) as ctx:
                execute_request({
                    "operation": "op_spawn",
                    "request_id": "req-spawn-err-1",
                    "job_spec": canon_spec,
                    "controller_spec_hash": correct_shash,
                    "job_id": "tampered-job-id-12345",
                })
            self.assertIn("unauthorized job_id", str(ctx.exception))

            # 2. Reject controller_spec_hash mismatch
            with self.assertRaises(ValueError) as ctx:
                execute_request({
                    "operation": "op_spawn",
                    "request_id": "req-spawn-err-2",
                    "job_spec": canon_spec,
                    "controller_spec_hash": "sha256:" + "0" * 64,
                    "job_id": correct_jid,
                })
            self.assertIn("controller_spec_hash mismatch", str(ctx.exception))

            # 3. Legitimate spawn succeeds and saves submission_spec in JobRecord
            res = execute_request({
                "operation": "op_spawn",
                "request_id": "req-spawn-ok",
                "job_spec": canon_spec,
                "controller_spec_hash": correct_shash,
                "job_id": correct_jid,
            })
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["job_id"], correct_jid)

            store = ExecutionJobStore(self.root)
            saved = store.get(correct_jid)
            self.assertIsNotNone(saved)
            self.assertEqual(saved.submission_spec, canon_spec)
        finally:
            try:
                execute_request({
                    "operation": "op_cancel",
                    "request_id": "req-spawn-clean",
                    "job_id": correct_jid,
                })
            except Exception:
                pass
            if old_env is None:
                os.environ.pop("DEVORCH_JOBS_CONFIG", None)
            else:
                os.environ["DEVORCH_JOBS_CONFIG"] = old_env

    def test_remote_helper_job_start_validates_canonical_spec_and_controller_hash(self):
        """remote_helper job_start rejects wire job_id mismatch and populates submission_spec."""
        import os
        from dev_orchestrator.ai.remote_helper import execute_request
        from dev_orchestrator.jobs.store import ExecutionJobStore
        from dev_orchestrator.storage.json_store import write_json

        cfg_file = self.root / "projects_job_start.json"
        write_json(
            cfg_file,
            {
                "runtime_root": str(self.root),
                "projects": {
                    "p1": {
                        "repo_path": str(self.repo_dir),
                        "commands": {
                            "ro": {"argv": ["python", "-c", "print('ok')"], "cwd": ".", "effect_class": "read_only"}
                        },
                    }
                },
            },
        )
        old_env = os.environ.get("DEVORCH_JOBS_CONFIG")
        os.environ["DEVORCH_JOBS_CONFIG"] = str(cfg_file)
        try:
            spec = JobSpec(
                project_id="p1",
                command_ref="ro",
                idempotency_key="canon-jobstart-key-1",
                kind="validation",
                transport="local",
            )
            canon_spec = spec.to_canonical_dict()
            correct_shash = spec_hash(spec)
            correct_jid = job_id_for(spec)

            # Reject wire job_id mismatch
            with self.assertRaises(ValueError) as ctx:
                execute_request({
                    "operation": "job_start",
                    "request_id": "req-js-err-1",
                    "job_spec": canon_spec,
                    "controller_spec_hash": correct_shash,
                    "job_id": "tampered-wire-jid",
                })
            self.assertIn("unauthorized job_id", str(ctx.exception))

            # Legitimate job_start succeeds
            res = execute_request({
                "operation": "job_start",
                "request_id": "req-js-ok",
                "job_spec": canon_spec,
                "controller_spec_hash": correct_shash,
                "job_id": correct_jid,
            })
            self.assertEqual(res["status"], "started")
            self.assertEqual(res["job_id"], correct_jid)

            store = ExecutionJobStore(self.root)
            saved = store.get(correct_jid)
            self.assertIsNotNone(saved)
            self.assertEqual(saved.submission_spec, canon_spec)
        finally:
            try:
                execute_request({
                    "operation": "job_cancel",
                    "request_id": "req-js-clean",
                    "job_id": correct_jid,
                })
            except Exception:
                pass
            if old_env is None:
                os.environ.pop("DEVORCH_JOBS_CONFIG", None)
            else:
                os.environ["DEVORCH_JOBS_CONFIG"] = old_env

    def test_remote_helper_job_start_accepts_chained_retry_successor_job_id(self):
        """job_start folds every retry segment when authorizing a chained successor id."""
        import os
        from dev_orchestrator.ai.remote_helper import execute_request
        from dev_orchestrator.storage.json_store import write_json

        cfg_file = self.root / "projects_chained_job_start.json"
        write_json(
            cfg_file,
            {
                "runtime_root": str(self.root),
                "projects": {
                    "p1": {
                        "repo_path": str(self.repo_dir),
                        "commands": {
                            "ro": {
                                "argv": ["python", "-c", "print('ok')"],
                                "cwd": ".",
                                "effect_class": "read_only",
                            }
                        },
                    }
                },
            },
        )
        old_env = os.environ.get("DEVORCH_JOBS_CONFIG")
        os.environ["DEVORCH_JOBS_CONFIG"] = str(cfg_file)
        try:
            base_key = "chain-job-start"
            first_request = "retry-request-1"
            second_request = "retry-request-2"
            chained_key = f"{base_key}:retry:{first_request}:retry:{second_request}"
            base_id = job_id_for({"project_id": "p1", "idempotency_key": base_key})
            first_id = retry_successor_id(base_id, first_request)
            chained_id = retry_successor_id(first_id, second_request)
            spec = JobSpec(
                project_id="p1",
                command_ref="ro",
                idempotency_key=chained_key,
            )
            with patch(
                "dev_orchestrator.jobs.transport.LocalJobTransport.job_start",
                return_value={"status": "started"},
            ):
                result = execute_request({
                    "operation": "job_start",
                    "request_id": "req-chain-job-start",
                    "job_id": chained_id,
                    "job_spec": spec.to_canonical_dict(),
                    "controller_spec_hash": spec_hash(spec),
                })
            self.assertEqual(result["job_id"], chained_id)
            self.assertEqual(result["status"], "started")
        finally:
            if old_env is None:
                os.environ.pop("DEVORCH_JOBS_CONFIG", None)
            else:
                os.environ["DEVORCH_JOBS_CONFIG"] = old_env

    def test_remote_helper_op_spawn_accepts_chained_retry_and_explicit_retry_precedence(self):
        """op_spawn folds chained retries while explicit retry identity remains authoritative."""
        import os
        from dev_orchestrator.ai.remote_helper import execute_request
        from dev_orchestrator.storage.json_store import write_json

        cfg_file = self.root / "projects_chained_op_spawn.json"
        write_json(
            cfg_file,
            {
                "runtime_root": str(self.root),
                "projects": {
                    "p1": {
                        "repo_path": str(self.repo_dir),
                        "commands": {
                            "ro": {
                                "argv": ["python", "-c", "print('ok')"],
                                "cwd": ".",
                                "effect_class": "read_only",
                            }
                        },
                    }
                },
            },
        )
        old_env = os.environ.get("DEVORCH_JOBS_CONFIG")
        os.environ["DEVORCH_JOBS_CONFIG"] = str(cfg_file)
        try:
            base_key = "chain-op-spawn"
            first_request = "retry-request-a"
            second_request = "retry-request-b"
            chained_key = f"{base_key}:retry:{first_request}:retry:{second_request}"
            base_id = job_id_for({"project_id": "p1", "idempotency_key": base_key})
            first_id = retry_successor_id(base_id, first_request)
            chained_id = retry_successor_id(first_id, second_request)
            spec = JobSpec(
                project_id="p1",
                command_ref="ro",
                idempotency_key=chained_key,
            )
            with patch(
                "dev_orchestrator.jobs.transport.LocalJobTransport.job_start",
                return_value={"status": "started"},
            ):
                chained_result = execute_request({
                    "operation": "op_spawn",
                    "request_id": "req-chain-op-spawn",
                    "job_id": chained_id,
                    "job_spec": spec.to_canonical_dict(),
                    "controller_spec_hash": spec_hash(spec),
                })
                explicit_id = retry_successor_id(chained_id, "explicit-retry")
                explicit_spec = JobSpec(
                    project_id="p1",
                    command_ref="ro",
                    idempotency_key="not-the-explicit-retry-identity",
                )
                explicit_result = execute_request({
                    "operation": "op_spawn",
                    "request_id": "req-explicit-op-spawn",
                    "job_id": explicit_id,
                    "job_spec": explicit_spec.to_canonical_dict(),
                    "controller_spec_hash": spec_hash(explicit_spec),
                    "retry_of": chained_id,
                    "retry_request_id": "explicit-retry",
                })
            self.assertEqual(chained_result["job_id"], chained_id)
            self.assertEqual(explicit_result["job_id"], explicit_id)
        finally:
            if old_env is None:
                os.environ.pop("DEVORCH_JOBS_CONFIG", None)
            else:
                os.environ["DEVORCH_JOBS_CONFIG"] = old_env

    def test_ssh_job_transport_sends_canonical_job_spec_and_controller_hash(self):
        """SSHJobTransport.job_start sends complete job_spec, controller_spec_hash, and parameters."""
        mock_run = MagicMock(return_value={
            "status": "success",
            "payload": {"status": "started", "job_id": "job-mock-1"},
        })
        transport = SSHJobTransport(MagicMock())
        transport._delegate._run_remote_helper = mock_run
        spec = JobSpec(
            project_id="p1",
            command_ref="build",
            idempotency_key="key-ssh-job-1",
            kind="validation",
            transport="ssh",
            metadata={"parameters": {"env": "prod"}},
        )
        res = transport.job_start(spec)
        self.assertEqual(res["status"], "started")
        mock_run.assert_called_once()
        envelope = mock_run.call_args[0][0]
        self.assertEqual(envelope["operation"], "job_start")
        self.assertEqual(envelope["job_spec"], spec.to_canonical_dict())
        self.assertEqual(envelope["controller_spec_hash"], spec_hash(spec))
        self.assertEqual(envelope["parameters"], {"env": "prod"})

    def test_ssh_spawn_pins_policy_and_resolution_digests_and_rejects_drift(self):
        """SSHMachineTransport.spawn pins digests from op_resolve and rejects policy mismatch or drift."""
        import os
        from dev_orchestrator.transport.ssh import SSHMachineTransport
        from dev_orchestrator.transport.hosts import TransportHostProfile
        from dev_orchestrator.transport.contracts import MachineOperation, TransportRejectedError
        from dev_orchestrator.ai.remote_helper import execute_request
        from dev_orchestrator.storage.json_store import write_json

        pinned_digest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        different_digest = "sha256:2222222222222222222222222222222222222222222222222222222222222222"

        profile = TransportHostProfile(
            host_id="remote1",
            candidate_order=["ssh"],
            ssh={"peer": "remote1.test"},
            approved_policy_pins={"cmd1": pinned_digest},
        )
        transport = SSHMachineTransport(profile)

        op = MachineOperation(
            project_id="p1",
            command_ref="cmd1",
            idempotency_key="ssh-spawn-key-1",
            parameters={"param1": "val1"},
        )

        # 1. op_resolve reports mismatched execution_policy_digest -> raises TransportRejectedError
        with patch.object(transport, "resolve", return_value={
            "status": "ok",
            "execution_policy_digest": different_digest,
            "parameters_digest": "sha256:4444",
            "resolution_digest": "sha256:5555",
        }):
            with self.assertRaises(TransportRejectedError) as ctx:
                transport.spawn(op)
            self.assertIn("remote_policy_mismatch", str(ctx.exception))

        # 2. op_resolve reports matching policy pin -> JobSpec is built with all three digests and sent
        mock_call = MagicMock(return_value={"status": "ok", "job_id": "job-spawned-1"})
        with patch.object(transport, "resolve", return_value={
            "status": "ok",
            "execution_policy_digest": pinned_digest,
            "parameters_digest": "sha256:4444",
            "resolution_digest": "sha256:5555",
        }), patch.object(transport, "_call_helper", mock_call):
            res = transport.spawn(op)
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.job_id, "job-spawned-1")
            mock_call.assert_called_once()
            env = mock_call.call_args[0][0]
            self.assertEqual(env["operation"], "op_spawn")
            sent_spec = env["job_spec"]
            self.assertEqual(sent_spec["execution_policy_digest"], pinned_digest)
            self.assertEqual(sent_spec["parameters_digest"], "sha256:4444")
            self.assertEqual(sent_spec["resolution_digest"], "sha256:5555")

        # 3. In remote_helper: reject spawn that supplies parameters while spec carries no parameters_digest
        cfg_file = self.root / "projects_drift.json"
        write_json(
            cfg_file,
            {
                "runtime_root": str(self.root),
                "projects": {
                    "p1": {
                        "repo_path": str(self.repo_dir),
                        "commands": {
                            "cmd_with_param": {
                                "argv": ["python", "-c", "print('ok')"],
                                "cwd": ".",
                                "effect_class": "read_only",
                                "parameters": {"env": {"type": "enum", "allowed_values": ["dev", "prod"]}},
                            }
                        },
                    }
                },
            },
        )
        old_env = os.environ.get("DEVORCH_JOBS_CONFIG")
        os.environ["DEVORCH_JOBS_CONFIG"] = str(cfg_file)
        try:
            spec_no_p_digest = JobSpec(
                project_id="p1",
                command_ref="cmd_with_param",
                idempotency_key="key-no-p-digest",
                transport="ssh",
                execution_policy_digest="sha256:0000",
                resolution_digest="sha256:0000",
            )
            with self.assertRaises(ValueError) as ctx:
                execute_request({
                    "operation": "op_spawn",
                    "request_id": "req-no-p",
                    "job_spec": spec_no_p_digest.to_canonical_dict(),
                    "controller_spec_hash": spec_hash(spec_no_p_digest),
                    "parameters": {"env": "prod"},
                })
            self.assertIn("missing parameters_digest", str(ctx.exception))
        finally:
            if old_env is None:
                os.environ.pop("DEVORCH_JOBS_CONFIG", None)
            else:
                os.environ["DEVORCH_JOBS_CONFIG"] = old_env


if __name__ == "__main__":
    unittest.main()
