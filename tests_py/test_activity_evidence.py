import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dev_orchestrator.adapters.agent_files import AgentFilesAdapter
from dev_orchestrator.core.watchdog import (
    build_progress_fingerprint,
    canonical_path,
    collect_progress_signals,
    is_watchdog_owned_path,
)
from dev_orchestrator.monitor.telemetry import worker_telemetry


class ActivityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.repo_dir = self.root / "repo"
        self.runtime_dir = self.root / "runtime"
        self.repo_dir.mkdir(parents=True)
        self.runtime_dir.mkdir(parents=True)
        self.runs_file = self.runtime_dir / "runs.jsonl"

        # Setup basic project repo structure
        (self.repo_dir / "agent").mkdir(parents=True)
        (self.repo_dir / ".devorch").mkdir(parents=True)
        (self.repo_dir / "agent" / "next.md").write_text("# P1.0 Task 1\nStatus: READY_TO_RUN", encoding="utf-8")
        (self.repo_dir / "agent" / "CURRENT.md").write_text("- P1.0 RUNNING", encoding="utf-8")
        (self.repo_dir / ".devorch" / "status.json").write_text('{"phase": "monitor"}', encoding="utf-8")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_mirror_loop_rehearsal_unfiltered_vs_watchdog_safe(self):
        """Touching .devorch/status.json advances unfiltered legacy activity/telemetry
        but leaves watchdog_safe and progress_fingerprint unchanged."""
        project = {
            "project_id": "test-proj",
            "repo_path": str(self.repo_dir),
            "worker_runtime": ".devorch",
            "watchdog": {"enabled": True},
        }
        adapter = AgentFilesAdapter(runtime_root=self.runtime_dir)

        # Baseline collection
        status1 = adapter.snapshot(project, self.runs_file)
        act1 = status1.get("activity") or {}
        self.assertIn("watchdog_safe", act1)
        safe1 = act1["watchdog_safe"]
        legacy_last_1 = act1.get("last_activity_at")
        safe_last_1 = safe1.get("last_activity_at")

        last_prog_1, fp1, signals1 = collect_progress_signals(status1, self.runtime_dir)
        self.assertEqual(signals1.get("activity_evidence"), "available")
        self.assertTrue(len(fp1) >= 16)

        # Rehearsal: wait a small moment and update ONLY .devorch/status.json (status mirror write)
        time.sleep(0.05)
        now_iso = datetime.now(timezone.utc).isoformat()
        (self.repo_dir / ".devorch" / "status.json").write_text(
            json.dumps({"phase": "actuation", "written_at": now_iso}),
            encoding="utf-8",
        )

        status2 = adapter.snapshot(project, self.runs_file)
        act2 = status2.get("activity") or {}
        safe2 = act2.get("watchdog_safe") or {}
        legacy_last_2 = act2.get("last_activity_at")
        safe_last_2 = safe2.get("last_activity_at")

        # Legacy activity MUST advance because .devorch/status.json was touched
        self.assertNotEqual(legacy_last_1, legacy_last_2)

        # Watchdog-safe activity MUST NOT advance because .devorch/status.json is self-excluded!
        self.assertEqual(safe_last_1, safe_last_2)

        last_prog_2, fp2, signals2 = collect_progress_signals(status2, self.runtime_dir)

        # The progress fingerprint MUST be byte-for-byte identical!
        self.assertEqual(fp1, fp2)
        self.assertEqual(last_prog_1, last_prog_2)

    def test_real_worker_file_updates_watchdog_safe_and_fingerprint(self):
        """Updating an actual worker file advances both legacy and watchdog_safe."""
        project = {
            "project_id": "test-proj",
            "repo_path": str(self.repo_dir),
            "worker_runtime": ".devorch",
            "watchdog": {"enabled": True},
        }
        adapter = AgentFilesAdapter(runtime_root=self.runtime_dir)

        status1 = adapter.snapshot(project, self.runs_file)
        safe1 = status1["activity"]["watchdog_safe"]
        last_prog_1, fp1, signals1 = collect_progress_signals(status1, self.runtime_dir)

        time.sleep(0.05)
        # Update worker progress file
        (self.repo_dir / "agent" / "next.md").write_text("# P1.0 Task 1 (updated)\nStatus: RUNNING", encoding="utf-8")

        status2 = adapter.snapshot(project, self.runs_file)
        safe2 = status2["activity"]["watchdog_safe"]
        self.assertNotEqual(safe1["last_activity_at"], safe2["last_activity_at"])

        last_prog_2, fp2, signals2 = collect_progress_signals(status2, self.runtime_dir)
        # Fingerprint MUST change
        self.assertNotEqual(fp1, fp2)

    def test_dual_provenance_fingerprints(self):
        """watchdog_safe carries dual provenance verification fingerprints."""
        project = {
            "project_id": "test-proj",
            "repo_path": str(self.repo_dir),
            "worker_runtime": ".devorch",
        }
        adapter = AgentFilesAdapter(runtime_root=self.runtime_dir)
        status = adapter.snapshot(project, self.runs_file)
        safe = status["activity"]["watchdog_safe"]

        self.assertIn("repo_root_fingerprint", safe)
        self.assertIn("runtime_root_fingerprint", safe)
        self.assertTrue(len(safe["repo_root_fingerprint"]) >= 16)
        self.assertTrue(len(safe["runtime_root_fingerprint"]) >= 16)

    def test_telemetry_additive_field(self):
        """worker_telemetry emits watchdog_safe_activity_age_seconds additively."""
        now = datetime.now(timezone.utc)
        runs = self.runtime_dir / "runs.jsonl"
        running = {
            "kind": "task",
            "state": "running",
            "pid": 12345,
            "started_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        info = worker_telemetry(
            {"id": "test-proj"},
            running,
            "P1.0",
            now.isoformat(),
            runs,
            now=now,
            watchdog_safe_activity_at=now.isoformat(),
        )
        self.assertIn("last_activity_age_seconds", info)
        self.assertIn("watchdog_safe_activity_age_seconds", info)
        self.assertEqual(info["last_activity_age_seconds"], 0.0)
        self.assertEqual(info["watchdog_safe_activity_age_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
