"""Authoritative lifecycle state and centralized invariant evaluation.

The authority and its transition journal are stored inside
``transition-executor.json``.  Repository agent files and the Planner,
Reviewer, execution-context, and Watchdog ledgers are projections or evidence;
none may independently redefine the current task.
"""
from __future__ import annotations

import copy
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


def open_declaration_gate(
    authority: dict[str, Any],
    *,
    gate_id: str,
    reason: str,
    task_id: str,
    head: str,
    declaration_hash: str = "",
    evidence: dict[str, Any] | None = None,
) -> bool:
    """Atomically set owner_gate and lifecycle_state='OWNER_GATE' for declaration refusal.

    Stores the safe prior resumable state in the gate. Leaves lifecycle ownership,
    generation, and transition identity unchanged. Repeating the same refusal
    performs no write or timestamp refresh.
    Returns True if mutated, False if byte-stable identical gate already active.
    """
    existing_gate = authority.get("owner_gate")
    if (
        isinstance(existing_gate, dict)
        and existing_gate.get("code") == "CONTROL_PLANE_DECLARATION_REQUIRED"
        and existing_gate.get("gate_id") == gate_id
        and existing_gate.get("head") == head
        and str(authority.get("lifecycle_state")) == "OWNER_GATE"
    ):
        return False

    current_state = str(authority.get("lifecycle_state") or "READY_TO_RUN")
    if current_state == "OWNER_GATE" and isinstance(existing_gate, dict) and existing_gate.get("resume_state"):
        resume_state = existing_gate["resume_state"]
    else:
        resume_state = current_state if current_state != "OWNER_GATE" else "READY_TO_RUN"

    gate = {
        "code": "CONTROL_PLANE_DECLARATION_REQUIRED",
        "gate_id": gate_id,
        "state": "OWNER_GATE",
        "resume_state": resume_state,
        "reason": reason,
        "task_id": task_id,
        "head": head,
        "declaration_hash": declaration_hash,
        "evidence": evidence or {},
        "recorded_at": (
            existing_gate.get("recorded_at")
            if (isinstance(existing_gate, dict) and existing_gate.get("gate_id") == gate_id and existing_gate.get("recorded_at"))
            else utc_now_iso()
        ),
    }
    authority["owner_gate"] = gate
    authority["lifecycle_state"] = "OWNER_GATE"
    authority["updated_at"] = utc_now_iso()
    return True


def resolve_declaration_gate(
    authority: dict[str, Any],
    *,
    reason: str,
    resolved_at: str | None = None,
    task_id: str | None = None,
) -> bool:
    """Resolve an authoritative CONTROL_PLANE_DECLARATION_REQUIRED gate.

    Atomically appends gate evidence to bounded resolved_owner_gates, clears owner_gate,
    and restores only the validated stored resumable state.
    Lazily upgrades authority schema_version to 2.
    """
    current_gate = authority.get("owner_gate")
    if not (isinstance(current_gate, dict) and current_gate.get("code") == "CONTROL_PLANE_DECLARATION_REQUIRED"):
        return False
    if task_id is not None:
        gate_task = current_gate.get("task_id")
        if gate_task and gate_task != task_id:
            return False
        auth_task = authority.get("current_task_id")
        if auth_task and auth_task != task_id:
            return False

    resolved_entry = copy.deepcopy(current_gate)
    resolved_entry["resolved_at"] = resolved_at or utc_now_iso()
    resolved_entry["resolution_reason"] = reason

    history = authority.setdefault("resolved_owner_gates", [])
    if not any(g.get("gate_id") == resolved_entry.get("gate_id") for g in history):
        history.append(resolved_entry)
        if len(history) > 50:
            history.pop(0)

    resume_state = current_gate.get("resume_state") or "READY_TO_RUN"
    authority["owner_gate"] = None
    authority["lifecycle_state"] = resume_state
    authority["schema_version"] = 2
    authority["updated_at"] = utc_now_iso()
    return True


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
                # Only accepted history may supersede a pending-review flag:
                # a barrier for the same task, or a consumed handoff (immutable
                # accepted history, including legacy chains that skipped or
                # renamed intermediate task identities).  An unconsumed barrier
                # for an unrelated task is live work, not history, and must
                # never drain another task's obligation -- P16.13's own recovery
                # path mints exactly such freshly stamped cross-task barriers.
                if not (
                    str(barrier.get("task_id") or "") == str(row.get("task_id") or "")
                    or barrier.get("handoff_consumed") is True
                ):
                    continue
                barrier_at = parse_utc(
                    barrier.get("handoff_consumed_at")
                    or barrier.get("recorded_at")
                    or barrier.get("completed_at")
                )
                # Unknown ordering cannot prove supersession: fail closed and
                # keep the obligation rather than draining it.
                if completed_at is None or barrier_at is None:
                    continue
                if barrier_at >= completed_at:
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
                owners.append({
                    "role": "review_obligation", "id": review_id,
                    "task_id": row.get("task_id"),
                    # A durably blocked/recovery_required actuation cannot drain
                    # on its own, so the authority must gate rather than pin
                    # silently on it forever.
                    "actuation_state": (
                        transition.get("state") if isinstance(transition, dict) else None
                    ),
                })
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


