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


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_cli(*args):
    env = dict(os.environ); env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "dev_orchestrator", *args], cwd=ROOT,
        env=env, text=True, capture_output=True, timeout=12,
    )


class ControlDaemonTests(unittest.TestCase):
    def test_daemon_owns_control_surface_in_same_pid(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); runtime = base / "runtime"; repo = base / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
            (repo / "seed.txt").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            config = base / "projects.json"
            config.write_text(json.dumps({"projects": [{
                "project_id": "p1", "repo_path": str(repo),
                "conversation_binding": {"transport": "browser_bridge", "adapter": "chatgpt_web", "binding_id": "conv-p1"}
            }]}), encoding="utf-8")
            web_port, bridge_port, control_port = free_port(), free_port(), free_port()
            common = ["--runtime-root", str(runtime)]
            started = run_cli(
                "start-daemon", "--config", str(config), "--web-root", str(ROOT / "web"),
                "--listen", "127.0.0.1", "--port", str(web_port),
                "--bridge-listen", "127.0.0.1", "--bridge-port", str(bridge_port),
                "--control-listen", "127.0.0.1", "--control-port", str(control_port),
                "--interval", "5", *common,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            try:
                pid = json.loads(started.stdout)["pid"]
                control = json.loads((runtime / "control.json").read_text(encoding="utf-8"))
                daemon = json.loads((runtime / "daemon.json").read_text(encoding="utf-8"))
                self.assertEqual(control["pid"], pid)
                self.assertEqual(daemon["pid"], pid)
                self.assertEqual(control["port"], control_port)
                self.assertEqual(daemon["control_port"], control_port)
                conn = http.client.HTTPConnection("127.0.0.1", control_port, timeout=2)
                conn.request("GET", "/v1/health")
                response = conn.getresponse(); body = json.loads(response.read()); conn.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(body["surface"], "conversation-control-v1")
            finally:
                stopped = run_cli("stop-daemon", *common)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)


if __name__ == "__main__":
    unittest.main()
