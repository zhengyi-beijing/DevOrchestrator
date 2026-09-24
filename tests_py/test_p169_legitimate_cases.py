import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from dev_orchestrator.core.execution_lifecycle import (
    FINDING_SUPPRESSED_LIVE,
    close_lineage_record,
    load_execution_lineage,
    observe_executions,
    open_execution_obligation,
    record_execution_observation,
)
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.watchdog import WatchdogCoordinator


class TestLegitimateCases(unittest.TestCase):
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
                        "execution_loss_confirmations": 2,
                        "execution_loss_max_recoveries": 3,
                        "provider_output_grace_seconds": 120,
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

    def test_completed_execution_zero_retries_status_ok(self):
        """Completed execution reaches terminal outcome cleanly, produces zero retries, and status is ok."""
        executor = TransitionExecutor(self.runtime)
        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-clean-done",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-clean-done",
            state="running",
            worker_pid=12345,
            provider_output_observed=True,
            started_at="2020-01-01T10:00:00Z",
        )
        close_lineage_record(
            self.runtime,
            project_id="p1",
            source_request_id="req-clean-done",
            terminal_outcome="completed",
        )
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "req-clean-done": {
                    "execution_id": "exec-clean-done",
                    "project_id": "p1",
                    "source_request_id": "req-clean-done",
                    "task_id": "t1",
                    "state": "completed",
                    "started_at": "2020-01-01T10:00:00Z",
                    "completed_at": "2020-01-01T10:30:00Z",
                    "head": self.initial_head,
                    "branch": "main",
                    "pid": 12345,
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        watchdog = WatchdogCoordinator(self.runtime)
        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="READY_TO_RUN")
            ticks = watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor)
            self.assertEqual(len(ticks), 1)
            self.assertEqual(ticks[0].get("status"), "ok")
            prow = watchdog.project_state("p1")
            self.assertEqual(prow["execution_loss"]["status"], "ok")
            self.assertEqual(len(prow["execution_loss"]["unresolved_invariants"]), 0)
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 0)

    def test_explicit_cancellation_zero_retries(self):
        """Explicitly cancelled execution has terminal outcome 'cancelled', produces zero retries."""
        executor = TransitionExecutor(self.runtime)
        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-cancelled",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        close_lineage_record(
            self.runtime,
            project_id="p1",
            source_request_id="req-cancelled",
            terminal_outcome="cancelled",
        )
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "req-cancelled": {
                    "execution_id": "exec-cancelled",
                    "project_id": "p1",
                    "source_request_id": "req-cancelled",
                    "task_id": "t1",
                    "state": "cancelled",
                    "head": self.initial_head,
                    "branch": "main",
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        watchdog = WatchdogCoordinator(self.runtime)
        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="READY_TO_RUN")
            ticks = watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor)
            self.assertEqual(len(ticks), 1)
            self.assertEqual(ticks[0].get("status"), "ok")
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 0)

    def test_broker_declared_failure_zero_retries(self):
        """Authoritative broker failure is recorded as terminal 'failed', produces zero execution-loss retries."""
        executor = TransitionExecutor(self.runtime)
        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-failed",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        close_lineage_record(
            self.runtime,
            project_id="p1",
            source_request_id="req-failed",
            terminal_outcome="failed",
        )
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "req-failed": {
                    "execution_id": "exec-failed",
                    "project_id": "p1",
                    "source_request_id": "req-failed",
                    "task_id": "t1",
                    "state": "failed",
                    "head": self.initial_head,
                    "branch": "main",
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        watchdog = WatchdogCoordinator(self.runtime)
        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="READY_TO_RUN")
            ticks = watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor)
            self.assertEqual(len(ticks), 1)
            self.assertEqual(ticks[0].get("status"), "ok")
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 0)

    def test_daemon_restart_with_durable_live_broker_execution(self):
        """When daemon restarts while an execution is running in broker, live probe confirms it is alive,
        preventing spurious loss classification and spurious retries."""
        executor = TransitionExecutor(self.runtime)
        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-live-broker",
            task_id="t1",
            engine="aibroker",
            backend_handle="broker-handle-123",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-live-broker",
            state="running",
            worker_pid=99999,
        )
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "req-live-broker": {
                    "execution_id": "exec-broker-1",
                    "project_id": "p1",
                    "source_request_id": "req-live-broker",
                    "broker_request_id": "broker-handle-123",
                    "engine": "aibroker",
                    "task_id": "t1",
                    "state": "running",
                    "head": self.initial_head,
                    "branch": "main",
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        # Mock broker port confirming execution is running
        mock_port = MagicMock()
        mock_port.status.return_value = {"status": "running"}

        # Fresh WatchdogCoordinator (after restart)
        watchdog = WatchdogCoordinator(self.runtime, ai_execution_port=mock_port)
        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="WORKER_RUNNING", worker_state="running", pid=99999)
            snap["broker_execution"] = {"state": "running", "request_id": "broker-handle-123"}
            snap["active_roles"] = ["worker"]
            ticks = watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor)
            self.assertEqual(len(ticks), 1)
            self.assertEqual(ticks[0].get("status"), "ok")
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 0)

    def test_provider_startup_within_grace_zero_retries(self):
        """Worker running without provider output within startup grace period produces zero retries."""
        executor = TransitionExecutor(self.runtime)
        now_dt = datetime.now(timezone.utc)
        from datetime import timedelta
        recent_started = (now_dt - timedelta(seconds=10)).isoformat()

        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-startup-grace",
            task_id="t1",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-startup-grace",
            state="running",
            worker_pid=44444,
            provider_output_observed=False,
            started_at=recent_started,
        )
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "req-startup-grace": {
                    "execution_id": "exec-grace-1",
                    "project_id": "p1",
                    "source_request_id": "req-startup-grace",
                    "task_id": "t1",
                    "state": "running",
                    "started_at": recent_started,
                    "head": self.initial_head,
                    "branch": "main",
                    "pid": 44444,
                    "provider_output_observed": False,
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        alive_probe = lambda p: True
        watchdog = WatchdogCoordinator(self.runtime, liveness_probe=alive_probe)
        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="WORKER_RUNNING", worker_state="running", pid=44444)
            snap["active_roles"] = ["worker"]
            ticks = watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor, now=now_dt)
            self.assertEqual(len(ticks), 1)
            self.assertEqual(ticks[0].get("status"), "ok")
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 0)

    def test_temporarily_missing_local_projection_with_confirmed_live_backend_proof(self):
        """When local snapshot projection is temporarily missing active roles but backend liveness
        proves the execution is alive, finding is suppressed_live and produces zero retries."""
        executor = TransitionExecutor(self.runtime)
        open_execution_obligation(
            self.runtime,
            project_id="p1",
            source_request_id="req-temp-missing",
            task_id="t1",
            engine="aibroker",
            backend_handle="broker-temp-456",
            launch_anchor={"git_head": self.initial_head, "branch": "main"},
        )
        record_execution_observation(
            self.runtime,
            project_id="p1",
            source_request_id="req-temp-missing",
            state="running",
            worker_pid=77777,
            started_at="2020-01-01T10:00:00Z",
        )
        exec_file = self.runtime / "transition-executor.json"
        exec_data = {
            "version": 1,
            "executions": {
                "req-temp-missing": {
                    "execution_id": "exec-temp-1",
                    "project_id": "p1",
                    "source_request_id": "req-temp-missing",
                    "broker_request_id": "broker-temp-456",
                    "engine": "aibroker",
                    "task_id": "t1",
                    "state": "running",
                    "head": self.initial_head,
                    "branch": "main",
                    "pid": 77777,
                }
            }
        }
        exec_file.write_text(json.dumps(exec_data), encoding="utf-8")

        mock_port = MagicMock()
        mock_port.status.return_value = {"status": "running"}

        watchdog = WatchdogCoordinator(self.runtime, ai_execution_port=mock_port)
        dummy_signals = ("2026-09-24T12:05:00Z", "fp123", {"activity_evidence": "available", "sources": {}})
        with patch("dev_orchestrator.core.watchdog.collect_progress_signals", return_value=dummy_signals):
            snap = self._make_snapshot(state="READY_TO_RUN", worker_state=None, pid=None)
            ticks = watchdog.advance(self.config_path, {"projects": [snap]}, executor=executor)
            self.assertEqual(len(ticks), 1)
            self.assertEqual(ticks[0].get("status"), "ok")
            prow = watchdog.project_state("p1")
            self.assertEqual(len(prow["execution_loss"]["actionable_findings"]), 0)
            inbox_files = list((self.runtime / "control" / "inbox").glob("*.json"))
            self.assertEqual(len(inbox_files), 0)


if __name__ == "__main__":
    unittest.main()
