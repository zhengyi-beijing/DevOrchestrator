"""Pure, non-authoritative convergence model and shadow evaluation package.

This package models a single durable Work Record, pure decision evaluator,
and replay/shadow evaluation harness for DevOrchestrator P17.

CRITICAL ARCHITECTURAL CONSTRAINTS:
1. P17 is a pure, non-authoritative prototype. This package NEVER writes
   production lifecycle state, ledgers, or control files.
2. It is imported by NO production runtime module (daemon, transition-executor,
   ai-planner, ai-reviewer, control-command coordinator, etc.).
3. Only the shadow adapter may use read-only lifecycle-authority parsers and
   repository readers.
4. Non-shadow convergence modules must not import daemon, transition-executor
   write paths, or control-command mutators.
"""
from __future__ import annotations

from dev_orchestrator.convergence.work_record import (
    WorkRecord,
    validate_work_record,
    canonical_json,
    work_record_digest,
    apply_work_record_cas,
    CASConflictError,
)
from dev_orchestrator.convergence.evidence import (
    EvidenceItem,
    ConflictClaim,
    SharedCredentialLease,
    EvidenceSnapshot,
    get_required_source,
    has_unresolved_ambiguity,
    snapshot_digest,
)
from dev_orchestrator.convergence.policy import (
    Policy,
    ProblemBudget,
    build_policy,
)
from dev_orchestrator.convergence.problems import (
    FailureClass,
    normalized_problem_fingerprint,
    select_next_problem,
)
from dev_orchestrator.convergence.findings import (
    Finding,
    FindingSeverity,
    parse_structured_findings,
    OutputInvalidError,
)
from dev_orchestrator.convergence.verification import (
    AcceptanceKind,
    VerificationRecord,
    is_goal_satisfied,
    validate_owner_override,
)
from dev_orchestrator.convergence.invariants import (
    CONVERGENCE_INVARIANT_CODES,
    LIFECYCLE_INVARIANT_MAP,
    CPF_SCENARIO_MAP,
    evaluate_convergence_invariants,
    InvariantVerdict,
)
from dev_orchestrator.convergence.evaluator import (
    DecisionKind,
    Decision,
    decide,
)
from dev_orchestrator.convergence.human_request import (
    HumanRequest,
    answer_human_request,
    find_active_human_request,
)
from dev_orchestrator.convergence.successor import (
    compute_handoff_idempotency_key,
    build_resumable_handoff,
    propose_successor_publication,
)
from dev_orchestrator.convergence.preflight import (
    PreflightVerdict,
    CapabilityConstraint,
    evaluate_preflight,
    classify_recurrence,
    get_seeded_rules,
)
from dev_orchestrator.convergence.effects import (
    EffectState,
    ExecutionEffectPort,
    reconcile_lease,
)
from dev_orchestrator.convergence.actuator_guard import (
    ActuatorGuard,
    GuardVerdict,
)
from dev_orchestrator.convergence.replay import (
    ReplayCase,
    ReplayHarness,
    ReplayReport,
    decision_trace_hash,
)
from dev_orchestrator.convergence.shadow import (
    ReadOnlyEvidenceRoot,
    FenceViolation,
    ShadowEvaluator,
    SHADOW_NAMESPACE,
)

__all__ = [
    "WorkRecord",
    "validate_work_record",
    "canonical_json",
    "work_record_digest",
    "apply_work_record_cas",
    "CASConflictError",
    "EvidenceItem",
    "ConflictClaim",
    "SharedCredentialLease",
    "EvidenceSnapshot",
    "get_required_source",
    "has_unresolved_ambiguity",
    "snapshot_digest",
    "Policy",
    "ProblemBudget",
    "build_policy",
    "FailureClass",
    "normalized_problem_fingerprint",
    "select_next_problem",
    "Finding",
    "FindingSeverity",
    "parse_structured_findings",
    "OutputInvalidError",
    "AcceptanceKind",
    "VerificationRecord",
    "is_goal_satisfied",
    "validate_owner_override",
    "CONVERGENCE_INVARIANT_CODES",
    "LIFECYCLE_INVARIANT_MAP",
    "CPF_SCENARIO_MAP",
    "evaluate_convergence_invariants",
    "InvariantVerdict",
    "DecisionKind",
    "Decision",
    "decide",
    "HumanRequest",
    "answer_human_request",
    "find_active_human_request",
    "compute_handoff_idempotency_key",
    "build_resumable_handoff",
    "propose_successor_publication",
    "PreflightVerdict",
    "CapabilityConstraint",
    "evaluate_preflight",
    "classify_recurrence",
    "get_seeded_rules",
    "EffectState",
    "ExecutionEffectPort",
    "reconcile_lease",
    "ActuatorGuard",
    "GuardVerdict",
    "ReplayCase",
    "ReplayHarness",
    "ReplayReport",
    "decision_trace_hash",
    "ReadOnlyEvidenceRoot",
    "FenceViolation",
    "ShadowEvaluator",
    "SHADOW_NAMESPACE",
]
