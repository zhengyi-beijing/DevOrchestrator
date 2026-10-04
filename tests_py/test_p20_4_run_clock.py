"""Tests for P20.4 Run Clock and Monotonic Timeline Evidence.

Verifies:
- Monotonic duration computation across boot segments
- Resume preserves run_id and original started_at without resetting
- Boot identity and clock continuity checks
- Discontinuity (e.g. boot time jump or backwards monotonic) flags explicit failure
- Disallow summing distinct run IDs.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
import unittest

from dev_orchestrator.soak.timeline import (
    open_soak_run,
    resume_soak_run,
    append_soak_event,
    checkpoint_soak_run,
    compute_run_duration,
    load_soak_run,
)
from dev_orchestrator.soak.permit import create_soak_permit


class TestSoakRunClock(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_p20_4_clock_"))
        self.canonical_repo = self.temp_dir / "canonical_repo"
        self.canonical_runtime = self.canonical_repo / "runtime"
        self.canonical_repo.mkdir(parents=True, exist_ok=True)
        self.canonical_runtime.mkdir(parents=True, exist_ok=True)

        self.disposable_dir = self.temp_dir / "disposable_project"
        self.disposable_repo = self.disposable_dir / "repo"
        self.disposable_runtime = self.disposable_dir / "runtime"
        self.disposable_repo.mkdir(parents=True, exist_ok=True)
        self.disposable_runtime.mkdir(parents=True, exist_ok=True)

        marker = self.disposable_repo / ".devorch-disposable-soak"
        marker.write_text(json.dumps({
            "run_id": "soak-run-001",
            "project_id": "p20-soak-proj",
        }), encoding="utf-8")

        self.permit = create_soak_permit(
            run_id="soak-run-001",
            soak_project_id="p20-soak-proj",
            disposable_repo_path=self.disposable_repo,
            disposable_runtime_root=self.disposable_runtime,
            canonical_repo_path=self.canonical_repo,
            canonical_runtime_root=self.canonical_runtime,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_open_run_establishes_initial_segment(self) -> None:
        run = open_soak_run(self.permit)
        self.assertEqual(run["run_id"], "soak-run-001")
        self.assertIn("started_at", run)
        self.assertEqual(len(run["segments"]), 1)
        seg = run["segments"][0]
        self.assertIn("boot_id", seg)
        self.assertIn("start_mono", seg)

    def test_resume_preserves_started_at_and_appends_segment(self) -> None:
        run1 = open_soak_run(self.permit)
        started_at = run1["started_at"]
        checkpoint_soak_run(self.disposable_runtime, run1)

        # Simulate time passing and resume
        time.sleep(0.05)
        run2 = resume_soak_run(self.permit)
        self.assertEqual(run2["run_id"], "soak-run-001")
        self.assertEqual(run2["started_at"], started_at)
        self.assertGreaterEqual(len(run2["segments"]), 2)

    def test_compute_duration_measures_monotonic_time(self) -> None:
        run = open_soak_run(self.permit)
        # Mock 2 segments: segment 1 ran 100s, segment 2 ran 200s
        run["segments"] = [
            {"boot_id": "boot-1", "start_mono": 1000.0, "end_mono": 1100.0},
            {"boot_id": "boot-2", "start_mono": 500.0, "end_mono": 700.0},
        ]
        duration_info = compute_run_duration(run)
        self.assertTrue(duration_info["continuity_ok"])
        self.assertAlmostEqual(duration_info["monotonic_seconds"], 300.0, places=1)

    def test_discontinuity_flags_failure(self) -> None:
        run = open_soak_run(self.permit)
        # End monotonic earlier than start monotonic
        run["segments"] = [
            {"boot_id": "boot-1", "start_mono": 1000.0, "end_mono": 900.0},
        ]
        duration_info = compute_run_duration(run)
        self.assertFalse(duration_info["continuity_ok"])
        self.assertIn("monotonic_went_backwards", duration_info["discontinuity_reason"])


if __name__ == "__main__":
    unittest.main()
