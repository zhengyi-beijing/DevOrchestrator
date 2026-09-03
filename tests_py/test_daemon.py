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
    return subprocess.run(
        [sys.executable, "-m", "dev_orchestrator", *args], cwd=ROOT,
        env=env, text=True, capture_output=True, timeout=12,
    )


def make_repo(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "README.txt").write_text("fixture", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "fixture"], check=True, stdout=subprocess.DEVNULL)
class DaemonLifecycleTests(unittest.TestCase):
    def test_one_daemon_pid_owns_monitor_and_web_for_two_projects(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runtime = base / "runtime"
            repo_a, repo_b = base / "a", base / "b"
            make_repo(repo_a)
            make_repo(repo_b)
            config = {"projects": [
                {
                    "project_id": "a", "repo_path": str(repo_a),
                    "conversation_binding": {"transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "A"},
                },
                {
                    "project_id": "b", "repo_path": str(repo_b),
                    "conversation_binding": {"transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "B"},
                },
            ]}
            config_path = base / "projects.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            port = free_port()

            started = run_cli(
                "start-daemon", "--config", str(config_path),
                "--runtime-root", str(runtime), "--web-root", str(ROOT / "web"),
                "--listen", "127.0.0.1", "--port", str(port), "--interval", "5",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            daemon = json.loads(started.stdout)
            self.assertEqual(daemon["state"], "running")
            daemon_pid = daemon["pid"]
            daemon_hb = json.loads((runtime / "daemon.json").read_text(encoding="utf-8"))
            monitor_hb = json.loads((runtime / "monitor.json").read_text(encoding="utf-8"))
            web_hb = json.loads((runtime / "web.json").read_text(encoding="utf-8"))
            self.assertEqual(daemon_hb["pid"], daemon_pid)
            self.assertEqual(monitor_hb["pid"], daemon_pid)
            self.assertEqual(web_hb["pid"], daemon_pid)

            status = run_cli("status-daemon", "--runtime-root", str(runtime))
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertTrue(json.loads(status.stdout)["process_alive"])

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/api/summary")
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(body["project_count"], 2)

            stopped = run_cli("stop-daemon", "--runtime-root", str(runtime))
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertEqual(json.loads(stopped.stdout)["state"], "stopped")
            after = run_cli("status-daemon", "--runtime-root", str(runtime))
            self.assertFalse(json.loads(after.stdout)["process_alive"])


if __name__ == "__main__":
    unittest.main()
