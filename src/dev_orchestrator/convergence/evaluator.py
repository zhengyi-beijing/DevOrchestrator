"""Pure ConvergenceEvaluator v0: total, deterministic, side-effect-free decide().

Input: (WorkRecord, EvidenceSnapshot, Policy, now)
Output: Decision (exactly one of the 12 frozen DecisionKind values).
Never writes files, starts processes, touches git, or mutates the Work Record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Mapping

from dev_orchestrator.convergence.evidence import EvidenceSnapshot, has_unresolved_ambiguity, snapshot_digest
from dev_orchestrator.convergence.findings import FindingSeverity
from dev_orchestrator.convergence.policy import Policy
from dev_orchestrator.convergence.problems import FailureClass, ProblemBudget, ProblemTracker
from dev_orchestrator.convergence.verification import is_goal_satisfied
from dev_orchestrator.convergence.work_record import WorkRecord, work_record_digest


class DecisionKind(str, Enum):
    NOOP_ACTIVE = "NOOP_ACTIVE"
    WAIT_UNTIL = "WAIT_UNTIL"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    RETRY_SAME_STRATEGY = "RETRY_SAME_STRATEGY"
    RETRY_NEW_STRATEGY = "RETRY_NEW_STRATEGY"
    FAILOVER_RESOURCE = "FAILOVER_RESOURCE"
    ESCALATE_CAPABILITY = "ESCALATE_CAPABILITY"
    REQUEST_HUMAN = "REQUEST_HUMAN"
    WRITE_HANDOFF = "WRITE_HANDOFF"
    SATISFY_GOAL = "SATISFY_GOAL"
    PUBLISH_SUCCESSOR = "PUBLISH_SUCCESSOR"


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str
    problem_id: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    invariant_citations: tuple[str, ...] = field(default_factory=tuple)
    evidence_digests: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "problem_id": self.problem_id,
            "parameters": dict(self.parameters),
            "idempotency_key": self.idempotency_key,
            "invariant_citations": list(self.invariant_citations),
            "evidence_digests": list(self.evidence_digests),
        }


def _make_idempotency_key(
    goal_id: str,
    kind: DecisionKind,
    problem_id: str | None,
    work_digest: str,
    ev_digest: str,
) -> str:
    raw = f"{goal_id}:{kind.value}:{problem_id or 'none'}:{work_digest}:{ev_digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def decide(
    work_record: WorkRecord,
    evidence: EvidenceSnapshot,
    policy: Policy,
    now: str | datetime | None = None,
) -> Decision:
    """Pure, deterministic, side-effect-free decision function.

    Takes a WorkRecord snapshot, EvidenceSnapshot, Policy, and current time.
    Returns exactly one typed Decision with invariant citations and idempotency key.
    """
    if now is None:
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
    elif isinstance(now, datetime):
        now_dt = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        now_iso = now_dt.isoformat()
    else:
        now_iso = str(now)
        try:
            now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except ValueError:
            now_dt = datetime.now(timezone.utc)

    w_digest = work_record_digest(work_record)
    ev_digest = snapshot_digest(evidence)

    def make_decision(
        kind: DecisionKind,
        reason: str,
        problem_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        invariants: tuple[str, ...] = (),
    ) -> Decision:
        idemp = _make_idempotency_key(work_record.goal_id, kind, problem_id, w_digest, ev_digest)
        return Decision(
            kind=kind,
            reason=reason,
            problem_id=problem_id,
            parameters=parameters or {},
            idempotency_key=idemp,
            invariant_citations=invariants,
            evidence_digests=(w_digest, ev_digest),
        )

    # 1. EMERGENCY BRAKE
    # Authenticated pause/stop is an out-of-band safety interlock checked first.
    if evidence.emergency_pause_asserted:
        return make_decision(
            DecisionKind.WAIT_UNTIL,
            "Emergency pause/stop asserted by owner; side effects are gated",
            parameters={"emergency_pause": True},
            invariants=("EMERGENCY_BRAKE", "SINGLE_AUTHORITY"),
        )

    # 2. STATUS == DONE
    if work_record.status == "DONE":
        # Check if successor needs publication
        if work_record.successor and work_record.successor.get("publication_state") == "PENDING":
            return make_decision(
                DecisionKind.PUBLISH_SUCCESSOR,
                "Goal is terminal DONE; successor is ready for publication",
                parameters={"successor_goal_id": work_record.successor["successor_goal_id"]},
                invariants=("SUCCESSOR_DETERMINISM", "ACCEPTANCE_BEFORE_ADVANCE"),
            )
        return make_decision(
            DecisionKind.NOOP_ACTIVE,
            "Goal is terminal DONE with no pending successor publication",
            invariants=("SINGLE_AUTHORITY",),
        )

    # 3. HUMAN REQUEST OPEN / ANSWERED
    hr = work_record.human_request
    if hr is not None:
        answer = hr.get("answer")
        if answer and answer.get("option_id"):
            # Request answered! Find chosen option and effect
            chosen_opt_id = answer["option_id"]
            matched_opts = [o for o in hr.get("options", []) if o.get("option_id") == chosen_opt_id]
            effect = matched_opts[0].get("effect", "resume") if matched_opts else "resume"
            return make_decision(
                DecisionKind.EXECUTE,
                f"Human request answered with option {chosen_opt_id!r}; applying effect {effect!r}",
                parameters={"human_answer": answer, "effect": effect},
                invariants=("HUMAN_REQUEST_DISCHARGEABLE", "PROGRESS_TOTALITY"),
            )
        else:
            # Unanswered human request
            return make_decision(
                DecisionKind.REQUEST_HUMAN,
                f"Active human request {hr.get('question_id')!r} is awaiting owner input",
                parameters={"question_id": hr.get("question_id"), "concrete_question": hr.get("concrete_question")},
                invariants=("HUMAN_TYPED", "PROGRESS_TOTALITY"),
            )

    # 4. STATUS == NEEDS_HUMAN without active human request
    if work_record.status == "NEEDS_HUMAN":
        if work_record.handoff is None:
            return make_decision(
                DecisionKind.WRITE_HANDOFF,
                "Goal is in NEEDS_HUMAN state; writing structured resumable HANDOFF",
                invariants=("COMPLETE_EXHAUSTION", "PROGRESS_TOTALITY"),
            )
        return make_decision(
            DecisionKind.NOOP_ACTIVE,
            "Goal is in NEEDS_HUMAN state with complete structured HANDOFF; awaiting owner action",
            invariants=("COMPLETE_EXHAUSTION",),
        )

    # 5. ACTIVE LEASE LIVENESS
    lease = work_record.active_lease
    if lease is not None:
        # Check if lease is reported live in evidence
        # If evidence indicates lease is live, NOOP_ACTIVE
        lease_live = False
        for item in evidence.items:
            if item.source == "process_probe" and item.data.get("role") == lease.get("role"):
                lease_live = bool(item.data.get("alive"))
                break
            if item.source == "broker_effect" and item.data.get("execution_id") == lease.get("execution_id"):
                lease_live = item.data.get("state") in ("running", "live")
                break
        else:
            # If no probe specifically says dead, assume active if acquired recently or has heartbeat
            lease_live = True

        if lease_live:
            return make_decision(
                DecisionKind.NOOP_ACTIVE,
                f"Active execution lease held by role {lease.get('role')!r} is live",
                parameters={"role": lease.get("role"), "attempt_id": lease.get("attempt_id")},
                invariants=("SINGLE_ACTIVE_LEASE", "NOOP_ACTIVE"),
            )

    # 6. BOUNDED WAIT
    wait = work_record.wait
    if wait is not None:
        not_before = wait.get("not_before")
        if not_before:
            try:
                nb_dt = datetime.fromisoformat(not_before.replace("Z", "+00:00"))
                if now_dt < nb_dt:
                    return make_decision(
                        DecisionKind.WAIT_UNTIL,
                        f"Waiting until {not_before} due to {wait.get('reason_code')!r}",
                        parameters={"not_before": not_before, "reset_source": wait.get("reset_source")},
                        invariants=("WAIT_IS_NOT_PROGRESS",),
                    )
            except ValueError:
                pass

    # 7. GOAL SATISFACTION EVALUATION
    satisfied, sat_reason = is_goal_satisfied(work_record)
    if satisfied:
        if work_record.successor and work_record.successor.get("publication_state") != "PUBLISHED":
            return make_decision(
                DecisionKind.PUBLISH_SUCCESSOR,
                "Goal is satisfied and verified; publishing deterministic successor",
                parameters={"successor_goal_id": work_record.successor["successor_goal_id"]},
                invariants=("SUCCESSOR_DETERMINISM", "ACCEPTANCE_BEFORE_ADVANCE"),
            )
        return make_decision(
            DecisionKind.SATISFY_GOAL,
            f"Goal criteria and verification satisfied: {sat_reason}",
            invariants=("ACCEPTANCE_BEFORE_ADVANCE",),
        )

    # 8. FAIL-CLOSED AMBIGUITY
    if has_unresolved_ambiguity(evidence):
        return make_decision(
            DecisionKind.REQUEST_HUMAN,
            "Unresolved ambiguity or conflicting evidence detected; failing closed",
            problem_id="integrity_ambiguity",
            parameters={"failure_class": FailureClass.INTEGRITY_OR_IDENTITY_AMBIGUITY.value},
            invariants=("FAIL_CLOSED_AMBIGUITY", "HUMAN_TYPED"),
        )

    # 9. CURRENT PROBLEM EVALUATION & STRATEGY ESCALATION
    curr_prob = work_record.current_problem
    if curr_prob is not None:
        p_id = curr_prob.get("problem_id", "p-default")
        f_class = curr_prob.get("failure_class", FailureClass.IMPLEMENTATION_DEFECT.value)
        budget = policy.get_budget_for_problem(p_id)

        # Count prior attempts for this problem
        matching_attempts = [a for a in work_record.attempts if a.get("problem_id") == p_id]
        total_attempts = len(matching_attempts)
        resource_attempts = sum(1 for a in matching_attempts if a.get("strategy_id") == "resource_retry")
        strategy_attempts = sum(1 for a in matching_attempts if a.get("strategy_id") == "strategy_change")
        escalations = sum(1 for a in matching_attempts if a.get("strategy_id") == "capability_escalation")
        output_repairs = sum(1 for a in matching_attempts if a.get("strategy_id") == "output_repair")

        # Quota / transient resource failure with known reset
        if f_class in (FailureClass.RESOURCE_TRANSIENT.value, FailureClass.PROVIDER_UNAVAILABLE.value):
            # Check policy quota reset rules
            for rule_pattern, reset_info in policy.quota_reset_rules.items():
                if rule_pattern in curr_prob.get("criterion_or_invariant_id", "") or rule_pattern in p_id:
                    reset_time = reset_info.get("reset_at") or (now_dt.isoformat())
                    return make_decision(
                        DecisionKind.WAIT_UNTIL,
                        f"Quota or resource transient failure with known reset at {reset_time}",
                        problem_id=p_id,
                        parameters={"not_before": reset_time, "reset_source": "policy_quota_reset"},
                        invariants=("WAIT_IS_NOT_PROGRESS",),
                    )

            # Resource failover if budget permits
            if resource_attempts < budget.max_resource_attempts:
                return make_decision(
                    DecisionKind.FAILOVER_RESOURCE,
                    f"Transient provider/resource error for problem {p_id!r}; failing over at same tier",
                    problem_id=p_id,
                    parameters={"attempt": resource_attempts + 1},
                    invariants=("BOUNDED_PROBLEM", "PROGRESS_TOTALITY"),
                )

        # OUTPUT_INVALID: schema/validator repair at same tier first
        if f_class == FailureClass.OUTPUT_INVALID.value:
            if output_repairs < policy.max_output_invalid_repairs:
                return make_decision(
                    DecisionKind.RETRY_SAME_STRATEGY,
                    f"Malformed output for problem {p_id!r}; running bounded schema repair at same capability tier",
                    problem_id=p_id,
                    parameters={"repair_attempt": output_repairs + 1, "validator_feedback": True},
                    invariants=("BOUNDED_PROBLEM", "PROGRESS_TOTALITY"),
                )

        # Implementation / verification defect: try new strategy
        if f_class in (FailureClass.IMPLEMENTATION_DEFECT.value, FailureClass.VERIFICATION_FAILURE.value):
            if strategy_attempts < budget.max_strategy_attempts:
                return make_decision(
                    DecisionKind.RETRY_NEW_STRATEGY,
                    f"Defect on problem {p_id!r}; trying bounded changed strategy",
                    problem_id=p_id,
                    parameters={"strategy_attempt": strategy_attempts + 1},
                    invariants=("BOUNDED_PROBLEM", "PROGRESS_TOTALITY"),
                )
            elif escalations < budget.max_capability_escalations:
                return make_decision(
                    DecisionKind.ESCALATE_CAPABILITY,
                    f"Strategy attempts exhausted for {p_id!r}; escalating capability tier",
                    problem_id=p_id,
                    parameters={"escalation_count": escalations + 1},
                    invariants=("BOUNDED_PROBLEM", "PROGRESS_TOTALITY"),
                )

        # Reasoning / strategy defect: escalate capability tier
        if f_class == FailureClass.REASONING_OR_STRATEGY_DEFECT.value:
            if escalations < budget.max_capability_escalations:
                return make_decision(
                    DecisionKind.ESCALATE_CAPABILITY,
                    f"Reasoning defect for {p_id!r}; escalating capability tier",
                    problem_id=p_id,
                    parameters={"escalation_count": escalations + 1},
                    invariants=("BOUNDED_PROBLEM", "PROGRESS_TOTALITY"),
                )

        # Auth or permission failure: requires human
        if f_class == FailureClass.AUTH_OR_PERMISSION.value:
            return make_decision(
                DecisionKind.REQUEST_HUMAN,
                f"Missing credentials or authorization for {p_id!r}; human action required",
                problem_id=p_id,
                parameters={"failure_class": f_class},
                invariants=("HUMAN_TYPED", "PROGRESS_TOTALITY"),
            )

        # Irreversible authorization required: requires human
        if f_class == FailureClass.SAFETY_OR_IRREVERSIBLE_AUTHORIZATION.value:
            return make_decision(
                DecisionKind.REQUEST_HUMAN,
                f"Safety or irreversible operation boundary reached for {p_id!r}; human authorization required",
                problem_id=p_id,
                parameters={"failure_class": f_class},
                invariants=("HUMAN_TYPED", "EMERGENCY_BRAKE"),
            )

        # If total attempts or strategy budgets exhausted
        return make_decision(
            DecisionKind.WRITE_HANDOFF,
            f"All retry/escalation budgets exhausted for problem {p_id!r}; writing resumable HANDOFF",
            problem_id=p_id,
            parameters={"total_attempts": total_attempts},
            invariants=("COMPLETE_EXHAUSTION", "BOUNDED_PROBLEM"),
        )

    # 10. VERIFICATION / EXECUTION ROUTING
    # If there is unverified progress (e.g. attempt finished, needs verification)
    if work_record.attempts and not work_record.verification:
        return make_decision(
            DecisionKind.VERIFY,
            "Execution completed; requesting independent typed verification",
            invariants=("ACCEPTANCE_BEFORE_ADVANCE", "PROGRESS_TOTALITY"),
        )

    # 11. DEFAULT PROGRESS (EXECUTE)
    return make_decision(
        DecisionKind.EXECUTE,
        "Goal is open with no active lease, pending problem, or blocking evidence; executing work",
        invariants=("PROGRESS_TOTALITY", "SINGLE_AUTHORITY"),
    )
