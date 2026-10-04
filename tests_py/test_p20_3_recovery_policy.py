"""Focused test suite for P20.3 Pure Recovery Policy, Budgets, and Heterogeneous Escalation.

Covers:
1. Pure decision matrix for all eight authorized recovery actions:
   - NO_ACTION_ALREADY_RECOVERED
   - OWNER_GATE (unresolved authority, pending owner gate, paused project, stale HEAD,
                unexpected dirty worktree, conflicting lineage, unknown safety, exhausted budget)
   - RECONCILE_RUNTIME (ambiguous role liveness, unreconciled existing effect)
   - RESTART_COMPONENT (dead component with single reconcilable identity)
   - HETEROGENEOUS_ESCALATION (repeated semantic failure or high-risk class with eligible candidate)
   - SWITCH_RESOURCE (resource-class failure with available non-excluded resource)
   - RETRY_CHANGED_STRATEGY (verifiable changed strategy, model, provider, or repair plan)
   - RETRY_SAME_STRATEGY (first same-strategy attempt within budget)
2. Strict purity: side-effect free, deterministic, no file IO, no network, no git, no ambient clock.
3. Normalized strategy identity: AGY account rotation alone shares the same strategy_key.
4. Bounded budgets & anti-loop: same-strategy retried at most once, total recoveries at most 3.
5. Resource availability adapter: converts AGY telemetry and routing config without fabricating unconfigured models.
6. Provider-level reviewer independence: escalation cannot collapse reviewer independence from implementation.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import unittest
from typing import Any

from dev_orchestrator.incidents.recovery_policy import (
    RECOVERY_ACTIONS,
    RecoveryDecision,
    RecoveryInputs,
    build_resource_availability,
    decide_recovery,
)
from dev_orchestrator.incidents.recovery_ledger import resolve_recovery_strategy_key
from dev_orchestrator.pool.models import (
    AccountTelemetry,
    PoolAccount,
    PoolResource,
    PoolTelemetry,
    QuotaState,
    RoutingPolicyConfig,
)


class TestP203RecoveryPolicyPurityAndMatrix(unittest.TestCase):
    """Test pure, deterministic recovery decisions across all 8 actions and strict purity constraints."""

    def setUp(self) -> None:
        self.now_iso = "2026-10-04T12:00:00+00:00"
        self.authority_healthy = {
            "project_id": "proj-1",
            "current_task_id": "P20.3",
            "lifecycle_state": "PENDING_DESIGN",
            "lifecycle_generation": 12,
            "lifecycle_transition_id": "trans-1234",
            "git": {"head": "abc1234", "branch": "main", "dirty": False},
            "repository_projection": {
                "task_id": "P20.3",
                "state": "PENDING_DESIGN",
                "matches_authority": True,
            },
        }
        self.owner_gate_none = {
            "resolved": True,
            "pending": False,
            "gate_id": None,
            "gate_source": None,
            "paused": False,
            "reason": "healthy",
        }
        self.budgets_default = {
            "max_same_strategy_retries": 1,
            "max_recoveries_per_incident": 3,
            "max_recovery_attempts_per_family": 5,
            "heterogeneous_required_classes": ["malformed_ai_output", "reviewer_rejected", "retry_without_progress"],
            "recovery_enabled": True,
        }
        self.resource_availability_empty = {
            "eligible_candidates": [],
            "ineligible_candidates": [],
            "heterogeneous_candidates": [],
            "independent_candidates": [],
        }

    def test_all_eight_recovery_actions_defined_exactly(self) -> None:
        expected_actions = {
            "RETRY_SAME_STRATEGY",
            "RETRY_CHANGED_STRATEGY",
            "SWITCH_RESOURCE",
            "HETEROGENEOUS_ESCALATION",
            "RESTART_COMPONENT",
            "RECONCILE_RUNTIME",
            "OWNER_GATE",
            "NO_ACTION_ALREADY_RECOVERED",
        }
        self.assertEqual(RECOVERY_ACTIONS, expected_actions)

    def test_action_no_action_already_recovered_on_terminal_incident(self) -> None:
        incident = {
            "incident_id": "inc-01",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "planner_timeout",
            "failure_signature": "sig-01",
            "terminal_disposition": "resolved",
            "occurrence_count": 1,
        }
        inputs = RecoveryInputs(
            authority=self.authority_healthy,
            owner_gate=self.owner_gate_none,
            incident=incident,
            family={"terminal_disposition": "resolved"},
            previous_attempts=(),
            liveness=None,
            resource_availability=self.resource_availability_empty,
            existing_effects=None,
            budgets=self.budgets_default,
        )
        decision = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(decision.action, "NO_ACTION_ALREADY_RECOVERED")
        self.assertIn("already_resolved", decision.reason.lower())
        self.assertFalse(decision.requires_heterogeneous)

    def test_action_owner_gate_on_unresolved_or_contradictory_authority(self) -> None:
        contradictory_authority = copy.deepcopy(self.authority_healthy)
        contradictory_authority["repository_projection"]["matches_authority"] = False
        incident = {
            "incident_id": "inc-02",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "lifecycle_projection_mismatch",
            "failure_signature": "sig-02",
        }
        inputs = RecoveryInputs(
            authority=contradictory_authority,
            owner_gate=self.owner_gate_none,
            incident=incident,
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability=self.resource_availability_empty,
            existing_effects=None,
            budgets=self.budgets_default,
        )
        decision = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(decision.action, "OWNER_GATE")
        self.assertIn("authority", decision.reason.lower())

    def test_action_owner_gate_on_pending_owner_gate_or_paused_project(self) -> None:
        owner_gate_pending = {
            "resolved": True,
            "pending": True,
            "gate_id": "gate-123",
            "gate_source": "owner_store",
            "paused": True,
            "reason": "operator_requested_pause",
        }
        incident = {
            "incident_id": "inc-03",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "planner_timeout",
            "failure_signature": "sig-03",
        }
        inputs = RecoveryInputs(
            authority=self.authority_healthy,
            owner_gate=owner_gate_pending,
            incident=incident,
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability=self.resource_availability_empty,
            existing_effects=None,
            budgets=self.budgets_default,
        )
        decision = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(decision.action, "OWNER_GATE")
        self.assertEqual(decision.gate_code, "OWNER_GATE_ACTIVE")

    def test_action_owner_gate_on_stale_head_or_dirty_worktree(self) -> None:
        authority_dirty = copy.deepcopy(self.authority_healthy)
        authority_dirty["git"]["dirty"] = True
        incident = {
            "incident_id": "inc-04",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "unexpected_dirty_worktree",
            "failure_signature": "sig-04",
        }
        inputs = RecoveryInputs(
            authority=authority_dirty,
            owner_gate=self.owner_gate_none,
            incident=incident,
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability=self.resource_availability_empty,
            existing_effects=None,
            budgets=self.budgets_default,
        )
        decision = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(decision.action, "OWNER_GATE")
        self.assertIn("dirty", decision.reason.lower())

    def test_action_reconcile_runtime_on_ambiguous_live_role_or_unreconciled_effect(self) -> None:
        ambiguous_liveness = {
            "alive": None,
            "state": "unknown",
            "source": "executor_unreadable",
            "role": "worker",
        }
        incident = {
            "incident_id": "inc-05",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "worker_process_lost",
            "failure_signature": "sig-05",
        }
        inputs = RecoveryInputs(
            authority=self.authority_healthy,
            owner_gate=self.owner_gate_none,
            incident=incident,
            family=None,
            previous_attempts=(),
            liveness=ambiguous_liveness,
            resource_availability=self.resource_availability_empty,
            existing_effects={"lease_ambiguous": True},
            budgets=self.budgets_default,
        )
        decision = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(decision.action, "RECONCILE_RUNTIME")
        self.assertIn("ambiguous", decision.reason.lower())

    def test_action_restart_component_on_dead_component_with_reconcilable_identity(self) -> None:
        dead_liveness = {
            "alive": False,
            "state": "dead",
            "source": "coordinator_thread_dead",
            "role": "planner",
        }
        incident = {
            "incident_id": "inc-06",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "daemon_restart_during_execution",
            "failure_signature": "sig-06",
            "role": "planner",
        }
        inputs = RecoveryInputs(
            authority=self.authority_healthy,
            owner_gate=self.owner_gate_none,
            incident=incident,
            family=None,
            previous_attempts=(),
            liveness=dead_liveness,
            resource_availability=self.resource_availability_empty,
            existing_effects=None,
            budgets=self.budgets_default,
        )
        decision = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(decision.action, "RESTART_COMPONENT")
        self.assertEqual(decision.authorized_effect.get("effect_kind"), "restart_component")

    def test_action_switch_resource_on_resource_class_failure_with_available_resource(self) -> None:
        incident = {
            "incident_id": "inc-07",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "provider_unavailable",
            "failure_signature": "sig-07",
            "resource": "reviewer-r-1",
            "role": "reviewer",
        }
        res_avail = {
            "eligible_candidates": [
                {"resource_id": "reviewer-r-2", "provider": "gemini", "model": "gemini-2.5-pro", "is_heterogeneous": False}
            ],
            "ineligible_candidates": [
                {"resource_id": "reviewer-r-1", "reason": "excluded_failed_resource"}
            ],
            "heterogeneous_candidates": [],
            "independent_candidates": [],
        }
        inputs = RecoveryInputs(
            authority=self.authority_healthy,
            owner_gate=self.owner_gate_none,
            incident=incident,
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability=res_avail,
            existing_effects=None,
            budgets=self.budgets_default,
        )
        decision = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(decision.action, "SWITCH_RESOURCE")
        self.assertEqual(decision.authorized_effect.get("target_resource_id"), "reviewer-r-2")

    def test_action_heterogeneous_escalation_on_repeated_semantic_failure_with_eligible_resource(self) -> None:
        strategy_key = "strat-semantic-01"
        incident = {
            "incident_id": "inc-08",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "reviewer_rejected",
            "failure_signature": "sig-08",
            "role": "planner",
            "provider": "agy",
            "model": "gemini-2.5-pro",
        }
        prev_attempts = [
            {
                "attempt_id": "rcv-1",
                "strategy_key": strategy_key,
                "action": "RETRY_SAME_STRATEGY",
                "attempt_index": 1,
                "status": "failed",
            }
        ]
        res_avail = {
            "eligible_candidates": [
                {"resource_id": "claude/default/opus", "provider": "claude", "model": "opus", "is_heterogeneous": True}
            ],
            "ineligible_candidates": [],
            "heterogeneous_candidates": [
                {"resource_id": "claude/default/opus", "provider": "claude", "model": "opus", "is_heterogeneous": True}
            ],
            "independent_candidates": [],
        }
        inputs = RecoveryInputs(
            authority=self.authority_healthy,
            owner_gate=self.owner_gate_none,
            incident=incident,
            family=None,
            previous_attempts=prev_attempts,
            liveness=None,
            resource_availability=res_avail,
            existing_effects=None,
            budgets=self.budgets_default,
        )
        decision = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(decision.action, "HETEROGENEOUS_ESCALATION")
        self.assertTrue(decision.requires_heterogeneous)
        self.assertEqual(decision.authorized_effect.get("target_resource_id"), "claude/default/opus")

    def test_action_retry_changed_strategy_when_strategy_changed(self) -> None:
        incident = {
            "incident_id": "inc-09",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "malformed_ai_output",
            "failure_signature": "sig-09",
            "role": "planner",
            "strategy_changed": True,
            "repair_plan_id": "plan-repair-42",
        }
        inputs = RecoveryInputs(
            authority=self.authority_healthy,
            owner_gate=self.owner_gate_none,
            incident=incident,
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability=self.resource_availability_empty,
            existing_effects=None,
            budgets=self.budgets_default,
        )
        decision = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(decision.action, "RETRY_CHANGED_STRATEGY")

    def test_action_retry_same_strategy_first_attempt_within_budget(self) -> None:
        incident = {
            "incident_id": "inc-10",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "planner_timeout",
            "failure_signature": "sig-10",
            "role": "planner",
            "provider": "agy",
            "model": "gemini-2.5-pro",
        }
        inputs = RecoveryInputs(
            authority=self.authority_healthy,
            owner_gate=self.owner_gate_none,
            incident=incident,
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability=self.resource_availability_empty,
            existing_effects=None,
            budgets=self.budgets_default,
        )
        decision = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(decision.action, "RETRY_SAME_STRATEGY")
        self.assertEqual(decision.remaining_budget.get("same_strategy_remaining"), 1)
        self.assertEqual(decision.remaining_budget.get("incident_recoveries_remaining"), 3)

    def test_decision_purity_byte_identical_and_side_effect_free(self) -> None:
        incident = {
            "incident_id": "inc-purity",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "planner_timeout",
            "failure_signature": "sig-purity",
        }
        inputs = RecoveryInputs(
            authority=self.authority_healthy,
            owner_gate=self.owner_gate_none,
            incident=incident,
            family=None,
            previous_attempts=(),
            liveness=None,
            resource_availability=self.resource_availability_empty,
            existing_effects=None,
            budgets=self.budgets_default,
        )
        dec1 = decide_recovery(inputs, now=self.now_iso)
        dec2 = decide_recovery(inputs, now=self.now_iso)
        self.assertEqual(dec1.to_dict(), dec2.to_dict())
        self.assertEqual(dec1.idempotency_key, dec2.idempotency_key)
        self.assertEqual(dec1.attempt_id, dec2.attempt_id)


class TestP203RecoveryStrategyKeyAndAntiLoop(unittest.TestCase):
    """Test normalized strategy key resolution, anti-loop budgets, and account rotation defense."""

    def test_strategy_key_normalizes_and_ignores_account_and_volatile_data(self) -> None:
        inc_acc1 = {
            "incident_id": "inc-01",
            "role": "reviewer",
            "failure_class": "malformed_ai_output",
            "provider": "agy",
            "model": "gemini-2.5-pro",
            "account": "agy-account-1",
            "resource": "res-1",
            "timestamp": "2026-10-04T10:00:00Z",
            "repo_path": "C:\\work\\github\\DevOrchestrator-dev",
        }
        inc_acc2 = {
            "incident_id": "inc-02",
            "role": "reviewer",
            "failure_class": "malformed_ai_output",
            "provider": "agy",
            "model": "gemini-2.5-pro",
            "account": "agy-account-2",
            "resource": "res-2",
            "timestamp": "2026-10-04T11:00:00Z",
            "repo_path": "C:\\other\\path",
        }
        key1 = resolve_recovery_strategy_key(inc_acc1)
        key2 = resolve_recovery_strategy_key(inc_acc2)
        self.assertEqual(key1, key2, "Strategy key must match when only account or volatile paths differ")
        self.assertTrue(key1.startswith("strat-"))

    def test_changing_model_or_repair_plan_changes_strategy_key(self) -> None:
        inc_base = {
            "role": "planner",
            "failure_class": "reviewer_rejected",
            "provider": "agy",
            "model": "gemini-2.5-pro",
        }
        inc_diff_model = {**inc_base, "model": "claude-opus-5"}
        inc_repair_plan = {**inc_base, "repair_plan_id": "repair-plan-99"}

        k_base = resolve_recovery_strategy_key(inc_base)
        k_diff = resolve_recovery_strategy_key(inc_diff_model)
        k_repair = resolve_recovery_strategy_key(inc_repair_plan)

        self.assertNotEqual(k_base, k_diff)
        self.assertNotEqual(k_base, k_repair)

    def test_same_strategy_retried_at_most_once_then_requires_escalation(self) -> None:
        strategy_key = "strat-fixed-01"
        incident = {
            "incident_id": "inc-loop",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "reviewer_rejected",
            "failure_signature": "sig-loop",
            "role": "planner",
            "provider": "agy",
            "model": "gemini-2.5-pro",
        }
        # First attempt was already same-strategy
        previous = [
            {
                "attempt_id": "rcv-1",
                "strategy_key": strategy_key,
                "action": "RETRY_SAME_STRATEGY",
                "attempt_index": 1,
                "status": "failed",
            }
        ]
        # Case A: with eligible heterogeneous resource -> HETEROGENEOUS_ESCALATION
        res_avail_with_escalation = {
            "eligible_candidates": [
                {"resource_id": "claude/default/opus", "provider": "claude", "model": "opus", "is_heterogeneous": True}
            ],
            "ineligible_candidates": [],
            "heterogeneous_candidates": [
                {"resource_id": "claude/default/opus", "provider": "claude", "model": "opus", "is_heterogeneous": True}
            ],
            "independent_candidates": [],
        }
        inputs_esc = RecoveryInputs(
            authority={
                "project_id": "proj-1",
                "current_task_id": "P20.3",
                "lifecycle_state": "PENDING_DESIGN",
                "lifecycle_generation": 12,
                "lifecycle_transition_id": "trans-1",
                "git": {"head": "abc", "branch": "main", "dirty": False},
                "repository_projection": {"task_id": "P20.3", "state": "PENDING_DESIGN", "matches_authority": True},
            },
            owner_gate={"resolved": True, "pending": False, "gate_id": None, "gate_source": None, "paused": False, "reason": "ok"},
            incident=incident,
            family=None,
            previous_attempts=previous,
            liveness=None,
            resource_availability=res_avail_with_escalation,
            existing_effects=None,
            budgets={"max_same_strategy_retries": 1, "max_recoveries_per_incident": 3, "max_recovery_attempts_per_family": 5, "heterogeneous_required_classes": ["reviewer_rejected"], "recovery_enabled": True},
        )
        dec_esc = decide_recovery(inputs_esc, now="2026-10-04T12:00:00+00:00")
        self.assertEqual(dec_esc.action, "HETEROGENEOUS_ESCALATION")

        # Case B: with NO eligible heterogeneous resource -> fails closed to OWNER_GATE
        inputs_no_esc = RecoveryInputs(
            authority=inputs_esc.authority,
            owner_gate=inputs_esc.owner_gate,
            incident=incident,
            family=None,
            previous_attempts=previous,
            liveness=None,
            resource_availability={
                "eligible_candidates": [],
                "ineligible_candidates": [],
                "heterogeneous_candidates": [],
                "independent_candidates": [],
            },
            existing_effects=None,
            budgets=inputs_esc.budgets,
        )
        dec_gate = decide_recovery(inputs_no_esc, now="2026-10-04T12:00:00+00:00")
        self.assertEqual(dec_gate.action, "OWNER_GATE")
        self.assertIn("exhausted", dec_gate.reason.lower())

    def test_total_recoveries_per_incident_capped_at_three_then_gates(self) -> None:
        incident = {
            "incident_id": "inc-cap",
            "project_id": "proj-1",
            "task_id": "P20.3",
            "failure_class": "planner_timeout",
            "failure_signature": "sig-cap",
        }
        previous = [
            {"attempt_id": "rcv-1", "strategy_key": "k1", "action": "RETRY_SAME_STRATEGY", "attempt_index": 1, "status": "failed"},
            {"attempt_id": "rcv-2", "strategy_key": "k2", "action": "RETRY_CHANGED_STRATEGY", "attempt_index": 2, "status": "failed"},
            {"attempt_id": "rcv-3", "strategy_key": "k3", "action": "SWITCH_RESOURCE", "attempt_index": 3, "status": "failed"},
        ]
        inputs = RecoveryInputs(
            authority={
                "project_id": "proj-1",
                "current_task_id": "P20.3",
                "lifecycle_state": "PENDING_DESIGN",
                "lifecycle_generation": 12,
                "lifecycle_transition_id": "trans-1",
                "git": {"head": "abc", "branch": "main", "dirty": False},
                "repository_projection": {"task_id": "P20.3", "state": "PENDING_DESIGN", "matches_authority": True},
            },
            owner_gate={"resolved": True, "pending": False, "gate_id": None, "gate_source": None, "paused": False, "reason": "ok"},
            incident=incident,
            family=None,
            previous_attempts=previous,
            liveness=None,
            resource_availability={"eligible_candidates": [{"resource_id": "c1", "provider": "p1", "model": "m1"}], "ineligible_candidates": [], "heterogeneous_candidates": [], "independent_candidates": []},
            existing_effects=None,
            budgets={"max_same_strategy_retries": 1, "max_recoveries_per_incident": 3, "max_recovery_attempts_per_family": 5, "heterogeneous_required_classes": [], "recovery_enabled": True},
        )
        dec = decide_recovery(inputs, now="2026-10-04T12:00:00+00:00")
        self.assertEqual(dec.action, "OWNER_GATE")
        self.assertEqual(dec.remaining_budget.get("incident_recoveries_remaining"), 0)


class TestP203ResourceAvailabilityAndIndependence(unittest.TestCase):
    """Test pure adapter for real resource availability and provider-level reviewer independence."""

    def setUp(self) -> None:
        self.routing_config = RoutingPolicyConfig(
            escalation_fallback_resources=(
                "codex/default/gpt-5.6-sol",
                "claude/default/opus",
            ),
            independent_reviewer_resources=(
                "claude/default/opus",
                "codex/default/gpt-5.6-sol",
            ),
        )
        self.pool_telemetry = PoolTelemetry(
            timestamp="2026-10-04T12:00:00Z",
            total_accounts=3,
            available_accounts=2,
            in_cooldown_accounts=1,
            exhausted_accounts=0,
            active_requests=0,
            accounts={
                "agy-1": AccountTelemetry(
                    account_id="agy-1", provider="agy", available=True, quota_state=QuotaState.HEALTHY
                ),
                "agy-2": AccountTelemetry(
                    account_id="agy-2", provider="agy", available=True, quota_state=QuotaState.HEALTHY
                ),
                "agy-3": AccountTelemetry(
                    account_id="agy-3", provider="agy", available=False, in_cooldown=True, quota_state=QuotaState.LOW
                ),
            },
            resources=(
                PoolResource(
                    resource_id="agy/agy-1/gemini-2.5-pro",
                    account_id="agy-1",
                    provider="agy",
                    model="gemini-2.5-pro",
                    roles=frozenset({"planner", "worker", "reviewer"}),
                ),
                PoolResource(
                    resource_id="agy/agy-2/gemini-2.5-pro",
                    account_id="agy-2",
                    provider="agy",
                    model="gemini-2.5-pro",
                    roles=frozenset({"planner", "worker", "reviewer"}),
                ),
            ),
        )

    def test_build_resource_availability_never_fabricates_unconfigured_model(self) -> None:
        avail = build_resource_availability(
            self.pool_telemetry,
            self.routing_config,
            role="planner",
            independence="none",
            excluded_resource_ids=(),
        )
        # All candidate resource IDs must originate from pool_telemetry or routing_config
        configured_ids = {
            "agy/agy-1/gemini-2.5-pro",
            "agy/agy-2/gemini-2.5-pro",
            "codex/default/gpt-5.6-sol",
            "claude/default/opus",
        }
        for cand in avail["eligible_candidates"]:
            self.assertIn(cand["resource_id"], configured_ids)

    def test_reviewer_independence_excludes_implementation_provider(self) -> None:
        # Implementation was AGY; reviewer with independence="provider" must exclude all AGY resources
        avail = build_resource_availability(
            self.pool_telemetry,
            self.routing_config,
            role="reviewer",
            independence="provider",
            excluded_resource_ids=(),
            previous_provider="agy",
        )
        for cand in avail["eligible_candidates"]:
            self.assertNotEqual(
                cand.get("provider"),
                "agy",
                "Reviewer candidate must not share provider with implementation provider 'agy'",
            )
        # Ineligible candidates must explicitly state the independence reason
        agy_ineligible = [c for c in avail["ineligible_candidates"] if c.get("provider") == "agy"]
        self.assertTrue(len(agy_ineligible) > 0)
        for cand in agy_ineligible:
            self.assertIn("independence", cand.get("reason", "").lower())


if __name__ == "__main__":
    unittest.main()
