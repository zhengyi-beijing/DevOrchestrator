import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dev_orchestrator.ai.aibroker_subprocess import AIBrokerClientConfig, AIBrokerExecutionPort
from dev_orchestrator.ai.contracts import AIRoleRequest, AIRoleResult
from dev_orchestrator.ai.execution_transport import (
    ExecutionTransport,
    ExecutionTransportError,
    ExecutionTransportResult,
    LocalTransport,
    SSHTransport,
    SSHTransportConfig,
)
from dev_orchestrator.ai.remote_helper import handle_request
from dev_orchestrator.ai.runtime_config import load_aibroker_execution_port

ROOT = Path(__file__).resolve().parents[1]


def make_dummy_request(request_id: str = "req-123") -> AIRoleRequest:
    return AIRoleRequest(
        role="planner",
        prompt="plan the task",
        working_directory=Path(ROOT),
        task_run_id="T1",
        project_id="P1",
        request_id=request_id,
    )


def make_dummy_config() -> AIBrokerClientConfig:
    return AIBrokerClientConfig(
        broker_repo=ROOT,
        config_path=ROOT / "config.json",
        python_executable=Path("python"),
    )


class P13LocalTransportTests(unittest.TestCase):
    def test_satisfies_protocol(self):
        transport = LocalTransport()
        self.assertIsInstance(transport, ExecutionTransport)

    def test_dispatch_success(self):
        transport = LocalTransport()
        req = make_dummy_request()
        cfg = make_dummy_config()

        mock_payload = {
            "success": True,
            "execution_id": "exec-1",
            "content": "output from agent",
            "raw": {"cost": 0.01},
        }

        mock_completed = subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps(mock_payload),
            stderr="",
        )

        with patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run", return_value=mock_completed):
            result = transport.dispatch(req, cfg, {}, 30.0)
            self.assertIsInstance(result, ExecutionTransportResult)
            self.assertEqual(result.transport_name, "local")
            self.assertEqual(result.payload["content"], "output from agent")
            self.assertEqual(result.status, "available")

    def test_dispatch_failure_raises_clean_error_no_fallback(self):
        transport = LocalTransport()
        req = make_dummy_request()
        cfg = make_dummy_config()

        mock_completed = subprocess.CompletedProcess(
            args=["python"],
            returncode=1,
            stdout="",
            stderr="Subprocess exploded",
        )

        with patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run", return_value=mock_completed):
            with self.assertRaises(ExecutionTransportError) as ctx:
                transport.dispatch(req, cfg, {}, 30.0)
            self.assertIn("exit 1", str(ctx.exception))
            self.assertIn("Subprocess exploded", str(ctx.exception))

    def test_status_subprocess_not_found_returns_none(self):
        transport = LocalTransport()
        cfg = make_dummy_config()
        mock_completed = subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps({"status": "not_found"}),
            stderr="",
        )
        with patch("dev_orchestrator.ai.execution_transport.subprocess.run", return_value=mock_completed):
            result = transport.status("req-404", cfg, {})
            self.assertIsNone(result)

    def test_status_subprocess_found_returns_payload(self):
        transport = LocalTransport()
        cfg = make_dummy_config()
        mock_completed = subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps({"status": "succeeded", "request_id": "req-1"}),
            stderr="",
        )
        with patch("dev_orchestrator.ai.execution_transport.subprocess.run", return_value=mock_completed):
            result = transport.status("req-1", cfg, {})
            self.assertEqual(result, {"status": "succeeded", "request_id": "req-1"})

    def test_interrupt_subprocess_not_found_returns_none(self):
        transport = LocalTransport()
        cfg = make_dummy_config()
        mock_completed = subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps({"status": "not_found"}),
            stderr="",
        )
        with patch("dev_orchestrator.ai.execution_transport.subprocess.run", return_value=mock_completed):
            result = transport.interrupt("req-404", "stop", cfg, {})
            self.assertIsNone(result)

    def test_interrupt_subprocess_found_returns_payload(self):
        transport = LocalTransport()
        cfg = make_dummy_config()
        mock_completed = subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps({"status": "interrupted", "request_id": "req-1"}),
            stderr="",
        )
        with patch("dev_orchestrator.ai.execution_transport.subprocess.run", return_value=mock_completed):
            result = transport.interrupt("req-1", "stop", cfg, {})
            self.assertEqual(result, {"status": "interrupted", "request_id": "req-1"})


