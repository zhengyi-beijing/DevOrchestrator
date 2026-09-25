"""Deterministic AGY-first model routing policy with reviewer independence and escalation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from dev_orchestrator.ai.contracts import AIRoleRequest, ResourceContext

from .agy_pool import AGYResourcePool
from .models import (
    FailureSignature,
    PolicyDisposition,
    RoutingCandidate,
    RoutingPolicyConfig,
    RoutingRecommendation,
    RoutingTier,
)


class AGYFirstRoutingPolicy:
    """Routes AI role requests with AGY-first preference, reviewer independence, and same-failure escalation."""

    def __init__(
        self,
        pool: AGYResourcePool,
        config: Optional[RoutingPolicyConfig] = None,
    ) -> None:
        self.pool = pool
        self.config = config if config is not None else RoutingPolicyConfig()

    def route(
        self,
        request: AIRoleRequest,
        failure_history: Optional[Sequence[Mapping[str, Any]]] = None,
        strategy_changed: bool = False,
        current_failure_signature: Optional[str] = None,
        baseline_resources: Optional[Sequence[str]] = None,
        now: Optional[datetime] = None,
    ) -> RoutingRecommendation:
        """Evaluate and route request across AGY pool and heterogeneous baselines."""
        dt_now = now if now is not None else datetime.now(timezone.utc)
        fallbacks = tuple(baseline_resources) if baseline_resources else self.config.escalation_fallback_resources
        candidates: list[RoutingCandidate] = []
        pool_telemetry = self.pool.telemetry(now=dt_now)
        telemetry_dict = pool_telemetry.to_dict()

        # Step 1: Enforce Reviewer Independence Invariant
        # High-risk / final technical review cannot be weakened by near-zero marginal cost execution
        if request.role in ("reviewer", "adjudicator"):
            prev_ctx = request.previous_resource_context
            prev_provider = None
            prev_account = None
            prev_res = None
            if prev_ctx is not None:
                if isinstance(prev_ctx, ResourceContext):
                    prev_provider = prev_ctx.provider
                    prev_account = prev_ctx.account
                    prev_res = prev_ctx.resource_id
                elif isinstance(prev_ctx, Mapping):
                    prev_provider = prev_ctx.get("provider")
                    prev_account = prev_ctx.get("account")
                    prev_res = prev_ctx.get("resource_id")

            if request.independence == "provider" and prev_provider == "agy":
                # Strict provider independence required from AGY worker
                # Mark all AGY pool resources ineligible
                for res in pool_telemetry.resources:
                    candidates.append(
                        RoutingCandidate(
                            resource_id=res.resource_id,
                            provider=res.provider,
                            account_id=res.account_id,
                            eligible=False,
                            score=0,
                            tier=RoutingTier.AGY_POOL,
                            reasons=("provider independence required from worker provider 'agy'",),
                        )
                    )
                # Select from independent reviewer resources
                chosen_reviewer_res = self.config.independent_reviewer_resources[0] if self.config.independent_reviewer_resources else fallbacks[0]
                candidates.append(
                    RoutingCandidate(
                        resource_id=chosen_reviewer_res,
                        provider=chosen_reviewer_res.split("/")[0],
                        account_id=chosen_reviewer_res.split("/")[1] if "/" in chosen_reviewer_res else "default",
                        eligible=True,
                        score=100,
                        tier=RoutingTier.INDEPENDENT_REVIEWER,
                        reasons=(),
                    )
                )
                return RoutingRecommendation(
                    selected_resource_id=chosen_reviewer_res,
                    routing_tier=RoutingTier.INDEPENDENT_REVIEWER,
                    account_id=chosen_reviewer_res.split("/")[1] if "/" in chosen_reviewer_res else "default",
                    provider=chosen_reviewer_res.split("/")[0],
                    model=chosen_reviewer_res.split("/")[2] if chosen_reviewer_res.count("/") >= 2 else "default",
                    is_escalated=True,
                    escalation_reason="reviewer_independence_enforced: provider independence required from worker provider 'agy'",
                    same_failure_prevented=False,
                    independence_enforced=True,
                    candidates=tuple(candidates),
                    telemetry_snapshot=telemetry_dict,
                    policy_disposition=PolicyDisposition.UNSUPPORTED,
                    decision_reason="selected independent reviewer resource outside AGY provider to guarantee reviewer independence",
                )

        # Step 2: Enforce Same-Failure Retry & Heterogeneous Escalation Invariant
        # "Prefer parallel use of the three AGY accounts on independent work; do not rotate accounts blindly against the same failure signature."
        # "On repeated equivalent failure, require a changed strategy/context or escalate to a heterogeneous model/provider rather than consuming another AGY account with the same approach."
        if failure_history:
            last_failure = failure_history[-1]
            last_provider = last_failure.get("provider") or ("agy" if "agy" in str(last_failure.get("resource_id", "")) else None)
            last_sig = last_failure.get("failure_signature")
            curr_sig = current_failure_signature or last_sig

            if last_provider == "agy" and last_sig and (curr_sig == last_sig) and not strategy_changed:
                # Same failure signature on AGY without strategy change!
                # Block blind rotation to another AGY account!
                for res in pool_telemetry.resources:
                    candidates.append(
                        RoutingCandidate(
                            resource_id=res.resource_id,
                            provider=res.provider,
                            account_id=res.account_id,
                            eligible=False,
                            score=0,
                            tier=RoutingTier.AGY_POOL,
                            reasons=(f"same failure signature '{curr_sig}' without strategy change blocks blind AGY rotation",),
                        )
                    )

                # Escalate to heterogeneous model/provider
                escalation_res = fallbacks[0]
                candidates.append(
                    RoutingCandidate(
                        resource_id=escalation_res,
                        provider=escalation_res.split("/")[0],
                        account_id=escalation_res.split("/")[1] if "/" in escalation_res else "default",
                        eligible=True,
                        score=90,
                        tier=RoutingTier.HETEROGENEOUS_ESCALATION,
                        reasons=(),
                    )
                )
                return RoutingRecommendation(
                    selected_resource_id=escalation_res,
                    routing_tier=RoutingTier.HETEROGENEOUS_ESCALATION,
                    account_id=escalation_res.split("/")[1] if "/" in escalation_res else "default",
                    provider=escalation_res.split("/")[0],
                    model=escalation_res.split("/")[2] if escalation_res.count("/") >= 2 else "default",
                    is_escalated=True,
                    escalation_reason=f"same_failure_heterogeneous_escalation: identical failure signature '{curr_sig}' without strategy change; blind AGY account rotation prevented; escalating to heterogeneous provider",
                    same_failure_prevented=True,
                    independence_enforced=False,
                    candidates=tuple(candidates),
                    telemetry_snapshot=telemetry_dict,
                    policy_disposition=PolicyDisposition.UNSUPPORTED,
                    decision_reason=f"escalated to heterogeneous resource {escalation_res!r} to break repeated failure cycle",
                )

        # Step 3: AGY-First Pool Acquisition
        # Filter excluded accounts from request
        excluded_accounts: set[str] = set()
        for ex in request.excluded_resource_ids:
            if "/" in ex:
                parts = ex.split("/")
                if len(parts) >= 2 and parts[0] == "agy":
                    excluded_accounts.add(parts[1])
            elif ex in self.pool.account_ids:
                excluded_accounts.add(ex)

        # Check account independence if requested
        if request.role in ("reviewer", "adjudicator") and request.independence == "account":
            prev_ctx = request.previous_resource_context
            prev_account = None
            if prev_ctx is not None:
                if isinstance(prev_ctx, ResourceContext):
                    prev_account = prev_ctx.account
                elif isinstance(prev_ctx, Mapping):
                    prev_account = prev_ctx.get("account")
            if prev_account:
                excluded_accounts.add(prev_account)

        acquired_res, acquire_reason = self.pool.acquire(
            role=request.role,
            quality=request.quality,
            excluded_accounts=excluded_accounts,
            now=dt_now,
        )

        if acquired_res is not None:
            # AGY pool resource successfully allocated!
            candidates.append(
                RoutingCandidate(
                    resource_id=acquired_res.resource_id,
                    provider=acquired_res.provider,
                    account_id=acquired_res.account_id,
                    eligible=True,
                    score=100,
                    tier=RoutingTier.AGY_POOL,
                    reasons=(),
                )
            )
            return RoutingRecommendation(
                selected_resource_id=acquired_res.resource_id,
                routing_tier=RoutingTier.AGY_POOL,
                account_id=acquired_res.account_id,
                provider=acquired_res.provider,
                model=acquired_res.model,
                is_escalated=False,
                escalation_reason=None,
                same_failure_prevented=False,
                independence_enforced=(request.independence == "account" and bool(excluded_accounts)),
                candidates=tuple(candidates),
                telemetry_snapshot=telemetry_dict,
                policy_disposition=PolicyDisposition.SUPPORTED,
                decision_reason=f"allocated AGY pool resource {acquired_res.resource_id} on {acquired_res.account_id}",
            )

        # Step 4: AGY Pool Exhausted or In Cooldown -> Heterogeneous Escalation
        for res in pool_telemetry.resources:
            candidates.append(
                RoutingCandidate(
                    resource_id=res.resource_id,
                    provider=res.provider,
                    account_id=res.account_id,
                    eligible=False,
                    score=0,
                    tier=RoutingTier.AGY_POOL,
                    reasons=(acquire_reason,),
                )
            )

        fallback_res = fallbacks[0]
        candidates.append(
            RoutingCandidate(
                resource_id=fallback_res,
                provider=fallback_res.split("/")[0],
                account_id=fallback_res.split("/")[1] if "/" in fallback_res else "default",
                eligible=True,
                score=80,
                tier=RoutingTier.HETEROGENEOUS_ESCALATION,
                reasons=(),
            )
        )

        return RoutingRecommendation(
            selected_resource_id=fallback_res,
            routing_tier=RoutingTier.HETEROGENEOUS_ESCALATION,
            account_id=fallback_res.split("/")[1] if "/" in fallback_res else "default",
            provider=fallback_res.split("/")[0],
            model=fallback_res.split("/")[2] if fallback_res.count("/") >= 2 else "default",
            is_escalated=True,
            escalation_reason=f"agy_pool_capacity_exhausted: {acquire_reason}",
            same_failure_prevented=False,
            independence_enforced=False,
            candidates=tuple(candidates),
            telemetry_snapshot=telemetry_dict,
            policy_disposition=PolicyDisposition.UNCERTAIN,
            decision_reason=f"AGY pool unavailable ({acquire_reason}); escalated to heterogeneous baseline {fallback_res}",
        )
