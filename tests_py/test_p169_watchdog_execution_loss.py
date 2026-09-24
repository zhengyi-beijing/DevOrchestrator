from datetime import datetime, timezone
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
    resolve_execution_liveness,
)
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.watchdog import WatchdogCoordinator, resolve_watchdog_policy
from tests_py.test_transition_executor_aibroker import FakePort


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

    def test_real_recovery_launch_end_to_end_without_handwritten_lineage(self):
        """End-to-end test driving real recovery-launch without hand-written lineage links,
        verifying EXECUTION_LOSS_RESOLVED is emitted and health settles to ok."""
        # Set up clean task in next.md
        (self.repo / "agent" / "next.md").write_text("# Task t1\nTask: t1\nStatus: **READY_TO_RUN**\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-am", "setup t1 in next.md"], check=True, capture_output=True)
        proc = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
        head_commit = proc.stdout.strip()

        port = FakePort()
        executor = TransitionExecutor(self.runtime, ai_execution_port=port)
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "req-real": {
                    "execution_id": "exec-real",
                    "project_id": "p1",
                    "source_request_id": "req-real",
                    "task_id": "t1",
                    "state": "running",
                    "started_at": "2020-01-01T10:00:00Z",
                    "head": head_commit,
                    "branch": "main",
                    "pid": 77777,
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-real",
            task_id="t1",
            launch_anchor={"git_head": head_commit, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-real",
            state="running",
            worker_pid=77777,
            started_at="2020-01-01T10:00:00Z",
        )

        dead_probe = lambda p: False
        watchdog = WatchdogCoordinator(self.runtime, liveness_probe=dead_probe)

        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            # Tick 1: Watchdog detects execution loss, reconciles row, and enqueues continue command
            snap = self._make_snapshot(state="READY_TO_RUN", worker_state="not_started", pid=None)
            snap["git"]["head"] = head_commit
            snap["telemetry"] = {"task_id": "t1"}
            ticks1 = watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor)
            self.assertEqual(ticks1[0].get("status"), "execution_loss_detected")

            inv_key = invariant_key_for("p1", "req-real")
            cmd_id = f"wd-xl-{inv_key}"
            inbox_cmd = self.runtime / "control" / "inbox" / f"{cmd_id}.json"
            self.assertTrue(inbox_cmd.is_file())

            # Row is explicitly reconciled
            ex_st = executor.state()
            self.assertEqual(ex_st["executions"]["req-real"]["state"], "explicitly_reconciled")
            self.assertEqual(ex_st["executions"]["req-real"]["reconciled_by"], cmd_id)

            # Actuation: launch replacement via start_control without hand-written lineage
            project_dict = {
                "project_id": "p1",
                "repo_path": str(self.repo),
                "watchdog": {"enabled": True},
                "execution": {
                    "enabled": True,
                    "owner_authorized": True,
                    "engine": "aibroker",
                    "worker_quality": "balanced",
                    "allowed_next_actions": ["continue_current_stage", "next_task"],
                },
            }
            launch_res = executor.start_control(
                project_dict,
                snap,
                source_request_id=cmd_id,
            )
            self.assertIsNotNone(launch_res)

            # Verify that replacement obligation auto-linked recovery_of_lineage_key
            lineage_data = load_execution_lineage(self.runtime)
            rep_lkey = lineage_key_for("p1", cmd_id)
            self.assertIn(rep_lkey, lineage_data["records"])
            rep_rec = lineage_data["records"][rep_lkey]
            orig_lkey = lineage_key_for("p1", "req-real")
            self.assertEqual(rep_rec.get("recovery_of_lineage_key"), orig_lkey)

            # Simulate replacement execution finishing with terminal completed
            close_lineage_record(
                self.runtime,
                project_id="p1",
                source_request_id=cmd_id,
                terminal_outcome="completed",
            )
            ex_st = executor.state()
            if cmd_id in ex_st["executions"]:
                ex_st["executions"][cmd_id]["state"] = "completed"
                (self.runtime / "transition-executor.json").write_text(json.dumps(ex_st), encoding="utf-8")

            # Remove inbox command to simulate command completion
            inbox_cmd.unlink(missing_ok=True)

            # Tick 2: Watchdog observes replacement execution completed -> resolves finding
            ticks2 = watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor)
            self.assertEqual(ticks2[0].get("status"), "ok")

            # Verify finding is resolved and health is ok
            lineage_after = load_execution_lineage(self.runtime)
            orig_finding = lineage_after["records"][orig_lkey]["findings"][0]
            self.assertEqual(orig_finding["state"], "resolved")

            for th in list(executor._threads.values()):
                if th.is_alive():
                    th.join(timeout=2.0)

    def test_max_recoveries_budget_and_escalation_survive_tick_boundary(self):
        """Multi-tick test asserting recovery_attempts budget (max 2) persists across
        tick boundaries in execution-lineage.json and escalates without runaway retries."""
        executor = TransitionExecutor(self.runtime)
        # Configure max 2 recoveries, 1s backoff for multi-tick testing
        cfg = {
            "projects": [
                {
                    "project_id": "p1",
                    "repo_path": str(self.repo),
                    "watchdog": {
                        "enabled": True,
                        "auto_recovery": True,
                        "execution_loss_detection": True,
                        "execution_loss_confirmations": 1,
                        "execution_loss_max_recoveries": 2,
                        "execution_loss_backoff_seconds": 1,
                    },
                }
            ]
        }
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")

        exec_file = self.runtime / "transition-executor.json"
        exec_file.write_text(json.dumps({
            "version": 1,
            "executions": {
                "req-esc": {
                    "execution_id": "exec-esc",
                    "project_id": "p1",
                    "source_request_id": "req-esc",
                    "task_id": "t1",
                    "state": "running",
                    "started_at": "2020-01-01T10:00:00Z",
                    "head": self.initial_head,
                    "branch": "main",
                    "pid": 88888,
                }
            }
        }), encoding="utf-8")

        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-esc",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-esc",
            state="running",
            worker_pid=88888,
            started_at="2020-01-01T10:00:00Z",
        )

        dead_probe = lambda p: False
        watchdog1 = WatchdogCoordinator(self.runtime, liveness_probe=dead_probe)

        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        inv_key = invariant_key_for("p1", "req-esc")
        lkey = lineage_key_for("p1", "req-esc")

        t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 9, 24, 12, 0, 5, tzinfo=timezone.utc)
        t2 = datetime(2026, 9, 24, 12, 0, 10, tzinfo=timezone.utc)
        t3 = datetime(2026, 9, 24, 12, 0, 15, tzinfo=timezone.utc)

        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="READY_TO_RUN", worker_state="not_started", pid=None)

            # Tick 1: First recovery attempt
            watchdog1.advance(self.config_path, {"projects": [snap]}, executor=executor, now=t0)
            lineage1 = load_execution_lineage(self.runtime)
            f1 = lineage1["records"][lkey]["findings"][0]
            self.assertEqual(f1["recovery_attempts"], 1)

            # Clear enqueued command and clear slot phase to simulate recovery failure / disappearance
            cmd_file = self.runtime / "control" / "inbox" / f"wd-xl-{inv_key}.json"
            if cmd_file.is_file():
                cmd_file.unlink()
            watchdog1._cached_state["projects"]["p1"]["execution_loss_slots"].pop(inv_key, None)

            # Tick 2: Second recovery attempt
            watchdog1.advance(self.config_path, {"projects": [snap]}, executor=executor, now=t1)
            lineage2 = load_execution_lineage(self.runtime)
            f2 = lineage2["records"][lkey]["findings"][0]
            self.assertEqual(f2["recovery_attempts"], 2)

            if cmd_file.is_file():
                cmd_file.unlink()
            watchdog1._cached_state["projects"]["p1"]["execution_loss_slots"].pop(inv_key, None)

            # Tick 3: Budget exhausted (attempts 2 >= max 2) -> must ESCALATE!
            watchdog1.advance(self.config_path, {"projects": [snap]}, executor=executor, now=t2)
            lineage3 = load_execution_lineage(self.runtime)
            f3 = lineage3["records"][lkey]["findings"][0]
            self.assertEqual(f3["state"], "escalated")
            self.assertEqual(f3["recovery_attempts"], 2)

            # No command was enqueued on escalation
            self.assertFalse(cmd_file.is_file())

            # Tick 4: CRASH & RESTART fresh watchdog instance -> verifies escalation survives tick & daemon restart
            watchdog2 = WatchdogCoordinator(self.runtime, liveness_probe=dead_probe)
            watchdog2.advance(self.config_path, {"projects": [snap]}, executor=executor, now=t3)

            lineage4 = load_execution_lineage(self.runtime)
            f4 = lineage4["records"][lkey]["findings"][0]
            self.assertEqual(f4["state"], "escalated")
            self.assertEqual(f4["recovery_attempts"], 2)
            self.assertFalse(cmd_file.is_file())

    def test_broker_status_unknown_does_not_produce_liveness_dead(self):
        """Broker status 'unknown' or None return must produce LIVENESS_UNKNOWN,
        and must NOT treat provider silence as death proof or CAS explicitly_reconciled."""
        executor = TransitionExecutor(self.runtime)
        exec_file = self.runtime / "transition-executor.json"
        exec_file.write_text(json.dumps({
            "version": 1,
            "executions": {
                "req-broker": {
                    "execution_id": "exec-broker",
                    "project_id": "p1",
                    "source_request_id": "req-broker",
                    "task_id": "t1",
                    "state": "running",
                    "engine": "aibroker",
                    "broker_request_id": "br-req-123",
                    "head": self.initial_head,
                    "branch": "main",
                }
            }
        }), encoding="utf-8")

        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-broker",
            task_id="t1",
            engine="aibroker",
            backend_handle="br-req-123",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )

        mock_broker_port = unittest.mock.MagicMock()
        # Return unknown status
        mock_broker_port.status.return_value = {"status": "unknown"}

        lrec = load_execution_lineage(self.runtime)["records"][lineage_key_for("p1", "req-broker")]
        res = resolve_execution_liveness(
            lrec,
            executor_state=executor.state(),
            ai_execution_port=mock_broker_port,
        )
        self.assertEqual(res["verdict"], "unknown")

        # None return from broker status also produces unknown
        mock_broker_port.status.return_value = None
        res_none = resolve_execution_liveness(
            lrec,
            executor_state=executor.state(),
            ai_execution_port=mock_broker_port,
        )
        self.assertEqual(res_none["verdict"], "unknown")

        # Watchdog advance with broker returning unknown must NOT reconcile row
        watchdog = WatchdogCoordinator(self.runtime, ai_execution_port=mock_broker_port)
        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="READY_TO_RUN", worker_state="not_started", pid=None)
            watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor)

            ex_st = executor.state()
            self.assertEqual(ex_st["executions"]["req-broker"]["state"], "running")

    def test_running_without_provider_output_remains_diagnostic_only(self):
        """RUNNING_WITHOUT_PROVIDER_OUTPUT must remain diagnostic-only while liveness is alive
        or unknown, and must never authorize retry."""
        executor = TransitionExecutor(self.runtime)
        exec_file = self.runtime / "transition-executor.json"
        exec_file.write_text(json.dumps({
            "version": 1,
            "executions": {
                "req-long": {
                    "execution_id": "exec-long",
                    "project_id": "p1",
                    "source_request_id": "req-long",
                    "task_id": "t1",
                    "state": "running",
                    "started_at": "2020-01-01T10:00:00Z",
                    "provider_output_observed": False,
                    "head": self.initial_head,
                    "branch": "main",
                    "pid": 33333,
                }
            }
        }), encoding="utf-8")

        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-long",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-long",
            state="running",
            worker_pid=33333,
            started_at="2020-01-01T10:00:00Z",
            provider_output_observed=False,
        )

        # Worker is alive with matching PID and started_at
        alive_probe = lambda p: True
        watchdog = WatchdogCoordinator(self.runtime, liveness_probe=alive_probe)

        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="EXECUTING", worker_state="running", pid=33333)
            snap["worker"]["started_at"] = "2020-01-01T10:00:00Z"
            ticks = watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor)

            # Row remains running, no recovery command enqueued
            ex_st = executor.state()
            self.assertEqual(ex_st["executions"]["req-long"]["state"], "running")
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 0)

            # Finding is suppressed_live
            lkey = lineage_key_for("p1", "req-long")
            lineage = load_execution_lineage(self.runtime)
            finding = lineage["records"][lkey]["findings"][0]
            self.assertEqual(finding["state"], "suppressed_live")

    def test_reconcile_blocked_and_recovery_required_rows(self):
        """reconcile_execution_loss must successfully reconcile non-active non-terminal rows
        in 'blocked' and 'recovery_required' states instead of returning unavailable."""
        executor = TransitionExecutor(self.runtime)
        exec_file = self.runtime / "transition-executor.json"
        exec_file.write_text(json.dumps({
            "version": 1,
            "executions": {
                "req-blk": {
                    "execution_id": "exec-blk",
                    "project_id": "p1",
                    "source_request_id": "req-blk",
                    "task_id": "t1",
                    "state": "blocked",
                    "head": self.initial_head,
                    "branch": "main",
                },
                "req-rec": {
                    "execution_id": "exec-rec",
                    "project_id": "p1",
                    "source_request_id": "req-rec",
                    "task_id": "t1",
                    "state": "recovery_required",
                    "head": self.initial_head,
                    "branch": "main",
                },
            }
        }), encoding="utf-8")

        res_blk = executor.reconcile_execution_loss(
            "req-blk",
            project_id="p1",
            invariant_key="inv-blk",
            command_id="wd-xl-inv-blk",
            expected_anchor={"git_head": self.initial_head, "branch": "main"},
            evidence={"verdict": "dead"},
        )
        self.assertEqual(res_blk.get("status"), "reconciled")
        ex_st = executor.state()
        self.assertEqual(ex_st["executions"]["req-blk"]["state"], "explicitly_reconciled")

        res_rec = executor.reconcile_execution_loss(
            "req-rec",
            project_id="p1",
            invariant_key="inv-rec",
            command_id="wd-xl-inv-rec",
            expected_anchor={"git_head": self.initial_head, "branch": "main"},
            evidence={"verdict": "dead"},
        )
        self.assertEqual(res_rec.get("status"), "reconciled")
        ex_st = executor.state()
        self.assertEqual(ex_st["executions"]["req-rec"]["state"], "explicitly_reconciled")


if __name__ == "__main__":
    unittest.main()