class P13SSHTransportTests(unittest.TestCase):
    def test_satisfies_protocol(self):
        cfg = SSHTransportConfig(peer="100.64.0.10")
        transport = SSHTransport(cfg)
        self.assertIsInstance(transport, ExecutionTransport)

    def test_path_mapping(self):
        cfg = SSHTransportConfig(
            peer="100.64.0.10",
            path_mapping={
                str(ROOT): "/home/ubuntu/repo",
                "/other/local": "/remote/other",
            },
        )
        transport = SSHTransport(cfg)
        # Root mapped
        self.assertEqual(transport.map_path(ROOT), "/home/ubuntu/repo")
        # Subdirectory mapped
        sub = ROOT / "src" / "file.py"
        self.assertEqual(transport.map_path(sub), "/home/ubuntu/repo/src/file.py")
        # Unmapped path unchanged
        self.assertEqual(transport.map_path("/unmapped/path"), "/unmapped/path")
        # Sibling directory sharing name prefix does not match
        self.assertEqual(transport.map_path(str(ROOT) + "-dev"), str(ROOT) + "-dev")

    def test_build_ssh_argv(self):
        cfg = SSHTransportConfig(
            peer="remote-host",
            user="devuser",
            port=2222,
            identity_file=Path("/home/user/.ssh/id_ed25519"),
            known_hosts_file=Path("/home/user/.ssh/known_hosts"),
            strict_host_key_checking="accept-new",
            remote_python="/usr/bin/python3",
            remote_helper_module="dev_orchestrator.ai.remote_helper",
            ssh_executable="ssh",
            connect_timeout_seconds=15.0,
        )
        transport = SSHTransport(cfg)
        argv = transport._build_ssh_argv()

        self.assertEqual(argv[0], "ssh")
        self.assertIn("-o", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertIn("ConnectTimeout=15", argv)
        self.assertIn("-p", argv)
        self.assertIn("2222", argv)
        self.assertIn("-i", argv)
        self.assertIn(str(Path("/home/user/.ssh/id_ed25519")), argv)
        self.assertIn("devuser@remote-host", argv)
        self.assertEqual(argv[-3:], ["/usr/bin/python3", "-m", "dev_orchestrator.ai.remote_helper"])

    def test_dispatch_success_with_correlation(self):
        cfg = SSHTransportConfig(peer="100.64.0.10")
        transport = SSHTransport(cfg)
        req = make_dummy_request("req-corr-1")
        b_cfg = make_dummy_config()

        helper_response = {
            "status": "success",
            "request_id": "req-corr-1",
            "host_identity": "100.64.0.10",
            "payload": {
                "success": True,
                "execution_id": "remote-exec-1",
                "content": "remote model completed task",
            },
        }

        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=json.dumps(helper_response).encode("utf-8"),
            stderr=b"",
        )

        with patch("subprocess.run", return_value=mock_completed) as mock_run:
            result = transport.dispatch(req, b_cfg, {}, 60.0)
            self.assertEqual(result.transport_name, "ssh")
            self.assertEqual(result.payload["execution_id"], "remote-exec-1")
            self.assertEqual(result.payload["content"], "remote model completed task")

            # Verify stdin payload
            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            sent_data = json.loads(kwargs["input"].decode("utf-8"))
            self.assertEqual(sent_data["operation"], "dispatch")
            self.assertEqual(sent_data["request_id"], "req-corr-1")

    def test_dispatch_correlation_mismatch_fails_closed(self):
        cfg = SSHTransportConfig(peer="100.64.0.10")
        transport = SSHTransport(cfg)
        req = make_dummy_request("req-sent")
        b_cfg = make_dummy_config()

        # Remote helper returns mismatched request_id
        helper_response = {
            "status": "success",
            "request_id": "req-different",
            "host_identity": "100.64.0.10",
            "payload": {"success": True},
        }

        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=json.dumps(helper_response).encode("utf-8"),
            stderr=b"",
        )

        with patch("subprocess.run", return_value=mock_completed):
            with self.assertRaises(ExecutionTransportError) as ctx:
                transport.dispatch(req, b_cfg, {}, 60.0)
            self.assertIn("correlation mismatch", str(ctx.exception))

    def test_dispatch_connection_failure_no_rdc_fallback(self):
        cfg = SSHTransportConfig(peer="100.64.0.10")
        transport = SSHTransport(cfg)
        req = make_dummy_request("req-fail")
        b_cfg = make_dummy_config()

        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=255,
            stdout=b"",
            stderr=b"ssh: connect to host 100.64.0.10 port 22: Connection timed out",
        )

        with patch("subprocess.run", return_value=mock_completed):
            with self.assertRaises(ExecutionTransportError) as ctx:
                transport.dispatch(req, b_cfg, {}, 60.0)
            self.assertIn("Connection timed out", str(ctx.exception))

    def test_host_identity_validation_mismatch_and_missing(self):
        cfg = SSHTransportConfig(peer="100.64.0.10")
        transport = SSHTransport(cfg)
        req = make_dummy_request("req-host-1")
        b_cfg = make_dummy_config()

        # 1. Missing host_identity fails closed
        resp_no_host = {
            "status": "success",
            "request_id": "req-host-1",
            "payload": {"success": True},
        }
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=json.dumps(resp_no_host).encode("utf-8"),
            stderr=b"",
        )
        with patch("subprocess.run", return_value=mock_completed):
            with self.assertRaises(ExecutionTransportError) as ctx:
                transport.dispatch(req, b_cfg, {}, 60.0)
            self.assertIn("missing valid host_identity", str(ctx.exception))

        # 2. Mismatched host_identity fails closed
        resp_bad_host = {
            "status": "success",
            "request_id": "req-host-1",
            "host_identity": "rogue-host",
            "payload": {"success": True},
        }
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=json.dumps(resp_bad_host).encode("utf-8"),
            stderr=b"",
        )
        with patch("subprocess.run", return_value=mock_completed):
            with self.assertRaises(ExecutionTransportError) as ctx:
                transport.dispatch(req, b_cfg, {}, 60.0)
            self.assertIn("host identity mismatch", str(ctx.exception))

        # 3. Expected host identity override config
        cfg_custom = SSHTransportConfig(peer="100.64.0.10", expected_host_identity="worker-box")
        transport_custom = SSHTransport(cfg_custom)
        resp_good_custom = {
            "status": "success",
            "request_id": "req-host-1",
            "host_identity": "worker-box",
            "payload": {"success": True},
        }
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=json.dumps(resp_good_custom).encode("utf-8"),
            stderr=b"",
        )
        with patch("subprocess.run", return_value=mock_completed):
            res = transport_custom.dispatch(req, b_cfg, {}, 60.0)
            self.assertEqual(res.host_identity, "worker-box")

    def test_host_key_rejection_fails_closed(self):
        cfg = SSHTransportConfig(peer="100.64.0.10")
        transport = SSHTransport(cfg)
        req = make_dummy_request("req-hk-fail")
        b_cfg = make_dummy_config()

        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=255,
            stdout=b"",
            stderr=b"Host key verification failed.",
        )
        with patch("subprocess.run", return_value=mock_completed):
            with self.assertRaises(ExecutionTransportError) as ctx:
                transport.dispatch(req, b_cfg, {}, 60.0)
            self.assertIn("Host key verification failed", str(ctx.exception))

    def test_hostile_identifiers_cleanly_serialized_in_json(self):
        cfg = SSHTransportConfig(peer="100.64.0.10")
        transport = SSHTransport(cfg)
        req = AIRoleRequest(
            role="planner; rm -rf / ; $(whoami)",
            prompt="do planning",
            working_directory=Path(ROOT),
            task_run_id="<script>alert('xss')</script>\n\nDROP TABLE",
            project_id="P1' OR '1'='1",
            request_id="req-hostile; rm -rf / ; $(whoami)",
        )
        b_cfg = make_dummy_config()

        helper_response = {
            "status": "success",
            "request_id": req.request_id,
            "host_identity": "100.64.0.10",
            "payload": {"success": True},
        }
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=json.dumps(helper_response).encode("utf-8"),
            stderr=b"",
        )
        with patch("subprocess.run", return_value=mock_completed) as mock_run:
            transport.dispatch(req, b_cfg, {}, 60.0)
            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            sent_data = json.loads(kwargs["input"].decode("utf-8"))
            self.assertEqual(sent_data["request"]["role"], "planner; rm -rf / ; $(whoami)")
            self.assertEqual(sent_data["request_id"], req.request_id)
            self.assertEqual(sent_data["request"]["project_id"], "P1' OR '1'='1")

    def test_large_and_unicode_stdin_payloads(self):
        cfg = SSHTransportConfig(peer="100.64.0.10")
        transport = SSHTransport(cfg)
        unicode_prompt = "你好，世界！🚀🌟 " * 5000  # ~120KB unicode string
        req = AIRoleRequest(
            role="planner",
            prompt=unicode_prompt,
            working_directory=Path(ROOT),
            task_run_id="T1",
            project_id="P1",
            request_id="req-unicode-large",
        )
        b_cfg = make_dummy_config()

        helper_response = {
            "status": "success",
            "request_id": "req-unicode-large",
            "host_identity": "100.64.0.10",
            "payload": {"success": True, "output": "processed large unicode"},
        }
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=json.dumps(helper_response).encode("utf-8"),
            stderr=b"",
        )
        with patch("subprocess.run", return_value=mock_completed) as mock_run:
            result = transport.dispatch(req, b_cfg, {}, 60.0)
            self.assertEqual(result.payload["output"], "processed large unicode")
            _, kwargs = mock_run.call_args
            sent_data = json.loads(kwargs["input"].decode("utf-8"))
            self.assertEqual(sent_data["request"]["prompt"], unicode_prompt)

    def test_timeout_vs_disconnect_distinction(self):
        cfg = SSHTransportConfig(peer="100.64.0.10")
        transport = SSHTransport(cfg)
        req = make_dummy_request("req-t-vs-d")
        b_cfg = make_dummy_config()

        # 1. TimeoutExpired raises error with "timed out"
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=30.0)):
            with self.assertRaises(ExecutionTransportError) as ctx:
                transport.dispatch(req, b_cfg, {}, 30.0)
            self.assertIn("timed out", str(ctx.exception).lower())

        # 2. Connection reset / disconnect raises error with "failed"
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=255,
            stdout=b"",
            stderr=b"ssh: Connection reset by peer",
        )
        with patch("subprocess.run", return_value=mock_completed):
            with self.assertRaises(ExecutionTransportError) as ctx:
                transport.dispatch(req, b_cfg, {}, 30.0)
            self.assertIn("failed", str(ctx.exception).lower())
            self.assertIn("Connection reset by peer", str(ctx.exception))

    def test_ssh_status_and_interrupt(self):
        cfg = SSHTransportConfig(peer="100.64.0.10")
        transport = SSHTransport(cfg)
        b_cfg = make_dummy_config()

        # 1. Status found
        resp_found = {
            "status": "success",
            "request_id": "req-stat-1",
            "host_identity": "100.64.0.10",
            "payload": {"status": "succeeded", "execution_id": "e1"},
        }
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout=json.dumps(resp_found).encode("utf-8"), stderr=b""
        )
        with patch("subprocess.run", return_value=mock_completed):
            st = transport.status("req-stat-1", b_cfg, {})
            self.assertEqual(st, {"status": "succeeded", "execution_id": "e1"})

        # 2. Status not_found returns None
        resp_not_found = {
            "status": "success",
            "request_id": "req-stat-404",
            "host_identity": "100.64.0.10",
            "payload": {"status": "not_found"},
        }
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout=json.dumps(resp_not_found).encode("utf-8"), stderr=b""
        )
        with patch("subprocess.run", return_value=mock_completed):
            st = transport.status("req-stat-404", b_cfg, {})
            self.assertIsNone(st)

        # 3. Interrupt found
        resp_interrupted = {
            "status": "success",
            "request_id": "req-int-1",
            "host_identity": "100.64.0.10",
            "payload": {"status": "interrupted"},
        }
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout=json.dumps(resp_interrupted).encode("utf-8"), stderr=b""
        )
        with patch("subprocess.run", return_value=mock_completed):
            res = transport.interrupt("req-int-1", "stop requested", b_cfg, {})
            self.assertEqual(res, {"status": "interrupted"})

        # 4. Interrupt not_found returns None
        mock_completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout=json.dumps(resp_not_found).encode("utf-8"), stderr=b""
        )
        with patch("subprocess.run", return_value=mock_completed):
            res = transport.interrupt("req-stat-404", "stop requested", b_cfg, {})
            self.assertIsNone(res)


