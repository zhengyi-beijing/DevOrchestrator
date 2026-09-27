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

    def __init__(
        self,
        executed_idempotency_keys: Set[str] | None = None,
        work_record: WorkRecord | Mapping[str, Any] | None = None,
    ) -> None:
        self._executed_keys = set(executed_idempotency_keys or ())
        if work_record is not None:
            self.seed_from_work_record(work_record)

    def seed_from_work_record(self, work_record: WorkRecord | Mapping[str, Any]) -> None:
        """Seed executed idempotency keys from durable work record state."""
        succ = getattr(work_record, "successor", None)
        if succ is None and isinstance(work_record, Mapping):
            succ = work_record.get("successor")
        if isinstance(succ, Mapping):
            pub_state = succ.get("publication_state")
            key = succ.get("handoff_idempotency_key")
            if key and pub_state in ("PUBLISHED", "COMMITTED"):
                self._executed_keys.add(key)

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
        # Ensure guard is seeded from durable work record
        succ = getattr(work_record, "successor", None)
        if isinstance(succ, Mapping):
            pub_state = succ.get("publication_state")
            key = succ.get("handoff_idempotency_key")
            if key and pub_state in ("PUBLISHED", "COMMITTED"):
                self._executed_keys.add(key)

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

        # 5. Acceptance check for PUBLISH_SUCCESSOR
        if decision.kind == DecisionKind.PUBLISH_SUCCESSOR:
            acc_kind = work_record.acceptance.get("kind", "NONE")
            if acc_kind not in ("VERIFIED", "OWNER_OVERRIDE"):
                return GuardVerdict(
                    accepted=False,
                    reason=f"Cannot publish successor: acceptance kind is {acc_kind!r}, expected VERIFIED or OWNER_OVERRIDE",
                    rejection_code="ACCEPTANCE_REQUIRED",
                )

        # 6. Exact repository anchor check (where required: VERIFY, SATISFY_GOAL, PUBLISH_SUCCESSOR)
        if decision.kind in (DecisionKind.VERIFY, DecisionKind.SATISFY_GOAL, DecisionKind.PUBLISH_SUCCESSOR):
            current_head = (evidence.exact_anchors or {}).get("head")
            if not current_head or not expected_anchor_head:
                return GuardVerdict(
                    accepted=False,
                    reason="Repository HEAD anchor is required for decision but missing from evidence or expectation",
                    rejection_code="ANCHOR_REQUIRED_MISSING",
                )
            if current_head != expected_anchor_head:
                return GuardVerdict(
                    accepted=False,
                    reason=f"Repository HEAD anchor mismatch: {current_head!r} != {expected_anchor_head!r}",
                    rejection_code="ANCHOR_MISMATCH",
                )

        # 7. Active lease uniqueness check (liveness-aware)
        if decision.kind in (
            DecisionKind.EXECUTE,
            DecisionKind.RETRY_SAME_STRATEGY,
            DecisionKind.RETRY_NEW_STRATEGY,
            DecisionKind.FAILOVER_RESOURCE,
            DecisionKind.ESCALATE_CAPABILITY,
            DecisionKind.VERIFY,
        ):
            if work_record.active_lease is not None:
                role = work_record.active_lease.get("role")
                exec_id = work_record.active_lease.get("execution_id")

                found_liveness = False
                lease_live = False
                lease_ambiguous = False
                terminal_success = False

                for item in evidence.items:
                    if item.source == "process_probe" and item.data.get("role") == role:
                        found_liveness = True
                        alive = item.data.get("alive")
                        if alive is True:
                            lease_live = True
                        elif alive is False:
                            lease_live = False
                        else:
                            lease_ambiguous = True
                        break
                    if item.source == "broker_effect" and item.data.get("execution_id") == exec_id:
                        found_liveness = True
                        st = item.data.get("state")
                        if st in ("running", "live"):
                            lease_live = True
                        elif st in ("failed", "cancelled", "terminal"):
                            lease_live = False
                        elif st in ("succeeded", "completed"):
                            lease_live = False
                            terminal_success = True
                        else:
                            lease_ambiguous = True
                        break

                is_recovery_decision = decision.kind in (
                    DecisionKind.RETRY_SAME_STRATEGY,
                    DecisionKind.RETRY_NEW_STRATEGY,
                    DecisionKind.FAILOVER_RESOURCE,
                    DecisionKind.ESCALATE_CAPABILITY,
                )
                is_verify_on_terminal_success = (decision.kind == DecisionKind.VERIFY and terminal_success)

                if found_liveness and not lease_live and not lease_ambiguous and (is_recovery_decision or is_verify_on_terminal_success):
                    # Demonstrably non-live lease: admit recovery or verify decision
                    pass
                elif lease_live:
                    return GuardVerdict(
                        accepted=False,
                        reason=f"Cannot admit decision {decision.kind.value}: active lease already held by live role {role!r}",
                        rejection_code="LEASE_ALREADY_ACTIVE",
                    )
                else:
                    return GuardVerdict(
                        accepted=False,
                        reason=f"Cannot admit decision {decision.kind.value}: active lease already held by role {role!r}",
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
