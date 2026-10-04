"""Tests for P20.4 Qualification Verifier.

Verifies:
- Duration gate: >= 21600 seconds continuous with continuity proof
- Task gate: >= 5 accepted tasks with Planner, Worker commit, Reviewer correlation
- Fault gate: 15/15 fault dispositions with honest mode labels
- CPF gate: 10/10 declared CPF scenarios converging to expectation
- Hard-zero metrics (manual continue, duplicate worker/job/handoff/successor, etc.)
- Explicit denominators and fail-closed reporting on UNKNOWN or discontinuity.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
import unittest

from dev_orchestrator.soak.qualification import evaluate_soak_qualification
from dev_orchestrator.soak.permit import create_soak_permit, save_soak_permit
from dev_orchestrator.soak.timeline import open_soak_run, checkpoint_soak_run


class TestSoakQualification(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_p20_4_qual_"))
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
        self.permit_path = self.disposable_dir / "soak-permit.json"
        save_soak_permit(self.permit_path, self.permit)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_qualification_fails_when_duration_below_21600s(self) -> None:
        run = open_soak_run(self.permit)
        run["segments"] = [
            {"boot_id": "boot-1", "start_mono": 1000.0, "end_mono": 1100.0},  # only 100s
        ]
        checkpoint_soak_run(self.disposable_runtime, run)

        report = evaluate_soak_qualification(self.canonical_runtime, self.permit)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertIn("duration_gate_failed", report["reasons"])
        self.assertLess(report["duration"]["monotonic_seconds"], 21600.0)

    def test_qualification_fails_when_tasks_under_5(self) -> None:
        run = open_soak_run(self.permit)
        run["segments"] = [
            {"boot_id": "boot-1", "start_mono": 1000.0, "end_mono": 23000.0},  # 22000s
        ]
        run["completed_tasks"] = ["SOAK-T1", "SOAK-T2"]  # only 2 tasks
        checkpoint_soak_run(self.disposable_runtime, run)

        report = evaluate_soak_qualification(self.canonical_runtime, self.permit)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertIn("tasks_gate_failed", report["reasons"])
        self.assertEqual(report['tasks']['completed_count'], 0)
        self.assertEqual(report['tasks']['declared_count'], 2)

    def test_qualification_fails_on_clock_discontinuity(self) -> None:
        run = open_soak_run(self.permit)
        run["segments"] = [
            {"boot_id": "boot-1", "start_mono": 2000.0, "end_mono": 1000.0},  # backwards
        ]
        run["completed_tasks"] = [f"SOAK-T{i}" for i in range(1, 6)]
        checkpoint_soak_run(self.disposable_runtime, run)

        report = evaluate_soak_qualification(self.canonical_runtime, self.permit)
        self.assertEqual(report["verdict"], "FAIL")
        self.assertIn("clock_discontinuity", report["reasons"])

    def test_fabricated_labels_and_clock_never_satisfy_live_gates(self) -> None:
        run = open_soak_run(self.permit)
        run["segments"] = [
            {"boot_id": "boot-1", "start_mono": 1000.0, "end_mono": 23000.0},  # 22000s
        ]
        run["completed_tasks"] = [f"SOAK-T{i}" for i in range(1, 6)]
        run["task_evidence"] = {
            f"SOAK-T{i}": {
                "planner_id": f"plan-t{i}",
                "worker_commit": f"commit-t{i}",
                "reviewer_verdict": "ACCEPT",
                "reviewer_id": f"rev-t{i}",
            }
            for i in range(1, 6)
        }
        run["fault_dispositions"] = {
            f"F{i:02d}": {"disposition": "automatic_recovery", "mode": "deterministic_test"}
            for i in range(1, 16)
        }
        run["cpf_convergences"] = {
            f"CPF-{i:02d}": {"outcome": "converged", "expected": "declared"}
            for i in range(1, 11)
        }
        run["hard_zeros"] = {
            "manual_continue": 0,
            "lifecycle_repair": 0,
            "duplicate_worker": 0,
            "duplicate_job": 0,
            "duplicate_handoff": 0,
            "duplicate_successor": 0,
            "wrong_task_execution": 0,
            "lifecycle_invariant_escape": 0,
            "silent_stall": 0,
            "infinite_retry": 0,
            "restart_regression": 0,
        }
        checkpoint_soak_run(self.disposable_runtime, run)

        report = evaluate_soak_qualification(self.canonical_runtime, self.permit)
        self.assertEqual(report['verdict'], 'FAIL')
        self.assertIn('clock_discontinuity', report['reasons'])
        self.assertTrue(any('uncorrelated_task' in reason for reason in report['reasons']))


if __name__ == "__main__":
    unittest.main()
