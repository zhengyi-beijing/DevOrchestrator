"""Deterministic successor derivation and structured resumable HANDOFF generation.

Successor publication is derived strictly from accepted verification, not markdown.
Produces single-writer transaction shapes without writing production state.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from dev_orchestrator.convergence.problems import ProblemTracker
from dev_orchestrator.convergence.verification import VerificationRecord
from dev_orchestrator.convergence.work_record import WorkRecord


def compute_handoff_idempotency_key(
    project_id: str,
    source_goal_id: str,
    successor_goal_id: str,
    acceptance_digest: str,
) -> str:
    """Compute a deterministic idempotency key for successor publication."""
    raw = f"{project_id}:{source_goal_id}:{successor_goal_id}:{acceptance_digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def propose_successor_publication(
    work_record: WorkRecord,
    successor_spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Model the single-writer transaction shape for successor publication.

    Pure helper; does not persist.
    """
    succ_id = str(successor_spec.get("successor_goal_id", "")).strip()
    if not succ_id:
        raise ValueError("successor_spec missing successor_goal_id")

    spec_raw = json.dumps(dict(successor_spec), sort_keys=True, separators=(",", ":"))
    spec_digest = hashlib.sha256(spec_raw.encode("utf-8")).hexdigest()

    acc_digest = hashlib.sha256(
        json.dumps(work_record.acceptance, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    idemp_key = compute_handoff_idempotency_key(
        work_record.project_id,
        work_record.goal_id,
        succ_id,
        acc_digest,
    )

    return {
        "action": "PUBLISH_SUCCESSOR_TRANSACTION",
        "source_terminal_status": "DONE",
        "source_goal_id": work_record.goal_id,
        "successor_goal_id": succ_id,
        "successor_spec_digest": spec_digest,
        "acceptance_digest": acc_digest,
        "handoff_idempotency_key": idemp_key,
        "clear_source_lease": True,
        "successor_initial_status": "OPEN",
    }


def build_resumable_handoff(
    work_record: WorkRecord,
    verification: VerificationRecord | Mapping[str, Any] | None = None,
    problem_tracker: ProblemTracker | Mapping[str, Any] | None = None,
    *,
    ruled_out_approaches: list[str] | None = None,
    learned_constraints: list[str] | None = None,
    recommended_action: str = "Review blocker evidence and provide direction",
) -> dict[str, Any]:
    """Construct a complete, structured, machine-readable resumable HANDOFF."""
    v_dict = verification.to_dict() if isinstance(verification, VerificationRecord) else dict(verification or {})
    p_dict = problem_tracker.to_dict() if isinstance(problem_tracker, ProblemTracker) else dict(problem_tracker or {})

    # Derive anchor
    anchor = work_record.repository_identity.get("branch", "")
    if v_dict.get("exact_head"):
        anchor = f"{anchor}@{v_dict['exact_head']}"

    return {
        "project_id": work_record.project_id,
        "goal_id": work_record.goal_id,
        "goal_spec_digest": work_record.goal_spec_digest,
        "exact_repository_anchor": anchor,
        "stable_problem_id": p_dict.get("problem_id") or (work_record.current_problem.get("problem_id") if work_record.current_problem else "unknown"),
        "failure_class": p_dict.get("failure_class") or (work_record.current_problem.get("failure_class") if work_record.current_problem else "unknown"),
        "attempts": [dict(a) for a in work_record.attempts],
        "verification_evidence": v_dict,
        "unresolved_blockers": [f for f in v_dict.get("structured_findings", []) if f.get("severity") == "BLOCKING"],
        "ruled_out_approaches": list(ruled_out_approaches or []),
        "learned_constraints": list(learned_constraints or []),
        "recommended_human_action": recommended_action,
        "resume_checkpoint": {
            "authority_revision": work_record.authority_revision,
            "goal_id": work_record.goal_id,
            "status": "NEEDS_HUMAN",
        },
    }
