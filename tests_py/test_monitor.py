import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.monitor.project import run_monitor_once


def digest_tree(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        h.update(path.relative_to(root).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class MonitorIntegrationTests(unittest.TestCase):
    def test_one_shot_monitor_is_read_only_and_writes_compatible_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            project = base / "project"
            runtime = base / "runtime"
            (project / "agent").mkdir(parents=True)
            (project / "tmp" / "worker-dsh").mkdir(parents=True)
            (project / "agent" / "next.md").write_text("# P4.3.3 ACCEPTED\nStatus: ACCEPTED / awaiting-next-phase\n", encoding="utf-8")
            (project / "agent" / "CURRENT.md").write_text("- P4.3.4 NOT STARTED\n", encoding="utf-8")
            status = {
                "state": "completed",
                "pid": 1234,
                "started_at": "2026-09-03T03:51:34Z",
                "updated_at": "2026-09-03T04:05:13Z",
                "exit_code": 0,
                "command": "dsh --profile headless next",
                "model": "profile:headless",
            }
            (project / "tmp" / "worker-dsh" / "status.json").write_text(json.dumps(status), encoding="utf-8")
            git(project, "init")
            git(project, "config", "user.email", "test@example.invalid")
            git(project, "config", "user.name", "Test")
            git(project, "add", ".")
            git(project, "commit", "-m", "fixture")

            config = {
                "projects": [{
                    "id": "labdemo",
                    "name": "LabDemo",
                    "root": str(project),
                    "worker_runtime": "tmp/worker-dsh",
                    "eta": {
                        "default_worker_minutes": {"min": 30, "max": 90},
                        "historical_min_samples": 3,
                        "stall_warning_minutes": 15,
                        "hard_timeout_minutes": 180,
                        "task_overrides": [],
                    },
                }]
            }
            config_path = base / "projects.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            before = digest_tree(project)
            summary = run_monitor_once(config_path, runtime)
            after = digest_tree(project)

            self.assertEqual(before, after)
            self.assertEqual(summary["project_count"], 1)
            snap = summary["projects"][0]
            self.assertEqual(snap["id"], "labdemo")
            self.assertEqual(snap["state"], "WAITING_PHASE_GATE")
            self.assertEqual(snap["git"]["dirty"], False)
            self.assertEqual(snap["worker"]["state"], "completed")
            self.assertEqual(snap["telemetry"]["task_id"], "P4.3.3")
            self.assertTrue((runtime / "summary.json").is_file())
            self.assertTrue((runtime / "projects" / "labdemo.json").is_file())
            runs = (runtime / "history" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(runs), 1)

            run_monitor_once(config_path, runtime)
            runs2 = (runtime / "history" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(runs2), 1, "terminal run history must be idempotent")


if __name__ == "__main__":
    unittest.main()
