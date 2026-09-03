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


if __name__ == "__main__":
    unittest.main()
