"""DevOrchestrator Convergence Subsystem (P17).

Single-authority goal convergence baseline architecture.
"""
from __future__ import annotations

from dev_orchestrator.convergence.evaluator import (
    Decision,
    DecisionKind,
    FailureClass,
    decide,
)
from dev_orchestrator.convergence.actuator_guard import (
    ActuatorGuard,
    GuardVerdict,
)
from dev_orchestrator.convergence.evidence import (
    ConflictClaim,
    EvidenceItem,
    EvidenceSnapshot,
    SharedCredentialLease,
    compute_item_digest,
    snapshot_digest,
)
from dev_orchestrator.convergence.invariants import (
    INVARIANT_CODES,
    InvariantVerdict,
    evaluate_convergence_invariants,
)
from dev_orchestrator.convergence.policy import (
    Policy,
    ProblemBudget,
    build_policy,
)
from dev_orchestrator.convergence.preflight import (
    PreflightClassification,
    PreflightRule,
    PreflightVerdict,
    derive_constraint_fingerprint,
    evaluate_preflight,
    load_seeded_preflight_rules,
)
from dev_orchestrator.convergence.replay import (
    ReplayCase,
    ReplayCaseResult,
    ReplayHarness,
    ReplayReport,
    decision_trace_hash,
)
from dev_orchestrator.convergence.shadow import (
    FenceViolation,
    ReadOnlyEvidenceRoot,
    ShadowEvaluator,
    validate_shadow_sink,
)
from dev_orchestrator.convergence.verification import (
    VerificationResult,
    evaluate_verification_record,
    is_goal_satisfied,
    validate_owner_override,
)
from dev_orchestrator.convergence.work_record import (
    ALLOWED_ACCEPTANCE_KINDS,
    ALLOWED_GOAL_STATUSES,
    WorkRecord,
    WorkRecordValidationError,
    validate_work_record,
    work_record_digest,
)
from dev_orchestrator.convergence.roots import (
    acquire_state_root_lock,
    resolve_runtime_root,
)
from dev_orchestrator.convergence.amendments import (
    LEGACY_CONTRACT_AMENDMENTS,
    RETIREMENT_DISPOSITIONS,
    load_legacy_contract_amendments,
    load_retirement_dispositions,
    validate_amendment_test_references,
)

__all__ = [
    "ALLOWED_ACCEPTANCE_KINDS",
    "ALLOWED_GOAL_STATUSES",
    "ActuatorGuard",
    "ConflictClaim",
    "Decision",
    "DecisionKind",
    "EvidenceItem",
    "EvidenceSnapshot",
    "FailureClass",
    "FenceViolation",
    "GuardVerdict",
    "INVARIANT_CODES",
    "InvariantVerdict",
    "LEGACY_CONTRACT_AMENDMENTS",
    "Policy",
    "PreflightClassification",
    "PreflightRule",
    "PreflightVerdict",
    "ProblemBudget",
    "RETIREMENT_DISPOSITIONS",
    "ReadOnlyEvidenceRoot",
    "ReplayCase",
    "ReplayCaseResult",
    "ReplayHarness",
    "ReplayReport",
    "ShadowEvaluator",
    "SharedCredentialLease",
    "VerificationResult",
    "WorkRecord",
    "WorkRecordValidationError",
    "acquire_state_root_lock",
    "build_policy",
    "compute_item_digest",
    "decide",
    "decision_trace_hash",
    "derive_constraint_fingerprint",
    "evaluate_convergence_invariants",
    "evaluate_preflight",
    "evaluate_verification_record",
    "is_goal_satisfied",
    "load_legacy_contract_amendments",
    "load_retirement_dispositions",
    "load_seeded_preflight_rules",
    "resolve_runtime_root",
    "snapshot_digest",
    "validate_amendment_test_references",
    "validate_owner_override",
    "validate_shadow_sink",
    "validate_work_record",
    "work_record_digest",
]
