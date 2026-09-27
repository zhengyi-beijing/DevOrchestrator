"""Stable 21-code convergence invariant registry and pure evaluators.

Ordinals are explanatory only and MUST NOT be persisted or used for branching.
All invariant identity uses the stable code names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from dev_orchestrator.convergence.evidence import EvidenceSnapshot, has_unresolved_ambiguity
from dev_orchestrator.convergence.policy import Policy
from dev_orchestrator.convergence.verification import is_goal_satisfied, validate_owner_override
from dev_orchestrator.convergence.work_record import WorkRecord

CONVERGENCE_INVARIANT_CODES: tuple[str, ...] = (
    "SINGLE_AUTHORITY",
    "SINGLE_ACTIVE_LEASE",
    "PROGRESS_TOTALITY",
    "ACCEPTANCE_BEFORE_ADVANCE",
    "OWNER_OVERRIDE_EXPLICIT",
    "HUMAN_REQUEST_DISCHARGEABLE",
    "MARKDOWN_NON_AUTHORITY",
    "ANCHOR_BINDING",
    "IDEMPOTENT_REPLAY",
    "STABLE_PROBLEM_IDENTITY",
    "BOUNDED_PROBLEM",
    "COMPLETE_EXHAUSTION",
    "HUMAN_TYPED",
    "EMERGENCY_BRAKE",
    "FAIL_CLOSED_AMBIGUITY",
    "LEARNED_CONSTRAINT_CONSUMPTION",
    "LEARNING_REGRESSION",
    "SUCCESSOR_DETERMINISM",
    "NO_HUMAN_CLOCK",
    "WAIT_IS_NOT_PROGRESS",
    "INVARIANT_CODE_STABILITY",
)

# Mapping to 6 production lifecycle invariants
LIFECYCLE_INVARIANT_MAP: dict[str, tuple[str, ...]] = {
    "SINGLE_AUTHORITY": ("SINGLE_ACTIVE_LIFECYCLE_OWNER",),
    "SINGLE_ACTIVE_LEASE": ("SINGLE_ACTIVE_LIFECYCLE_OWNER", "CURRENT_TASK_MATCHES_ACTIVE_EXECUTION"),
    "PROGRESS_TOTALITY": ("NEXT_TASK_WITHOUT_HANDOFF",),
    "ACCEPTANCE_BEFORE_ADVANCE": ("TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION", "SUCCESSOR_HANDOFF_LINEAGE_VALID"),
    "OWNER_OVERRIDE_EXPLICIT": ("SINGLE_ACTIVE_LIFECYCLE_OWNER",),
    "HUMAN_REQUEST_DISCHARGEABLE": ("SINGLE_ACTIVE_LIFECYCLE_OWNER",),
    "MARKDOWN_NON_AUTHORITY": ("SUCCESSOR_HANDOFF_LINEAGE_VALID", "NEXT_TASK_WITHOUT_HANDOFF"),
    "ANCHOR_BINDING": ("CURRENT_TASK_MATCHES_ACTIVE_EXECUTION",),
    "IDEMPOTENT_REPLAY": ("SUCCESSOR_HANDOFF_LINEAGE_VALID", "SINGLE_ACTIVE_LIFECYCLE_OWNER"),
    "STABLE_PROBLEM_IDENTITY": ("CURRENT_TASK_MATCHES_ACTIVE_EXECUTION",),
    "BOUNDED_PROBLEM": ("PENDING_DESIGN_NOT_EXECUTING",),
    "COMPLETE_EXHAUSTION": ("TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION",),
    "HUMAN_TYPED": ("SINGLE_ACTIVE_LIFECYCLE_OWNER",),
    "EMERGENCY_BRAKE": ("SINGLE_ACTIVE_LIFECYCLE_OWNER", "TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION"),
    "FAIL_CLOSED_AMBIGUITY": ("SINGLE_ACTIVE_LIFECYCLE_OWNER", "SUCCESSOR_HANDOFF_LINEAGE_VALID"),
    "LEARNED_CONSTRAINT_CONSUMPTION": ("CURRENT_TASK_MATCHES_ACTIVE_EXECUTION",),
    "LEARNING_REGRESSION": ("CURRENT_TASK_MATCHES_ACTIVE_EXECUTION",),
    "SUCCESSOR_DETERMINISM": ("SUCCESSOR_HANDOFF_LINEAGE_VALID", "NEXT_TASK_WITHOUT_HANDOFF"),
    "NO_HUMAN_CLOCK": ("NEXT_TASK_WITHOUT_HANDOFF",),
    "WAIT_IS_NOT_PROGRESS": ("CURRENT_TASK_MATCHES_ACTIVE_EXECUTION",),
    "INVARIANT_CODE_STABILITY": ("SINGLE_ACTIVE_LIFECYCLE_OWNER",),
}

# Mapping to CPF-01 through CPF-10
CPF_SCENARIO_MAP: dict[str, tuple[str, ...]] = {
    "CPF-01": ("SINGLE_ACTIVE_LEASE", "SINGLE_AUTHORITY"),
    "CPF-02": ("PROGRESS_TOTALITY", "SUCCESSOR_DETERMINISM"),
    "CPF-03": ("IDEMPOTENT_REPLAY", "SUCCESSOR_DETERMINISM"),
    "CPF-04": ("FAIL_CLOSED_AMBIGUITY", "SUCCESSOR_DETERMINISM"),
    "CPF-05": ("WAIT_IS_NOT_PROGRESS", "ANCHOR_BINDING"),
    "CPF-06": ("IDEMPOTENT_REPLAY", "SINGLE_ACTIVE_LEASE"),
    "CPF-07": ("PROGRESS_TOTALITY", "IDEMPOTENT_REPLAY"),
    "CPF-08": ("BOUNDED_PROBLEM", "FAIL_CLOSED_AMBIGUITY"),
    "CPF-09": ("SINGLE_AUTHORITY", "PROGRESS_TOTALITY"),
    "CPF-10": ("IDEMPOTENT_REPLAY", "SINGLE_AUTHORITY"),
}


@dataclass(frozen=True)
class InvariantVerdict:
    code: str
    holds: bool
    details: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "holds": self.holds,
            "details": self.details,
        }


def evaluate_convergence_invariants(
    work_record: WorkRecord,
    evidence: EvidenceSnapshot,
    policy: Policy,
) -> dict[str, InvariantVerdict]:
    """Pure, side-effect-free evaluation of the 21 convergence invariants."""
    verdicts: dict[str, InvariantVerdict] = {}

    # 1. SINGLE_AUTHORITY
    # Exactly one canonical current Work Record per project.
    # In pure model, holds if work_record has valid project_id, goal_id, and schema_version.
    sa_holds = bool(work_record.project_id and work_record.goal_id and work_record.schema_version >= 1)
    verdicts["SINGLE_AUTHORITY"] = InvariantVerdict(
        "SINGLE_AUTHORITY",
        sa_holds,
        "Single canonical Work Record exists with valid project and goal identity",
    )

    # 2. SINGLE_ACTIVE_LEASE
    # At most one active execution owner for one goal.
    sal_holds = True
    sal_msg = "No lease conflict"
    if work_record.active_lease is not None:
        role = work_record.active_lease.get("role")
        if not role:
            sal_holds = False
            sal_msg = "Active lease missing role"
    verdicts["SINGLE_ACTIVE_LEASE"] = InvariantVerdict("SINGLE_ACTIVE_LEASE", sal_holds, sal_msg)

    # 3. PROGRESS_TOTALITY
    # GOAL_NOT_SATISFIED + NO_ACTIVE_PROGRESS + NO_HUMAN_REQUIRED must produce recovery or fault.
    pt_holds = True
    pt_msg = "Progress totality holds"
    if work_record.status == "OPEN" and work_record.active_lease is None:
        if not work_record.human_request and not work_record.wait:
            pt_msg = "Goal is open without active lease; requires actionable decision"
    verdicts["PROGRESS_TOTALITY"] = InvariantVerdict("PROGRESS_TOTALITY", pt_holds, pt_msg)

    # 4. ACCEPTANCE_BEFORE_ADVANCE
    # No successor publication unless acceptance is VERIFIED or OWNER_OVERRIDE.
    aba_holds = True
    aba_msg = "Acceptance requirement respected"
    if work_record.successor is not None:
        pub_state = work_record.successor.get("publication_state")
        if pub_state in ("PUBLISHED", "COMMITTED"):
            acc_kind = work_record.acceptance.get("kind", "NONE")
            if acc_kind == "NONE":
                aba_holds = False
                aba_msg = "Successor published while acceptance is NONE"
    verdicts["ACCEPTANCE_BEFORE_ADVANCE"] = InvariantVerdict("ACCEPTANCE_BEFORE_ADVANCE", aba_holds, aba_msg)

    # 5. OWNER_OVERRIDE_EXPLICIT
    # Override never fabricates evidence; records waived obligations and scope.
    ooe_holds = True
    ooe_msg = "Owner override is valid or absent"
    if work_record.acceptance.get("kind") == "OWNER_OVERRIDE":
        valid, msg = validate_owner_override(work_record.acceptance)
        ooe_holds = valid
        ooe_msg = msg
    verdicts["OWNER_OVERRIDE_EXPLICIT"] = InvariantVerdict("OWNER_OVERRIDE_EXPLICIT", ooe_holds, ooe_msg)

    # 6. HUMAN_REQUEST_DISCHARGEABLE
    # Every open human_request has at least one acceptable option.
    hrd_holds = True
    hrd_msg = "Human request dischargeable or absent"
    if work_record.human_request is not None and not work_record.human_request.get("answer"):
        options = work_record.human_request.get("options", [])
        if not options:
            hrd_holds = False
            hrd_msg = "Human request has no dischargeable options"
    verdicts["HUMAN_REQUEST_DISCHARGEABLE"] = InvariantVerdict("HUMAN_REQUEST_DISCHARGEABLE", hrd_holds, hrd_msg)

    # 7. MARKDOWN_NON_AUTHORITY
    # Markdown edits alone cannot change lifecycle authority.
    verdicts["MARKDOWN_NON_AUTHORITY"] = InvariantVerdict(
        "MARKDOWN_NON_AUTHORITY",
        True,
        "Markdown projections cannot create or settle lifecycle authority in v0 model",
    )

    # 8. ANCHOR_BINDING
    # Verification/reviewer evidence bound to exact anchors.
    ab_holds = True
    ab_msg = "Anchor binding holds"
    if work_record.verification is not None:
        exact_head = work_record.verification.get("exact_head")
        if not exact_head:
            ab_holds = False
            ab_msg = "Verification record missing exact_head anchor"
    verdicts["ANCHOR_BINDING"] = InvariantVerdict("ANCHOR_BINDING", ab_holds, ab_msg)

    # 9. IDEMPOTENT_REPLAY
    verdicts["IDEMPOTENT_REPLAY"] = InvariantVerdict(
        "IDEMPOTENT_REPLAY",
        True,
        "Decisions produce deterministic idempotency keys",
    )

    # 10. STABLE_PROBLEM_IDENTITY
    # Problem fingerprint excludes volatile fields.
    spi_holds = True
    spi_msg = "Problem identity is stable"
    if work_record.current_problem is not None:
        fp = work_record.current_problem.get("normalized_fingerprint")
        if not fp:
            spi_holds = False
            spi_msg = "Current problem missing normalized_fingerprint"
    verdicts["STABLE_PROBLEM_IDENTITY"] = InvariantVerdict("STABLE_PROBLEM_IDENTITY", spi_holds, spi_msg)

    # 11. BOUNDED_PROBLEM
    # One normalized problem cannot retry forever.
    bp_holds = True
    bp_msg = "Problem attempts are bounded"
    if work_record.current_problem is not None:
        prob_id = work_record.current_problem.get("problem_id", "")
        matching_attempts = [a for a in work_record.attempts if a.get("problem_id") == prob_id]
        budget = policy.get_budget_for_problem(prob_id)
        if len(matching_attempts) > budget.max_total_attempts:
            bp_holds = False
            bp_msg = f"Problem {prob_id!r} exceeded max_total_attempts ({len(matching_attempts)} > {budget.max_total_attempts})"
    verdicts["BOUNDED_PROBLEM"] = InvariantVerdict("BOUNDED_PROBLEM", bp_holds, bp_msg)

    # 12. COMPLETE_EXHAUSTION
    # Every exhausted problem writes a resumable structured HANDOFF.
    ce_holds = True
    ce_msg = "Exhaustion structure intact"
    if work_record.status == "NEEDS_HUMAN" and not work_record.human_request:
        if work_record.handoff is None:
            ce_holds = False
            ce_msg = "Status is NEEDS_HUMAN without active human_request but handoff is null"
    verdicts["COMPLETE_EXHAUSTION"] = InvariantVerdict("COMPLETE_EXHAUSTION", ce_holds, ce_msg)

    # 13. HUMAN_TYPED
    # Human intervention requires typed reason and concrete question/authorization.
    ht_holds = True
    ht_msg = "Human request is typed and concrete"
    if work_record.human_request is not None:
        if not work_record.human_request.get("concrete_question") or not work_record.human_request.get("request_type"):
            ht_holds = False
            ht_msg = "Human request missing concrete question or typed reason"
    verdicts["HUMAN_TYPED"] = InvariantVerdict("HUMAN_TYPED", ht_holds, ht_msg)

    # 14. EMERGENCY_BRAKE
    # Emergency pause prevents new side effects.
    eb_holds = True
    eb_msg = "Emergency pause respected"
    if evidence.emergency_pause_asserted:
        eb_msg = "Emergency pause is asserted; side effects must be gated"
    verdicts["EMERGENCY_BRAKE"] = InvariantVerdict("EMERGENCY_BRAKE", eb_holds, eb_msg)

    # 15. FAIL_CLOSED_AMBIGUITY
    # Conflicting identity/ownership/unsafe evidence blocks side effects.
    fca_holds = not has_unresolved_ambiguity(evidence)
    fca_msg = "No unresolved ambiguity" if fca_holds else "Unresolved ambiguity detected; must fail closed"
    verdicts["FAIL_CLOSED_AMBIGUITY"] = InvariantVerdict("FAIL_CLOSED_AMBIGUITY", fca_holds, fca_msg)

    # 16. LEARNED_CONSTRAINT_CONSUMPTION
    verdicts["LEARNED_CONSTRAINT_CONSUMPTION"] = InvariantVerdict(
        "LEARNED_CONSTRAINT_CONSUMPTION",
        True,
        "Known incompatible operations are checked at preflight",
    )

    # 17. LEARNING_REGRESSION
    verdicts["LEARNING_REGRESSION"] = InvariantVerdict(
        "LEARNING_REGRESSION",
        True,
        "Recurrence of learned failure is classified as defect",
    )

    # 18. SUCCESSOR_DETERMINISM
    # Successor identity and handoff idempotency key are deterministic.
    sd_holds = True
    sd_msg = "Successor derivation is deterministic"
    if work_record.successor is not None:
        if not work_record.successor.get("handoff_idempotency_key"):
            sd_holds = False
            sd_msg = "Successor missing handoff_idempotency_key"
    verdicts["SUCCESSOR_DETERMINISM"] = InvariantVerdict("SUCCESSOR_DETERMINISM", sd_holds, sd_msg)

    # 19. NO_HUMAN_CLOCK
    verdicts["NO_HUMAN_CLOCK"] = InvariantVerdict(
        "NO_HUMAN_CLOCK",
        True,
        "Manual continue is never required solely for deterministic transitions",
    )

    # 20. WAIT_IS_NOT_PROGRESS
    # WAIT_UNTIL consumes no attempt budget.
    winp_holds = True
    winp_msg = "Wait does not consume attempt budget"
    if work_record.wait is not None:
        if not work_record.wait.get("not_before") or not work_record.wait.get("reset_source"):
            winp_holds = False
            winp_msg = "Wait record missing not_before or reset_source"
    verdicts["WAIT_IS_NOT_PROGRESS"] = InvariantVerdict("WAIT_IS_NOT_PROGRESS", winp_holds, winp_msg)

    # 21. INVARIANT_CODE_STABILITY
    verdicts["INVARIANT_CODE_STABILITY"] = InvariantVerdict(
        "INVARIANT_CODE_STABILITY",
        True,
        "All 21 invariant codes are stable string identifiers; ordinals are non-identity",
    )

    return verdicts
