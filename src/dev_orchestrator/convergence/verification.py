"""Typed verification evidence, acceptance models, and GoalSatisfied verdict.

Enforces that markdown COMPLETE or prose counts cannot satisfy a goal;
structured verification and strict non-waivable safety obligations are required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from dev_orchestrator.convergence.findings import Finding, FindingSeverity
from dev_orchestrator.convergence.work_record import WorkRecord


class AcceptanceKind(str, Enum):
    NONE = "NONE"
    VERIFIED = "VERIFIED"
    OWNER_OVERRIDE = "OWNER_OVERRIDE"


NON_WAIVABLE_OBLIGATIONS = frozenset({
    "no_unresolved_blocking_finding",
    "no_active_execution_lease",
    "no_unresolved_integrity_ambiguity",
    "emergency_brake_enforcement",
    "explicit_safety_or_irreversible_action_authorization",
})


@dataclass(frozen=True)
class VerificationRecord:
    verification_id: str
    project_id: str
    goal_id: str
    branch: str
    exact_head: str
    clean_status_fingerprint: str
    acceptance_criterion_results: dict[str, bool]
    executed_checks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    reviewer_identity: str | None = None
    reviewer_decision: str | None = None
    structured_findings: tuple[Finding, ...] = field(default_factory=tuple)
    verified_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "branch": self.branch,
            "exact_head": self.exact_head,
            "clean_status_fingerprint": self.clean_status_fingerprint,
            "acceptance_criterion_results": dict(self.acceptance_criterion_results),
            "executed_checks": [dict(c) for c in self.executed_checks],
            "reviewer_identity": self.reviewer_identity,
            "reviewer_decision": self.reviewer_decision,
            "structured_findings": [f.to_dict() for f in self.structured_findings],
            "verified_at": self.verified_at,
        }

    @property
    def has_blocking_finding(self) -> bool:
        return any(f.severity == FindingSeverity.BLOCKING for f in self.structured_findings)


def validate_owner_override(override_dict: Mapping[str, Any]) -> tuple[bool, str]:
    """Validate an OWNER_OVERRIDE record.

    Ensures owner, scope, reason are recorded, and no non-waivable obligation
    is waived.
    """
    for req in ("owner_id", "command_id", "scope", "reason", "waived_obligations"):
        if req not in override_dict:
            return False, f"OWNER_OVERRIDE missing required field {req!r}"

    waived = override_dict.get("waived_obligations", [])
    if not isinstance(waived, (list, tuple)):
        return False, "waived_obligations must be a list or tuple"

    # Check non-waivable obligations
    for w in waived:
        w_norm = str(w).strip().lower().replace(" ", "_").replace("-", "_")
        for nw in NON_WAIVABLE_OBLIGATIONS:
            if nw in w_norm:
                return False, f"OWNER_OVERRIDE cannot waive non-waivable safety obligation: {w!r}"

    return True, "OWNER_OVERRIDE is valid"


def is_goal_satisfied(
    work_record: WorkRecord,
    verification: VerificationRecord | None = None,
    acceptance_override: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Determine whether GoalSatisfied invariant holds.

    Returns (is_satisfied, reason).
    """
    acceptance = acceptance_override or work_record.acceptance
    acc_kind = acceptance.get("kind", "NONE")

    if acc_kind == "NONE":
        return False, "Acceptance is NONE; goal cannot advance without verification or owner override"

    # Invariant: No active lease may exist when satisfying goal
    if work_record.active_lease is not None:
        return False, "Active execution lease still exists; cannot satisfy goal"

    # Use verification from argument or work_record
    vr = verification
    if vr is None and work_record.verification:
        vr = VerificationRecord(
            verification_id=work_record.verification.get("verification_id", ""),
            project_id=work_record.project_id,
            goal_id=work_record.goal_id,
            branch=work_record.repository_identity.get("branch", ""),
            exact_head=work_record.verification.get("exact_head", ""),
            clean_status_fingerprint=work_record.verification.get("clean_status_fingerprint", ""),
            acceptance_criterion_results=work_record.verification.get("acceptance_criterion_results", {}),
            executed_checks=tuple(work_record.verification.get("executed_checks", ())),
            reviewer_identity=work_record.verification.get("reviewer_identity"),
            reviewer_decision=work_record.verification.get("reviewer_decision"),
            structured_findings=tuple(work_record.verification.get("structured_findings", ())),
            verified_at=work_record.verification.get("verified_at", ""),
        )

    if acc_kind == "OWNER_OVERRIDE":
        valid, msg = validate_owner_override(acceptance)
        if not valid:
            return False, f"Invalid OWNER_OVERRIDE: {msg}"
        # Even with owner override, if there is a verification with an unresolved BLOCKING finding,
        # non-waivable safety rule blocks satisfaction
        if vr is not None and vr.has_blocking_finding:
            return False, "Unresolved BLOCKING finding present; cannot satisfy goal even with OWNER_OVERRIDE"
        return True, "Goal satisfied via valid OWNER_OVERRIDE"

    if acc_kind == "VERIFIED":
        if vr is None:
            return False, "VERIFIED acceptance requires structured VerificationRecord"

        # Check blocking findings
        if vr.has_blocking_finding:
            return False, "Verification has unresolved BLOCKING findings"

        # Check reviewer decision
        if vr.reviewer_decision not in ("ACCEPT", "NEXT"):
            return False, f"Reviewer decision is {vr.reviewer_decision!r}, expected ACCEPT"

        # Check all criteria passed
        if not vr.acceptance_criterion_results:
            return False, "No acceptance criterion results recorded"
        for crit, passed in vr.acceptance_criterion_results.items():
            if not passed:
                return False, f"Acceptance criterion {crit!r} failed"

        # Check executed checks
        if not vr.executed_checks:
            return False, "No test or build checks executed in verification record"
        for check in vr.executed_checks:
            if check.get("exit_status") != 0:
                return False, f"Executed check {check.get('command')!r} failed with exit status {check.get('exit_status')}"

        return True, "Goal satisfied via VERIFIED acceptance evidence"

    return False, f"Unknown acceptance kind: {acc_kind!r}"
