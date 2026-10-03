"""Taxonomy mapping helper for P20.2 incident harvesting.

Maps detector findings to canonical FailureIncident failure_classes without
modifying existing semantic payloads or altering signature fingerprint inputs.
"""
from __future__ import annotations

from typing import Any, Mapping

from dev_orchestrator.incidents.contract import FAILURE_CLASSES


def map_to_failure_class(
    detector_name: str,
    semantic: Mapping[str, Any] | None,
    evidence: Mapping[str, Any] | None,
) -> str:
    """Map detector findings and semantic/evidence attributes to one of the 19 contract FAILURE_CLASSES.

    Guarantees the returned failure class is a valid member of FAILURE_CLASSES.
    Default fallback is 'unknown_requires_owner'.
    """
    sem = dict(semantic or {})
    ev = dict(evidence or {})

    # If already a valid member of FAILURE_CLASSES, return directly
    raw_class = sem.get("failure_class")
    if isinstance(raw_class, str) and raw_class in FAILURE_CLASSES:
        return raw_class

    det = detector_name.lower().strip() if detector_name else ""

    # Planner
    if "planner" in det or sem.get("role") == "planner" or sem.get("stage") == "planning":
        reason = str(ev.get("reason") or ev.get("planner_last_failure") or sem.get("error_type") or "").lower()
        if "timeout" in reason or "timed out" in reason or "deadline" in reason:
            return "planner_timeout"
        if "malformed" in reason or "extraction" in reason or "json" in reason or "marker" in reason:
            return "malformed_ai_output"
        return "malformed_ai_output"

    # Reviewer
    if "reviewer" in det or sem.get("role") == "reviewer" or sem.get("stage") == "reviewing":
        reason = str(ev.get("reason") or sem.get("error_type") or "").lower()
        decision = str(ev.get("decision") or "").lower()
        if "timeout" in reason or "timed out" in reason or "deadline" in reason:
            return "reviewer_timeout"
        if decision in {"reject", "remediate"} or "reject" in reason:
            return "reviewer_rejected"
        return "reviewer_rejected"

    # Watchdog recovery
    if det == "watchdog_recovery" or raw_class == "watchdog_stall":
        diag = str(ev.get("diagnosis") or sem.get("diagnosis_code") or "").lower()
        if "lineage" in diag or "worker" in diag or "hung" in diag or "process" in diag:
            return "worker_process_lost"
        if "projection" in diag or "mismatch" in diag:
            return "lifecycle_projection_mismatch"
        return "worker_process_lost"

    # Worker / Execution loss
    if det in {"worker_loss", "execution_loss", "actionable_execution_loss"} or raw_class == "execution_loss":
        term = str(ev.get("terminal_outcome") or "").lower()
        inv = str(ev.get("invariant_key") or sem.get("invariant_identifier") or "").lower()
        if "stale_predecessor" in term or "predecessor" in term or "fenced" in term:
            return "worker_process_lost"
        if "missing" in term or "record" in term:
            return "missing_execution_record"
        return "worker_process_lost"

    # Transition Executor
    if "transition" in det:
        reason = str(ev.get("reason") or "").lower()
        if "handoff" in reason and ("missing" in reason or "lost" in reason):
            return "lost_handoff"
        if "duplicate" in reason:
            return "duplicate_request"
        if "projection" in reason or "mismatch" in reason or "repository changed" in reason:
            return "lifecycle_projection_mismatch"
        if "contradict" in reason or "ambiguous" in reason:
            return "unknown_requires_owner"
        return "unknown_requires_owner"

    # Daemon restart
    if "restart" in det:
        kind = str(sem.get("restart_kind") or ev.get("restart_kind") or "").lower()
        if "handoff" in kind or "transition" in kind:
            return "daemon_restart_during_handoff"
        return "daemon_restart_during_execution"

    # AIBroker / resource routing
    if "broker" in det or "routing" in det:
        cls_hint = str(ev.get("classification") or sem.get("classification") or "").lower()
        err_msg = str(ev.get("error") or ev.get("reason") or "").lower()
        if cls_hint in {"quota_exhausted", "rate_limited"} or "quota" in err_msg or "rate limit" in err_msg:
            return "provider_quota_exhausted"
        if (
            cls_hint in {"provider_temporarily_unavailable", "resource_unavailable"}
            or "provider" in err_msg
            or "candidate" in err_msg
        ):
            return "provider_unavailable"
        if "broker" in err_msg or "connection refused" in err_msg:
            return "aibroker_unavailable"
        return "provider_unavailable"

    # Execution transport
    if "transport" in det:
        return "transport_unavailable"

    # Durable jobs
    if "job" in det:
        fkind = str(ev.get("failure_kind") or sem.get("failure_kind") or "").lower()
        if "transport" in fkind or "channel" in fkind or "connection" in fkind:
            return "transport_unavailable"
        if "duplicate" in fkind:
            return "duplicate_request"
        if "worker" in fkind or "loss" in fkind:
            return "worker_process_lost"
        return "transport_unavailable"

    return "unknown_requires_owner"
