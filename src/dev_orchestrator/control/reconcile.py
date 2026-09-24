"""Read-only, exact stale-technical-review reconciliation eligibility."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from dev_orchestrator.core.repository import is_git_ancestor, read_repository_truth
from dev_orchestrator.core.staged_roadmap import read_successor
from dev_orchestrator.core.task_status import parse_task_status
from dev_orchestrator.core.transition_executor import is_pre_provider_worktree_unsafe_failure
from dev_orchestrator.storage.json_store import read_json


_ACTIVE_EXECUTIONS = frozenset({"launching", "running"})
_ACTIVE_REVIEWS = frozenset({"launching", "running", "recovery_required"})
_ACTIVE_PLANS = frozenset({"planning", "reviewing", "remediating", "applying"})
_SOURCE_KINDS = frozenset({"owner_start", "control", "decision", "remediation"})
_RESOURCE_FIELDS = ("resource_id", "provider", "account", "model")
_BROKER_FIELDS = ("dispatch_id", "decision_id", "execution_id")


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _rows(runtime: Path, filename: str, key: str) -> dict[str, dict[str, Any]]:
    raw = read_json(runtime / filename, {})
    values = raw.get(key) if isinstance(raw, dict) else None
    return {
        name: value for name, value in (values or {}).items()
        if isinstance(name, str) and isinstance(value, dict)
    }


def _reviewer_ready(project: dict[str, Any] | None) -> bool:
    if not isinstance(project, dict):
        return False
    execution = project.get("execution")
    roles = project.get("ai_roles")
    reviewer = roles.get("reviewer") if isinstance(roles, dict) else None
    return isinstance(execution, dict) and execution.get("engine") == "aibroker" and isinstance(reviewer, dict) and reviewer.get("enabled") is True


def _active_reason(
    snapshot: dict[str, Any], project_id: str, executions: dict[str, dict[str, Any]],
    reviews: dict[str, dict[str, Any]], plans: dict[str, dict[str, Any]],
) -> str:
    if any(row.get("project_id") == project_id and row.get("state") in _ACTIVE_EXECUTIONS for row in executions.values()):
        return "active execution makes reconcile ambiguous"
    if any(row.get("project_id") == project_id and row.get("state") in _ACTIVE_REVIEWS for row in reviews.values()):
        return "active reviewer makes reconcile ambiguous"
    if any(row.get("project_id") == project_id and row.get("state") in _ACTIVE_PLANS for row in plans.values()):
        return "active planner makes reconcile ambiguous"
    reviewer = snapshot.get("reviewer")
    if isinstance(reviewer, dict) and reviewer.get("state") in _ACTIVE_REVIEWS:
        return "active reviewer makes reconcile ambiguous"
    planner = snapshot.get("planner")
    if isinstance(planner, dict) and planner.get("state") in _ACTIVE_PLANS:
        return "active planner makes reconcile ambiguous"
    worker = snapshot.get("worker")
    if isinstance(worker, dict) and (worker.get("state") in {"starting", "running"} or worker.get("process_alive") is True):
        return "active execution makes reconcile ambiguous"
    lifecycle = str(snapshot.get("lifecycle_state") or snapshot.get("state") or "")
    if lifecycle in {"PLANNING", "REVIEWING_PLAN", "REMEDIATING_PLAN", "APPLYING_PLAN", "REVIEWING", "WORKER_RUNNING"}:
        return "active AI role makes reconcile ambiguous"
    return ""


def _consumed_review_ids(
    project_id: str, executions: dict[str, dict[str, Any]], reviews: dict[str, dict[str, Any]],
) -> set[str]:
    """Return review ids durably consumed by retry or reanchor lineage.

    ``recovery_of`` is immutable TransitionExecutor lineage.  Reanchor has no
    retry adapter, so any durable same-project ``reconcile_of`` occurrence is a
    consumption barrier, regardless of its terminal state.
    """
    consumed: set[str] = set()
    for row in executions.values():
        if not isinstance(row, dict):
            continue
        row_project = _text(row.get("project_id"))
        if row_project not in {None, project_id}:
            continue
        recovery_of = _text(row.get("recovery_of"))
        if recovery_of is not None:
            consumed.add(recovery_of)
    for row in reviews.values():
        if not isinstance(row, dict) or row.get("project_id") != project_id:
            continue
        reconcile_of = _text(row.get("reconcile_of"))
        if reconcile_of is not None:
            consumed.add(reconcile_of)
    return consumed


def resolve_reconcile_candidate(
    snapshot: dict[str, Any], runtime_root: Path | str, project_config: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve exactly one re-reviewable stale REMEDIATE occurrence, or fail closed.

    The resolver is deterministic and read-only.  It intentionally returns no
    partial target: callers can expose or execute reconcile only from its exact
    result and must re-resolve immediately before launch.
    """
    if not isinstance(snapshot, dict):
        return None, "project snapshot is unavailable"
    project_id = _text(snapshot.get("project_id") or snapshot.get("id"))
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    task_id = _text(telemetry.get("task_id"))
    if project_id is None or task_id is None:
        return None, "current project/task identity is incomplete"
    if parse_task_status(snapshot.get("next_status")).is_completed() and (
        str(snapshot.get("state") or "").upper() in {"IDLE", "COMPLETED", "TERMINAL"}
        or str(snapshot.get("lifecycle_state") or "").upper() in {"IDLE", "COMPLETED", "TERMINAL"}
    ):
        return None, "current task is terminal; stale review is audit history only"
    if not _reviewer_ready(project_config):
        return None, "technical reviewer is not configured for reconcile"
    if project_config.get("project_id") != project_id:
        return None, "project configuration identity changed"
    repo_path = _text(project_config.get("repo_path")) or _text(snapshot.get("repo_path"))
    if repo_path is None:
        return None, "repository path is unavailable"
    truth = read_repository_truth(repo_path)
    if not truth.valid:
        return None, "current repository truth is unavailable"
    if truth.dirty:
        return None, "current repository is dirty"
    git = snapshot.get("git") if isinstance(snapshot.get("git"), dict) else {}
    monitor_status_hash = _text(git.get("status_hash"))
    if (
        git.get("branch") != truth.branch
        or git.get("head") != truth.head
        or bool(git.get("dirty"))
        or (monitor_status_hash is not None and monitor_status_hash != truth.status_hash)
    ):
        return None, "current monitor repository identity is stale"

    runtime = Path(runtime_root)
    executions = _rows(runtime, "transition-executor.json", "executions")
    reviews = _rows(runtime, "ai-reviewer.json", "reviews")
    decisions = _rows(runtime, "review-decisions.json", "decisions")
    plans = _rows(runtime, "ai-planner.json", "plans")
    active = _active_reason(snapshot, project_id, executions, reviews, plans)
    if active:
        return None, active
    consumed_review_ids = _consumed_review_ids(project_id, executions, reviews)

    matches: list[dict[str, Any]] = []
    errors: list[str] = []
    for review_id, decision in sorted(decisions.items()):
        if review_id in consumed_review_ids:
            continue
        if not (
            decision.get("project_id") == project_id
            and decision.get("task_id") == task_id
            and decision.get("decision") == "remediate"
            and decision.get("next_action") == "continue_current_stage"
            and decision.get("disposition") == "apply"
            and decision.get("role") == "reviewer"
            and decision.get("event") == "worker_done"
        ):
            continue
        old_head = _text(decision.get("head"))
        if decision.get("branch") != truth.branch or old_head is None or old_head == truth.head:
            continue
        decision_hash = _text(decision.get("review_status_hash"))
        review = reviews.get(review_id)
        if (
            not isinstance(review, dict) or review.get("state") != "completed"
            or decision.get("request_id") != review_id or decision_hash is None
            or review.get("review_status_hash") != decision_hash
        ):
            errors.append("stale remediation decision lacks completed technical-review evidence")
            continue
        remediation = executions.get(review_id)
        # A completed remediation belongs to its normal descendant-review path;
        # it consumes this historical REMEDIATE and is never a reanchor target.
        if isinstance(remediation, dict) and remediation.get("state") == "completed":
            continue
        if not (
            isinstance(remediation, dict)
            and remediation.get("source_request_id") == review_id
            and remediation.get("project_id") == project_id
            and remediation.get("task_id") == task_id
            and remediation.get("branch") == truth.branch
            and remediation.get("head") == old_head
            and is_pre_provider_worktree_unsafe_failure(remediation)
        ):
            errors.append("stale remediation review lacks exact unresolved pre-provider WorktreeUnsafeError evidence")
            continue
        source_id = _text(review.get("source_request_id"))
        source = executions.get(source_id or "")
        if (
            review.get("project_id") != project_id or review.get("task_id") != task_id
            or review.get("branch") != truth.branch or review.get("head") != old_head
            or not isinstance(source, dict)
        ):
            errors.append("stale remediation review lineage is incomplete")
            continue
        resource = source.get("resource_context")
        if not (
            source.get("project_id") == project_id and source.get("task_id") == task_id
            and source.get("branch") == truth.branch
            and source.get("repo_path") == repo_path and source.get("engine") == "aibroker"
            and source.get("state") == "completed" and source.get("source_kind") in _SOURCE_KINDS
            and all(_text(source.get(field)) is not None for field in _BROKER_FIELDS)
            and isinstance(resource, dict) and all(_text(resource.get(field)) is not None for field in _RESOURCE_FIELDS)
        ):
            errors.append("stale remediation review lacks completed AIBroker source resource evidence")
            continue
        source_head = _text(source.get("head"))
        if source_head is None or not is_git_ancestor(repo_path, source_head, old_head):
            errors.append("completed source launch HEAD is not an ancestor of reviewed HEAD")
            continue
        if not is_git_ancestor(repo_path, old_head, truth.head):
            errors.append("stale reviewed HEAD is not an ancestor of current HEAD")
            continue
        matches.append({
            "target_id": review_id,
            "source_request_id": source_id,
            "task_id": task_id,
            "branch": truth.branch,
            "reviewed_head": old_head,
            "current_head": truth.head,
            "prior_reason": str(decision.get("reason") or ""),
            "resource_context": copy.deepcopy(resource),
        })
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, "multiple stale technical-review reconcile candidates"
    return None, errors[0] if errors else "no exact stale technical-review reconcile target"