_BLOCKED_TRANSITION_STATES = frozenset({"failed", "owner_gate", "waiting_recovery"})
_NO_EXECUTABLE_SUCCESSOR_KINDS = frozenset({"absent", "end_of_roadmap", "unlisted"})
_INVALID_SUCCESSOR_KINDS = frozenset({"ambiguous", "error", "invalid"})


def _task_complete_settlements(
    executions: dict[str, Any], project_id: str, task_id: str,
) -> list[dict[str, Any]]:
    """Return durable terminal settlements for one reviewed task."""
    return [
        row for row in executions.values()
        if isinstance(row, dict)
        and row.get("project_id") == project_id
        and row.get("task_id") == task_id
        and row.get("state") == "settled"
        and row.get("outcome") == "task_complete"
    ]


def _successor_resolution_evidence(
    snapshot: dict[str, Any], task_id: str,
) -> dict[str, Any]:
    """Resolve current successor truth without converting errors into absence."""
    repo_path = snapshot.get("repo_path") or snapshot.get("root")
    if not repo_path:
        return {"kind": "error", "reason": "repository path unavailable"}
    from dev_orchestrator.core.successor_consistency import resolve_successor

    try:
        resolution = resolve_successor(repo_path, task_id)
    except Exception as exc:  # unreadable evidence must fail closed
        return {"kind": "error", "reason": str(exc)}
    return {
        "kind": resolution.kind,
        "reason": resolution.reason,
        "successor_task_id": resolution.successor_task_id,
        "evidence": resolution.evidence,
    }


