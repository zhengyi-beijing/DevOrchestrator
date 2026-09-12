import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-m", "dev_orchestrator", *args], cwd=ROOT, env=env, text=True, capture_output=True, timeout=10)


class CliLifecycleTests(unittest.TestCase):
    def test_web_start_status_stop_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            port = free_port()
            common = ["--runtime-root", str(runtime)]
            started = run_cli("start-web", "--listen", "127.0.0.1", "--port", str(port), "--web-root", str(ROOT / "web"), *common)
            self.assertEqual(started.returncode, 0, started.stderr)
            payload = json.loads(started.stdout)
            self.assertEqual(payload["state"], "running")
            status = run_cli("status-web", *common)
            self.assertEqual(status.returncode, 0, status.stderr)
            status_payload = json.loads(status.stdout)
            self.assertTrue(status_payload["process_alive"])
            self.assertEqual(status_payload["port"], port)

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/summary")
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(body["project_count"], 0)

            stopped = run_cli("stop-web", *common)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertEqual(json.loads(stopped.stdout)["state"], "stopped")
            after = run_cli("status-web", *common)
            self.assertFalse(json.loads(after.stdout)["process_alive"])

    def test_project_status_and_continue_are_project_id_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            (runtime / "projects").mkdir()
            (runtime / "projects" / "p1.json").write_text(json.dumps({
                "project_id": "p1", "state": "READY_TO_RUN", "conversation_binding": None,
            }), encoding="utf-8")
            (runtime / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")
            status = run_cli("project-status", "p1", "--runtime-root", str(runtime))
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["project_id"], "p1")
            queued = run_cli("project-continue", "p1", "--runtime-root", str(runtime))
            self.assertEqual(queued.returncode, 0, queued.stderr)
            payload = json.loads(queued.stdout)
            self.assertEqual((payload["project_id"], payload["action"], payload["state"]), ("p1", "continue", "pending"))
            inbox = list((runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox), 1)
            self.assertNotIn("conversation", inbox[0].read_text(encoding="utf-8"))

    def test_project_status_cli_projects_active_broker_execution(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            (runtime / "projects").mkdir()
            (runtime / "projects" / "p1.json").write_text(json.dumps({
                "project_id": "p1", "state": "READY_TO_RUN", "lifecycle_state": "READY_TO_RUN",
            }), encoding="utf-8")
            (runtime / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")
            (runtime / "transition-executor.json").write_text(json.dumps({
                "version": 1,
                "executions": {
                    "req-1": {
                        "project_id": "p1",
                        "source_request_id": "req-1",
                        "state": "running",
                        "engine": "aibroker",
                        "broker_request_id": "brk-99",
                        "started_at": "2026-09-11T12:00:00+00:00",
                    }
                }
            }), encoding="utf-8")
            status = run_cli("project-status", "p1", "--runtime-root", str(runtime))
            self.assertEqual(status.returncode, 0, status.stderr)
            data = json.loads(status.stdout)
            self.assertEqual(data["state"], "WORKER_RUNNING")
            self.assertEqual(data["lifecycle_state"], "EXECUTING")
            self.assertIn("broker_execution", data)
            self.assertEqual(data["broker_execution"]["broker_request_id"], "brk-99")

    def test_project_continue_fails_when_daemon_is_not_running(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td); (runtime / "projects").mkdir()
            (runtime / "projects" / "p1.json").write_text(json.dumps({"project_id": "p1"}), encoding="utf-8")
            result = run_cli("project-continue", "p1", "--runtime-root", str(runtime))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("daemon is not running", result.stderr)

    def test_stop_daemon_does_not_interrupt_broker_when_recorded_pid_is_already_dead(self):
        import dev_orchestrator.cli as cli
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td); (runtime / "daemon.pid").write_text("4242", encoding="utf-8")
            with patch.object(cli, "is_pid_alive", return_value=False), patch.object(cli, "_interrupt_active_broker_dispatches") as interrupt:
                cli.cmd_stop_daemon(SimpleNamespace(runtime_root=str(runtime)))
                interrupt.assert_not_called()
            payload = json.loads((runtime / "daemon.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["tree_stopped"]); self.assertEqual(payload["broker_interrupts"], [])

    def test_stop_daemon_interrupts_broker_only_after_verified_tree_stop(self):
        import dev_orchestrator.cli as cli
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td); (runtime / "daemon.pid").write_text("4242", encoding="utf-8")
            with patch.object(cli, "is_pid_alive", return_value=True), patch.object(cli, "terminate_process_tree", return_value=True), patch.object(cli, "_interrupt_active_broker_dispatches", return_value=[{"request_id":"r"}]) as interrupt:
                cli.cmd_stop_daemon(SimpleNamespace(runtime_root=str(runtime)))
                interrupt.assert_called_once_with(runtime)
            payload = json.loads((runtime / "daemon.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["tree_stopped"]); self.assertEqual(payload["broker_interrupts"], [{"request_id":"r"}])

    def test_cli_project_context_and_validate_config(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

            ctx_payload = {
                "schema_version": 1,
                "project_id": "p1",
                "goals": ["Ship CLI context"],
                "architecture": ["CLI subcommand"],
                "protected_scope": ["Production infra"],
                "safety_constraints": ["Fail closed"],
                "validation_commands": ["python -m unittest"],
                "runtime_assumptions": ["Python 3.11"],
                "key_decisions": ["D1 schema"],
            }
            (repo / "agent").mkdir(parents=True, exist_ok=True)
            (repo / "agent" / "project-context.json").write_text(json.dumps(ctx_payload), encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "ctx"], cwd=repo, check=True, capture_output=True)

            config = base / "projects.json"
            config.write_text(json.dumps({"projects": [{
                "project_id": "p1",
                "repo_path": str(repo),
                "adapter": "agent_files",
                "project_context": {
                    "enabled": True,
                    "document_path": "agent/project-context.json",
                    "require_valid": True,
                },
            }]}), encoding="utf-8")

            # 1. validate-config on valid project
            val_ok = run_cli("validate-config", "--config", str(config))
            self.assertEqual(val_ok.returncode, 0, val_ok.stderr)
            val_data = json.loads(val_ok.stdout)
            self.assertTrue(val_data["valid"])
            self.assertEqual(val_data["projects"][0]["project_context_state"], "ready")
            self.assertIsNone(val_data["projects"][0]["project_context_error"])

            # 2. project-context on valid project (both --project-id and positional)
            pctx_ok = run_cli("project-context", "--config", str(config), "--project-id", "p1")
            self.assertEqual(pctx_ok.returncode, 0, pctx_ok.stderr)
            pctx_data = json.loads(pctx_ok.stdout)
            self.assertEqual(pctx_data["state"], "ready")
            self.assertGreater(pctx_data["rendered_chars"], 0)
            self.assertFalse(pctx_data["truncated"])

            pctx_pos = run_cli("project-context", "p1", "--config", str(config))
            self.assertEqual(pctx_pos.returncode, 0, pctx_pos.stderr)

            # 3. Invalid context
            (repo / "agent" / "project-context.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "bad-ctx"], cwd=repo, check=True, capture_output=True)

            val_bad = run_cli("validate-config", "--config", str(config))
            self.assertEqual(val_bad.returncode, 1)
            val_bad_data = json.loads(val_bad.stdout)
            self.assertFalse(val_bad_data["valid"])
            self.assertEqual(val_bad_data["projects"][0]["project_context_state"], "invalid")
            self.assertIsNotNone(val_bad_data["projects"][0]["project_context_error"])

            pctx_bad = run_cli("project-context", "--config", str(config), "--project-id", "p1")
            self.assertEqual(pctx_bad.returncode, 1)
            pctx_bad_data = json.loads(pctx_bad.stdout)
            self.assertEqual(pctx_bad_data["state"], "invalid")


if __name__ == "__main__":
    unittest.main()
