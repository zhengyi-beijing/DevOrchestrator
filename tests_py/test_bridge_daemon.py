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
    return subprocess.run([sys.executable, "-m", "dev_orchestrator", *args], cwd=ROOT, env=env, text=True, capture_output=True, timeout=12)


class BridgeDaemonTests(unittest.TestCase):
    def test_daemon_owns_bridge_monitor_and_dashboard_in_one_pid(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); runtime = base / "runtime"
            repo = base / "repo"; repo.mkdir()
            subprocess.run(["git","-C",str(repo),"init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git","-C",str(repo),"config","user.email","t@example.invalid"], check=True)
            subprocess.run(["git","-C",str(repo),"config","user.name","T"], check=True)
            (repo / "seed.txt").write_text("x", encoding="utf-8")
            subprocess.run(["git","-C",str(repo),"add","."], check=True)
            subprocess.run(["git","-C",str(repo),"commit","-m","seed"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            config = base / "projects.json"
            config.write_text(json.dumps({"projects":[{
                "project_id":"p1", "repo_path":str(repo), "worker_runtime":"tmp/worker",
                "conversation_binding":{"transport":"browser_bridge","adapter":"chatgpt_web","binding_id":"conv-p1"}
            }]}), encoding="utf-8")
            web_port, bridge_port = free_port(), free_port()
            common = ["--runtime-root",str(runtime)]
            started = run_cli("start-daemon", "--config",str(config), "--web-root",str(ROOT / "web"),
                              "--listen","127.0.0.1", "--port",str(web_port),
                              "--bridge-listen","127.0.0.1", "--bridge-port",str(bridge_port),
                              "--interval","5", *common)
            self.assertEqual(started.returncode, 0, started.stderr)
            try:
                pid = json.loads(started.stdout)["pid"]
                daemon = json.loads((runtime / "daemon.json").read_text(encoding="utf-8"))
                monitor = json.loads((runtime / "monitor.json").read_text(encoding="utf-8"))
                web = json.loads((runtime / "web.json").read_text(encoding="utf-8"))
                bridge = json.loads((runtime / "bridge.json").read_text(encoding="utf-8"))
                self.assertEqual({pid, daemon["pid"], monitor["pid"], web["pid"], bridge["pid"]}, {pid})
                self.assertEqual(bridge["port"], bridge_port)
            finally:
                stopped = run_cli("stop-daemon", *common)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)


if __name__ == "__main__":
    unittest.main()