def _roadmap_successor_obligation(
    snapshot: dict[str, Any],
    authority: Any,
    authority_task: str,
    authority_state: str,
    owners: list[dict[str, Any]],
    executions: dict[str, Any],
    transitions: dict[str, Any],
    project_id: str,
) -> dict[str, Any] | None:
    """Return the missing roadmap handoff owed by a terminal authority, if any.

    Only a quiescent terminal authority qualifies.  Repository markdown is a
    projection and must not be required to confirm the terminal transition.
    An existing handoff or in-flight transition for the source already owns
    the successor.  A refused transition, or successor evidence that is zero,
    ambiguous or invalid, is reported non-recoverable so it fails closed
    instead of being actuated blindly.
    """
    if not isinstance(authority, dict) or not authority_task:
        return None
    if authority_state not in {"COMPLETE", "SETTLED"} or authority.get("owner_gate") or owners:
        return None
    repo_path = snapshot.get("repo_path") or snapshot.get("root")
    if not repo_path:
        return None
    for row in executions.values():
        if (
            isinstance(row, dict)
            and row.get("project_id") == project_id
            and row.get("state") == "handoff"
            and (row.get("source_task_id") or row.get("task_id")) == authority_task
        ):
            return None
    blocked_transitions = []
    for row in transitions.values():
        if (
            not isinstance(row, dict)
            or row.get("project_id") != project_id
            or row.get("source_task_id") != authority_task
        ):
            continue
        if row.get("state") not in _BLOCKED_TRANSITION_STATES:
            return None
        blocked_transitions.append(row.get("transition_id"))
    from dev_orchestrator.core.successor_consistency import resolve_successor

    try:
        resolution = resolve_successor(repo_path, authority_task)
        kind, reason = resolution.kind, resolution.reason
    except Exception as exc:  # unreadable evidence gates, never recovers
        resolution, kind, reason = None, "error", str(exc)
    staged_evidence = (
        resolution.evidence in {"staged_claim", "roadmap+staged_claim"}
        if resolution is not None else False
    )
    if kind == "successor" and not staged_evidence:
        kind = "invalid"
        reason = (
            "terminal closure requires exactly one valid staged successor "
            f"declaring predecessor {authority_task}"
        )
    return {
        "source_task_id": authority_task,
        "target_task_id": resolution.successor_task_id if resolution else None,
        "kind": kind,
        "reason": reason,
        "blocked_transitions": blocked_transitions,
        # A unique staged claim may safely repair a null/unlisted roadmap in
        # the executor before the handoff is recorded.  A roadmap-only edge is
        # insufficient for terminal closure because it lacks the required
        # matching predecessor declaration.
        "recoverable": (
            kind in {"successor", "inconsistent"}
            and staged_evidence
            and not blocked_transitions
        ),
    }


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
    raw_authority_state = str(authority.get("lifecycle_state") or "") if isinstance(authority, dict) else ""
    authority_state = raw_authority_state
    if isinstance(authority, dict) and authority.get("owner_gate"):
        authority_state = "OWNER_GATE"
    owners = active_owners(project_id, executor_state, planner_state, reviewer_state)
    worker_owners = [owner for owner in owners if owner.get("role") == "worker"]
    owner_tasks = {str(owner.get("task_id") or "") for owner in owners if owner.get("task_id")}
    executions = executor_state.get("executions") if isinstance(executor_state, dict) else {}
    transitions = executor_state.get("transitions") if isinstance(executor_state, dict) else {}
    handoffs = [
        row for row in (executions or {}).values()
        if isinstance(row, dict) and row.get("project_id") == project_id and row.get("state") == "handoff"
    ]
    # NEXT_TASK_WITHOUT_HANDOFF is derived exclusively from the decisions
    # ledger.  Without it the invariant is not satisfied, it is unevaluable, so
    # record that explicitly instead of letting a call site publish a
    # satisfied verdict it was never given the evidence to reach.
    decisions_available = isinstance(decisions_state, dict)
    decisions = decisions_state.get("decisions") if decisions_available else {}
    next_decisions: list[dict[str, Any]] = []
    terminal_closures: list[dict[str, Any]] = []
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
        # A durably blocked/recovery_required actuation is not a lost handoff
        # that recovery can rebuild: the handoff was attempted and refused, and
        # the row is never revisited.  Retrying it every tick cannot converge,
        # so it is owner-gate evidence rather than recovery input.
        actuation_blocked = (
            isinstance(actuation, dict)
            and actuation.get("state") in {"blocked", "recovery_required"}
        )
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
        successor_resolution = None
        if not satisfied:
            terminal_settlements = _task_complete_settlements(
                executions or {}, project_id, str(row.get("task_id") or ""),
            )
            if terminal_settlements:
                # A technical review may be accepted after the task was already
                # terminal-settled (for example a rereview of the accepted HEAD).
                # That historical NEXT_TASK row is not a perpetual demand to invent
                # a successor.  The settlement satisfies it only while fresh,
                # authoritative repository evidence still has no executable
                # successor.  If a successor is staged later, or the evidence is
                # invalid/ambiguous, the obligation remains live and the ordinary
                # handoff/fail-closed paths continue to apply.
                successor_resolution = _successor_resolution_evidence(
                    snapshot, str(row.get("task_id") or ""),
                )
                successor_resolution["terminal_settlement_request_ids"] = sorted(
                    str(item.get("source_request_id") or "")
                    for item in terminal_settlements
                )
                if successor_resolution.get("kind") in _NO_EXECUTABLE_SUCCESSOR_KINDS:
                    terminal_closures.append({
                        "request_id": request_id,
                        "task_id": row.get("task_id"),
                        "successor_resolution": successor_resolution,
                    })
                    satisfied = True
        if not satisfied:
            next_decisions.append({
                **row, "request_id": request_id,
                "actuation_blocked": actuation_blocked,
                **({"successor_resolution": successor_resolution}
                   if successor_resolution is not None else {}),
            })

    current_holds = not worker_owners or all(str(owner.get("task_id") or "") == authority_task for owner in worker_owners)
    terminal_holds = not (
        (raw_authority_state in {"COMPLETE", "SETTLED"} or authority_state in {"COMPLETE", "SETTLED"})
        and worker_owners
    )
    pending_holds = not (
        (raw_authority_state == "PENDING_DESIGN" or authority_state == "PENDING_DESIGN")
        and worker_owners
    )
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
    # A terminal authority whose roadmap names a successor owes a durable
    # handoff even when no NEXT review decision survives (for example the
    # roadmap edge was restored after the task was settled task_complete).
    roadmap_successor = None
    authority_has_terminal_closure = any(
        str(row.get("task_id") or "") == authority_task
        for row in terminal_closures
    )
    if decisions_available and not next_decisions and not authority_has_terminal_closure:
        roadmap_successor = _roadmap_successor_obligation(
            snapshot, authority, authority_task, authority_state,
            owners, executions or {}, transitions or {}, project_id,
        )
    next_without = bool(next_decisions) or roadmap_successor is not None
    # Recovery can only rebuild a genuinely missing handoff.  When every
    # unsatisfied decision is durably blocked, autonomous recovery cannot
    # converge, so the finding is not recoverable and must fail closed to an
    # owner gate instead of being retried on every tick.
    # Absent evidence is never treated as a recoverable condition either: it
    # gates for owner disposition instead of driving blind recovery attempts.
    if not decisions_available:
        next_recoverable = False
    elif next_decisions:
        invalid_successor_evidence = any(
            (row.get("successor_resolution") or {}).get("kind")
            in _INVALID_SUCCESSOR_KINDS
            for row in next_decisions
        )
        next_recoverable = (
            not invalid_successor_evidence
            and any(not row.get("actuation_blocked") for row in next_decisions)
        )
    elif roadmap_successor is not None:
        next_recoverable = bool(roadmap_successor.get("recoverable"))
    else:
        next_recoverable = True
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
        InvariantFinding("NEXT_TASK_WITHOUT_HANDOFF",
                         not next_without if decisions_available else False,
                         next_recoverable,
                         "accepted NEXT_TASK must have a durable handoff"
                         if decisions_available else
                         "decisions ledger unavailable: NEXT_TASK_WITHOUT_HANDOFF is unevaluable",
                         {"next_decisions": [
                              {"request_id": row.get("request_id"), "task_id": row.get("task_id"),
                               "actuation_blocked": bool(row.get("actuation_blocked")),
                               **({"successor_resolution": row.get("successor_resolution")}
                                  if row.get("successor_resolution") is not None else {})}
                              for row in next_decisions
                          ], "handoffs": len(handoffs),
                          "transitions": len(transitions or {}),
                          "evidence_unavailable": not decisions_available,
                          **({"terminal_closures": terminal_closures}
                             if terminal_closures else {}),
                          **({"roadmap_successor": roadmap_successor}
                             if roadmap_successor is not None else {})}),
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
