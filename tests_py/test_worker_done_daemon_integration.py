import http.client
import json
import tempfile
import time
import unittest
from pathlib import Path

from tests_py.test_daemon import ROOT, free_port, run_cli


def git(root: Path, *args: str) -> str:
    import subprocess
    proc = subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        text=True, capture_output=True,
    )
    return proc.stdout.strip()


def make_completed_repo(root: Path) -> str:
    (root / "agent").mkdir(parents=True)
    (root / "tmp" / "worker").mkdir(parents=True)
    (root / "agent" / "next.md").write_text(
        "# P4.3.4\nStatus: awaiting review\n", encoding="utf-8"
    )
    (root / "agent" / "CURRENT.md").write_text("# fixture\n", encoding="utf-8")
    (root / "tmp" / "worker" / "status.json").write_text(json.dumps({
        "state": "completed",
        "pid": 999999,
        "started_at": "2026-09-04T00:00:00Z",
        "updated_at": "2026-09-04T00:01:00Z",
        "exit_code": 0,
        "command": "dsh --profile headless next",
        "model": "fixture",
    }), encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


def claim(port: int, binding_id: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    body = json.dumps({"adapter": "chatgpt_web", "binding_id": binding_id})
    conn.request("POST", "/v1/claim", body=body, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, (json.loads(raw) if raw else None)

class WorkerDoneDaemonIntegrationTests(unittest.TestCase):
    def test_completed_worker_is_dispatched_by_same_pid_daemon(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); runtime = base / "runtime"; repo = base / "repo"
            head = make_completed_repo(repo)
            config = base / "projects.json"
            config.write_text(json.dumps({"projects": [{
                "project_id": "p1",
                "repo_path": str(repo),
                "worker_runtime": "tmp/worker",
                "conversation_binding": {
                    "transport": "browser_bridge",
                    "adapter": "chatgpt_web",
                    "binding_id": "conv-p1",
                },
            }]}), encoding="utf-8")
            web_port, bridge_port = free_port(), free_port()
            common = ["--runtime-root", str(runtime)]
            started = run_cli(
                "start-daemon", "--config", str(config),
                "--web-root", str(ROOT / "web"),
                "--listen", "127.0.0.1", "--port", str(web_port),
                "--bridge-listen", "127.0.0.1", "--bridge-port", str(bridge_port),
                "--interval", "5", *common,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            try:
                pid = json.loads(started.stdout)["pid"]
                daemon = json.loads((runtime / "daemon.json").read_text(encoding="utf-8"))
                monitor = json.loads((runtime / "monitor.json").read_text(encoding="utf-8"))
                web = json.loads((runtime / "web.json").read_text(encoding="utf-8"))
                bridge = json.loads((runtime / "bridge.json").read_text(encoding="utf-8"))
                self.assertEqual({pid, daemon["pid"], monitor["pid"], web["pid"], bridge["pid"]}, {pid})

                ledger = json.loads((runtime / "dispatcher-state.json").read_text(encoding="utf-8"))
                occurrence = next(iter(ledger["worker_done"]["p1"]["occurrences"].values()))
                self.assertEqual(occurrence["state"], "prepared")
                self.assertEqual(occurrence["delivery_state"], "unbound")

                status0, payload0 = claim(bridge_port, "conv-p1")
                self.assertEqual(status0, 204)
                self.assertIsNone(payload0)

                deadline = time.time() + 8
                status, payload = 204, None
                while time.time() < deadline and status == 204:
                    time.sleep(0.25)
                    status, payload = claim(bridge_port, "conv-p1")
                self.assertEqual(status, 200)
                self.assertEqual(payload["project_id"], "p1")
                self.assertEqual(payload["event"], "worker_done")
                self.assertEqual(payload["role"], "reviewer")
                self.assertEqual(payload["task_id"], "P4.3.4")
                self.assertEqual(payload["head"], head)
                self.assertIn("WORKER_DONE", payload["prompt"])

                status2, payload2 = claim(bridge_port, "conv-p1")
                self.assertEqual(status2, 204)
                self.assertIsNone(payload2)
            finally:
                stopped = run_cli("stop-daemon", *common)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)


if __name__ == "__main__":
    unittest.main()