class P13RemoteHelperTests(unittest.TestCase):
    def test_handle_request_dispatch(self):
        request_envelope = {
            "operation": "dispatch",
            "request_id": "req-rh-1",
            "request": {
                "project_id": "p1",
                "role": "planner",
                "prompt": "do something",
                "working_directory": str(ROOT),
                "timeout_seconds": 30.0,
            },
            "broker_repo": str(ROOT),
            "config_path": str(ROOT / "config.json"),
            "env": {},
            "timeout_seconds": 30.0,
        }

        mock_completed = subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout=json.dumps({"success": True, "execution_id": "exec-rh"}),
            stderr="",
        )

        with patch("dev_orchestrator.ai.remote_helper.subprocess.run", return_value=mock_completed):
            resp = handle_request(request_envelope)
            self.assertEqual(resp["status"], "success")
            self.assertEqual(resp["request_id"], "req-rh-1")
            self.assertEqual(resp["payload"]["execution_id"], "exec-rh")

    def test_handle_request_unknown_operation(self):
        request_envelope = {
            "operation": "unsupported_op",
            "request_id": "req-bad",
        }
        resp = handle_request(request_envelope)
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["request_id"], "req-bad")
        self.assertIn("unsupported", resp["error"].lower())


class P13RuntimeConfigTransportTests(unittest.TestCase):
    def test_load_aibroker_execution_port_local(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            cfg_file = runtime / "aibroker-execution.json"
            cfg_file.write_text(json.dumps({
                "python_executable": "python",
                "broker_repo": str(ROOT),
                "config_path": str(ROOT / "config.json"),
                "transport": "local",
            }), encoding="utf-8")

            port = load_aibroker_execution_port(runtime)
            self.assertIsNotNone(port)
            self.assertIsInstance(port.transport, LocalTransport)

    def test_load_aibroker_execution_port_ssh(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            cfg_file = runtime / "aibroker-execution.json"
            cfg_file.write_text(json.dumps({
                "python_executable": "python",
                "broker_repo": str(ROOT),
                "config_path": str(ROOT / "config.json"),
                "transport": "ssh",
                "ssh_peer": "100.64.0.22",
                "ssh_user": "runner",
                "ssh_port": 222,
                "path_mapping": {str(ROOT): "/remote/path"},
            }), encoding="utf-8")

            port = load_aibroker_execution_port(runtime)
            self.assertIsNotNone(port)
            self.assertIsInstance(port.transport, SSHTransport)
            self.assertEqual(port.transport.ssh_config.peer, "100.64.0.22")
            self.assertEqual(port.transport.ssh_config.user, "runner")
            self.assertEqual(port.transport.ssh_config.port, 222)
            self.assertEqual(port.transport.ssh_config.path_mapping[str(ROOT)], "/remote/path")