def _git_distance(repo_path: str, ancestor: str, descendant: str) -> int | None:
    import subprocess
    try:
        out = subprocess.run(["git", "rev-list", "--count", f"{ancestor}..{descendant}"], cwd=repo_path, capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _rereview_task_matches_current_or_pending_successor(
    repo_path: str,
    reviewed_task_id: str,
    current_task_id: str,
    snapshot: dict[str, Any],
) -> bool:
    """Allow re-review of the current task or its exact staged predecessor only.

    Cross-task recovery is intentionally narrow: the monitor must already
    advertise the reviewed task's canonical staged successor, and that
    successor must still be IDLE/PENDING DESIGN.  This lets a bounded
    predecessor remediation be independently re-reviewed after next.md has
    advanced without making older unrelated task reviews eligible.
    """
    if reviewed_task_id == current_task_id:
        return True
    lifecycle = str(snapshot.get("lifecycle_state") or snapshot.get("state") or "")
    if snapshot.get("state") != "IDLE" or lifecycle != "IDLE":
        return False
    if not parse_task_status(snapshot.get("next_status")).is_pending_design():
        return False
    roadmap = read_successor(repo_path, reviewed_task_id)
    return (
        roadmap.kind == "successor"
        and _text(roadmap.successor_task_id) == current_task_id
    )


def resolve_rereview_candidate(
    snapshot: dict[str, Any], runtime_root: Path | str, project_config: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve one stale reviewer lineage that may be re-reviewed at a clean descendant HEAD.

    Three immutable lineages qualify:

    - a failed reviewer attempt that produced no durable decision,
    - an accepted NEXT decision whose reviewed HEAD is now an ancestor of the
      current clean descendant HEAD, and
    - a REMEDIATE decision whose reviewed HEAD is now an ancestor of a clean
      descendant containing the bounded remediation.

    The reviewed task may be the monitor's current task, or the exact staged
    predecessor of a still-IDLE/PENDING-DESIGN current task.  In every case the
    re-review launches one independent reviewer at the current clean descendant
    HEAD; it never launches a Worker and never imports the prior verdict.
    """
    if not isinstance(snapshot, dict):
        return None, "project snapshot is unavailable"
    project_id = _text(snapshot.get("project_id") or snapshot.get("id"))
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    current_task_id = _text(telemetry.get("task_id"))
    if project_id is None or current_task_id is None or not _reviewer_ready(project_config):
        return None, "current project/task/reviewer identity is incomplete"
    # A terminal task has no pending implementation whose technical verdict can
    # affect lifecycle progression.  Historical stale review lineages remain in
    # the immutable audit stores, but must not surface as actionable controls.
    if parse_task_status(snapshot.get("next_status")).is_completed() and (
        str(snapshot.get("state") or "").upper() in {"IDLE", "COMPLETED", "TERMINAL"}
        or str(snapshot.get("lifecycle_state") or "").upper() in {"IDLE", "COMPLETED", "TERMINAL"}
    ):
        return None, "current task is terminal; stale review is audit history only"
    repo_path = _text(project_config.get("repo_path")) or _text(snapshot.get("repo_path"))
    truth = read_repository_truth(repo_path or "")
    if not truth.valid or truth.dirty:
        return None, "current repository is unavailable or dirty"
    git = snapshot.get("git") if isinstance(snapshot.get("git"), dict) else {}
    if git.get("branch") != truth.branch or git.get("head") != truth.head or bool(git.get("dirty")):
        return None, "current monitor repository identity is stale"
    runtime = Path(runtime_root)
    executions = _rows(runtime, "transition-executor.json", "executions")
    reviews = _rows(runtime, "ai-reviewer.json", "reviews")
    decisions = _rows(runtime, "review-decisions.json", "decisions")
    plans = _rows(runtime, "ai-planner.json", "plans")
    active = _active_reason(snapshot, project_id, executions, reviews, plans)
    if active:
        return None, active
    consumed = {target for row in reviews.values() if row.get("project_id") == project_id for target in [_text(row.get("rereview_of"))] if target}
    matches = []
    for review_id, review in sorted(reviews.items()):
        if review_id in consumed:
            continue
        reviewed_task_id = _text(review.get("task_id"))
        old_head = _text(review.get("head"))
        if not (
            review.get("project_id") == project_id
            and reviewed_task_id is not None
            and _rereview_task_matches_current_or_pending_successor(
                repo_path or "", reviewed_task_id, current_task_id, snapshot
            )
            and review.get("branch") == truth.branch
            and old_head and old_head != truth.head and review.get("review_dirty") is False
            and is_git_ancestor(repo_path or "", old_head, truth.head)
        ):
            continue
        decision = decisions.get(review_id)
        if isinstance(decision, dict):
            # A completed durable NEXT or REMEDIATE decision may be re-reviewed
            # only after its anchor becomes a clean ancestor.  The prior verdict
            # is context, never imported as the current verdict.
            decision_hash = _text(decision.get("review_status_hash"))
            if not (
                review.get("state") == "completed"
                and decision.get("request_id") == review_id
                and decision.get("project_id") == project_id
                and decision.get("task_id") == reviewed_task_id
                and decision.get("disposition") == "apply"
                and decision.get("role") == "reviewer"
                and decision.get("event") == "worker_done"
                and decision.get("branch") == truth.branch
                and _text(decision.get("head")) == old_head
                and decision_hash is not None
                and review.get("review_status_hash") == decision_hash
            ):
                continue
            pair = (decision.get("decision"), decision.get("next_action"))
            if pair == ("next", "next_task"):
                kind = "next"
                prior_reason = (
                    _text(review.get("reason"))
                    or _text(decision.get("reason"))
                    or "stale accepted NEXT review"
                )
            elif pair == ("remediate", "continue_current_stage"):
                kind = "remediate"
                prior_reason = (
                    _text(review.get("reason"))
                    or _text(decision.get("reason"))
                    or "stale REMEDIATE review"
                )
            else:
                continue
        else:
            if review.get("state") != "failed":
                continue
            kind = "failed"
            prior_reason = _text(review.get("reason")) or "review infrastructure failure"
        source_id = _text(review.get("source_request_id")); source = executions.get(source_id or "")
        resource = source.get("resource_context") if isinstance(source, dict) else None
        if not (
            isinstance(source, dict)
            and source.get("state") == "completed"
            and source.get("engine") == "aibroker"
            and source.get("project_id") == project_id
            and source.get("task_id") == reviewed_task_id
            and isinstance(resource, dict)
        ):
            continue
        matches.append({
            "target_id": review_id,
            "source_request_id": source_id,
            "task_id": reviewed_task_id,
            "branch": truth.branch,
            "reviewed_head": old_head,
            "current_head": truth.head,
            "prior_reason": prior_reason,
            "resource_context": copy.deepcopy(resource),
            "kind": kind,
        })
    if matches:
        # Reviews and decisions are immutable audit history.  For descendant
        # re-review, select the nearest stale ancestor deterministically; older
        # lineage remains preserved but must not globally block the current
        # task anchor.
        matches.sort(key=lambda row: (
            int(_git_distance(repo_path or "", row["reviewed_head"], truth.head) or 10**9),
            row["target_id"],
        ))
        return matches[0], ""
    return None, "no safe stale-review descendant re-review target"


def resolve_retry_candidate(
    snapshot: dict[str, Any], runtime_root: Path | str, project_config: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve one exact failed technical review at the current clean HEAD.

    Retry is intentionally narrower than reconcile: it only applies when the
    reviewer failed before producing a durable decision, the repository/task
    identity is unchanged, and the completed AIBroker Worker lineage is intact.
    """
    if not isinstance(snapshot, dict):
        return None, "project snapshot is unavailable"
    project_id = _text(snapshot.get("project_id") or snapshot.get("id"))
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    task_id = _text(telemetry.get("task_id"))
    if project_id is None or task_id is None:
        return None, "current project/task identity is incomplete"
    if parse_task_status(snapshot.get("next_status")).is_completed() and (
        str(snapshot.get("state") or "").upper() in {"IDLE", "COMPLETED", "TERMINAL"}
        or str(snapshot.get("lifecycle_state") or "").upper() in {"IDLE", "COMPLETED", "TERMINAL"}
    ):
        return None, "current task is terminal; stale review is audit history only"
    if not _reviewer_ready(project_config):
        return None, "technical reviewer is not configured for retry"
    if project_config.get("project_id") != project_id:
        return None, "project configuration identity changed"
    repo_path = _text(project_config.get("repo_path")) or _text(snapshot.get("repo_path"))
    if repo_path is None:
        return None, "repository path is unavailable"
    truth = read_repository_truth(repo_path)
    if not truth.valid:
        return None, "current repository truth is unavailable"
    if truth.dirty:
        return None, "current repository is dirty"
    git = snapshot.get("git") if isinstance(snapshot.get("git"), dict) else {}
    monitor_status_hash = _text(git.get("status_hash"))
    if (
        git.get("branch") != truth.branch
        or git.get("head") != truth.head
        or bool(git.get("dirty"))
        or (monitor_status_hash is not None and monitor_status_hash != truth.status_hash)
    ):
        return None, "current monitor repository identity is stale"

    runtime = Path(runtime_root)
    executions = _rows(runtime, "transition-executor.json", "executions")
    reviews = _rows(runtime, "ai-reviewer.json", "reviews")
    decisions = _rows(runtime, "review-decisions.json", "decisions")
    plans = _rows(runtime, "ai-planner.json", "plans")
    active = _active_reason(snapshot, project_id, executions, reviews, plans)
    if active:
        return None, active

    consumed = {
        target
        for row in reviews.values()
        if row.get("project_id") == project_id
        for target in [_text(row.get("retry_of"))]
        if target is not None
    }
    decided = {
        _text(row.get("request_id")) or key
        for key, row in decisions.items()
        if row.get("project_id") == project_id
    }
    matches: list[dict[str, Any]] = []
    errors: list[str] = []
    for review_id, review in sorted(reviews.items()):
        if review_id in consumed or review_id in decided:
            continue
        if not (
            review.get("project_id") == project_id
            and review.get("task_id") == task_id
            and review.get("state") == "failed"
            and review.get("branch") == truth.branch
            and _text(review.get("head")) == truth.head
            and review.get("review_dirty") is False
        ):
            continue
        review_status_hash = _text(review.get("review_status_hash"))
        if review_status_hash is None:
            errors.append("failed technical review lacks repository status hash evidence")
            continue
        if review_status_hash != truth.status_hash:
            errors.append("failed technical review repository status hash does not match current truth")
            continue
        source_id = _text(review.get("source_request_id"))
        source = executions.get(source_id or "")
        resource = source.get("resource_context") if isinstance(source, dict) else None
        if not (
            isinstance(source, dict)
            and source.get("project_id") == project_id
            and source.get("task_id") == task_id
            and source.get("repo_path") == repo_path
            and source.get("branch") == truth.branch
            and source.get("engine") == "aibroker"
            and source.get("state") == "completed"
            and source.get("source_kind") in _SOURCE_KINDS
            and all(_text(source.get(field)) is not None for field in _BROKER_FIELDS)
            and isinstance(resource, dict)
            and all(_text(resource.get(field)) is not None for field in _RESOURCE_FIELDS)
        ):
            errors.append("failed technical review lacks completed AIBroker source resource evidence")
            continue
        source_head = _text(source.get("head"))
        if source_head is None or not is_git_ancestor(repo_path, source_head, truth.head):
            errors.append("failed technical review source HEAD is not an ancestor of current HEAD")
            continue
        reason = _text(review.get("reason"))
        if reason is None:
            errors.append("failed technical review lacks terminal failure reason")
            continue
        matches.append({
            "target_id": review_id,
            "source_request_id": source_id,
            "task_id": task_id,
            "branch": truth.branch,
            "current_head": truth.head,
            "prior_reason": reason,
            "resource_context": copy.deepcopy(resource),
            "completed_at": review.get("completed_at"),
        })
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, "multiple exact failed technical-review retry candidates"
    return None, errors[0] if errors else "no safe exact retry target"
