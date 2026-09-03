"""Deterministic, fail-closed agent router.

Routing is side-effect free and never starts a model. For every registered
backend it records eligibility/rejection evidence, applies the mandatory
filters in order, scores eligible candidates and selects the highest score
(ties resolve lexicographically by backend id).

Mandatory filters, in order:

1. explicit exclusion by the request;
2. probe availability;
3. quota not ``EXHAUSTED``;
4. requested role supported;
5. all required capability tags present.

Routing fails closed per backend: if one backend's ``probe()`` raises, that
backend is recorded ineligible with explicit probe-error evidence and the
remaining backends are still evaluated, so a healthy fallback stays
selectable. The router never silently falls back around an explicit
exclusion or a missing required capability: when nothing is eligible the
selected backend is ``None`` and every rejection reason is part of the
decision.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from dev_orchestrator.agents.models import (
    AgentRequest,
    BackendStatus,
    QuotaState,
    RoutingCandidate,
    RoutingDecision,
)
from dev_orchestrator.agents.registry import BackendRegistry

_QUOTA_POINTS: Dict[QuotaState, int] = {
    QuotaState.HEALTHY: 40,
    QuotaState.UNKNOWN: 30,
    QuotaState.CONSERVE: 20,
    QuotaState.LOW: 10,
    QuotaState.EXHAUSTED: 0,
}

_PREFERENCE_FIRST = 100
_PREFERENCE_STEP = 10
_PREFERENCE_FLOOR = 10


def _quota_points(quota: QuotaState) -> int:
    return _QUOTA_POINTS.get(quota, 0)


def _preference_points(backend_id: str, preferred_backends: Tuple[str, ...]) -> int:
    """Positional preference bonus: first +100, descending to a floor of +10."""
    for index, candidate_id in enumerate(preferred_backends):
        if candidate_id == backend_id:
            return max(_PREFERENCE_FLOOR, _PREFERENCE_FIRST - _PREFERENCE_STEP * index)
    return 0


class AgentRouter:
    """Routes one ``AgentRequest`` across a ``BackendRegistry``."""

    def __init__(self, registry: BackendRegistry) -> None:
        if not isinstance(registry, BackendRegistry):
            raise TypeError("AgentRouter requires a BackendRegistry")
        self._registry = registry

    @property
    def registry(self) -> BackendRegistry:
        return self._registry

    def route(self, request: AgentRequest) -> RoutingDecision:
        """Evaluate every registered backend and pick the best eligible one."""
        candidates: List[RoutingCandidate] = []
        excluded = request.excluded_backends
        required = request.required_capabilities

        for backend in self._registry.all():
            backend_id = backend.backend_id
            reasons: List[str] = []
            status: Optional[BackendStatus] = None

            if backend_id in excluded:
                reasons.append("excluded by request")
            else:
                try:
                    status = backend.probe()
                except Exception as exc:
                    # Fail closed for this backend only: keep evaluating the
                    # remaining backends with explicit probe-error evidence.
                    reasons.append("probe error: {0}".format(exc))
            if status is not None:
                if not status.available:
                    detail = status.reason or "backend reported unavailable"
                    reasons.append("probe unavailable: {0}".format(detail))
                elif status.quota == QuotaState.EXHAUSTED:
                    reasons.append("quota EXHAUSTED")
                else:
                    caps = backend.capabilities()
                    if request.role not in caps.roles:
                        reasons.append(
                            "role {0} not supported".format(request.role.value)
                        )
                    else:
                        missing = required - caps.tags
                        if missing:
                            reasons.append(
                                "missing required capability: {0}".format(
                                    ", ".join(sorted(missing))
                                )
                            )

            eligible = not reasons
            score = 0
            if eligible and status is not None:
                score = _quota_points(status.quota)
                score += _preference_points(backend_id, request.preferred_backends)
            candidates.append(
                RoutingCandidate(
                    backend_id=backend_id,
                    eligible=eligible,
                    score=score,
                    reasons=tuple(reasons),
                )
            )

        ordered = _order_candidates(candidates)
        eligible_candidates = [candidate for candidate in ordered if candidate.eligible]
        if not eligible_candidates:
            return RoutingDecision(
                selected_backend_id=None,
                candidates=tuple(ordered),
                reason=_no_eligible_reason(ordered),
            )

        best = min(eligible_candidates, key=lambda candidate: (-candidate.score, candidate.backend_id))
        return RoutingDecision(
            selected_backend_id=best.backend_id,
            candidates=tuple(ordered),
            reason="selected backend {0!r} with score {1}".format(
                best.backend_id, best.score
            ),
        )


def _order_candidates(candidates: List[RoutingCandidate]) -> List[RoutingCandidate]:
    """Deterministic evidence order: eligible (score desc, id asc), then not."""
    eligible = sorted(
        (c for c in candidates if c.eligible),
        key=lambda c: (-c.score, c.backend_id),
    )
    ineligible = sorted(
        (c for c in candidates if not c.eligible), key=lambda c: c.backend_id
    )
    return eligible + ineligible


def _no_eligible_reason(candidates: List[RoutingCandidate]) -> str:
    if not candidates:
        return "no backends registered"
    rejected = ", ".join(
        "{0} ({1})".format(c.backend_id, "; ".join(c.reasons))
        for c in sorted(candidates, key=lambda c: c.backend_id)
    )
    return "no eligible backend; rejected: {0}".format(rejected)
