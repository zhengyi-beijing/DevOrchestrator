import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.monitor.project import run_monitor_once


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class AgentFilesTaskIdFallbackTests(unittest.TestCase):
    def test_task_id_falls_back_to_full_next_document(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            (repo / "agent").mkdir(parents=True)
            (repo / "tmp" / "worker-dsh").mkdir(parents=True)
            (repo / "agent" / "next.md").write_text(
                "# NEXT — LabDemo\n\nSTATUS: **P5.1 REVIEWER REMEDIATION / ONE TASK AUTHORIZED**\n",
                encoding="utf-8",
            )
            status = {
                "state": "completed", "pid": 999999,
                "started_at": "2026-09-04T06:59:34Z",
                "updated_at": "2026-09-04T07:10:00Z",
                "exit_code": 0, "command": "dsh --profile headless next",
            }
            (repo / "tmp" / "worker-dsh" / "status.json").write_text(
                json.dumps(status), encoding="utf-8"
            )
            git(repo, "init"); git(repo, "config", "user.email", "test@example.invalid")
            git(repo, "config", "user.name", "Test"); git(repo, "add", ".")
            git(repo, "commit", "-m", "fixture")
            config = base / "projects.json"
            config.write_text(json.dumps({"projects": [{
                "project_id": "labdemo", "repo_path": str(repo),
                "worker_runtime": "tmp/worker-dsh",
            }]}), encoding="utf-8")
            summary = run_monitor_once(config, runtime)
            self.assertEqual(summary["projects"][0]["telemetry"]["task_id"], "P5.1")


if __name__ == "__main__":
    unittest.main()