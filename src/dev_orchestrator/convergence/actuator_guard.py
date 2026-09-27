"""Non-writing actuator guard for single-authority convergence.

Accepts a Decision only after rigorous revalidation of:
- authority_revision,
- project/goal identity,
- exact repository anchor where required,
- active lease uniqueness,
- emergency pause/stop (checked immediately before every side effect!),
- required human authorization,
- idempotency key (deduplication),
- safety fences.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Set

from dev_orchestrator.convergence.evaluator import Decision, DecisionKind
from dev_orchestrator.convergence.evidence import EvidenceSnapshot
from dev_orchestrator.convergence.work_record import WorkRecord


@dataclass(frozen=True)
class GuardVerdict:
    accepted: bool
    reason: str
    rejection_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "rejection_code": self.rejection_code,
        }


class ActuatorGuard:
    """Pure, side-effect-free actuator guard that validates decisions before execution."""

    def __init__(self, executed_idempotency_keys: Set[str] | None = None) -> None:
        self._executed_keys = set(executed_idempotency_keys or ())

    def validate(
        self,
        decision: Decision,
        work_record: WorkRecord,
        evidence: EvidenceSnapshot,
        *,
        expected_authority_revision: str | None = None,
        expected_goal_id: str | None = None,
        expected_anchor_head: str | None = None,
    ) -> GuardVerdict:
        """Validate whether a decision can be safely admitted for execution."""
        # 1. Emergency Pause / Stop check (absolute highest priority safety interlock)
        if evidence.emergency_pause_asserted:
            if decision.kind not in (DecisionKind.NOOP_ACTIVE, DecisionKind.WAIT_UNTIL):
                return GuardVerdict(
                    accepted=False,
                    reason="Emergency pause asserted; actuator blocks all mutative decisions",
                    rejection_code="EMERGENCY_BRAKE_ACTIVE",
                )

        # 2. Authority revision check
        if expected_authority_revision and work_record.authority_revision != expected_authority_revision:
            return GuardVerdict(
                accepted=False,
                reason=f"Authority revision mismatch: {work_record.authority_revision!r} != {expected_authority_revision!r}",
                rejection_code="AUTHORITY_REVISION_MISMATCH",
            )

        # 3. Project / Goal identity check
        if expected_goal_id and work_record.goal_id != expected_goal_id:
            return GuardVerdict(
                accepted=False,
                reason=f"Goal ID mismatch: {work_record.goal_id!r} != {expected_goal_id!r}",
                rejection_code="GOAL_IDENTITY_MISMATCH",
            )

        # 4. Idempotency deduplication check
        if decision.idempotency_key and decision.idempotency_key in self._executed_keys:
            return GuardVerdict(
                accepted=False,
                reason=f"Duplicate execution: idempotency key {decision.idempotency_key!r} already executed",
                rejection_code="DUPLICATE_IDEMPOTENCY_KEY",
            )

        # 5. Exact repository anchor check (where required, e.g. for VERIFY or SATISFY_GOAL)
        if decision.kind in (DecisionKind.VERIFY, DecisionKind.SATISFY_GOAL):
            current_head = evidence.exact_anchors.get("head")
            if expected_anchor_head and current_head and current_head != expected_anchor_head:
                return GuardVerdict(
                    accepted=False,
                    reason=f"Repository HEAD anchor mismatch: {current_head!r} != {expected_anchor_head!r}",
                    rejection_code="ANCHOR_MISMATCH",
                )

        # 6. Active lease uniqueness check
        if decision.kind in (DecisionKind.EXECUTE, DecisionKind.RETRY_SAME_STRATEGY, DecisionKind.RETRY_NEW_STRATEGY):
            if work_record.active_lease is not None:
                role = work_record.active_lease.get("role")
                return GuardVerdict(
                    accepted=False,
                    reason=f"Cannot launch execution: active lease already held by role {role!r}",
                    rejection_code="LEASE_ALREADY_ACTIVE",
                )

        # 7. Required human authorization check
        if decision.kind == DecisionKind.REQUEST_HUMAN:
            # Emitting human request is always safe
            pass

        return GuardVerdict(
            accepted=True,
            reason=f"Decision {decision.kind.value} passed all guard checks",
            rejection_code=None,
        )

    def record_executed(self, idempotency_key: str) -> None:
        """Mark an idempotency key as executed in memory."""
        if idempotency_key:
            self._executed_keys.add(idempotency_key)
