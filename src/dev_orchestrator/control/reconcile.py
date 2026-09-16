"""Read-only, exact stale-technical-review reconciliation eligibility."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from dev_orchestrator.core.repository import is_git_ancestor, read_repository_truth
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
