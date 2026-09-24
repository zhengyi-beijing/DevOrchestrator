import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dev_orchestrator.core.diagnostics import ACTIVE_WORKER_STATES
from dev_orchestrator.core.execution_lifecycle import (
    FINDING_ACTIONABLE_DEAD,
    FINDING_OPEN,
    FINDING_RECONCILED_PENDING_RETRY,
    FINDING_RECOVERING,
    FINDING_RESOLVED,
    FINDING_SUPPRESSED_LIVE,
    close_lineage_record,
    invariant_key_for,
    lineage_key_for,
    load_execution_lineage,
    observe_executions,
    open_execution_obligation,
    record_execution_observation,
)
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.watchdog import WatchdogCoordinator, resolve_watchdog_policy


class TestWatchdogExecutionLoss(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.runtime = Path(self.temp_dir) / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.repo = Path(self.temp_dir) / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(self.repo)], check=True, capture_output=True)
        (self.repo / "README.md").write_text("# Hello\n", encoding="utf-8")
        (self.repo / "agent").mkdir(parents=True, exist_ok=True)
        (self.repo / "agent" / "next.md").write_text("# Next\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Tester"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "tester@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "initial"], check=True, capture_output=True)
        proc = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
        self.initial_head = proc.stdout.strip()

        self.config_path = Path(self.temp_dir) / "projects.json"
        self.config = {
            "projects": [
                {
                    "project_id": "p1",
                    "repo_path": str(self.repo),
                    "watchdog": {
                        "enabled": True,
                        "auto_recovery": True,
                        "execution_loss_detection": True,
                        "execution_loss_confirmations": 1,
                        "execution_loss_max_recoveries": 3,
                    },
                }
            ]
        }
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_snapshot(self, state="READY_TO_RUN", worker_state=None, pid=None):
        return {
            "project_id": "p1",
            "repo_path": str(self.repo),
            "state": state,
            "lifecycle_state": state,
            "git": {"head": self.initial_head, "branch": "main", "dirty": False},
            "worker": {"state": worker_state, "pid": pid},
            "activity": {
                "watchdog_safe": {
                    "repo_scope": "canonical",
                    "repo_root_fingerprint": unittest.mock.ANY,
                    "last_activity_at": "2026-09-24T12:00:00Z",
                }
            },
        }

    def test_exact_p1_failure_sequence_and_recovery(self):
        """P1 failure: Worker runs without provider output, disappears without terminal state,
        a new epoch/file touch occurs, watchdog detects loss, non-ok health, performs safe recovery,
        and replacement completion closes finding."""
        executor = TransitionExecutor(self.runtime)

        # 1. Accepted continue and launching row
        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="p1-req-vanished",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="p1-req-vanished",
            state="running",
            worker_pid=99999,
            provider_output_observed=False,
            started_at="2020-01-01T10:00:00Z",
        )

        # Add running row to executor
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "p1-req-vanished": {
                    "execution_id": "exec-vanished",
                    "project_id": "p1",
                    "source_request_id": "p1-req-vanished",
                    "task_id": "t1",
                    "state": "running",
                    "started_at": "2020-01-01T10:00:00Z",
                    "head": self.initial_head,
                    "branch": "main",
                    "pid": 99999,
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        # 2. Worker process disappears (PID dead) and row drops from executor
        dead_liveness_probe = lambda p: False

        # Simulate execution dropping from executor (vanished!)
        exec_file.write_text(json.dumps({"version": 1, "executions": {}}), encoding="utf-8")

        # 3. Project returns to READY_TO_RUN, new activity/epoch injected
        os.utime(str(self.repo / "agent" / "next.md"), None)

        watchdog = WatchdogCoordinator(self.runtime, liveness_probe=dead_liveness_probe)

        # Mock collect_progress_signals to return valid available signals
        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="READY_TO_RUN")
            summary = {"projects": [snap]}
            ticks = watchdog.advance(self.config_path, summary, executor=executor)

            # Watchdog must NOT report ok!
            self.assertEqual(len(ticks), 1)
            self.assertNotEqual(ticks[0].get("status"), "ok")
            self.assertEqual(ticks[0].get("status"), "execution_loss_detected")

            # Check project row in watchdog state
            prow = watchdog.project_state("p1")
            self.assertIsNotNone(prow)
            self.assertIn("execution_loss", prow)
            inv_key = invariant_key_for("p1", "p1-req-vanished")
            self.assertIn(inv_key, prow["execution_loss"]["unresolved_invariants"])

            # Verify that recovery was triggered: wd-xl command enqueued in control/inbox
            cmd_id = f"wd-xl-{inv_key}"
            inbox_cmd = self.runtime / "control" / "inbox" / f"{cmd_id}.json"
            self.assertTrue(inbox_cmd.is_file())
            cmd_payload = json.loads(inbox_cmd.read_text(encoding="utf-8"))
            self.assertEqual(cmd_payload["action"], "continue")
            self.assertEqual(cmd_payload["project_id"], "p1")

            # Verify executor row is marked explicitly_reconciled tombstone
            ex_st = executor.state()
            self.assertIn("p1-req-vanished", ex_st["executions"])
            rec_row = ex_st["executions"]["p1-req-vanished"]
            self.assertEqual(rec_row["state"], "explicitly_reconciled")
            self.assertEqual(rec_row["reason"], "watchdog_execution_loss")

            # 4. Now simulate replacement execution launching and reaching terminal completed
            open_execution_obligation(
                self.runtime,
                project_id="p1",
                source_request_id="p1-req-replacement",
                task_id="t1",
                launch_anchor={"git_head": self.initial_head, "branch": "main"},
                recovery_of_lineage_key=lineage_key_for("p1", "p1-req-vanished"),
            )
            record_execution_observation(
                self.runtime,
                project_id="p1",
                source_request_id="p1-req-replacement",
                state="running",
                worker_pid=88888,
                provider_output_observed=True,
                started_at="2020-01-01T11:00:00Z",
            )
            close_lineage_record(
                self.runtime,
                project_id="p1",
                source_request_id="p1-req-replacement",
                terminal_outcome="completed",
            )
            inbox_cmd.unlink(missing_ok=True)
            ticks2 = watchdog.advance(self.config_path, summary, executor=executor)
            self.assertEqual(len(ticks2), 1)
            self.assertEqual(ticks2[0].get("status"), "ok")
            prow2 = watchdog.project_state("p1")
            self.assertEqual(len(prow2["execution_loss"]["unresolved_invariants"]), 0)

    def test_second_active_row_conflict_prevents_duplicate_launch(self):
        """If a second launching/running row exists for the project, reconciliation returns conflict,
        _has_active_execution remains true, no command is enqueued, and error reports duplicate_execution_present."""
        executor = TransitionExecutor(self.runtime)
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "req-stale": {
                    "execution_id": "exec-stale",
                    "project_id": "p1",
                    "source_request_id": "req-stale",
                    "task_id": "t1",
                    "state": "running",
                    "started_at": "2020-01-01T10:00:00Z",
                    "head": self.initial_head,
                    "branch": "main",
                    "pid": 22222,
                },
                "req-active-other": {
                    "execution_id": "exec-other",
                    "project_id": "p1",
                    "source_request_id": "req-active-other",
                    "task_id": "t1",
                    "state": "running",
                    "started_at": "2020-01-01T10:05:00Z",
                    "head": self.initial_head,
                    "branch": "main",
                    "pid": 33333,
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-stale",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-stale",
            state="running",
            worker_pid=22222,
            started_at="2020-01-01T10:00:00Z",
        )

        dead_probe = lambda p: False
        watchdog = WatchdogCoordinator(self.runtime, liveness_probe=dead_probe)

        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="READY_TO_RUN")
            summary = {"projects": [snap]}
            watchdog.advance(self.config_path, summary, executor=executor)

            prow = watchdog.project_state("p1")
            # Should record duplicate_execution_present and enqueue NOTHING
            self.assertEqual(prow.get("last_error"), "duplicate_execution_present")
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 0)

    def test_stale_snapshot_activity_defers_enqueue_until_refreshed(self):
        """If row is explicitly_reconciled but snapshot still claims active worker,
        strict post-reconciliation guard keeps finding in reconciled_pending_retry and defers enqueue."""
        executor = TransitionExecutor(self.runtime)
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "req-1": {
                    "execution_id": "exec-1",
                    "project_id": "p1",
                    "source_request_id": "req-1",
                    "task_id": "t1",
                    "state": "running",
                    "started_at": "2020-01-01T10:00:00Z",
                    "head": self.initial_head,
                    "branch": "main",
                    "pid": 11111,
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-1",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-1",
            state="running",
            worker_pid=11111,
            started_at="2020-01-01T10:00:00Z",
        )

        dead_probe = lambda p: False
        watchdog = WatchdogCoordinator(self.runtime, liveness_probe=dead_probe)

        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            # Snapshot still reports worker running!
            snap_active = self._make_snapshot(state="READY_TO_RUN", worker_state="running", pid=11111)
            watchdog.advance(self.config_path, {"projects": [snap_active]}, executor=executor)

            # Row got reconciled:
            ex_st = executor.state()
            self.assertEqual(ex_st["executions"]["req-1"]["state"], "explicitly_reconciled")

            # But no continue command enqueued yet because snapshot was active!
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 0)

            # Next tick: snapshot refreshed to inactive worker
            snap_inactive = self._make_snapshot(state="READY_TO_RUN", worker_state="not_started", pid=None)
            watchdog.advance(self.config_path, {"projects": [snap_inactive]}, executor=executor)

            # Now continue command was enqueued!
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 1)

    def test_completion_race_preserves_genuine_outcome_and_suppresses_retry(self):
        """If worker genuinely completes before reconciliation CAS, genuine outcome is preserved and retry suppressed."""
        executor = TransitionExecutor(self.runtime)
        exec_file = self.runtime / "transition-executor.json"
        # Stale row originally running
        exec_data = {
            "version": 1,
            "executions": {
                "req-race": {
                    "execution_id": "exec-race",
                    "project_id": "p1",
                    "source_request_id": "req-race",
                    "task_id": "t1",
                    "state": "completed",
                    "completed_at": "2026-09-24T12:00:00Z",
                    "head": self.initial_head,
                    "branch": "main",
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        res = executor.reconcile_execution_loss(
            "req-race",
            project_id="p1",
            invariant_key="inv-race",
            command_id="wd-xl-inv-race",
            expected_anchor={"git_head": self.initial_head, "branch": "main"},
            evidence={"verdict": "dead"},
        )
        self.assertEqual(res.get("status"), "superseded_by_terminal")
        # Row state was preserved as completed
        ex_st = executor.state()
        self.assertEqual(ex_st["executions"]["req-race"]["state"], "completed")

    def test_crash_boundary_resumption(self):
        """If daemon crashes after reservation/reconciliation but before command enqueue,
        a fresh watchdog instance on next advance loads the pending reservation and resumes
        with the exact same command ID without re-reserving or creating a duplicate row."""
        executor = TransitionExecutor(self.runtime)
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "req-crash": {
                    "execution_id": "exec-crash",
                    "project_id": "p1",
                    "source_request_id": "req-crash",
                    "task_id": "t1",
                    "state": "running",
                    "started_at": "2020-01-01T10:00:00Z",
                    "head": self.initial_head,
                    "branch": "main",
                    "pid": 55555,
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-crash",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-crash",
            state="running",
            worker_pid=55555,
            started_at="2020-01-01T10:00:00Z",
        )

        dead_probe = lambda p: False
        watchdog1 = WatchdogCoordinator(self.runtime, liveness_probe=dead_probe)

        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            # First tick: snapshot claims active worker so enqueue is deferred (pending retry)
            snap_active = self._make_snapshot(state="READY_TO_RUN", worker_state="running", pid=55555)
            watchdog1.advance(self.config_path, {"projects": [snap_active]}, executor=executor)

            inv_key = invariant_key_for("p1", "req-crash")
            expected_cid = f"wd-xl-{inv_key}"

            # Verify slot was persisted in state file with phase reconciled_pending_retry
            state_file = self.runtime / "watchdog.json"
            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            prow = persisted["projects"]["p1"]
            slot = prow["execution_loss_slots"][inv_key]
            self.assertEqual(slot["phase"], "reconciled_pending_retry")
            self.assertEqual(slot["command_id"], expected_cid)

            # SIMULATE DAEMON CRASH & RESTART:
            # Instantiate brand new WatchdogCoordinator
            watchdog2 = WatchdogCoordinator(self.runtime, liveness_probe=dead_probe)

            # Advance with inactive worker snapshot
            snap_inactive = self._make_snapshot(state="READY_TO_RUN", worker_state="not_started", pid=None)
            watchdog2.advance(self.config_path, {"projects": [snap_inactive]}, executor=executor)

            # Verify that command was enqueued using the EXACT same command_id
            inbox_cmd = self.runtime / "control" / "inbox" / f"{expected_cid}.json"
            self.assertTrue(inbox_cmd.is_file())
            cmd = json.loads(inbox_cmd.read_text(encoding="utf-8"))
            self.assertEqual(cmd["command_id"], expected_cid)

            # Verify no duplicate command files or extra slots created
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 1)

    def test_tombstone_missing_row_recovery(self):
        """When an execution disappears completely from transition-executor.json,
        reconcile_execution_loss CAS inserts an explicitly_reconciled tombstone row
        and enqueues recovery continue command."""
        executor = TransitionExecutor(self.runtime)
        # Empty executor ledger
        exec_file = self.runtime / "transition-executor.json"
        exec_file.write_text(json.dumps({"version": 1, "executions": {}}), encoding="utf-8")

        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-dropped",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-dropped",
            state="running",
            worker_pid=66666,
            started_at="2020-01-01T10:00:00Z",
        )

        dead_probe = lambda p: False
        watchdog = WatchdogCoordinator(self.runtime, liveness_probe=dead_probe)

        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="READY_TO_RUN", worker_state="not_started", pid=None)
            watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor)

            # Executor now contains tombstone row
            ex_st = executor.state()
            self.assertIn("req-dropped", ex_st["executions"])
            self.assertEqual(ex_st["executions"]["req-dropped"]["state"], "explicitly_reconciled")

            # Command enqueued
            inv_key = invariant_key_for("p1", "req-dropped")
            inbox_cmd = self.runtime / "control" / "inbox" / f"wd-xl-{inv_key}.json"
            self.assertTrue(inbox_cmd.is_file())


if __name__ == "__main__":
    unittest.main()

