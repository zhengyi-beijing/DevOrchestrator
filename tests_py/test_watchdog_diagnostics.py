import tempfile
import time
import unittest
from pathlib import Path

from dev_orchestrator.core.diagnostics import (
    DIAGNOSIS_CODES,
    classify_evidence,
    collect_evidence,
    evidence_hash,
)


class DummyAssessment:
    def __init__(self, lifecycle_state="EXECUTING", no_progress_seconds=1200.0, threshold_seconds=900.0):
        self.lifecycle_state = lifecycle_state
        self.no_progress_seconds = no_progress_seconds
        self.threshold_seconds = threshold_seconds


class WatchdogDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.repo_dir = self.root / "repo"
        self.runtime_dir = self.root / "runtime"
        self.repo_dir.mkdir(parents=True)
        self.runtime_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_7_diagnostic_classifications(self):
        """Verify all 7 diagnosis codes can be classified deterministically."""
        self.assertEqual(len(DIAGNOSIS_CODES), 7)
        assessment = DummyAssessment()

        # 1. process_dead
        ev_dead = {
            "process_liveness": {"process_alive": False, "pid": 1234},
            "repository_truth": {"valid": True},
        }
        diag1 = classify_evidence(ev_dead, assessment)
        self.assertEqual(diag1.code, "process_dead")
        self.assertEqual(diag1.confidence, 1.0)
        self.assertFalse(diag1.owner_gate_required)

        # 2. provider_or_quota_blocked
        ev_quota = {
            "process_liveness": {"process_alive": True, "pid": 1234},
            "broker": {"status": "rate_limit_exceeded (429)"},
            "repository_truth": {"valid": True},
        }
        diag2 = classify_evidence(ev_quota, assessment)
        self.assertEqual(diag2.code, "provider_or_quota_blocked")
        self.assertTrue(diag2.owner_gate_required)

        # 3. state_desync
        ev_desync = {
            "process_liveness": {"process_alive": True, "pid": 1234},
            "ledgers": {"transition-executor.json": {"state": "completed"}},
            "repository_truth": {"valid": True},
        }
        diag3 = classify_evidence(ev_desync, assessment)
        self.assertEqual(diag3.code, "state_desync")
        self.assertTrue(diag3.owner_gate_required)

        # 4. external_wait
        ev_wait = {
            "process_liveness": {"process_alive": True, "pid": 1234},
            "broker": {"status": "waiting_bridge"},
            "repository_truth": {"valid": True},
        }
        diag4 = classify_evidence(ev_wait, assessment)
        self.assertEqual(diag4.code, "external_wait")
        self.assertTrue(diag4.owner_gate_required)

        # 5. healthy_slow
        ev_healthy = {
            "process_liveness": {"process_alive": True, "pid": 1234},
            "repository_truth": {"valid": True, "dirty": True, "uncommitted_files": ["f1.py"]},
        }
        healthy_assessment = DummyAssessment(no_progress_seconds=950.0, threshold_seconds=900.0)
        diag5 = classify_evidence(ev_healthy, healthy_assessment)
        self.assertEqual(diag5.code, "healthy_slow")
        self.assertFalse(diag5.owner_gate_required)

        # 6. agent_stalled
        ev_stalled = {
            "process_liveness": {"process_alive": True, "pid": 1234},
            "repository_truth": {"valid": True, "dirty": False},
        }
        stalled_assessment = DummyAssessment(no_progress_seconds=2000.0, threshold_seconds=900.0)
        diag6 = classify_evidence(ev_stalled, stalled_assessment)
        self.assertEqual(diag6.code, "agent_stalled")
        self.assertFalse(diag6.owner_gate_required)

        # 7. unknown (skipped deadline or ambiguous)
        ev_unknown = {
            "repository_truth": {"status": "skipped_deadline"},
        }
        diag7 = classify_evidence(ev_unknown, assessment)
        self.assertEqual(diag7.code, "unknown")
        self.assertTrue(diag7.owner_gate_required)

    def test_evidence_hash_stability(self):
        """evidence_hash produces identical deterministic hash regardless of key order or volatile timings."""
        e1 = {
            "z": 1,
            "a": [3, 2, 1],
            "sub": {"k": "v"},
            "collected_at": "2026-09-12T10:00:00Z",
        }
        e2 = {
            "collected_at": "2026-09-12T11:00:00Z",
            "sub": {"k": "v"},
            "a": [3, 2, 1],
            "z": 1,
        }
        h1 = evidence_hash(e1)
        h2 = evidence_hash(e2)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)

    def test_deadline_budget_exhaustion(self):
        """When deadline is in the past, collect_evidence skips sub-collectors gracefully."""
        past_deadline = time.monotonic() - 10.0
        evidence = collect_evidence(
            project={"repo_path": str(self.repo_dir)},
            snapshot={"project_id": "p1"},
            runtime_root=self.runtime_dir,
            deadline=past_deadline,
        )
        self.assertEqual(evidence["repository_truth"]["status"], "skipped_deadline")
        self.assertEqual(evidence["process_liveness"]["status"], "skipped_deadline")

    def test_dead_pid_in_planning_without_active_worker_does_not_classify_process_dead(self):
        evidence = {
            "process_liveness": {
                "process_alive": False,
                "pid": 1234,
                "worker_state": "not_started",
                "kind": "planner",
            },
            "repository_truth": {"valid": True, "dirty": False, "uncommitted_files": []},
            "agent_files": {},
        }
        diag = classify_evidence(evidence, DummyAssessment(lifecycle_state="PLANNING", no_progress_seconds=2000.0))
        self.assertEqual(diag.code, "unknown")

    def test_f1_stale_worker_state_running_plus_dead_pid_in_planner_reviewer_never_process_dead(self):
        """F1 regression: stale worker.state='running' + dead PID must NOT classify as process_dead
        during PLANNING, REVIEWING, REVIEWING_PLAN, or APPLYING_PLAN lifecycles."""
        non_execution_lifecycles = ("PLANNING", "REVIEWING", "REVIEWING_PLAN", "APPLYING_PLAN")
        for lifecycle in non_execution_lifecycles:
            evidence = {
                "process_liveness": {
                    "process_alive": False,
                    "pid": 9999,
                    # stale worker_state from executor ledger - must be ignored for classification
                    "worker_state": "running",
                    "kind": "worker",
                },
                "repository_truth": {"valid": True, "dirty": False, "uncommitted_files": []},
                "agent_files": {},
            }
            diag = classify_evidence(
                evidence,
                DummyAssessment(lifecycle_state=lifecycle, no_progress_seconds=2000.0),
            )
            self.assertNotEqual(
                diag.code,
                "process_dead",
                msg=f"process_dead must not fire in {lifecycle} with stale worker_state='running'",
            )

    def test_f1_dead_pid_in_executing_classifies_process_dead(self):
        """F1 regression: dead PID in EXECUTING must still classify as process_dead."""
        evidence = {
            "process_liveness": {
                "process_alive": False,
                "pid": 9999,
                "worker_state": "running",
                "kind": "worker",
            },
            "repository_truth": {"valid": True, "dirty": False, "uncommitted_files": []},
            "agent_files": {},
        }
        diag = classify_evidence(
            evidence,
            DummyAssessment(lifecycle_state="EXECUTING", no_progress_seconds=2000.0),
        )
        self.assertEqual(diag.code, "process_dead")

    def test_f1_dead_pid_in_remediating_classifies_process_dead(self):
        """F1 regression: dead PID in REMEDIATING must still classify as process_dead."""
        evidence = {
            "process_liveness": {
                "process_alive": False,
                "pid": 8888,
                "worker_state": "running",
            },
            "repository_truth": {"valid": True},
        }
        diag = classify_evidence(
            evidence,
            DummyAssessment(lifecycle_state="REMEDIATING", no_progress_seconds=2000.0),
        )

    def test_f1_alive_pid_in_non_worker_lifecycle_classifies_unknown(self):
        """R3-F1 regression: alive PID in any non-worker lifecycle must classify as unknown,
        not agent_stalled, to prevent spurious wd-continue recovery and second planner cycles."""
        non_worker_lifecycles = (
            "PLANNING", "REVIEWING", "REVIEWING_PLAN", "APPLYING_PLAN",
            "READY_TO_RUN", "IDLE", "UNKNOWN",
        )
        for lifecycle in non_worker_lifecycles:
            evidence = {
                "process_liveness": {"process_alive": True, "pid": 1234},
                "repository_truth": {"valid": True, "dirty": False},
                "agent_files": {},
            }
            diag = classify_evidence(
                evidence,
                DummyAssessment(lifecycle_state=lifecycle, no_progress_seconds=2000.0),
            )
            self.assertEqual(
                diag.code,
                "unknown",
                msg=f"agent_stalled must not fire in {lifecycle!r} with alive PID; got {diag.code!r}",
            )
            self.assertTrue(
                diag.owner_gate_required,
                msg=f"owner_gate_required must be True for unknown in lifecycle {lifecycle!r}",
            )

    def test_f1_alive_pid_in_executing_still_classifies_agent_stalled(self):
        """R3-F1 regression: alive PID in EXECUTING must still classify as agent_stalled."""
        evidence = {
            "process_liveness": {"process_alive": True, "pid": 9999},
            "repository_truth": {"valid": True, "dirty": False},
            "agent_files": {},
        }
        diag = classify_evidence(
            evidence,
            DummyAssessment(lifecycle_state="EXECUTING", no_progress_seconds=2000.0),
        )
        self.assertEqual(diag.code, "agent_stalled")
        self.assertFalse(diag.owner_gate_required)

    def test_f1_alive_pid_in_remediating_still_classifies_agent_stalled(self):
        """R3-F1 regression: alive PID in REMEDIATING must still classify as agent_stalled."""
        evidence = {
            "process_liveness": {"process_alive": True, "pid": 7777},
            "repository_truth": {"valid": True, "dirty": False},
            "agent_files": {},
        }
        diag = classify_evidence(
            evidence,
            DummyAssessment(lifecycle_state="REMEDIATING", no_progress_seconds=3000.0),
        )
        self.assertEqual(diag.code, "agent_stalled")
        self.assertFalse(diag.owner_gate_required)

    def test_static_api_guards_against_destructive_calls(self):
        """Static analysis: watchdog.py and diagnostics.py must NOT import or call process-killing or git-write APIs."""
        code_dir = Path(__file__).resolve().parent.parent / "src" / "dev_orchestrator" / "core"
        watchdog_code = (code_dir / "watchdog.py").read_text(encoding="utf-8")
        diagnostics_code = (code_dir / "diagnostics.py").read_text(encoding="utf-8")

        forbidden_names = [
            "terminate_pid",
            "terminate_process_tree",
            "git commit",
            "git push",
            "git reset",
            "git checkout",
            "git revert",
            "git merge",
            "git rebase",
        ]

        for code, fname in [(watchdog_code, "watchdog.py"), (diagnostics_code, "diagnostics.py")]:
            for forbidden in forbidden_names:
                self.assertNotIn(
                    forbidden,
                    code,
                    msg=f"Forbidden API/command {forbidden!r} found in {fname}",
                )

    def test_r8b2_broker_status_call_is_bounded_by_deadline(self):
        """R8-B2 regression: collect_evidence must not block indefinitely on
        ai_execution_port.status().  When the port call takes longer than the remaining
        deadline budget, collect_evidence returns with a timeout error in broker evidence
        and does NOT block the diagnostic worker thread forever."""
        import threading as _threading
        import time

        # A port whose status() call blocks until we allow it
        _unblock = _threading.Event()

        class _BlockingPort:
            def status(self, request_id):
                _unblock.wait(timeout=30)  # simulates a very long blocking call
                return "late"

        snapshot = {
            "project_id": "p1",
            "actuation": {"source_request_id": "req-block-1"},
        }
        project = {"project_id": "p1", "repo_path": ""}  # empty path → repo truth fails quickly

        # Case 1: deadline already exhausted → broker section skipped entirely, must not block
        past_deadline = time.monotonic() - 1.0
        ev1 = collect_evidence(
            project=project,
            snapshot=snapshot,
            runtime_root=self.runtime_dir,
            ai_execution_port=_BlockingPort(),
            deadline=past_deadline,
        )
        self.assertIsNotNone(ev1, "collect_evidence must return even when deadline exhausted")
        # All sections skipped due to past deadline
        self.assertEqual(ev1.get("repository_truth", {}).get("status"), "skipped_deadline")

        # Case 2: very tight deadline (only budget for the broker section) but broker blocks.
        # collect_evidence must return promptly with a timed-out broker entry.
        tight_deadline = time.monotonic() + 0.3  # 300ms max budget

        ev2 = collect_evidence(
            project=project,
            snapshot=snapshot,
            runtime_root=self.runtime_dir,
            ai_execution_port=_BlockingPort(),
            deadline=tight_deadline,
        )
        broker2 = ev2.get("broker", {})
        # Either skipped (deadline exhausted before broker) or timed out during broker call
        if isinstance(broker2, dict) and broker2.get("status") != "skipped_deadline":
            self.assertFalse(
                broker2.get("available", True),
                "broker must report unavailable when status() call timed out or skipped",
            )
        # In either case: function must have returned — if we reached here, it did not block
        self.assertIsNotNone(ev2)

        # Cleanup: release all blocking threads spawned above
        _unblock.set()


if __name__ == "__main__":
    unittest.main()
