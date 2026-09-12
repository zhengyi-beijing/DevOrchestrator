import datetime as _dt
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_orchestrator.core.watchdog import (
    WATCHDOG_COMMAND_PREFIX,
    WatchdogCoordinator,
)


def _recent_completed_at(minutes_ago: float = 5.0) -> str:
    """Return a recent ISO-8601 UTC timestamp for use in test fixtures."""
    return (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)
    ).isoformat()


class DummyProgressChannel:
    def __init__(self):
        self.events = []

    def emit(self, project_id, milestone, **kwargs):
        self.events.append((project_id, milestone, kwargs))


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (root / "README.md").write_text("# Test", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WatchdogRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.repo_dir = self.root / "repo"
        self.runtime_dir = self.root / "runtime"
        self.repo_dir.mkdir(parents=True)
        self.runtime_dir.mkdir(parents=True)
        _init_git_repo(self.repo_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_auto_recovery_disabled_no_command(self):
        """When auto_recovery is disabled (default), no recovery command is queued."""
        coordinator = WatchdogCoordinator(self.runtime_dir)
        prow = {"attempts": {}}
        att = {
            "attempt_key": "att-1",
            "run_scope_key": "rscope-1",
            "diagnosis": "agent_stalled",
            "owner_gate_required": True,
        }
        coordinator._check_and_trigger_recovery(
            project_config={"project_id": "p1", "repo_path": str(self.repo_dir)},
            snapshot={"project_id": "p1"},
            project_row=prow,
            attempt_key="att-1",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        inbox_files = list((self.runtime_dir / "control" / "inbox").glob("*.json"))
        self.assertEqual(len(inbox_files), 0)

    def test_two_phase_recovery_execution(self):
        """Eligible stall with auto_recovery=true executes reserve and enqueue."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: True,  # FR2B-LIVE-IDENTITY: stub confirms PID alive
        )
        att = {
            "attempt_key": "att-auto",
            "run_scope_key": "rscope-1",
            "task_id": "T1",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            # FR-2A: completed_at required for recovery to proceed
            "completed_at": _recent_completed_at(),
            # R3-F1/F2: evidence must match current snapshot for recovery to proceed
            "evidence_hash": "bc9a67e4be8cf301",
            "evidence": {
                "process_liveness": {"process_alive": True, "pid": 1234, "worker_state": "running"},
            },
        }
        prow = {"attempts": {"att-auto": att}}
        coordinator._cached_state["projects"]["p1"] = prow

        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # R3-F1: snapshot must show EXECUTING lifecycle for agent_stalled recovery
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 1234, "state": "running"}},
            project_row=prow,
            attempt_key="att-auto",
            attempt_record=att,
        )

        # 1. RESERVE check
        expected_cid = f"{WATCHDOG_COMMAND_PREFIX}att-auto"
        self.assertEqual(prow["recovery_slots"]["rscope-1"], expected_cid)
        rec = att.get("recovery")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["action"], "continue")
        self.assertEqual(rec["command_id"], expected_cid)
        self.assertEqual(rec["state"], "requested")

        # 2. ENQUEUE check: file in control inbox
        inbox_file = self.runtime_dir / "control" / "inbox" / f"{expected_cid}.json"
        self.assertTrue(inbox_file.is_file())
        cmd_data = json.loads(inbox_file.read_text(encoding="utf-8"))
        self.assertEqual(cmd_data["action"], "continue")
        self.assertEqual(cmd_data["project_id"], "p1")
        self.assertEqual(cmd_data["command_id"], expected_cid)

        # 3. RECOVERY_STARTED milestone emitted
        rec_events = [e for e in channel.events if e[1] == "RECOVERY_STARTED"]
        self.assertEqual(len(rec_events), 1)
        self.assertEqual(rec_events[0][2]["details"]["command_id"], expected_cid)

    def test_single_recovery_per_run_scope_budget(self):
        """Second recovery attempt in same run scope is blocked with OWNER_GATE."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: True,  # FR2B-LIVE-IDENTITY: stub confirms PID alive
        )
        prow = {
            "recovery_slots": {"rscope-1": "wd-prior-attempt"},
            "attempts": {},
        }
        att2 = {
            "attempt_key": "att-second",
            "run_scope_key": "rscope-1",
            "task_id": "T1",
            "state": "completed",
            "diagnosis": "agent_stalled",
            # FR-2A: completed_at required so check reaches slot verification
            "completed_at": _recent_completed_at(),
            "evidence_hash": "cacf7f4ba8bc2796",
            "evidence": {
                "process_liveness": {"process_alive": True, "pid": 9876, "worker_state": "running"},
            },
        }
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # R3-F1: EXECUTING lifecycle so agent_stalled check passes before slot check
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 9876, "state": "running"}},
            project_row=prow,
            attempt_key="att-second",
            attempt_record=att2,
        )
        self.assertIsNone(att2.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "recovery_slot_already_consumed")

    def test_reconcile_crashed_before_enqueue(self):
        """If coordinator crashed after RESERVE but before ENQUEUE, reconcile marks unresolved."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

        cid = "wd-crashed-before-enqueue"
        att = {
            "attempt_key": "att-crash",
            "run_scope_key": "rscope-1",
            "recovery": {
                "action": "continue",
                "state": "reserved",
                "command_id": cid,
            },
        }
        coordinator._cached_state["projects"]["p1"] = {
            "attempts": {"att-crash": att},
            "recovery_slots": {"rscope-1": cid},
        }

        # Neither inbox nor history has the file!
        coordinator._reconcile_recoveries()

        self.assertEqual(att["recovery"]["state"], "unresolved")
        self.assertIn("never enqueued", att["recovery"]["reason"])
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["gate"], "recovery-unresolved")

    def test_reconcile_completed_from_history(self):
        """If recovery command was executed and moved to history, reconcile marks completed."""
        coordinator = WatchdogCoordinator(self.runtime_dir)

        cid = "wd-att-finished"
        history_dir = self.runtime_dir / "control" / "history"
        history_dir.mkdir(parents=True)
        (history_dir / f"{cid}.json").write_text(
            json.dumps({"command_id": cid, "state": "accepted"}),
            encoding="utf-8",
        )

        att = {
            "attempt_key": "att-finished",
            "recovery": {
                "action": "continue",
                "state": "requested",
                "command_id": cid,
            },
        }
        coordinator._cached_state["projects"]["p1"] = {
            "attempts": {"att-finished": att},
            "recovery_slots": {"rscope-1": cid},
        }

        coordinator._reconcile_recoveries()

        self.assertEqual(att["recovery"]["state"], "completed")
        self.assertIn("accepted", att["recovery"]["reason"])

    def test_reconcile_history_state_mapping_uses_real_control_vocabulary(self):
        coordinator = WatchdogCoordinator(self.runtime_dir)
        history_dir = self.runtime_dir / "control" / "history"
        history_dir.mkdir(parents=True)
        for cid, outcome in {
            "wd-accepted": "accepted",
            "wd-blocked": "blocked",
            "wd-owner-gate": "owner_gate",
            "wd-unknown": "mystery_state",
        }.items():
            (history_dir / f"{cid}.json").write_text(
                json.dumps({"command_id": cid, "state": outcome}),
                encoding="utf-8",
            )

        attempts = {}
        for index, cid in enumerate(("wd-accepted", "wd-blocked", "wd-owner-gate", "wd-unknown"), start=1):
            attempts[f"att-{index}"] = {
                "attempt_key": f"att-{index}",
                "recovery": {"action": "continue", "state": "requested", "command_id": cid},
            }
        coordinator._cached_state["projects"]["p1"] = {
            "attempts": attempts,
            "recovery_slots": {},
        }

        coordinator._reconcile_recoveries()

        self.assertEqual(attempts["att-1"]["recovery"]["state"], "completed")
        self.assertEqual(attempts["att-2"]["recovery"]["state"], "blocked")
        self.assertEqual(attempts["att-3"]["recovery"]["state"], "blocked")
        self.assertEqual(attempts["att-4"]["recovery"]["state"], "unknown")

    def test_running_attempt_does_not_trigger_recovery_or_gate(self):
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        att = {
            "attempt_key": "att-running",
            "run_scope_key": "rscope-1",
            "state": "running",
            "diagnosis": None,
            "owner_gate_required": False,
        }

        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir)},
            project_row={"attempts": {"att-running": att}},
            attempt_key="att-running",
            attempt_record=att,
        )

        self.assertIsNone(att.get("recovery"))
        self.assertEqual([event for event in channel.events if event[1] == "OWNER_GATE"], [])

    def test_owner_gate_occurrence_keys_are_reason_specific(self):
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        attempt = {
            "attempt_key": "att-gate",
            "task_id": "T1",
            "diagnosis": "unknown",
            "evidence_hash": "abcd1234",
        }

        coordinator._emit_owner_gate_once("p1", attempt, "diagnosis_unknown_requires_owner")
        coordinator._emit_owner_gate_once("p1", attempt, "process_alive_ambiguous")

        gate_keys = [event[2]["occurrence_key"] for event in channel.events if event[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_keys), 2)
        self.assertNotEqual(gate_keys[0], gate_keys[1])
        self.assertTrue(gate_keys[0].startswith("att-gate:gate:"))

    def test_f2_process_dead_recovery_uses_evidence_not_snapshot_worker(self):
        """F2 regression: process_dead recovery must consult process_liveness from diagnostic
        evidence (actual PID probe), NOT snapshot.worker.process_alive from executor ledger."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

        # snapshot.worker.process_alive=False (stale ledger value) but evidence says alive=True
        stale_snapshot = {
            "project_id": "p1",
            "repo_path": str(self.repo_dir),
            "worker": {"kind": "task", "state": "running", "process_alive": False, "pid": 1234},
        }
        att_stale_alive = {
            "attempt_key": "att-f2-stale-alive",
            "run_scope_key": "rscope-f2",
            "task_id": "T1",
            "state": "completed",
            "diagnosis": "process_dead",
            "owner_gate_required": False,
            "evidence_hash": "8b115b0d4538629a",
            # Actual PID probe says alive=True (contradicts stale snapshot)
            "evidence": {
                "process_liveness": {"process_alive": True, "pid": 1234},
            },
        }
        prow = {"attempts": {"att-f2-stale-alive": att_stale_alive}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot=stale_snapshot,
            project_row=prow,
            attempt_key="att-f2-stale-alive",
            attempt_record=att_stale_alive,
        )
        # Must NOT recover - evidence says alive, ignore stale snapshot.worker.process_alive
        self.assertIsNone(att_stale_alive.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "process_alive_ambiguous")

    def test_f2_process_dead_recovery_proceeds_when_evidence_confirms_dead(self):
        """F2 regression: process_dead recovery proceeds when evidence.process_liveness confirms dead PID."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: False,  # FR2B-LIVE-IDENTITY: stub confirms PID dead
        )

        att_confirmed_dead = {
            "attempt_key": "att-f2-confirmed",
            "run_scope_key": "rscope-f2b",
            "task_id": "T1",
            "state": "completed",
            "diagnosis": "process_dead",
            "owner_gate_required": False,
            # FR-2A: completed_at required; FR-2B: snapshot must supply matching pid
            "completed_at": _recent_completed_at(),
            "evidence_hash": "46002a75ab06c6c7",
            # Evidence confirms process is truly dead
            "evidence": {
                "process_liveness": {"process_alive": False, "pid": 5678},
            },
        }
        prow = {"attempts": {"att-f2-confirmed": att_confirmed_dead}}
        coordinator._cached_state["projects"]["p1"] = prow
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # FR-2B: worker pid in snapshot must match evidence pid for process_dead
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "worker": {"pid": 5678, "state": "completed"}},
            project_row=prow,
            attempt_key="att-f2-confirmed",
            attempt_record=att_confirmed_dead,
        )
        # Must recover since evidence.process_liveness.process_alive is False
        rec = att_confirmed_dead.get("recovery")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["action"], "continue")
        self.assertIn(rec["state"], ("requested", "reserved"))

    def test_f2_overlay_managed_runs_sets_process_alive_false_for_completed(self):
        """F2 regression: overlay_managed_runs sets worker.process_alive=False for completed runs.
        Watchdog recovery must use diagnostic evidence, not this stale overlay field."""
        from dev_orchestrator.core.transition_executor import TransitionExecutor

        ledger_data = {"version": 1, "executions": {
            "run-p1": {
                "project_id": "p1", "source_request_id": "run-p1",
                "source_kind": "control", "task_id": "T10",
                "backend_id": "dsh", "state": "completed",
                "started_at": "2026-09-10T01:00:00+00:00",
                "completed_at": "2026-09-10T01:30:00+00:00",
            }
        }}
        (self.runtime_dir / "transition-executor.json").write_text(
            json.dumps(ledger_data), encoding="utf-8"
        )

        raw = {"projects": [{"project_id": "p1", "state": "WORKER_RUNNING",
                              "worker": {"kind": "task", "state": "running", "process_alive": True},
                              "telemetry": {"task_id": "T10", "run_id": None}}]}
        executor = TransitionExecutor(self.runtime_dir)
        overlaid = executor.overlay_managed_runs(raw)
        # After overlay, worker.process_alive should be False for completed run
        snap = overlaid["projects"][0]
        self.assertFalse(snap["worker"]["process_alive"])
        # process_alive=False in snapshot must NOT cause recovery without evidence
        # - watchdog reads evidence.process_liveness, not snapshot.worker.process_alive

    # -----------------------------------------------------------------------
    # R3-F1 regressions
    # -----------------------------------------------------------------------

    def test_f1_agent_stalled_recovery_requires_worker_lifecycle_state(self):
        """R3-F1: agent_stalled recovery must be blocked when current snapshot lifecycle
        is not a worker-expected state (EXECUTING / REMEDIATING).  A stale alive PID
        from a previous execution in PLANNING must not enqueue a wd- continue command."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

        att = {
            "attempt_key": "att-f1-planning",
            "run_scope_key": "rscope-f1",
            "task_id": "T1",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": "bc9a67e4be8cf301",
            "evidence": {
                "process_liveness": {"process_alive": True, "pid": 1234, "worker_state": "running"},
            },
            "completed_at": "2099-01-01T00:00:00+00:00",  # far future → evidence not stale
        }
        prow = {"attempts": {"att-f1-planning": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # Current snapshot: lifecycle is PLANNING (not EXECUTING/REMEDIATING)
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "state": "PLANNING", "lifecycle_state": "PLANNING",
                      "worker": {"pid": 1234, "state": "running"}},
            project_row=prow,
            attempt_key="att-f1-planning",
            attempt_record=att,
        )
        # Must NOT enqueue wd- because lifecycle is PLANNING
        self.assertIsNone(att.get("recovery"))
        inbox_files = list((self.runtime_dir / "control" / "inbox").glob("wd-*.json"))
        self.assertEqual(len(inbox_files), 0)
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("non_worker_lifecycle", gate_events[0][2]["details"]["reason"])

    def test_f1_agent_stalled_worker_state_not_active_blocks_recovery(self):
        """R3-F1: agent_stalled recovery blocked when evidence worker_state is not active."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

        att = {
            "attempt_key": "att-f1-completed-worker",
            "run_scope_key": "rscope-f1b",
            "task_id": "T2",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": "f4bf04ef9f2c66f7",
            "evidence": {
                "process_liveness": {
                    "process_alive": True, "pid": 5678,
                    "worker_state": "completed",  # not an active execution state
                },
            },
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
        prow = {"attempts": {"att-f1-completed-worker": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 5678, "state": "completed"}},
            project_row=prow,
            attempt_key="att-f1-completed-worker",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("worker_not_active", gate_events[0][2]["details"]["reason"])

    def test_f1_planning_pending_design_stale_pid_cannot_enqueue_wd(self):
        """R3-F1 end-to-end: PLANNING + PENDING DESIGN + alive stale PID cannot result in
        wd-continue being enqueued or a second planner cycle being started.

        Scenario:
          1. Project previously EXECUTING with PID=1234.
          2. Watchdog diagnosed agent_stalled for that execution (evidence: PID=1234 alive).
          3. Worker completed normally, review finished, project now PLANNING + PENDING DESIGN.
          4. Stale PID=1234 still alive (OS reuse or long-lived monitor process).
          5. Dedup path fires (same fingerprint) with auto_recovery=True.
          6. Expected: owner_gate emitted, no wd- command enqueued, no planner started.
        """
        from dev_orchestrator.core.control_commands import ControlCommandCoordinator

        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

        att = {
            "attempt_key": "att-f1-e2e",
            "run_scope_key": "rscope-e2e",
            "task_id": "T99",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": "bc9a67e4be8cf301",
            "evidence": {
                "process_liveness": {"process_alive": True, "pid": 1234, "worker_state": "running"},
            },
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
        prow = {"attempts": {"att-f1-e2e": att}}
        coordinator._cached_state["projects"]["p1"] = prow

        planning_snapshot = {
            "project_id": "p1",
            "repo_path": str(self.repo_dir),
            "state": "PLANNING",
            "lifecycle_state": "PLANNING",
            "next_status": "PENDING DESIGN: implement feature X",
            "worker": {"pid": 1234},  # stale PID still in snapshot
        }

        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot=planning_snapshot,
            project_row=prow,
            attempt_key="att-f1-e2e",
            attempt_record=att,
        )

        # 1. No recovery was reserved
        self.assertIsNone(att.get("recovery"))
        # 2. No wd- command was enqueued
        inbox_files = list((self.runtime_dir / "control" / "inbox").glob("wd-*.json"))
        self.assertEqual(len(inbox_files), 0, "wd- command must not be enqueued for PLANNING lifecycle")
        # 3. OWNER_GATE was emitted
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertGreater(len(gate_events), 0, "OWNER_GATE must be emitted for PLANNING+stale PID scenario")
        # 4. ControlCommandCoordinator has nothing to process
        ctrl = ControlCommandCoordinator(self.runtime_dir)
        inbox_dir = self.runtime_dir / "control" / "inbox"
        if inbox_dir.exists():
            self.assertEqual(list(inbox_dir.glob("*.json")), [])

    # -----------------------------------------------------------------------
    # R3-F2 regressions
    # -----------------------------------------------------------------------

    def test_f2_agent_stalled_recovery_blocked_when_pid_differs(self):
        """R3-F2: agent_stalled recovery blocked when current snapshot PID differs from evidence PID.
        A new execution started after diagnosis with a different PID must not be recovered
        using stale evidence from the old execution."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

        att = {
            "attempt_key": "att-f2-pid-diff",
            "run_scope_key": "rscope-f2a",
            "task_id": "T3",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": "9d5dbaeb4fcb643e",
            "evidence": {
                "process_liveness": {"process_alive": True, "pid": 1111, "worker_state": "running"},
            },
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
        prow = {"attempts": {"att-f2-pid-diff": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # New execution with different PID
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 9999, "state": "running"}},
            project_row=prow,
            attempt_key="att-f2-pid-diff",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("pid_mismatch", gate_events[0][2]["details"]["reason"])

    def test_f2_process_dead_recovery_blocked_when_pid_differs(self):
        """R3-F2: process_dead recovery blocked when current snapshot PID differs from evidence PID."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

        att = {
            "attempt_key": "att-f2-dead-pid-diff",
            "run_scope_key": "rscope-f2b",
            "task_id": "T4",
            "state": "completed",
            "diagnosis": "process_dead",
            "owner_gate_required": False,
            "evidence_hash": "9ad1184a878e181d",
            "evidence": {
                "process_liveness": {"process_alive": False, "pid": 2222},
            },
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
        prow = {"attempts": {"att-f2-dead-pid-diff": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # New execution started with different PID
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 8888, "state": "running"}},
            project_row=prow,
            attempt_key="att-f2-dead-pid-diff",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("pid_mismatch", gate_events[0][2]["details"]["reason"])

    def test_f2_evidence_stale_beyond_cooldown_blocks_recovery(self):
        """R3-F2: recovery blocked when evidence is older than cooldown window.
        Prevents auto_recovery toggled on long after diagnosis from consuming stale evidence."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: True,  # FR2B-LIVE-IDENTITY: stub confirms PID alive
        )

        # completed_at is in the past beyond any cooldown (simulated by old timestamp)
        att = {
            "attempt_key": "att-f2-stale",
            "run_scope_key": "rscope-f2c",
            "task_id": "T5",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": "341c0da696fd584b",
            "evidence": {
                "process_liveness": {"process_alive": True, "pid": 3333, "worker_state": "running"},
            },
            "completed_at": "2020-01-01T00:00:00+00:00",  # very old evidence
        }
        prow = {
            "attempts": {"att-f2-stale": att},
            "cooldown_minutes": 30,
        }
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 3333, "state": "running"}},
            project_row=prow,
            attempt_key="att-f2-stale",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("stale", gate_events[0][2]["details"]["reason"])

    def test_f2_auto_recovery_toggled_on_within_cooldown_allows_recovery(self):
        """R3-F2: if auto_recovery is toggled on within the cooldown window (fresh evidence),
        recovery DOES proceed (PID matches, lifecycle matches, evidence fresh)."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: True,  # FR2B-LIVE-IDENTITY: stub confirms PID alive
        )

        import datetime as dt
        # completed 5 minutes ago, within 30-minute cooldown
        recent_completed = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        ).isoformat()

        att = {
            "attempt_key": "att-f2-fresh",
            "run_scope_key": "rscope-f2d",
            "task_id": "T6",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": "d1237131c6857856",
            "evidence": {
                "process_liveness": {"process_alive": True, "pid": 4444, "worker_state": "running"},
            },
            "completed_at": recent_completed,
        }
        prow = {
            "attempts": {"att-f2-fresh": att},
            "cooldown_minutes": 30,
        }
        coordinator._cached_state["projects"]["p1"] = prow
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 4444, "state": "running"}},
            project_row=prow,
            attempt_key="att-f2-fresh",
            attempt_record=att,
        )
        # Should succeed (evidence is fresh, PID matches, lifecycle correct)
        rec = att.get("recovery")
        self.assertIsNotNone(rec, "Recovery should proceed with fresh evidence and matching PID")
        self.assertEqual(rec["action"], "continue")
        self.assertIn(rec["state"], ("requested", "reserved"))

    def test_f6_prune_retains_consumed_recovery_slot_beyond_attempt_window(self):
        """R3-F6: pruning must not discard recovery_slots for run scopes whose attempt fell
        off the MAX_TERMINAL_ATTEMPTS_PER_PROJECT window.  The slot guards against duplicate
        recovery within the same run scope."""
        from dev_orchestrator.core.watchdog import MAX_TERMINAL_ATTEMPTS_PER_PROJECT

        coordinator = WatchdogCoordinator(self.runtime_dir)
        prow = {
            "attempts": {},
            "attempt_counts": {},
            "recovery_slots": {"rscope-old": "wd-old-attempt"},  # consumed slot, attempt already pruned
        }
        coordinator._cached_state["projects"]["p1"] = prow

        # Fill up the terminal window with MAX+1 fake terminal attempts for a different scope
        for i in range(MAX_TERMINAL_ATTEMPTS_PER_PROJECT + 1):
            prow["attempts"][f"att-{i}"] = {
                "attempt_key": f"att-{i}",
                "run_scope_key": "rscope-new",
                "state": "completed",
                "completed_at": f"2026-01-0{max(1, i % 9)}T00:00:00+00:00",
            }

        coordinator._save_state(coordinator._cached_state)

        # After pruning (triggered by _save_state), the slot for rscope-old must survive
        state = coordinator.state()
        slots = state["projects"]["p1"].get("recovery_slots", {})
        self.assertIn(
            "rscope-old",
            slots,
            "Consumed recovery slot must be retained even when corresponding attempt is pruned",
        )
        self.assertEqual(slots["rscope-old"], "wd-old-attempt")

    # -----------------------------------------------------------------------
    # P10-FR-1 regressions
    # -----------------------------------------------------------------------

    def test_fr1_stale_snapshot_not_used_at_diagnostic_completion(self):
        """P10-FR-1: Diagnostic starts in EXECUTING, lifecycle transitions to PLANNING and
        REVIEWING before completion.  Recovery actuation is deferred to the next advance tick
        which supplies a fresh snapshot.  No wd-continue must be reserved or enqueued when
        the fresh snapshot shows a non-worker lifecycle."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True, "pid": 2020, "worker_state": "running"}}
        eh = compute_ev_hash(evidence)

        for lifecycle in ("PLANNING", "REVIEWING"):
            with self.subTest(lifecycle=lifecycle):
                channel = DummyProgressChannel()
                coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

                att = {
                    "attempt_key": "att-fr1-deferred",
                    "run_scope_key": "rscope-fr1",
                    "task_id": "T-fr1",
                    "state": "completed",
                    "diagnosis": "agent_stalled",
                    "owner_gate_required": False,
                    "evidence_hash": eh,
                    "evidence": evidence,
                    "completed_at": "2099-01-01T00:00:00+00:00",
                }
                prow = {"attempts": {"att-fr1-deferred": att}}
                coordinator._cached_state["projects"]["p1"] = prow

                # Simulate the next advance() tick supplying a fresh snapshot with changed lifecycle
                coordinator._check_and_trigger_recovery(
                    project_config={
                        "project_id": "p1",
                        "repo_path": str(self.repo_dir),
                        "watchdog": {"enabled": True, "auto_recovery": True},
                    },
                    snapshot={
                        "project_id": "p1",
                        "repo_path": str(self.repo_dir),
                        "lifecycle_state": lifecycle,
                        "worker": {"pid": 2020, "state": "running"},
                    },
                    project_row=prow,
                    attempt_key="att-fr1-deferred",
                    attempt_record=att,
                )

                self.assertIsNone(att.get("recovery"), f"No recovery expected when lifecycle={lifecycle}")
                inbox_files = list((self.runtime_dir / "control" / "inbox").glob("wd-*.json"))
                self.assertEqual(len(inbox_files), 0, f"No wd- command expected when lifecycle={lifecycle}")
                gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
                self.assertGreater(len(gate_events), 0, f"OWNER_GATE must fire when lifecycle={lifecycle}")
                self.assertIn("non_worker_lifecycle", gate_events[0][2]["details"]["reason"])

    # -----------------------------------------------------------------------
    # P10-FR-2 regressions
    # -----------------------------------------------------------------------

    def test_fr2_altered_evidence_hash_quarantines_attempt(self):
        """P10-FR-2: If stored evidence_hash does not match recomputed hash, the attempt is
        quarantined and recovery is blocked."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

        att = {
            "attempt_key": "att-fr2-altered",
            "run_scope_key": "rscope-fr2a",
            "task_id": "T-fr2",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": "deadbeefdeadbeef",  # deliberately wrong hash
            "evidence": {
                "process_liveness": {"process_alive": True, "pid": 5000, "worker_state": "running"},
            },
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
        prow = {"attempts": {"att-fr2-altered": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING", "worker": {"pid": 5000}},
            project_row=prow,
            attempt_key="att-fr2-altered",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        self.assertEqual(att.get("state"), "quarantined")
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "evidence_hash_mismatch")

    def test_fr2_missing_evidence_hash_blocks_recovery(self):
        """P10-FR-2: Attempt without evidence_hash is blocked with owner gate."""
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)

        att = {
            "attempt_key": "att-fr2-nohash",
            "run_scope_key": "rscope-fr2b",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            # no evidence_hash field
            "evidence": {
                "process_liveness": {"process_alive": True, "pid": 5001, "worker_state": "running"},
            },
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
        prow = {"attempts": {"att-fr2-nohash": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING"},
            project_row=prow,
            attempt_key="att-fr2-nohash",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("evidence_hash", gate_events[0][2]["details"]["reason"])

    def test_fr2_missing_pid_blocks_recovery(self):
        """P10-FR-2: Evidence without pid in process_liveness is blocked with owner gate."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True}}  # no pid
        att = {
            "attempt_key": "att-fr2-nopid",
            "run_scope_key": "rscope-fr2c",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        prow = {"attempts": {"att-fr2-nopid": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING"},
            project_row=prow,
            attempt_key="att-fr2-nopid",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("missing_pid", gate_events[0][2]["details"]["reason"])

    def test_fr2_missing_process_alive_blocks_recovery(self):
        """P10-FR-2: Evidence without process_alive key in process_liveness is blocked."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"pid": 5002}}  # no process_alive
        att = {
            "attempt_key": "att-fr2-noalive",
            "run_scope_key": "rscope-fr2d",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        prow = {"attempts": {"att-fr2-noalive": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING"},
            project_row=prow,
            attempt_key="att-fr2-noalive",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("missing_process_alive", gate_events[0][2]["details"]["reason"])

    def test_fr2_malformed_completed_attempt_no_evidence_dict(self):
        """P10-FR-2: Attempt with non-dict evidence is blocked with owner gate (fail closed)."""
        att = {
            "attempt_key": "att-fr2-noev",
            "run_scope_key": "rscope-fr2e",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": "somevalue",
            "evidence": "corrupted-string-not-a-dict",
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        prow = {"attempts": {"att-fr2-noev": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING"},
            project_row=prow,
            attempt_key="att-fr2-noev",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("no_evidence", gate_events[0][2]["details"]["reason"])

    def test_fr2_agent_stalled_with_dead_pid_inconsistent_with_diagnosis(self):
        """P10-FR-2: agent_stalled diagnosis with process_alive=False is blocked (liveness
        inconsistent with diagnosis — agent_stalled requires a live process)."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": False, "pid": 5003}}
        att = {
            "attempt_key": "att-fr2-dead-stalled",
            "run_scope_key": "rscope-fr2f",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
            "completed_at": "2099-01-01T00:00:00+00:00",
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        prow = {"attempts": {"att-fr2-dead-stalled": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 5003}},
            project_row=prow,
            attempt_key="att-fr2-dead-stalled",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("inconsistent", gate_events[0][2]["details"]["reason"])

    # -----------------------------------------------------------------------
    # P10-FR-2A: completed_at must be present, parseable, not future, within window
    # -----------------------------------------------------------------------

    def test_fr2a_missing_completed_at_blocks_recovery(self):
        """FR-2A: Recovery must be blocked when completed_at is absent from the attempt record."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True, "pid": 7001, "worker_state": "running"}}
        att = {
            "attempt_key": "att-fr2a-missing-cat",
            "run_scope_key": "rscope-fr2a-1",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
            # no completed_at
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: True,  # FR2B-LIVE-IDENTITY: stub so check reaches FR-2A
        )
        prow = {"attempts": {"att-fr2a-missing-cat": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 7001, "state": "running"}},
            project_row=prow,
            attempt_key="att-fr2a-missing-cat",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "evidence_missing_completed_at")

    def test_fr2a_malformed_completed_at_blocks_recovery(self):
        """FR-2A: Recovery must be blocked when completed_at is present but not parseable."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True, "pid": 7002, "worker_state": "running"}}
        att = {
            "attempt_key": "att-fr2a-malformed-cat",
            "run_scope_key": "rscope-fr2a-2",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
            "completed_at": "not-a-valid-timestamp",
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: True,  # FR2B-LIVE-IDENTITY: stub so check reaches FR-2A
        )
        prow = {"attempts": {"att-fr2a-malformed-cat": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 7002, "state": "running"}},
            project_row=prow,
            attempt_key="att-fr2a-malformed-cat",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "evidence_unparseable_completed_at")

    def test_fr2a_future_completed_at_beyond_clock_skew_blocks_recovery(self):
        """FR-2A: Recovery must be blocked when completed_at is materially in the future
        (beyond the documented clock-skew tolerance)."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash
        from dev_orchestrator.core.watchdog import RECOVERY_COMPLETED_AT_CLOCK_SKEW_S

        evidence = {"process_liveness": {"process_alive": True, "pid": 7003, "worker_state": "running"}}
        # Set completed_at to well beyond the clock-skew tolerance
        far_future = (
            _dt.datetime.now(_dt.timezone.utc)
            + _dt.timedelta(seconds=RECOVERY_COMPLETED_AT_CLOCK_SKEW_S + 120)
        ).isoformat()
        att = {
            "attempt_key": "att-fr2a-future-cat",
            "run_scope_key": "rscope-fr2a-3",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
            "completed_at": far_future,
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: True,  # FR2B-LIVE-IDENTITY: stub so check reaches FR-2A
        )
        prow = {"attempts": {"att-fr2a-future-cat": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 7003, "state": "running"}},
            project_row=prow,
            attempt_key="att-fr2a-future-cat",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "evidence_completed_at_future")

    def test_fr2a_stale_completed_at_blocks_recovery(self):
        """FR-2A: Recovery must be blocked when completed_at is present and parseable but
        older than the configured cooldown window."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True, "pid": 7004, "worker_state": "running"}}
        att = {
            "attempt_key": "att-fr2a-stale-cat",
            "run_scope_key": "rscope-fr2a-4",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
            "completed_at": "2022-01-01T00:00:00+00:00",  # clearly stale
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: True,  # FR2B-LIVE-IDENTITY: stub so check reaches FR-2A
        )
        prow = {"attempts": {"att-fr2a-stale-cat": att}, "cooldown_minutes": 30}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 7004, "state": "running"}},
            project_row=prow,
            attempt_key="att-fr2a-stale-cat",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertIn("stale", gate_events[0][2]["details"]["reason"])

    # -----------------------------------------------------------------------
    # P10-FR-2B: absent current PID must fail closed for both diagnosis types
    # -----------------------------------------------------------------------

    def test_fr2b_agent_stalled_absent_current_pid_blocks_recovery(self):
        """FR-2B: agent_stalled recovery must be blocked when the current snapshot worker has
        no PID.  Absent identity is not a safe match — prefer safety over recovery availability."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True, "pid": 8001, "worker_state": "running"}}
        att = {
            "attempt_key": "att-fr2b-stalled-nopid",
            "run_scope_key": "rscope-fr2b-1",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        prow = {"attempts": {"att-fr2b-stalled-nopid": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # Current worker has no PID — should fail closed
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING",
                      "worker": {"state": "running"}},  # no "pid" key
            project_row=prow,
            attempt_key="att-fr2b-stalled-nopid",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "agent_stalled_current_pid_absent")

    def test_fr2b_agent_stalled_no_worker_in_snapshot_blocks_recovery(self):
        """FR-2B: agent_stalled recovery must be blocked when the snapshot has no worker at all.
        The current execution identity cannot be confirmed — fail closed."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True, "pid": 8002, "worker_state": "running"}}
        att = {
            "attempt_key": "att-fr2b-stalled-noworker",
            "run_scope_key": "rscope-fr2b-2",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        prow = {"attempts": {"att-fr2b-stalled-noworker": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # Snapshot has no worker dict at all
            snapshot={"project_id": "p1", "lifecycle_state": "EXECUTING"},
            project_row=prow,
            attempt_key="att-fr2b-stalled-noworker",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "agent_stalled_current_pid_absent")

    def test_fr2b_process_dead_absent_current_pid_blocks_recovery(self):
        """FR-2B: process_dead recovery must be blocked when the current snapshot worker has no PID.
        Auto-recovery must not proceed without a confirmed current execution identity."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": False, "pid": 8003}}
        att = {
            "attempt_key": "att-fr2b-dead-nopid",
            "run_scope_key": "rscope-fr2b-3",
            "state": "completed",
            "diagnosis": "process_dead",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        prow = {"attempts": {"att-fr2b-dead-nopid": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # Current snapshot has no worker pid
            snapshot={"project_id": "p1", "worker": {"state": "completed"}},  # no "pid" key
            project_row=prow,
            attempt_key="att-fr2b-dead-nopid",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "process_dead_current_pid_absent")

    def test_fr2b_process_dead_no_worker_in_snapshot_blocks_recovery(self):
        """FR-2B: process_dead recovery must be blocked when the snapshot has no worker at all
        (not just absent PID).  Block automatic recovery; prefer safety over recovery availability."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": False, "pid": 8004}}
        att = {
            "attempt_key": "att-fr2b-dead-noworker",
            "run_scope_key": "rscope-fr2b-4",
            "state": "completed",
            "diagnosis": "process_dead",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(self.runtime_dir, progress_channel=channel)
        prow = {"attempts": {"att-fr2b-dead-noworker": att}}
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # Snapshot has no worker dict at all
            snapshot={"project_id": "p1"},
            project_row=prow,
            attempt_key="att-fr2b-dead-noworker",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "process_dead_current_pid_absent")

    # -----------------------------------------------------------------------
    # P10-FR2B-LIVE-IDENTITY: live liveness re-probe at recovery actuation time
    # -----------------------------------------------------------------------

    def test_fr2b_live_agent_stalled_pid_dead_at_recovery(self):
        """FR2B-LIVE-IDENTITY: agent_stalled recovery must be blocked when the live re-probe
        at recovery time reveals the PID is no longer alive (process died after snapshot or
        PID was reused and the prior process ended).  Snapshot match alone is insufficient."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True, "pid": 9001, "worker_state": "running"}}
        att = {
            "attempt_key": "att-live-stalled-dead",
            "run_scope_key": "rscope-live-1",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            # Live re-probe says process is dead (died after snapshot was captured)
            liveness_probe=lambda p: False,
        )
        prow = {"attempts": {"att-live-stalled-dead": att}}
        coordinator._cached_state["projects"]["p1"] = prow
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 9001, "state": "running"}},
            project_row=prow,
            attempt_key="att-live-stalled-dead",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "agent_stalled_pid_not_alive_at_recovery")

    def test_fr2b_live_agent_stalled_current_worker_not_active(self):
        """FR2B-LIVE-IDENTITY: agent_stalled recovery must be blocked when the current snapshot
        worker state is terminal/inactive at recovery time.  A sufficiently current active Worker
        identity is required before enqueuing recovery."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True, "pid": 9002, "worker_state": "running"}}
        att = {
            "attempt_key": "att-live-stalled-inactive",
            "run_scope_key": "rscope-live-2",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: True,  # probe would pass; gate is worker state check
        )
        prow = {"attempts": {"att-live-stalled-inactive": att}}
        coordinator._cached_state["projects"]["p1"] = prow
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            # Current snapshot: worker state is "completed" (terminal), not an active state
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 9002, "state": "completed"}},
            project_row=prow,
            attempt_key="att-live-stalled-inactive",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "agent_stalled_current_worker_not_active")

    def test_fr2b_live_agent_stalled_liveness_probe_unavailable(self):
        """FR2B-LIVE-IDENTITY: agent_stalled recovery must fail closed when the liveness probe
        raises an exception.  An unavailable probe must not be treated as a safe match."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True, "pid": 9003, "worker_state": "running"}}
        att = {
            "attempt_key": "att-live-stalled-probe-err",
            "run_scope_key": "rscope-live-3",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()

        def _raise_probe(p):
            raise OSError("kernel probe unavailable")

        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=_raise_probe,
        )
        prow = {"attempts": {"att-live-stalled-probe-err": att}}
        coordinator._cached_state["projects"]["p1"] = prow
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 9003, "state": "running"}},
            project_row=prow,
            attempt_key="att-live-stalled-probe-err",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        # Probe exception → live_alive=None → not True → same gate as dead PID
        self.assertEqual(gate_events[0][2]["details"]["reason"], "agent_stalled_pid_not_alive_at_recovery")

    def test_fr2b_live_process_dead_pid_alive_at_recovery(self):
        """FR2B-LIVE-IDENTITY: process_dead recovery must be blocked when the live re-probe
        at recovery time reveals the PID is now alive (PID reused by a new process after the
        snapshot, or worker unexpectedly restarted).  Stale evidence must not trigger recovery."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": False, "pid": 9004}}
        att = {
            "attempt_key": "att-live-dead-alive",
            "run_scope_key": "rscope-live-4",
            "state": "completed",
            "diagnosis": "process_dead",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            # Live re-probe says process is now alive (PID reuse or restart after snapshot)
            liveness_probe=lambda p: True,
        )
        prow = {"attempts": {"att-live-dead-alive": att}}
        coordinator._cached_state["projects"]["p1"] = prow
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "worker": {"pid": 9004, "state": "running"}},
            project_row=prow,
            attempt_key="att-live-dead-alive",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "process_dead_pid_alive_at_recovery")

    def test_fr2b_live_process_dead_liveness_probe_unavailable(self):
        """FR2B-LIVE-IDENTITY: process_dead recovery must fail closed when the liveness probe
        raises an exception.  An unavailable probe means liveness cannot be freshly verified —
        fail closed and do not enqueue recovery."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": False, "pid": 9005}}
        att = {
            "attempt_key": "att-live-dead-probe-err",
            "run_scope_key": "rscope-live-5",
            "state": "completed",
            "diagnosis": "process_dead",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()

        def _raise_probe(p):
            raise PermissionError("liveness probe denied")

        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=_raise_probe,
        )
        prow = {"attempts": {"att-live-dead-probe-err": att}}
        coordinator._cached_state["projects"]["p1"] = prow
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "worker": {"pid": 9005, "state": "running"}},
            project_row=prow,
            attempt_key="att-live-dead-probe-err",
            attempt_record=att,
        )
        self.assertIsNone(att.get("recovery"))
        gate_events = [e for e in channel.events if e[1] == "OWNER_GATE"]
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0][2]["details"]["reason"], "process_dead_liveness_probe_unavailable")

    def test_fr2b_live_agent_stalled_proceeds_when_probe_confirms_alive(self):
        """FR2B-LIVE-IDENTITY: agent_stalled recovery proceeds when the live probe confirms
        the PID is still alive and the current worker state is active.  Existing safe recovery
        paths must be preserved when fresh evidence and current identity agree."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": True, "pid": 9010, "worker_state": "running"}}
        att = {
            "attempt_key": "att-live-stalled-ok",
            "run_scope_key": "rscope-live-10",
            "state": "completed",
            "diagnosis": "agent_stalled",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: True,  # confirms PID alive
        )
        prow = {"attempts": {"att-live-stalled-ok": att}}
        coordinator._cached_state["projects"]["p1"] = prow
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "lifecycle_state": "EXECUTING",
                      "worker": {"pid": 9010, "state": "running"}},
            project_row=prow,
            attempt_key="att-live-stalled-ok",
            attempt_record=att,
        )
        rec = att.get("recovery")
        self.assertIsNotNone(rec, "Recovery must proceed when probe confirms PID alive and worker active")
        self.assertEqual(rec["action"], "continue")
        self.assertIn(rec["state"], ("requested", "reserved"))

    def test_fr2b_live_process_dead_proceeds_when_probe_confirms_dead(self):
        """FR2B-LIVE-IDENTITY: process_dead recovery proceeds when the live probe confirms
        the PID is still dead.  Existing safe recovery paths must be preserved when fresh
        evidence and current identity agree."""
        from dev_orchestrator.core.diagnostics import evidence_hash as compute_ev_hash

        evidence = {"process_liveness": {"process_alive": False, "pid": 9011}}
        att = {
            "attempt_key": "att-live-dead-ok",
            "run_scope_key": "rscope-live-11",
            "state": "completed",
            "diagnosis": "process_dead",
            "owner_gate_required": False,
            "completed_at": _recent_completed_at(),
            "evidence_hash": compute_ev_hash(evidence),
            "evidence": evidence,
        }
        channel = DummyProgressChannel()
        coordinator = WatchdogCoordinator(
            self.runtime_dir, progress_channel=channel,
            liveness_probe=lambda p: False,  # confirms PID dead
        )
        prow = {"attempts": {"att-live-dead-ok": att}}
        coordinator._cached_state["projects"]["p1"] = prow
        coordinator._check_and_trigger_recovery(
            project_config={
                "project_id": "p1",
                "repo_path": str(self.repo_dir),
                "watchdog": {"enabled": True, "auto_recovery": True},
            },
            snapshot={"project_id": "p1", "repo_path": str(self.repo_dir),
                      "worker": {"pid": 9011, "state": "completed"}},
            project_row=prow,
            attempt_key="att-live-dead-ok",
            attempt_record=att,
        )
        rec = att.get("recovery")
        self.assertIsNotNone(rec, "Recovery must proceed when probe confirms PID dead")
        self.assertEqual(rec["action"], "continue")
        self.assertIn(rec["state"], ("requested", "reserved"))
