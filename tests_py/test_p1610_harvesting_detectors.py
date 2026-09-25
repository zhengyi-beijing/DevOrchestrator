import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from dev_orchestrator.incidents.harvesting import DETECTORS, harvest_tick
from dev_orchestrator.control.command_store import ControlCommandStore


class TestHarvestingDetectors(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "test-proj"
        self.config = {"projects": [{"project_id": self.project_id}]}

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_detector_watchdog_recovery(self):
        detector = DETECTORS["watchdog_recovery"]
        # Healthy fixture
        (self.runtime_root / "watchdog.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
        self.assertEqual(detector(self.runtime_root, self.config, {}, None, None, None, None, None), [])

        # Anomalous fixture
        wd_data = {
            "projects": {
                self.project_id: {
                    "history": [
                        {
                            "diagnosis": "worker_hung_no_lineage",
                            "recovery": "kill_and_relaunch",
                            "attempt_key": "att-123",
                            "task_id": "T1",
                        }
                    ]
                }
            }
        }
        (self.runtime_root / "watchdog.json").write_text(json.dumps(wd_data), encoding="utf-8")
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][2], "WATCHDOG_RECOVERY")
        self.assertEqual(anomalies[0][5], f"wd_rec:{self.project_id}:att-123")

    def test_detector_execution_loss(self):
        detector = DETECTORS["execution_loss"]
        # Healthy
        (self.runtime_root / "execution-lineage.json").write_text(json.dumps({"records": {}}), encoding="utf-8")
        self.assertEqual(detector(self.runtime_root, self.config, {}, None, None, None, None, None), [])

        # Anomalous
        lineage_data = {
            "records": {
                "lkey-1": {
                    "project_id": self.project_id,
                    "task_id": "T1",
                    "loss_detected": True,
                    "invariant_key": "inv-ex",
                }
            }
        }
        (self.runtime_root / "execution-lineage.json").write_text(json.dumps(lineage_data), encoding="utf-8")
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][2], "ACTIONABLE_EXECUTION_LOSS")

    def test_detector_unconsumed_plan_or_review(self):
        detector = DETECTORS["unconsumed_plan_or_review"]
        # Healthy
        (self.runtime_root / "ai-reviewer.json").write_text(json.dumps({"reviews": {}}), encoding="utf-8")
        self.assertEqual(detector(self.runtime_root, self.config, {}, None, None, None, None, None), [])

        # Anomalous
        rev_data = {
            "reviews": {
                "rev-1": {
                    "project_id": self.project_id,
                    "task_id": "T1",
                    "state": "failed",
                }
            }
        }
        (self.runtime_root / "ai-reviewer.json").write_text(json.dumps(rev_data), encoding="utf-8")
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][2], "UNCONSUMED_REVIEWER_FAILURE")

    def test_detector_launch_gap(self):
        detector = DETECTORS["launch_gap"]
        # Healthy
        healthy_summary = {"projects": [{"project_id": self.project_id, "state": "EXECUTING"}]}
        self.assertEqual(detector(self.runtime_root, self.config, healthy_summary, None, None, None, None, None), [])

        # Anomalous
        anom_summary = {
            "projects": [
                {
                    "project_id": self.project_id,
                    "task_id": "T-gap",
                    "lifecycle_state": "READY_TO_RUN",
                    "worker": {"process_alive": False},
                    "active_execution": None,
                    "watchdog": {"diagnosis": "ready_to_run_unlaunched"},
                }
            ]
        }
        anomalies = detector(self.runtime_root, self.config, anom_summary, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][2], "LAUNCH_GAP")

    def test_detector_restart_reconcile_outcome_change(self):
        from dev_orchestrator.control.surface import project_identity
        detector = DETECTORS["restart_reconcile_outcome_change"]
        cmd_store = ControlCommandStore(self.runtime_root)
        expected = project_identity({"project_id": self.project_id}, self.runtime_root)
        # Healthy
        cmd_store.submit({
            "schema_version": 1,
            "command_id": "cmd-rec-1",
            "project_id": self.project_id,
            "action": "reconcile",
            "expected": expected,
            "target": {},
        }, source="test")
        self.assertEqual(detector(self.runtime_root, self.config, {}, None, None, None, None, None), [])

        # Anomalous: settled as blocked
        cmd_store.settle(cmd_store.inbox / "cmd-rec-1.json", {
            "command_id": "cmd-rec-1",
            "project_id": self.project_id,
            "action": "reconcile",
            "state": "blocked",
            "reason": "state_desync",
        })
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][2], "RECONCILE_OUTCOME_CHANGE")

    def test_detector_recovery_exhaustion_or_livelock(self):
        detector = DETECTORS["recovery_exhaustion_or_livelock"]
        # Healthy
        (self.runtime_root / "execution-intent.json").write_text(json.dumps({"intents": {}}), encoding="utf-8")
        self.assertEqual(detector(self.runtime_root, self.config, {}, None, None, None, None, None), [])

        # Anomalous
        intent_data = {
            "intents": {
                "int-1": {
                    "project_id": self.project_id,
                    "task_id": "T1",
                    "state": "exhausted",
                    "reason": "recovery_budget_exhausted",
                }
            }
        }
        (self.runtime_root / "execution-intent.json").write_text(json.dumps(intent_data), encoding="utf-8")
        anomalies = detector(self.runtime_root, self.config, {}, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][2], "RECOVERY_EXHAUSTION_OR_LIVELOCK")

    def test_detector_control_only_intervention(self):
        from dev_orchestrator.control.surface import project_identity
        detector = DETECTORS["control_only_intervention"]
        cmd_store = ControlCommandStore(self.runtime_root)
        expected = project_identity({"project_id": self.project_id}, self.runtime_root)
        # Accepted continue command with identical inputs
        cmd_store.submit({
            "schema_version": 1,
            "command_id": "cmd-cont-1",
            "project_id": self.project_id,
            "action": "continue",
            "expected": expected,
            "target": {},
        }, source="test")
        cmd_store.settle(cmd_store.inbox / "cmd-cont-1.json", {
            "command_id": "cmd-cont-1",
            "project_id": self.project_id,
            "action": "continue",
            "state": "accepted",
            "pre_command_identity": {"lifecycle_state": "REVIEW_FAILED", "head": expected.get("head")},
        })
        summary_after = {
            self.project_id: {
                "lifecycle_state": "EXECUTING",
                "active_execution": True,
            }
        }
        anomalies = detector(self.runtime_root, self.config, summary_after, None, None, None, None, None)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][2], "CONTROL_ONLY_INTERVENTION")

    def test_detector_orchestrator_alive_task_stalled(self):
        detector = DETECTORS["orchestrator_alive_task_stalled"]
        (self.runtime_root / "ai-planner.json").write_text(json.dumps({"plans": {}}), encoding="utf-8")
        (self.runtime_root / "ai-reviewer.json").write_text(json.dumps({"reviews": {}}), encoding="utf-8")

        fake_planner = MagicMock()
        fake_planner.has_live_role.return_value = False
        fake_reviewer = MagicMock()
        fake_reviewer.has_live_role.return_value = False

        # Stalled snapshot: no gate, all roles dead, no legal wait, task inactive
        stalled_summary = {
            "projects": [
                {
                    "project_id": self.project_id,
                    "task_id": "T-stalled",
                    "lifecycle_state": "EXECUTING",
                    "worker": {"state": "idle", "pid": None},
                    "task_active": False,
                    "telemetry": {"watchdog_safe_activity_age_seconds": 601},
                }
            ]
        }
        anomalies = detector(
            self.runtime_root,
            self.config,
            stalled_summary,
            None,
            None,
            fake_planner,
            fake_reviewer,
            None,
        )
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][2], "ORCHESTRATOR_ALIVE_TASK_STALLED")

    def test_harvest_tick_deduplication(self):
        # Configure watchdog anomaly
        wd_data = {
            "projects": {
                self.project_id: {
                    "history": [
                        {
                            "diagnosis": "worker_hung_no_lineage",
                            "recovery": "kill_and_relaunch",
                            "attempt_key": "att-dedup-1",
                            "task_id": "T1",
                        }
                    ]
                }
            }
        }
        (self.runtime_root / "watchdog.json").write_text(json.dumps(wd_data), encoding="utf-8")

        # First harvest tick captures the incident
        fams1 = harvest_tick(self.runtime_root, config=self.config)
        self.assertEqual(len(fams1), 1)
        self.assertEqual(fams1[0]["recurrence_count"], 1)

        # Second harvest tick with exact same occurrence key is a deduplicated no-op
        fams2 = harvest_tick(self.runtime_root, config=self.config)
        self.assertEqual(len(fams2), 1)
        self.assertEqual(fams2[0]["recurrence_count"], 1)


if __name__ == "__main__":
    unittest.main()
