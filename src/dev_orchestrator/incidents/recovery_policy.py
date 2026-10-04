"""Pure, bounded, deterministic recovery decision policy and resource availability adapter.

Enforces:
1. Exactly the eight owner-authorized recovery actions in RECOVERY_ACTIONS.
2. Side-effect-free purity: identical inputs yield byte-identical decisions.
3. Fail-closed ladder:
   (1) already-resolved/terminal incident -> NO_ACTION_ALREADY_RECOVERED
   (2) unresolved or contradictory authority, pending owner gate, paused project,
       stale HEAD, unexpected dirty worktree, conflicting lineage, unknown safety,
       or exhausted budget -> OWNER_GATE
   (3) ambiguous live role or unreconciled existing effect -> RECONCILE_RUNTIME
   (4) component dead with a reconcilable single identity -> RESTART_COMPONENT
   (5) repeated unchanged semantic signature or high-risk class -> HETEROGENEOUS_ESCALATION
       when an eligible resource exists, else OWNER_GATE
   (6) resource-class failure with an available non-excluded resource -> SWITCH_RESOURCE
   (7) verifiable changed strategy/context/model/provider/repair plan -> RETRY_CHANGED_STRATEGY
   (8) first same-strategy attempt within budget -> RETRY_SAME_STRATEGY
4. Provider-level reviewer independence: escalation cannot collapse independence.
5. No fabricated models or providers: candidates derive solely from real configuration.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from dev_orchestrator.incidents.recovery_ledger import resolve_recovery_strategy_key

RECOVERY_ACTIONS: frozenset[str] = frozenset({
    "RETRY_SAME_STRATEGY",
    "RETRY_CHANGED_STRATEGY",
    "SWITCH_RESOURCE",
    "HETEROGENEOUS_ESCALATION",
    "RESTART_COMPONENT",
    "RECONCILE_RUNTIME",
    "OWNER_GATE",
    "NO_ACTION_ALREADY_RECOVERED",
})

_RESOURCE_FAILURE_CLASSES: frozenset[str] = frozenset({
    "provider_quota_exhausted",
    "provider_unavailable",
    "aibroker_unavailable",
    "transport_unavailable",
    "provider_temporarily_unavailable",
})


@dataclass(frozen=True)
class RecoveryDecision:
    """Frozen machine-readable recovery recommendation."""
    action: str
    reason: str
    authorized_effect: dict[str, Any]
    expected_authority_anchor: dict[str, Any]
    expected_source_anchor: dict[str, Any]
    remaining_budget: dict[str, Any]
    strategy_key: str
    attempt_id: str
    idempotency_key: str
    requires_heterogeneous: bool
    gate_code: str | None = None
    evidence_digests: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in RECOVERY_ACTIONS:
            raise ValueError(f"Unknown recovery action '{self.action}'; must be in {RECOVERY_ACTIONS}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "authorized_effect": dict(self.authorized_effect),
            "expected_authority_anchor": dict(self.expected_authority_anchor),
            "expected_source_anchor": dict(self.expected_source_anchor),
            "remaining_budget": dict(self.remaining_budget),
            "strategy_key": self.strategy_key,
            "attempt_id": self.attempt_id,
            "idempotency_key": self.idempotency_key,
            "requires_heterogeneous": self.requires_heterogeneous,
            "gate_code": self.gate_code,
            "evidence_digests": dict(self.evidence_digests),
        }


@dataclass(frozen=True)
class RecoveryInputs:
    """Explicit, frozen inputs required to compute a deterministic recovery decision."""
    authority: dict[str, Any]
    owner_gate: dict[str, Any] | None
    incident: dict[str, Any]
    family: dict[str, Any] | None
    previous_attempts: Sequence[dict[str, Any]]
    liveness: dict[str, Any] | None
    resource_availability: dict[str, Any]
    existing_effects: dict[str, Any] | None
    budgets: dict[str, Any]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decide_recovery(
    inputs: RecoveryInputs,
    *,
    now: str | datetime | None = None,
) -> RecoveryDecision:
    """Pure, deterministic, side-effect-free recovery decision function.

    Takes RecoveryInputs, performs fail-closed evaluation according to the frozen
    decision ladder, and returns exactly one RecoveryDecision.
    """
    authority = inputs.authority if isinstance(inputs.authority, dict) else {}
    owner_gate = inputs.owner_gate if isinstance(inputs.owner_gate, dict) else None
    incident = inputs.incident if isinstance(inputs.incident, dict) else {}
    family = inputs.family if isinstance(inputs.family, dict) else None
    previous_attempts = list(inputs.previous_attempts or ())
    liveness = inputs.liveness if isinstance(inputs.liveness, dict) else None
    res_avail = inputs.resource_availability if isinstance(inputs.resource_availability, dict) else {}
    existing_effects = inputs.existing_effects if isinstance(inputs.existing_effects, dict) else None
    budgets = inputs.budgets if isinstance(inputs.budgets, dict) else {}

    # Anchors
    project_id = str(authority.get("project_id") or incident.get("project_id") or "")
    task_id = str(authority.get("current_task_id") or incident.get("task_id") or "")
    generation = authority.get("lifecycle_generation")
    transition_id = authority.get("lifecycle_transition_id")
    git = authority.get("git") if isinstance(authority.get("git"), dict) else {}
    head = str(git.get("head") or "")

    expected_authority_anchor: dict[str, Any] = {
        "project_id": project_id,
        "task_id": task_id,
        "lifecycle_generation": generation,
        "lifecycle_transition_id": transition_id,
        "head": head,
    }

    incident_id = str(incident.get("incident_id") or "")
    family_id = str((family.get("family_id") if isinstance(family, dict) else "") or incident.get("failure_family") or (f"fam-{incident_id[:12]}" if incident_id else "fam-unknown"))
    failure_sig = str(incident.get("failure_signature") or "")
    source_event_id = str(incident.get("occurrence_key") or incident.get("source_event_id") or incident_id)

    expected_source_anchor: dict[str, Any] = {
        "incident_id": incident_id,
        "family_id": family_id,
        "failure_signature": failure_sig,
        "source_event_id": source_event_id,
    }

    # Strategy key
    strategy_key = resolve_recovery_strategy_key(incident)
    attempt_index = len(previous_attempts) + 1

    # Deterministic attempt_id & idempotency_key
    attempt_id_raw = f"{project_id}:{family_id}:{strategy_key}:{attempt_index}"
    attempt_id = f"rcv-{_sha256_text(attempt_id_raw)[:16]}"

    # Budget limits and tracking
    max_same_strategy = int(budgets.get("max_same_strategy_retries", 1))
    max_recoveries_incident = int(budgets.get("max_recoveries_per_incident", 3))
    max_family_attempts = int(budgets.get("max_recovery_attempts_per_family", 5))

    same_strategy_attempts = sum(
        1 for att in previous_attempts
        if isinstance(att, dict) and att.get("strategy_key") == strategy_key
    )
    total_incident_attempts = len(previous_attempts)

    same_strategy_remaining = max(0, max_same_strategy - same_strategy_attempts)
    incident_recoveries_remaining = max(0, max_recoveries_incident - total_incident_attempts)
    family_attempts_remaining = max(0, max_family_attempts - total_incident_attempts)

    remaining_budget: dict[str, Any] = {
        "same_strategy_remaining": same_strategy_remaining,
        "incident_recoveries_remaining": incident_recoveries_remaining,
        "family_attempts_remaining": family_attempts_remaining,
    }

    def _make_decision(
        action: str,
        reason: str,
        *,
        authorized_effect: dict[str, Any] | None = None,
        requires_heterogeneous: bool = False,
        gate_code: str | None = None,
    ) -> RecoveryDecision:
        idem_raw = f"{project_id}:{family_id}:{strategy_key}:{attempt_index}:{action}"
        idempotency_key = f"recov-{_sha256_text(idem_raw)[:24]}"
        return RecoveryDecision(
            action=action,
            reason=reason,
            authorized_effect=dict(authorized_effect or {}),
            expected_authority_anchor=expected_authority_anchor,
            expected_source_anchor=expected_source_anchor,
            remaining_budget=remaining_budget,
            strategy_key=strategy_key,
            attempt_id=attempt_id,
            idempotency_key=idempotency_key,
            requires_heterogeneous=requires_heterogeneous,
            gate_code=gate_code,
        )

    # -------------------------------------------------------------
    # (1) already-resolved/terminal incident -> NO_ACTION_ALREADY_RECOVERED
    # -------------------------------------------------------------
    terminal_disp = str(incident.get("terminal_disposition") or (family.get("terminal_disposition") if family else "") or "")
    if terminal_disp in {"resolved", "terminal", "closed", "already_recovered"} or (family and family.get("state") == "resolved"):
        return _make_decision(
            action="NO_ACTION_ALREADY_RECOVERED",
            reason=f"incident_already_resolved: terminal_disposition={terminal_disp or 'resolved'}",
        )

    # -------------------------------------------------------------
    # (2) Fail-closed safety gates & authority -> OWNER_GATE
    # -------------------------------------------------------------
    # Check owner gate input
    if owner_gate is not None:
        if owner_gate.get("pending") is True or owner_gate.get("gate_id") is not None:
            gate_id = owner_gate.get("gate_id") or "owner_gate"
            gate_code = "OWNER_GATE_ACTIVE"
            if "declaration" in str(owner_gate.get("gate_source") or "").lower() or "declaration" in str(owner_gate.get("reason") or "").lower():
                gate_code = "CONTROL_PLANE_DECLARATION_REQUIRED"
            return _make_decision(
                action="OWNER_GATE",
                reason=f"owner_gate_pending: {owner_gate.get('reason') or gate_id}",
                gate_code=gate_code,
            )
        if owner_gate.get("paused") is True:
            return _make_decision(
                action="OWNER_GATE",
                reason="project_paused_by_owner",
                gate_code="PROJECT_PAUSED",
            )
        if owner_gate.get("resolved") is False:
            return _make_decision(
                action="OWNER_GATE",
                reason=f"owner_gate_unresolved: {owner_gate.get('reason') or 'unresolved'}",
                gate_code="OWNER_AUTHORITY_UNRESOLVED",
            )

    # Authority projection & lifecycle consistency
    repo_proj = authority.get("repository_projection")
    if not isinstance(authority, dict) or not authority or not project_id:
        return _make_decision(
            action="OWNER_GATE",
            reason="missing_or_empty_authority",
            gate_code="LIFECYCLE_AUTHORITY_MISSING",
        )
    if isinstance(repo_proj, dict) and repo_proj.get("matches_authority") is False:
        return _make_decision(
            action="OWNER_GATE",
            reason="lifecycle_projection_mismatch: repository projection does not match authority",
            gate_code="LIFECYCLE_PROJECTION_MISMATCH",
        )
    if authority.get("conflicting_lineage") is True or incident.get("failure_class") == "conflicting_lineage":
        return _make_decision(
            action="OWNER_GATE",
            reason="conflicting_successor_lineage_claims",
            gate_code="SUCCESSOR_LINEAGE_CONFLICT",
        )

    # Git dirty or stale head
    failure_class = str(incident.get("failure_class") or "")
    if git.get("dirty") is True or failure_class == "unexpected_dirty_worktree":
        return _make_decision(
            action="OWNER_GATE",
            reason="unexpected_dirty_worktree: worktree is dirty; must be clean before recovery actuation",
            gate_code="DIRTY_WORKTREE_FENCE",
        )
    if failure_class == "stale_head" or authority.get("stale_head") is True:
        return _make_decision(
            action="OWNER_GATE",
            reason="stale_head: authoritative HEAD does not match repository HEAD",
            gate_code="STALE_HEAD_FENCE",
        )

    # Unknown safety class
    if failure_class == "unknown_requires_owner":
        return _make_decision(
            action="OWNER_GATE",
            reason="unknown_safety: incident classified as unknown_requires_owner",
            gate_code="UNKNOWN_SAFETY_GATE",
        )

    # Exhausted total budget
    if incident_recoveries_remaining <= 0:
        return _make_decision(
            action="OWNER_GATE",
            reason=f"recovery_budget_exhausted: {total_incident_attempts} attempts consumed (max {max_recoveries_incident})",
            gate_code="RECOVERY_BUDGET_EXHAUSTED",
        )

    # -------------------------------------------------------------
    # (3) Ambiguous live role or unreconciled existing effect -> RECONCILE_RUNTIME
    # -------------------------------------------------------------
    role = str(incident.get("role") or (liveness.get("role") if liveness else "") or "").lower()
    liveness_alive = liveness.get("alive") if liveness else None
    liveness_state = str(liveness.get("state") or "") if liveness else ""

    lease_ambiguous = existing_effects.get("lease_ambiguous") is True if existing_effects else False
    lease_live = existing_effects.get("lease_live") is True if existing_effects else False

    # Stale predecessor or ambiguous worker liveness (CPF-01)
    if (liveness is not None and liveness_alive is None) or lease_ambiguous:
        return _make_decision(
            action="RECONCILE_RUNTIME",
            reason=f"ambiguous_role_or_lease_liveness: role={role}, liveness_state={liveness_state}",
            authorized_effect={"effect_kind": "reconcile_runtime", "target_role": role},
        )

    # Active in-flight effect that requires runtime reconciliation
    if lease_live or (existing_effects and existing_effects.get("unreconciled_effect_id")):
        eff_id = str(existing_effects.get("unreconciled_effect_id") or "")
        return _make_decision(
            action="RECONCILE_RUNTIME",
            reason=f"unreconciled_existing_effect: effect_id={eff_id}",
            authorized_effect={"effect_kind": "reconcile_effect", "effect_id": eff_id},
        )

    # Accepted review verdict missing handoff (CPF-02) or journal recovery (CPF-03, CPF-07)
    if failure_class in {"lost_handoff", "missing_execution_record"} or incident.get("continuation_relation") == "missing_handoff_rebuild":
        return _make_decision(
            action="RECONCILE_RUNTIME",
            reason=f"missing_handoff_rebuild: {failure_class}",
            authorized_effect={"effect_kind": "rebuild_handoff", "target_id": task_id},
        )

    # -------------------------------------------------------------
    # (4) Component dead with a reconcilable single identity -> RESTART_COMPONENT
    # -------------------------------------------------------------
    if failure_class in {"daemon_restart_during_execution", "daemon_restart_during_handoff"}:
        return _make_decision(
            action="RESTART_COMPONENT",
            reason=f"daemon_restart_failure: {failure_class}",
            authorized_effect={"effect_kind": "restart_component", "target_component": "daemon"},
        )
    if liveness is not None and liveness_alive is False and role in {"planner", "reviewer", "supervisor"}:
        return _make_decision(
            action="RESTART_COMPONENT",
            reason=f"dead_coordinator_role: role={role}",
            authorized_effect={"effect_kind": "restart_component", "target_component": role},
        )

    # -------------------------------------------------------------
    # (5) Repeated unchanged semantic signature or high-risk class -> HETEROGENEOUS_ESCALATION
    # -------------------------------------------------------------
    strategy_changed = bool(
        incident.get("strategy_changed")
        or incident.get("repair_plan_id")
        or incident.get("context_revision")
    )
    hetero_classes = set(budgets.get("heterogeneous_required_classes") or ["malformed_ai_output", "reviewer_rejected", "retry_without_progress"])
    is_repeated_same_strategy = (same_strategy_attempts >= max_same_strategy)
    is_high_risk = failure_class in hetero_classes

    if not strategy_changed and (is_repeated_same_strategy or is_high_risk):
        # Check for eligible heterogeneous candidate
        hetero_candidates = res_avail.get("heterogeneous_candidates") or []
        eligible_hetero = [
            c for c in hetero_candidates
            if isinstance(c, dict) and c.get("is_heterogeneous", True)
        ]
        if eligible_hetero:
            target_res = eligible_hetero[0]
            target_res_id = str(target_res.get("resource_id") or "")
            return _make_decision(
                action="HETEROGENEOUS_ESCALATION",
                reason=f"heterogeneous_escalation_required: repeated={is_repeated_same_strategy}, high_risk={is_high_risk}",
                authorized_effect={"effect_kind": "escalate_resource", "target_resource_id": target_res_id},
                requires_heterogeneous=True,
            )
        else:
            # Fails closed to OWNER_GATE when no eligible heterogeneous resource exists
            return _make_decision(
                action="OWNER_GATE",
                reason=f"heterogeneous_escalation_exhausted: no eligible heterogeneous resource configured for {failure_class}",
                gate_code="HETEROGENEOUS_RESOURCE_UNAVAILABLE",
            )

    # -------------------------------------------------------------
    # (6) Resource-class failure with available non-excluded resource -> SWITCH_RESOURCE
    # -------------------------------------------------------------
    if failure_class in _RESOURCE_FAILURE_CLASSES or "resource" in failure_class:
        eligible_candidates = res_avail.get("eligible_candidates") or []
        if eligible_candidates:
            target_res = eligible_candidates[0]
            target_res_id = str(target_res.get("resource_id") or "")
            return _make_decision(
                action="SWITCH_RESOURCE",
                reason=f"switch_resource_for_{failure_class}",
                authorized_effect={"effect_kind": "switch_resource", "target_resource_id": target_res_id},
            )
        else:
            return _make_decision(
                action="OWNER_GATE",
                reason=f"all_eligible_resources_exhausted_for_{failure_class}",
                gate_code="RESOURCE_EXHAUSTION",
            )

    # -------------------------------------------------------------
    # (7) Verifiable changed strategy/context/model/provider/repair plan -> RETRY_CHANGED_STRATEGY
    # -------------------------------------------------------------
    strategy_changed = bool(
        incident.get("strategy_changed")
        or incident.get("repair_plan_id")
        or incident.get("context_revision")
    )
    if strategy_changed:
        repair_id = str(incident.get("repair_plan_id") or "")
        return _make_decision(
            action="RETRY_CHANGED_STRATEGY",
            reason="verifiable_changed_strategy",
            authorized_effect={"effect_kind": "retry_changed_strategy", "repair_plan_id": repair_id},
        )

    # -------------------------------------------------------------
    # (8) First same-strategy attempt within budget -> RETRY_SAME_STRATEGY
    # -------------------------------------------------------------
    if same_strategy_remaining > 0:
        return _make_decision(
            action="RETRY_SAME_STRATEGY",
            reason="retry_same_strategy_first_attempt",
            authorized_effect={"effect_kind": "retry_same_strategy", "target_id": task_id},
        )

    # Fallback fail-closed
    return _make_decision(
        action="OWNER_GATE",
        reason="no_viable_recovery_action_within_budget",
        gate_code="RECOVERY_BUDGET_EXHAUSTED",
    )


def build_resource_availability(
    pool_telemetry: Any,
    routing_config: Any,
    *,
    role: str = "planner",
    independence: str = "none",
    excluded_resource_ids: Sequence[str] = (),
    previous_provider: str | None = None,
) -> dict[str, Any]:
    """Pure adapter over AGYResourcePool telemetry and RoutingPolicyConfig.

    Categorizes resources into eligible, ineligible, heterogeneous, and independent.
    Never fabricates unconfigured models or providers.
    Enforces provider reviewer independence by excluding previous_provider when required.
    """
    excluded_set = set(excluded_resource_ids or ())
    eligible: list[dict[str, Any]] = []
    ineligible: list[dict[str, Any]] = []
    heterogeneous: list[dict[str, Any]] = []
    independent: list[dict[str, Any]] = []

    # 1. Pool resources
    pool_resources = getattr(pool_telemetry, "resources", ())
    if not pool_resources and isinstance(pool_telemetry, Mapping):
        pool_resources = pool_telemetry.get("resources", ())

    accounts_map: dict[str, Any] = {}
    if hasattr(pool_telemetry, "accounts"):
        accounts_map = dict(getattr(pool_telemetry, "accounts", {}))
    elif isinstance(pool_telemetry, Mapping):
        accounts_map = dict(pool_telemetry.get("accounts", {}))

    for res in pool_resources:
        res_id = getattr(res, "resource_id", None) or (res.get("resource_id") if isinstance(res, Mapping) else None)
        provider = getattr(res, "provider", None) or (res.get("provider") if isinstance(res, Mapping) else "agy")
        model = getattr(res, "model", None) or (res.get("model") if isinstance(res, Mapping) else "gemini-2.5-pro")
        account_id = getattr(res, "account_id", None) or (res.get("account_id") if isinstance(res, Mapping) else "")
        roles = getattr(res, "roles", None) or (res.get("roles") if isinstance(res, Mapping) else ())

        if not res_id:
            continue

        item = {
            "resource_id": res_id,
            "provider": provider,
            "model": model,
            "account_id": account_id,
            "is_heterogeneous": False,
        }

        # Check exclusion
        if res_id in excluded_set:
            ineligible.append({**item, "reason": "excluded_failed_resource"})
            continue

        # Check role compatibility
        if roles and role not in roles:
            ineligible.append({**item, "reason": f"role_{role}_not_supported"})
            continue

        # Check account cooldown / availability
        acc = accounts_map.get(account_id)
        if acc:
            available = getattr(acc, "available", True) if hasattr(acc, "available") else acc.get("available", True)
            in_cd = getattr(acc, "in_cooldown", False) if hasattr(acc, "in_cooldown") else acc.get("in_cooldown", False)
            if not available or in_cd:
                ineligible.append({**item, "reason": "account_unavailable_or_in_cooldown"})
                continue

        # Check provider independence for reviewer / adjudicator
        if role in {"reviewer", "adjudicator"} and independence == "provider" and previous_provider:
            if provider.lower() == previous_provider.lower():
                ineligible.append({**item, "reason": f"provider independence required from '{previous_provider}'"})
                continue

        eligible.append(item)

    # 2. Configured escalation and independent fallbacks from RoutingPolicyConfig
    fallbacks = getattr(routing_config, "escalation_fallback_resources", ())
    if not fallbacks and isinstance(routing_config, Mapping):
        fallbacks = routing_config.get("escalation_fallback_resources", ())

    indep_fallbacks = getattr(routing_config, "independent_reviewer_resources", ())
    if not indep_fallbacks and isinstance(routing_config, Mapping):
        indep_fallbacks = routing_config.get("independent_reviewer_resources", ())

    for fb_str in fallbacks:
        parts = fb_str.split("/")
        fb_provider = parts[0] if parts else "fallback"
        fb_model = parts[2] if len(parts) >= 3 else (parts[1] if len(parts) >= 2 else "default")
        fb_item = {
            "resource_id": fb_str,
            "provider": fb_provider,
            "model": fb_model,
            "account_id": parts[1] if len(parts) >= 2 else "default",
            "is_heterogeneous": True,
        }

        if fb_str in excluded_set:
            ineligible.append({**fb_item, "reason": "excluded_failed_resource"})
            continue

        if role in {"reviewer", "adjudicator"} and independence == "provider" and previous_provider:
            if fb_provider.lower() == previous_provider.lower():
                ineligible.append({**fb_item, "reason": f"provider independence required from '{previous_provider}'"})
                continue

        eligible.append(fb_item)
        heterogeneous.append(fb_item)

    for ind_str in indep_fallbacks:
        parts = ind_str.split("/")
        ind_provider = parts[0] if parts else "independent"
        ind_model = parts[2] if len(parts) >= 3 else (parts[1] if len(parts) >= 2 else "default")
        ind_item = {
            "resource_id": ind_str,
            "provider": ind_provider,
            "model": ind_model,
            "account_id": parts[1] if len(parts) >= 2 else "default",
            "is_heterogeneous": True,
        }
        if ind_str in excluded_set:
            continue
        if previous_provider and ind_provider.lower() == previous_provider.lower():
            continue
        independent.append(ind_item)

    return {
        "eligible_candidates": eligible,
        "ineligible_candidates": ineligible,
        "heterogeneous_candidates": heterogeneous,
        "independent_candidates": independent,
    }
