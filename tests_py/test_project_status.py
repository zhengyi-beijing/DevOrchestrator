import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.core.project_status import (
    write_execution_status,
    write_project_status,
)


def make_repo(root: Path) -> str:
    root.mkdir(parents=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "fixture"], check=True, stdout=subprocess.DEVNULL)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


class ProjectStatusTests(unittest.TestCase):
    def test_project_local_status_is_atomic_and_git_clean(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            head = make_repo(repo); runtime.mkdir()
            snapshot = {
                "project_id": "p1", "repo_path": str(repo), "state": "READY_TO_RUN",
                "orchestration_ready": True, "next_title": "P1 bounded task",
                "telemetry": {"task_id": "P1", "run_id": None},
                "worker": {"kind": "none", "state": "not_started", "process_alive": False},
                "git": {"branch": "master", "head": head, "dirty": False, "changed_entries": 0},
            }
            target = write_project_status(
                snapshot, runtime, phase="monitor", daemon_state="running", pid=123,
            )
            self.assertEqual(target, repo / ".devorch" / "status.json")
            status = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(status["project_id"], "p1")
            self.assertEqual(status["phase"], "monitor")
            self.assertEqual(status["task_id"], "P1")
            self.assertEqual(status["git"]["head"], head)
            porcelain = subprocess.check_output(
                ["git", "-C", str(repo), "status", "--porcelain"], text=True
            ).strip()
            self.assertEqual(porcelain, "")
            exclude = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"], text=True
            ).strip()
            exclude_path = Path(exclude) if Path(exclude).is_absolute() else repo / exclude
            self.assertIn(".devorch/", exclude_path.read_text(encoding="utf-8"))

    def test_execution_state_updates_status_without_waiting_for_monitor_tick(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); repo = base / "repo"; runtime = base / "runtime"
            make_repo(repo); runtime.mkdir()
            record = {
                "project_id": "p1", "repo_path": str(repo),
                "source_request_id": "owner-p1", "source_kind": "bootstrap",
                "task_id": "P1", "backend_id": "agy", "state": "running",
                "pid": 42, "started_at": "2026-09-05T01:00:00+00:00",
            }
            target = write_execution_status(record, runtime)
            status = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(status["phase"], "worker")
            self.assertEqual(status["worker"]["state"], "running")
            self.assertTrue(status["worker"]["process_alive"])
            record.update({
                "state": "completed", "exit_code": 0,
                "completed_at": "2026-09-05T01:05:00+00:00",
            })
            write_execution_status(record, runtime)
            status = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(status["worker"]["state"], "completed")
            self.assertFalse(status["worker"]["process_alive"])
            self.assertEqual(status["actuation"]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
