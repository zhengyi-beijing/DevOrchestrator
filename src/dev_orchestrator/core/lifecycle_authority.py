"""Authoritative lifecycle state and centralized invariant evaluation.

The authority and its transition journal are stored inside
``transition-executor.json``.  Repository agent files and the Planner,
Reviewer, execution-context, and Watchdog ledgers are projections or evidence;
none may independently redefine the current task.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from dev_orchestrator.core.task_status import parse_task_status
from dev_orchestrator.monitor.telemetry import extract_task_id
from dev_orchestrator.storage.json_store import parse_utc, utc_now_iso


AUTHORITY_SCHEMA_VERSION = 1
ACTIVE_EXECUTION_STATES = frozenset({"launching", "running"})
ACTIVE_PLAN_STATES = frozenset({"planning", "reviewing", "remediating", "applying"})
ACTIVE_REVIEW_STATES = frozenset({"launching", "running"})
INVARIANT_CODES = (
    "CURRENT_TASK_MATCHES_ACTIVE_EXECUTION",
    "TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION",
    "PENDING_DESIGN_NOT_EXECUTING",
    "SUCCESSOR_HANDOFF_LINEAGE_VALID",
    "NEXT_TASK_WITHOUT_HANDOFF",
    "SINGLE_ACTIVE_LIFECYCLE_OWNER",
)


@dataclass(frozen=True)
class InvariantFinding:
    code: str
    holds: bool
    recoverable: bool
    reason: str
    evidence: dict[str, Any]


def advertised_task_id(snapshot: dict[str, Any]) -> str | None:
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    value = telemetry.get("task_id") or snapshot.get("task_id") or extract_task_id(snapshot.get("next_title"))
    return str(value).strip() if value else None


def transition_id_for(project_id: str, source_task_id: str, target_task_id: str, generation: int) -> str:
    identity = f"{project_id}|{source_task_id}|{target_task_id}|{generation}"
    return "handoff:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def lifecycle_state_from_snapshot(snapshot: dict[str, Any]) -> str:
    status = parse_task_status(snapshot.get("next_status"))
    if status.is_pending_design():
        return "PENDING_DESIGN"
    if status.is_ready_to_run():
        return "READY_TO_RUN"
    if status.is_completed():
        return "COMPLETE"
    return str(snapshot.get("lifecycle_state") or snapshot.get("state") or "UNKNOWN").upper()


def new_authority(project_id: str, task_id: str, state: str) -> dict[str, Any]:
    return {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "project_id": project_id,
        "generation": 0,
        "current_task_id": task_id,
        "lifecycle_state": state,
        "source_task_id": None,
        "active_transition_id": None,
        "active_owner": None,
        "owner_gate": None,
        "updated_at": utc_now_iso(),
    }


def active_owners(
    project_id: str,
    executor_state: dict[str, Any],
    planner_state: dict[str, Any] | None = None,
    reviewer_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    owners: list[dict[str, Any]] = []
    executions = executor_state.get("executions") if isinstance(executor_state, dict) else {}
    lifecycle_barriers = [
        row for row in (executions or {}).values()
        if isinstance(row, dict)
        and row.get("project_id") == project_id
        and row.get("state") in {"handoff", "settled"}
    ]
    for row in (executions or {}).values():
        if not isinstance(row, dict) or row.get("project_id") != project_id:
            continue
        if row.get("state") in ACTIVE_EXECUTION_STATES:
            owners.append({"role": "worker", "id": row.get("source_request_id"), "task_id": row.get("task_id")})
            continue
        if row.get("state") == "completed" and row.get("review_state") == "pending":
            completed_at = parse_utc(row.get("completed_at") or row.get("started_at"))
            resolved_by_barrier = False
            for barrier in lifecycle_barriers:
                barrier_at = parse_utc(
                    barrier.get("handoff_consumed_at")
                    or barrier.get("recorded_at")
                    or barrier.get("completed_at")
                )
                if completed_at is None or barrier_at is None or barrier_at >= completed_at:
                    # Any later project lifecycle barrier supersedes older
                    # pending-review flags, including legacy chains that
                    # skipped or renamed intermediate task identities.
                    resolved_by_barrier = True
                    break
            if resolved_by_barrier:
                continue
            review_id = "ai_review:" + str(row.get("source_request_id") or "")
            reviews = reviewer_state.get("reviews") if isinstance(reviewer_state, dict) else {}
            review = reviews.get(review_id) if isinstance(reviews, dict) else None
            transition = executions.get(review_id) if isinstance(executions, dict) else None
            if not (
                isinstance(review, dict) and review.get("state") == "completed"
                and isinstance(transition, dict)
                and transition.get("state") not in {"blocked", "recovery_required"}
            ):
                owners.append({"role": "review_obligation", "id": review_id, "task_id": row.get("task_id")})
    plans = planner_state.get("plans") if isinstance(planner_state, dict) else {}
    for row in (plans or {}).values():
        if isinstance(row, dict) and row.get("project_id") == project_id and row.get("state") in ACTIVE_PLAN_STATES:
            owners.append({"role": "planner", "id": row.get("plan_id"), "task_id": row.get("task_id")})
    reviews = reviewer_state.get("reviews") if isinstance(reviewer_state, dict) else {}
    for row in (reviews or {}).values():
        if isinstance(row, dict) and row.get("project_id") == project_id and row.get("state") in ACTIVE_REVIEW_STATES:
            owners.append({
                "role": "reviewer", "id": row.get("review_id"),
                "task_id": row.get("task_id"),
                "source_request_id": row.get("source_request_id"),
            })
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for owner in owners:
        unique[(str(owner.get("role")), str(owner.get("id")))] = owner
    return list(unique.values())


def source_ownership_blockers(
    project_id: str,
    source_task_id: str,
    executor_state: dict[str, Any],
    planner_state: dict[str, Any] | None = None,
    reviewer_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        owner for owner in active_owners(project_id, executor_state, planner_state, reviewer_state)
        if str(owner.get("task_id") or "") == source_task_id
    ]


def evaluate_lifecycle_invariants(
    *,
    snapshot: dict[str, Any],
    executor_state: dict[str, Any],
    planner_state: dict[str, Any] | None = None,
    reviewer_state: dict[str, Any] | None = None,
    decisions_state: dict[str, Any] | None = None,
) -> tuple[InvariantFinding, ...]:
    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
    authorities = executor_state.get("lifecycle") if isinstance(executor_state, dict) else {}
    authority = authorities.get(project_id) if isinstance(authorities, dict) else None
    authority_task = str(authority.get("current_task_id") or "") if isinstance(authority, dict) else ""
    authority_state = str(authority.get("lifecycle_state") or "") if isinstance(authority, dict) else ""
    owners = active_owners(project_id, executor_state, planner_state, reviewer_state)
    worker_owners = [owner for owner in owners if owner.get("role") == "worker"]
    owner_tasks = {str(owner.get("task_id") or "") for owner in owners if owner.get("task_id")}
    executions = executor_state.get("executions") if isinstance(executor_state, dict) else {}
    transitions = executor_state.get("transitions") if isinstance(executor_state, dict) else {}
    handoffs = [
        row for row in (executions or {}).values()
        if isinstance(row, dict) and row.get("project_id") == project_id and row.get("state") == "handoff"
    ]
    decisions = decisions_state.get("decisions") if isinstance(decisions_state, dict) else {}
    next_decisions: list[dict[str, Any]] = []
    for decision_id, row in (decisions or {}).items():
        if not isinstance(row, dict) or row.get("project_id") != project_id:
            continue
        if not (
            row.get("disposition") == "apply"
            and row.get("decision") == "next"
            and row.get("next_action") == "next_task"
        ):
            continue
        request_id = str(row.get("request_id") or decision_id or "")
        actuation = (executions or {}).get(request_id)
        satisfied = isinstance(actuation, dict) and actuation.get("state") in {"handoff", "settled"}
        if not satisfied:
            decision_at = parse_utc(row.get("consumed_at") or row.get("created_at"))
            for candidate in (executions or {}).values():
                if not isinstance(candidate, dict):
                    continue
                if (
                    candidate.get("project_id") != project_id
                    or candidate.get("task_id") != row.get("task_id")
                    or candidate.get("state") not in {"handoff", "settled"}
                ):
                    continue
                candidate_at = parse_utc(
                    candidate.get("handoff_consumed_at")
                    or candidate.get("recorded_at")
                    or candidate.get("completed_at")
                )
                if decision_at is None or candidate_at is None or candidate_at >= decision_at:
                    satisfied = True
                    break
        if not satisfied:
            next_decisions.append({**row, "request_id": request_id})

    current_holds = not worker_owners or all(str(owner.get("task_id") or "") == authority_task for owner in worker_owners)
    terminal_holds = not (authority_state in {"COMPLETE", "SETTLED"} and worker_owners)
    pending_holds = not (authority_state == "PENDING_DESIGN" and worker_owners)
    lineage_bad = []
    for row in handoffs:
        source_task_id = row.get("source_task_id")
        # Consumed handoffs from the pre-P16.13 schema used task_id as the
        # predecessor identity. They are immutable accepted history, not live
        # malformed work items.
        if not source_task_id and row.get("handoff_consumed") is True:
            source_task_id = row.get("task_id")
        target_task_id = row.get("target_task_id") or row.get("next_task_id")
        if (
            not source_task_id
            or source_task_id != row.get("task_id")
            or not target_task_id
            or target_task_id == source_task_id
        ):
            lineage_bad.append(row)
    lineage_holds = not lineage_bad
    next_without = bool(next_decisions)
    single_owner_holds = len(owner_tasks) <= 1 and len([o for o in owners if o.get("role") != "review_obligation"]) <= 1

    return (
        InvariantFinding("CURRENT_TASK_MATCHES_ACTIVE_EXECUTION", current_holds, True,
                         "active Worker task must equal authoritative current task",
                         {"authority_task_id": authority_task, "active_workers": worker_owners}),
        InvariantFinding("TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION", terminal_holds, True,
                         "terminal authority cannot retain a running Worker",
                         {"authority_state": authority_state, "active_workers": worker_owners}),
        InvariantFinding("PENDING_DESIGN_NOT_EXECUTING", pending_holds, True,
                         "pending-design authority cannot execute a Worker",
                         {"authority_state": authority_state, "active_workers": worker_owners}),
        InvariantFinding("SUCCESSOR_HANDOFF_LINEAGE_VALID", lineage_holds, False,
                         "handoff lineage requires distinct source and target identities",
                         {"invalid_handoffs": lineage_bad}),
        InvariantFinding("NEXT_TASK_WITHOUT_HANDOFF", not next_without, True,
                         "accepted NEXT_TASK must have a durable handoff",
                         {"next_decisions": [
                              {"request_id": row.get("request_id"), "task_id": row.get("task_id")}
                              for row in next_decisions
                          ], "handoffs": len(handoffs),
                          "transitions": len(transitions or {})}),
        InvariantFinding("SINGLE_ACTIVE_LIFECYCLE_OWNER", single_owner_holds, False,
                         "only one lifecycle role may own a project",
                         {"owners": owners, "owner_tasks": sorted(owner_tasks)}),
    )


def invariant_payload(findings: tuple[InvariantFinding, ...]) -> list[dict[str, Any]]:
    return [
        {
            "code": item.code,
            "holds": item.holds,
            "recoverable": item.recoverable,
            "reason": item.reason,
            "evidence": item.evidence,
        }
        for item in findings
    ]


def epoch_for(authority: dict[str, Any]) -> str:
    evidence = {
        "project_id": authority.get("project_id"),
        "generation": authority.get("generation"),
        "current_task_id": authority.get("current_task_id"),
        "active_transition_id": authority.get("active_transition_id"),
        "active_owner": authority.get("active_owner"),
    }
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
