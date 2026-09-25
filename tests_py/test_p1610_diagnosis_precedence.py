"""Tests verifying that P16.7-P16.9 failure diagnoses take precedence over orchestrator_alive_task_stalled."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from dev_orchestrator.core.diagnostics import classify_evidence
from dev_orchestrator.core.watchdog import evaluate_stall, StallAssessment


class TestDiagnosisPrecedence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "test-proj"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ready_to_run_unlaunched_preempts_orchestrator_alive_task_stalled(self):
        evidence = {
            "orchestrator_alive_task_stalled": True,
            "process_liveness": {"process_alive": False, "pid": None},
        }
        assessment = StallAssessment(
            monitored=True,
            lifecycle_state="READY_TO_RUN",
            task_id="t1",
            run_key="rk1",
            run_scope_key="rsk1",
            no_progress_seconds=600.0,
            threshold_seconds=300.0,
            breached=True,
            progress_fingerprint="fp1",
            activity_evidence="available",
            active_execution=False,
            recovery_epoch=None,
            last_progress_at="2026-09-24T12:00:00Z",
            stall_classification="orchestrator_alive_task_stalled",
        )
        diag = classify_evidence(evidence, assessment)
        self.assertEqual(diag.code, "ready_to_run_unlaunched")

    def test_reviewer_failed_preempts_orchestrator_alive_task_stalled(self):
        evidence = {
            "orchestrator_alive_task_stalled": True,
            "ledgers": {
                "ai-reviewer.json": {
                    "state": "failed",
                    "reason": "reviewer crashed",
                }
            },
        }
        assessment = StallAssessment(
            monitored=True,
            lifecycle_state="REVIEW_FAILED",
            task_id="t1",
            run_key="rk1",
            run_scope_key="rsk1",
            no_progress_seconds=600.0,
            threshold_seconds=300.0,
            breached=True,
            progress_fingerprint="fp1",
            activity_evidence="available",
            active_execution=False,
            recovery_epoch=None,
            last_progress_at="2026-09-24T12:00:00Z",
            stall_classification="orchestrator_alive_task_stalled",
        )
        diag = classify_evidence(evidence, assessment)
        self.assertEqual(diag.code, "reviewer_failed")

    def test_planner_failed_preempts_orchestrator_alive_task_stalled(self):
        evidence = {
            "orchestrator_alive_task_stalled": True,
            "ledgers": {
                "ai-planner.json": {
                    "state": "failed",
                    "reason": "planner failed after timeout",
                }
            },
        }
        assessment = StallAssessment(
            monitored=True,
            lifecycle_state="PLAN_FAILED",
            task_id="t1",
            run_key="rk1",
            run_scope_key="rsk1",
            no_progress_seconds=600.0,
            threshold_seconds=300.0,
            breached=True,
            progress_fingerprint="fp1",
            activity_evidence="available",
            active_execution=False,
            recovery_epoch=None,
            last_progress_at="2026-09-24T12:00:00Z",
            stall_classification="orchestrator_alive_task_stalled",
        )
        diag = classify_evidence(evidence, assessment)
        self.assertEqual(diag.code, "planner_failed")

    def test_process_dead_preempts_orchestrator_alive_task_stalled(self):
        evidence = {
            "orchestrator_alive_task_stalled": True,
            "process_liveness": {"process_alive": False, "pid": 99999},
        }
        assessment = StallAssessment(
            monitored=True,
            lifecycle_state="EXECUTING",
            task_id="t1",
            run_key="rk1",
            run_scope_key="rsk1",
            no_progress_seconds=600.0,
            threshold_seconds=300.0,
            breached=True,
            progress_fingerprint="fp1",
            activity_evidence="available",
            active_execution=True,
            recovery_epoch=None,
            last_progress_at="2026-09-24T12:00:00Z",
            stall_classification="orchestrator_alive_task_stalled",
        )
        diag = classify_evidence(evidence, assessment)
        self.assertEqual(diag.code, "process_dead")

    def test_agent_stalled_preempts_orchestrator_alive_task_stalled(self):
        evidence = {
            "orchestrator_alive_task_stalled": True,
            "process_liveness": {"process_alive": True, "pid": 12345},
        }
        assessment = StallAssessment(
            monitored=True,
            lifecycle_state="EXECUTING",
            task_id="t1",
            run_key="rk1",
            run_scope_key="rsk1",
            no_progress_seconds=600.0,
            threshold_seconds=300.0,
            breached=True,
            progress_fingerprint="fp1",
            activity_evidence="available",
            active_execution=True,
            recovery_epoch=None,
            last_progress_at="2026-09-24T12:00:00Z",
            stall_classification="orchestrator_alive_task_stalled",
        )
        diag = classify_evidence(evidence, assessment)
        self.assertEqual(diag.code, "agent_stalled")

    def test_orchestrator_alive_task_stalled_fires_when_no_prior_diagnosis(self):
        evidence = {
            "orchestrator_alive_task_stalled": True,
            "process_liveness": {"process_alive": False, "pid": None},
        }
        assessment = StallAssessment(
            monitored=True,
            lifecycle_state="UNKNOWN_STAGE",
            task_id="t1",
            run_key="rk1",
            run_scope_key="rsk1",
            no_progress_seconds=600.0,
            threshold_seconds=300.0,
            breached=True,
            progress_fingerprint="fp1",
            activity_evidence="available",
            active_execution=False,
            recovery_epoch=None,
            last_progress_at="2026-09-24T12:00:00Z",
            stall_classification="orchestrator_alive_task_stalled",
        )
        diag = classify_evidence(evidence, assessment)
        self.assertEqual(diag.code, "orchestrator_alive_task_stalled")


if __name__ == "__main__":
    unittest.main()
