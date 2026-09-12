import json
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.adapters.agent_files import AgentFilesAdapter
from dev_orchestrator.core.watchdog import (
    WATCHDOG_MILESTONES,
    collect_progress_signals,
    is_watchdog_owned_path,
)


class WatchdogSelfExclusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.repo_dir = self.root / "repo"
        self.runtime_dir = self.repo_dir / ".devorch"
        self.repo_dir.mkdir(parents=True)
        self.runtime_dir.mkdir(parents=True)
        self.runs_file = self.runtime_dir / "runs.jsonl"

        (self.repo_dir / "agent").mkdir(parents=True)
        (self.repo_dir / "agent" / "next.md").write_text("# P1.0 Task\nStatus: RUNNING", encoding="utf-8")
        (self.repo_dir / "agent" / "CURRENT.md").write_text("- P1.0 RUNNING", encoding="utf-8")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_watchdog_milestones_and_source_self_excluded(self):
        """Milestones in WATCHDOG_MILESTONES and source=watchdog do not count as progress."""
        history_dir = self.runtime_dir / "history"
        history_dir.mkdir(parents=True)
        progress_file = history_dir / "progress.json"

        # Write progress notifications: one legitimate earlier event and several watchdog events with newer timestamps
        progress_data = {
            "notifications": [
                {
                    "notification_id": "legit-1",
                    "project_id": "test-p",
                    "milestone": "TASK_STARTED",
                    "timestamp": "2026-09-12T10:00:00Z",
                    "details": {"source": "worker"},
                },
                {
                    "notification_id": "wd-stall",
                    "project_id": "test-p",
                    "milestone": "STALL_DETECTED",
                    "timestamp": "2026-09-12T10:30:00Z",
                    "details": {"source": "watchdog"},
                },
                {
                    "notification_id": "wd-diag",
                    "project_id": "test-p",
                    "milestone": "DIAGNOSTIC_RESULT",
                    "timestamp": "2026-09-12T10:31:00Z",
                    "details": {"source": "watchdog"},
                },
                {
                    "notification_id": "wd-gate",
                    "project_id": "test-p",
                    "milestone": "OWNER_GATE",
                    "timestamp": "2026-09-12T10:32:00Z",
                    "details": {"source": "watchdog"},
                },
            ]
        }
        progress_file.write_text(json.dumps(progress_data), encoding="utf-8")

        adapter = AgentFilesAdapter(runtime_root=self.runtime_dir)
        project = {
            "project_id": "test-p",
            "repo_path": str(self.repo_dir),
            "worker_runtime": ".devorch",
        }
        snapshot = adapter.snapshot(project, self.runs_file)
        last_prog_at, fp, signals = collect_progress_signals(snapshot, self.runtime_dir)

        # The progress entry MUST be the legit earlier event, NOT the watchdog events!
        prog_entry = signals.get("progress_entry")
        self.assertIsNotNone(prog_entry)
        self.assertEqual(prog_entry.get("id"), "legit-1")
        self.assertEqual(prog_entry.get("milestone"), "TASK_STARTED")
        self.assertEqual(prog_entry.get("timestamp"), "2026-09-12T10:00:00Z")

    def test_status_mirrors_and_commands_self_excluded(self):
        """Status mirror, corrupt files, and wd-* command files never qualify as progress."""
        inbox_dir = self.runtime_dir / "control" / "inbox"
        inbox_dir.mkdir(parents=True)
        wd_cmd = inbox_dir / "wd-att001.json"
        wd_cmd.write_text('{"action": "continue"}', encoding="utf-8")

        corrupt_file = self.runtime_dir / "watchdog.json.corrupt-20260912"
        corrupt_file.write_text("corrupt", encoding="utf-8")

        status_mirror = self.runtime_dir / "status.json"
        status_mirror.write_text('{"phase": "actuation"}', encoding="utf-8")

        # Verify path ownership checks
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, wd_cmd, runtime_root=self.runtime_dir))
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, corrupt_file, runtime_root=self.runtime_dir))
        self.assertTrue(is_watchdog_owned_path(self.repo_dir, status_mirror, runtime_root=self.runtime_dir))

        adapter = AgentFilesAdapter(runtime_root=self.runtime_dir)
        project = {
            "project_id": "test-p",
            "repo_path": str(self.repo_dir),
            "worker_runtime": ".devorch",
        }
        snapshot = adapter.snapshot(project, self.runs_file)
        safe = snapshot["activity"]["watchdog_safe"]

        # None of these watchdog-owned files should appear in safe sources
        safe_paths = {s.get("path") for s in safe.get("sources", {}).values()}
        for p in (wd_cmd, corrupt_file, status_mirror):
            self.assertNotIn(str(p), safe_paths)


if __name__ == "__main__":
    unittest.main()
