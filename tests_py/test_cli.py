import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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

    def test_project_continue_fails_when_daemon_is_not_running(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td); (runtime / "projects").mkdir()
            (runtime / "projects" / "p1.json").write_text(json.dumps({"project_id": "p1"}), encoding="utf-8")
            result = run_cli("project-continue", "p1", "--runtime-root", str(runtime))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("daemon is not running", result.stderr)


if __name__ == "__main__":
    unittest.main()
