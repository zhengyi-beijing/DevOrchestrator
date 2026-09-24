import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_orchestrator.core.execution_lifecycle import (
    LINEAGE_STATE_FILE,
    ObligationPersistError,
    close_lineage_record,
    compute_lineage_integrity_hash,
    invariant_key_for,
    lineage_key_for,
    load_execution_lineage,
    observe_executions,
    open_execution_obligation,
    record_execution_observation,
    resolve_execution_liveness,
)
from dev_orchestrator.core.transition_executor import TransitionExecutor


class TestExecutionLineage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime = Path(self.temp_dir) / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.repo = Path(self.temp_dir) / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        import subprocess
        subprocess.run(["git", "init", str(self.repo)], check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_open_obligation_and_integrity_verification(self):
        """Obligation creates durable lineage file with verified integrity hash."""
        record = open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-123",
            task_id="t1",
            launch_anchor={"git_head": "abc1234"},
        )
        self.assertEqual(record["project_id"], "p1")
        self.assertEqual(record["source_request_id"], "req-123")
        self.assertEqual(record["lifecycle_phase"], "obligated")
        self.assertTrue(record["integrity_hash"])

        lineage = load_execution_lineage(self.runtime)
        lkey = lineage_key_for("p1", "req-123")
        self.assertIn(lkey, lineage["records"])
        stored = lineage["records"][lkey]
        self.assertEqual(stored["integrity_hash"], compute_lineage_integrity_hash(stored))

    def test_obligation_failure_blocks_launch(self):
        """Failure to persist obligation fails closed to blocked/execution_obligation_unpersisted."""
        executor = TransitionExecutor(self.runtime)

        project = {
            "project_id": "p1",
            "repo_path": str(self.repo),
        }
        policy = {
            "preferred_backends": ["fake"],
            "backends": {"fake": {}},
        }

        with patch("dev_orchestrator.core.transition_executor.read_repository_truth") as mock_truth, \
             patch("dev_orchestrator.core.transition_executor.open_execution_obligation", side_effect=ObligationPersistError("lock failure")), \
             patch.object(executor, "_router_for_policy") as mock_router:
            mock_truth.return_value.valid = True
            mock_truth.return_value.branch = "main"
            mock_truth.return_value.head = "head123"
            mock_truth.return_value.status_hash = "hash1"

            route = unittest.mock.MagicMock()
            route.selected_backend_id = "fake"
            route.candidates = ()
            router = unittest.mock.MagicMock()
            router.route.return_value = route
            mock_router.return_value = (router, {"fake": unittest.mock.MagicMock()})

            res = executor._launch(
                project,
                source_request_id="req-fail",
                source_kind="manual",
                task_id="t1",
                source_task_id="t0",
                branch="main",
                head="head123",
                worker_prompt="do task",
                policy=policy,
            )
            self.assertIsNone(res)

            # Ensure row in executor ledger is marked blocked with execution_obligation_unpersisted
            st = executor.state()
            active_or_blocked = [r for r in st.get("executions", {}).values() if r.get("source_request_id") == "req-fail"]
            self.assertEqual(len(active_or_blocked), 1)
            self.assertEqual(active_or_blocked[0]["state"], "blocked")
            self.assertIn("execution_obligation_unpersisted", active_or_blocked[0]["reason"])

    def test_corrupt_lineage_store_is_quarantined_and_degraded(self):
        """Corrupt execution-lineage.json payload is quarantined and sets degraded status."""
        lineage_file = self.runtime / LINEAGE_STATE_FILE
        lineage_file.write_text("{this is corrupt json!!", encoding="utf-8")

        lineage = load_execution_lineage(self.runtime)
        self.assertTrue(lineage["degraded"])
        self.assertIn("unreadable", lineage["degraded_reason"])

        quarantined = list(self.runtime.glob("execution-lineage.json.corrupt-*"))
        self.assertTrue(len(quarantined) >= 1)

    def test_reconstruct_obligation_from_launching_row(self):
        """observe_executions reconstructs missing obligation if executor row reached launching/running."""
        executor = TransitionExecutor(self.runtime)
        # Artificially insert a running row in transition-executor.json
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "exec-1": {
                    "execution_id": "exec-1",
                    "project_id": "p1",
                    "source_request_id": "req-orphan",
                    "task_id": "t1",
                    "state": "running",
                    "started_at": "2026-09-24T10:00:00Z",
                    "git_head": "head-orphan",
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        snapshot = {
            "project_id": "p1",
            "repo_path": str(self.repo),
            "state": "READY_TO_RUN",
        }
        policy = {"execution_loss_detection": True}

        summary = observe_executions(self.runtime, snapshot=snapshot, policy=policy, executor=executor)
        # Lineage obligation was reconstructed!
        lineage = load_execution_lineage(self.runtime)
        lkey = lineage_key_for("p1", "req-orphan")
        self.assertIn(lkey, lineage["records"])
        self.assertEqual(lineage["records"][lkey]["project_id"], "p1")

    def test_surviving_obligation_with_disappeared_row_detected(self):
        """An open obligation whose executor row disappeared produces EXECUTION_RECORD_DISAPPEARED."""
        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-vanished",
            task_id="t1",
            launch_anchor={"git_head": "head1"},
        )
        # Update obligation to running state
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-vanished",
            lifecycle_phase="running",
        )

        # Transition executor has NO record of this request
        executor = TransitionExecutor(self.runtime)

        snapshot = {
            "project_id": "p1",
            "repo_path": str(self.repo),
            "state": "READY_TO_RUN",
        }
        policy = {"execution_loss_detection": True, "execution_loss_confirmations": 1}

        summary = observe_executions(self.runtime, snapshot=snapshot, policy=policy, executor=executor)
        self.assertIn("loss_detected", summary["status"])
        inv_key = invariant_key_for("p1", "req-vanished")
        self.assertIn(inv_key, summary["unresolved_invariants"])
        finding = summary["findings"][inv_key]
        self.assertEqual(finding["classification"], "EXECUTION_RECORD_DISAPPEARED")

    def test_ordering_contract_terminal_row_before_closing_obligation(self):
        """close_lineage_record properly closes obligation when terminal outcome is written."""
        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-term",
            task_id="t1",
        )
        record = close_lineage_record(
            self.runtime,
            project_id="p1",
            source_request_id="req-term",
            outcome="completed",
            reason="worker finished cleanly",
        )
        self.assertEqual(record["terminal_outcome"], "completed")
        self.assertEqual(record["lifecycle_phase"], "terminal")

        # Verify load_execution_lineage reflects closure
        lineage = load_execution_lineage(self.runtime)
        lkey = lineage_key_for("p1", "req-term")
        self.assertEqual(lineage["records"][lkey]["terminal_outcome"], "completed")


if __name__ == "__main__":
    unittest.main()
