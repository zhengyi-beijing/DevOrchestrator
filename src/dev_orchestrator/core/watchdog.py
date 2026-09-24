"""Active-project progress watchdog and automatic diagnostics coordinator.

Pure policy, canonical path identity, dual provenance verification, clock-free
fingerprint purity, fail-closed state persistence, and two-phase safe recovery.
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from dev_orchestrator.core.diagnostics import (
    ACTIVE_WORKER_STATES,
    DIAGNOSIS_CODES,
    Diagnosis,
    WORKER_EXPECTED_LIFECYCLE_STATES,
    classify_evidence,
    collect_evidence,
    evidence_hash,
)
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.monitor.telemetry import extract_task_id
from dev_orchestrator.platform.process import is_pid_alive
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

WATCHDOG_SCHEMA_VERSION = 1
WATCHDOG_STATE_FILE = "watchdog.json"
WATCHDOG_COMMAND_PREFIX = "wd-"
MAX_TERMINAL_ATTEMPTS_PER_PROJECT = 20
NONTERMINAL_ATTEMPT_STATES = frozenset({"running", "reserved", "requested"})

# FR-2A: Allow up to this many seconds of clock skew when validating completed_at.
# A completed_at that is more than this far in the future is treated as invalid/tampered.
RECOVERY_COMPLETED_AT_CLOCK_SKEW_S = 60

ACTIVE_LIFECYCLE_STATES = frozenset({
    "PLANNING",
    "REVIEWING_PLAN",
    "REMEDIATING_PLAN",
    "APPLYING_PLAN",
    "EXECUTING",
    "REVIEWING",
    "REVIEW_FAILED",
    "PLAN_FAILED",
    "REMEDIATING",
    # READY_TO_RUN normally moves promptly through the guarded control path.
    # When it remains stale with no managed execution, it is a recoverable
    # launch gap rather than an owner decision.
    "READY_TO_RUN",
})

LIFECYCLE_OVERRIDE_FAMILY = {
    "PLANNING": "PLANNING",
    "REVIEWING_PLAN": "PLANNING",
    "REMEDIATING_PLAN": "PLANNING",
    "APPLYING_PLAN": "PLANNING",
    "EXECUTING": "EXECUTING",
    "REVIEWING": "REVIEWING",
    "REVIEW_FAILED": "REVIEWING",
    "PLAN_FAILED": "PLANNING",
    "REMEDIATING": "REMEDIATING",
    "READY_TO_RUN": "EXECUTING",
}

WATCHDOG_MILESTONES = frozenset({
    "STALL_DETECTED",
    "DIAGNOSTIC_STARTED",
    "DIAGNOSTIC_RESULT",
    "RECOVERY_STARTED",
    "EXECUTION_LOSS_DETECTED",
    "EXECUTION_LOSS_RECOVERY_STARTED",
    "EXECUTION_LOSS_ESCALATED",
    "EXECUTION_LOSS_RESOLVED",
})

WATCHDOG_OWNED_REPO_PATHS = (
    ".devorch/status.json",
)

WATCHDOG_OWNED_REPO_GLOBS = (
    ".devorch/watchdog*.json",
    ".devorch/watchdog.json.corrupt-*",
)

WATCHDOG_OWNED_RUNTIME_PATHS = (
    "watchdog.json",
)

WATCHDOG_OWNED_RUNTIME_GLOBS = (
    "watchdog.json.corrupt-*",
    "control/inbox/wd-*.json",
    "control/history/wd-*.json",
)

FINGERPRINT_FIELDS = frozenset({
    "git_head",
    "last_activity_at",
    "newest_kind",
    "newest_path",
    "sources",
    "changed_entries_considered",
    "role_records",
    "progress_entry",
    "job_records",
})

FINGERPRINT_FORBIDDEN = frozenset({
    "age_seconds",
    "watchdog_safe_activity_age_seconds",
    "last_activity_age_seconds",
    "no_progress_seconds",
    "now",
    "last_checked_at",
    "elapsed_seconds",
    "monotonic_seconds",
    "uptime_seconds",
    "excluded_paths",
    "excluded_count",
    "repo_root_fingerprint",
    "runtime_root_fingerprint",
    "repo_scope",
    "runtime_scope",
})

_FORBIDDEN_KEY_PATTERNS = (
    "age",
    "now",
    "elapsed",
    "monotonic",
    "uptime",
)


def canonical_path(p: Path | str) -> str:
    """Normcase, realpath and abspath canonical identity."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(p))))


def path_contains(root: Path | str, candidate: Path | str) -> bool:
    """Path-aware containment check via commonpath; never string prefix."""
    try:
        c_root = canonical_path(root)
        c_cand = canonical_path(candidate)
        return os.path.commonpath([c_root, c_cand]) == c_root
    except ValueError:
        # Cross-drive or UNC/drive comparison on Windows raises ValueError
        return False


def _glob_match_no_cross(rel: str, pattern: str) -> bool:
    """Match a glob without allowing basename wildcards to cross directories."""
    rel_path = Path(str(rel).replace("\\", "/"))
    pat_path = Path(str(pattern).replace("\\", "/"))
    if rel_path.parent != pat_path.parent:
        return False
    return fnmatch.fnmatch(rel_path.name, pat_path.name)


def _timestamp_is_newer(candidate: Any, current: Any) -> bool:
    """Compare timestamps by parsed UTC time, falling back to string order."""
    cand_text = str(candidate or "").strip()
    curr_text = str(current or "").strip()
    cand_dt = parse_utc(cand_text)
    curr_dt = parse_utc(curr_text)
    if cand_dt is not None:
        if curr_dt is None:
            return True
        if cand_dt != curr_dt:
            return cand_dt > curr_dt
    elif curr_dt is not None:
        return False
    return cand_text > curr_text


def _latest_timestamp_value(values: Sequence[Any]) -> Optional[str]:
    latest: Optional[str] = None
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if latest is None or _timestamp_is_newer(text, latest):
            latest = text
    return latest


def _attempt_sort_key(attempt: dict[str, Any]) -> tuple[datetime, str, str]:
    stamp = (
        attempt.get("completed_at")
        or attempt.get("started_at")
        or attempt.get("deadline_at")
        or attempt.get("requested_at")
        or ""
    )
    parsed = parse_utc(stamp) or datetime.min.replace(tzinfo=timezone.utc)
    return parsed, str(stamp or ""), str(attempt.get("attempt_key") or "")


def is_watchdog_owned_path(
    repo_root: Path | str,
    candidate: Path | str,
    *,
    runtime_root: Path | str | None = None,
) -> bool:
    """Check whether candidate path is owned by the watchdog.

    Pure function of its three arguments; resolves absolute canonical paths.
    Runtime-owned paths are evaluated only when runtime_root is provided and
    contained within repo_root.
    """
    c_repo = canonical_path(repo_root)
    cand_str = os.fspath(candidate)
    if os.path.isabs(cand_str):
        c_cand = canonical_path(cand_str)
    else:
        c_cand = canonical_path(Path(repo_root) / cand_str)

    # 1. Repo-owned exact paths
    for p in WATCHDOG_OWNED_REPO_PATHS:
        if c_cand == canonical_path(Path(repo_root) / p):
            return True

    # 2. Repo-owned globs
    if path_contains(repo_root, c_cand):
        try:
            rel = os.path.relpath(c_cand, c_repo).replace("\\", "/")
            for g in WATCHDOG_OWNED_REPO_GLOBS:
                if _glob_match_no_cross(rel, g):
                    return True
        except ValueError:
            pass

    # 3. Runtime-owned paths (only when runtime_root is inside repo_root)
    if runtime_root is not None and path_contains(repo_root, runtime_root):
        c_rt = canonical_path(runtime_root)
        for p in WATCHDOG_OWNED_RUNTIME_PATHS:
            if c_cand == canonical_path(Path(runtime_root) / p):
                return True
        if path_contains(runtime_root, c_cand):
            try:
                rel_rt = os.path.relpath(c_cand, c_rt).replace("\\", "/")
                for g in WATCHDOG_OWNED_RUNTIME_GLOBS:
                    if _glob_match_no_cross(rel_rt, g):
                        return True
            except ValueError:
                pass

    return False


def resolve_watchdog_policy(project: dict[str, Any]) -> dict[str, Any]:
    """Return normalized watchdog policy for project, never raising."""
    cfg = project.get("watchdog")
    if not isinstance(cfg, dict):
        return {
            "enabled": False,
            "no_progress_threshold_minutes": 15,
            "lifecycle_overrides": {},
            "cooldown_minutes": 30,
            "max_attempts_per_run": 3,
            "diagnostic_timeout_seconds": 120,
            "auto_recovery": False,
            "execution_loss_detection": True,
            "provider_output_grace_seconds": 60,
            "execution_loss_confirmations": 2,
            "execution_loss_max_recoveries": 3,
            "execution_loss_backoff_seconds": 30,
            "execution_loss_unknown_escalation_minutes": 15,
            "execution_acceptance_grace_seconds": 45,
            "live_proof_freshness_seconds": 30,
        }
    return {
        "enabled": cfg.get("enabled", True) if isinstance(cfg.get("enabled"), bool) else True,
        "no_progress_threshold_minutes": cfg.get("no_progress_threshold_minutes", 15),
        "lifecycle_overrides": dict(cfg.get("lifecycle_overrides") or {}),
        "cooldown_minutes": cfg.get("cooldown_minutes", 30),
        "max_attempts_per_run": cfg.get("max_attempts_per_run", 3),
        "diagnostic_timeout_seconds": cfg.get("diagnostic_timeout_seconds", 120),
        "auto_recovery": cfg.get("auto_recovery", False) if isinstance(cfg.get("auto_recovery"), bool) else False,
        "execution_loss_detection": cfg.get("execution_loss_detection", True) if isinstance(cfg.get("execution_loss_detection"), bool) else True,
        "provider_output_grace_seconds": int(cfg.get("provider_output_grace_seconds", 60)),
        "execution_loss_confirmations": int(cfg.get("execution_loss_confirmations", 2)),
        "execution_loss_max_recoveries": int(cfg.get("execution_loss_max_recoveries", 3)),
        "execution_loss_backoff_seconds": int(cfg.get("execution_loss_backoff_seconds", 30)),
        "execution_loss_unknown_escalation_minutes": int(cfg.get("execution_loss_unknown_escalation_minutes", 15)),
        "execution_acceptance_grace_seconds": int(cfg.get("execution_acceptance_grace_seconds", 45)),
        "live_proof_freshness_seconds": int(cfg.get("live_proof_freshness_seconds", 30)),
    }


def resolve_threshold_seconds(policy: dict[str, Any], lifecycle_state: str) -> float:
    """Resolve no-progress threshold in seconds for lifecycle_state."""
    st = str(lifecycle_state or "").strip().upper()
    overrides = policy.get("lifecycle_overrides") or {}
    if st in overrides:
        try:
            return float(overrides[st]) * 60.0
        except (TypeError, ValueError):
            pass
    family = LIFECYCLE_OVERRIDE_FAMILY.get(st)
    if family and family in overrides:
        try:
            return float(overrides[family]) * 60.0
        except (TypeError, ValueError):
            pass
    try:
        return float(policy.get("no_progress_threshold_minutes", 15)) * 60.0
    except (TypeError, ValueError):
        return 900.0


def _validate_fingerprint_payload_keys(payload: Any) -> None:
    """Fail closed if payload contains any unallowlisted or forbidden key."""
    if not isinstance(payload, dict):
        return
    for k, v in payload.items():
        if k in FINGERPRINT_FORBIDDEN:
            raise ValueError(f"forbidden key in progress fingerprint: {k!r}")
        lower_k = str(k).lower()
        tokens = set(re.split(r"[_\-.:]", lower_k))
        for pat in _FORBIDDEN_KEY_PATTERNS:
            if pat in tokens or lower_k == pat:
                raise ValueError(f"forbidden timing pattern {pat!r} in key {k!r}")
        if isinstance(v, dict):
            _validate_fingerprint_payload_keys(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    _validate_fingerprint_payload_keys(item)


def build_progress_fingerprint(payload: dict[str, Any]) -> str:
    """Produce deterministic clock-free progress fingerprint.

    Asserts fail-closed that all keys are in FINGERPRINT_FIELDS and no key
    intersects FINGERPRINT_FORBIDDEN.
    """
    if not isinstance(payload, dict):
        raise ValueError("progress fingerprint payload must be a dict")
    unknown = set(payload.keys()) - FINGERPRINT_FIELDS
    if unknown:
        raise ValueError(f"unallowlisted fields in progress fingerprint: {sorted(unknown)}")
    _validate_fingerprint_payload_keys(payload)

    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()[:16]


def resolve_run_key(snapshot: dict[str, Any], executor_state: dict[str, Any] | None = None) -> str:
    """Derive stable run_key without clock or uuid drift."""
    telemetry = snapshot.get("telemetry")
    if isinstance(telemetry, dict) and telemetry.get("run_id"):
        run_id = str(telemetry["run_id"]).strip()
        if run_id:
            return run_id

    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
    if isinstance(executor_state, dict):
        executions = executor_state.get("executions")
        if isinstance(executions, dict):
            latest_active_id: Optional[str] = None
            latest_active_ts: Optional[str] = None
            for rec in executions.values():
                if isinstance(rec, dict) and str(rec.get("project_id") or "") == project_id:
                    rec_state = str(rec.get("state") or "").lower()
                    if rec_state in ("completed", "failed", "cancelled", "handoff", "blocked", "settled"):
                        continue
                    active_id = rec.get("execution_id") or rec.get("request_id") or rec.get("run_id")
                    if active_id:
                        rec_ts = (
                            rec.get("updated_at")
                            or rec.get("started_at")
                            or rec.get("requested_at")
                            or rec.get("created_at")
                        )
                        if latest_active_id is None or _timestamp_is_newer(rec_ts, latest_active_ts):
                            latest_active_id = str(active_id).strip()
                            latest_active_ts = str(rec_ts or "")
            if latest_active_id:
                return latest_active_id

    role_run_id = snapshot.get("role_run_id")
    if role_run_id:
        return str(role_run_id).strip()

    task_id = str((telemetry or {}).get("task_id") or snapshot.get("task_id") or "")
    lifecycle_state = str(snapshot.get("lifecycle_state") or snapshot.get("status") or snapshot.get("state") or "")
    fallback_hash = hashlib.sha256(f"{project_id}|{task_id}|{lifecycle_state}".encode("utf-8")).hexdigest()[:12]
    return f"norun:{fallback_hash}"


def resolve_run_scope_key(project_id: str, task_id: str, lifecycle_state: str, run_key: str) -> str:
    return hashlib.sha256(f"{project_id}|{task_id}|{lifecycle_state}|{run_key}".encode("utf-8")).hexdigest()[:16]


def resolve_attempt_key(run_scope_key: str, progress_fingerprint: str) -> str:
    return hashlib.sha256(f"{run_scope_key}|{progress_fingerprint}".encode("utf-8")).hexdigest()[:16]


def _active_execution_id(
    snapshot: dict[str, Any], executor_state: dict[str, Any] | None = None,
) -> str | None:
    """Return a durable active execution identifier, never a worker PID."""
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
    if isinstance(executor_state, dict):
        executions = executor_state.get("executions")
        if isinstance(executions, dict):
            active: list[dict[str, Any]] = [
                rec for rec in executions.values()
                if isinstance(rec, dict)
                and str(rec.get("project_id") or "") == project_id
                and str(rec.get("state") or "").lower() in {"launching", "running"}
            ]
            if active:
                latest = max(active, key=lambda rec: str(rec.get("started_at") or ""))
                value = latest.get("execution_id") or latest.get("request_id") or latest.get("run_id") or latest.get("source_request_id")
                if value:
                    return str(value).strip()

    broker = snapshot.get("broker_execution") if isinstance(snapshot.get("broker_execution"), dict) else {}
    broker_active = str(broker.get("state") or "").lower() in {"launching", "running"}
    worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
    worker_active = str(worker.get("state") or "").lower() in ACTIVE_WORKER_STATES
    if not broker_active and not worker_active:
        return None
    run_id = str(telemetry.get("run_id") or "").strip()
    role_run_id = str(broker.get("role_run_id") or snapshot.get("role_run_id") or "").strip()
    return run_id or role_run_id or None


def _has_active_execution(
    snapshot: dict[str, Any], executor_state: dict[str, Any] | None = None,
) -> bool:
    """Use durable execution state, not a possibly recycled PID, as the guard."""
    if _active_execution_id(snapshot, executor_state):
        return True
    worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
    return str(worker.get("state") or "").lower() in ACTIVE_WORKER_STATES


def resolve_recovery_epoch(
    snapshot: dict[str, Any],
    executor_state: dict[str, Any] | None = None,
    runtime_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Build a durable epoch from authoritative task/plan/control/HEAD/run facts.

    A PID, timestamps, and watchdog observations are deliberately excluded.  An
    epoch transition is therefore evidence of a new authority boundary, rather
    than a transient liveness observation.
    """
    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "").strip()
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    task_id = str(
        telemetry.get("task_id")
        or snapshot.get("task_id")
        or extract_task_id(snapshot.get("next_title"))
        or ""
    ).strip()
    git = snapshot.get("git") if isinstance(snapshot.get("git"), dict) else {}
    head = str(git.get("head") or "").strip()
    if not head:
        repo_path = str(snapshot.get("repo_path") or snapshot.get("root") or "")
        if repo_path:
            truth = read_repository_truth(repo_path)
            if truth.valid:
                head = str(truth.head or "").strip()
    if not project_id or not task_id or not head:
        return None

    planner = snapshot.get("planner") if isinstance(snapshot.get("planner"), dict) else {}
    reviewer = snapshot.get("reviewer") if isinstance(snapshot.get("reviewer"), dict) else {}
    actuation = snapshot.get("actuation") if isinstance(snapshot.get("actuation"), dict) else {}

    plan_id = str(planner.get("plan_id") or telemetry.get("plan_id") or "").strip() or None
    if plan_id is None and runtime_root:
        planner_file = Path(runtime_root) / "ai-planner.json"
        if planner_file.is_file():
            pdata = read_json(planner_file, {})
            plans = pdata.get("plans") if isinstance(pdata, dict) else {}
            active_plans = [
                p for p in (plans or {}).values()
                if isinstance(p, dict)
                and str(p.get("project_id") or "") == project_id
                and p.get("state") in {"planning", "reviewing", "remediating", "applying"}
            ]
            if active_plans:
                latest_p = max(active_plans, key=lambda p: str(p.get("started_at") or ""))
                plan_id = str(latest_p.get("plan_id") or "").strip() or None

    review_id = str(reviewer.get("review_id") or "").strip() or None
    if review_id is None and runtime_root:
        reviewer_file = Path(runtime_root) / "ai-reviewer.json"
        if reviewer_file.is_file():
            rdata = read_json(reviewer_file, {})
            reviews = rdata.get("reviews") if isinstance(rdata, dict) else {}
            active_reviews = [
                r for r in (reviews or {}).values()
                if isinstance(r, dict)
                and str(r.get("project_id") or "") == project_id
                and r.get("state") in {"launching", "running"}
            ]
            if active_reviews:
                latest_r = max(active_reviews, key=lambda r: str(r.get("started_at") or ""))
                review_id = str(latest_r.get("review_id") or "").strip() or None

    control_id = str(actuation.get("source_request_id") or snapshot.get("source_request_id") or "").strip() or None

    exec_state = executor_state
    if exec_state is None and runtime_root:
        te_file = Path(runtime_root) / "transition-executor.json"
        if te_file.is_file():
            exec_state = read_json(te_file, {})

    evidence = {
        "project_id": project_id,
        "task_id": task_id,
        "head": head,
        "plan_id": plan_id,
        "review_id": review_id,
        "control_id": control_id,
        "execution_id": _active_execution_id(snapshot, exec_state),
    }
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"id": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], "evidence": evidence}


def compute_record_integrity_hash(attempt_record: dict[str, Any]) -> str:
    """Stable integrity hash over all fields that affect recovery actuation.

    Covers attempt_key, run_scope_key, diagnosis, completed_at, and evidence_hash.
    Any field tampered without updating this hash will be detected before RESERVE.
    """
    fields = {
        "attempt_key": str(attempt_record.get("attempt_key") or ""),
        "completed_at": str(attempt_record.get("completed_at") or ""),
        "diagnosis": str(attempt_record.get("diagnosis") or ""),
        "evidence_hash": str(attempt_record.get("evidence_hash") or ""),
        "recovery_epoch_id": str(attempt_record.get("recovery_epoch_id") or ""),
        "run_scope_key": str(attempt_record.get("run_scope_key") or ""),
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def collect_progress_signals(
    snapshot: dict[str, Any],
    runtime_root: Path | str | None,
) -> tuple[str | None, str, dict[str, Any]]:
    """Extract progress signals with dual provenance validation and self-exclusion.

    Returns (last_progress_at, progress_fingerprint, signal_sources).
    """
    activity = snapshot.get("activity")
    if not isinstance(activity, dict) or "watchdog_safe" not in activity:
        return None, "", {
            "activity_evidence": "unavailable",
            "activity_evidence_reason": "missing-activity-block",
        }

    safe = activity["watchdog_safe"]
    if not isinstance(safe, dict):
        return None, "", {
            "activity_evidence": "unavailable",
            "activity_evidence_reason": "malformed-activity-block",
        }

    repo_path = str(snapshot.get("repo_path") or snapshot.get("root") or "")
    repo_fp = safe.get("repo_root_fingerprint")
    repo_scope = safe.get("repo_scope")
    if repo_scope != "canonical" or not repo_fp:
        return None, "", {
            "activity_evidence": "unavailable",
            "activity_evidence_reason": "repo-root-unknown",
        }
    expected_repo_fp = hashlib.sha256(canonical_path(repo_path).encode("utf-8")).hexdigest()[:16]
    if repo_fp != expected_repo_fp:
        return None, "", {
            "activity_evidence": "unavailable",
            "activity_evidence_reason": "repo-root-mismatch",
        }

    # Dual provenance: runtime check
    if runtime_root is not None and path_contains(repo_path, runtime_root):
        runtime_scope = safe.get("runtime_scope")
        rt_fp = safe.get("runtime_root_fingerprint")
        if runtime_scope == "unknown" or not rt_fp:
            return None, "", {
                "activity_evidence": "unavailable",
                "activity_evidence_reason": "runtime-root-unaware",
            }
        expected_rt_fp = hashlib.sha256(canonical_path(runtime_root).encode("utf-8")).hexdigest()[:16]
        if rt_fp != expected_rt_fp:
            return None, "", {
                "activity_evidence": "unavailable",
                "activity_evidence_reason": "runtime-root-mismatch",
            }

    # Extract accepted durable signals
    safe_last_activity = safe.get("last_activity_at")
    newest_kind = safe.get("newest_kind")
    newest_path = safe.get("newest_path")
    sources_map = safe.get("sources") if isinstance(safe.get("sources"), dict) else {}
    changed_considered = int(safe.get("changed_entries_considered") or 0)

    git = snapshot.get("git") if isinstance(snapshot.get("git"), dict) else {}
    git_head = str(git.get("head") or "")

    # Role records (from runtime ledgers if runtime_root is provided)
    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
    role_records_summary: dict[str, Any] = {}
    latest_role_timestamp: Optional[str] = None
    if runtime_root is not None:
        rt = Path(runtime_root)
        for ledger_name in ("ai-planner.json", "ai-reviewer.json", "transition-executor.json"):
            ledger_file = rt / ledger_name
            if ledger_file.is_file():
                data = read_json(ledger_file, {})
                if isinstance(data, dict):
                    records = data.get("plans") or data.get("reviews") or data.get("executions") or {}
                    if isinstance(records, dict):
                        for rec in records.values():
                            if isinstance(rec, dict) and str(rec.get("project_id") or "") == project_id:
                                meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
                                if meta.get("source") == "watchdog":
                                    continue
                                ts = (
                                    rec.get("completed_at")
                                    or rec.get("started_at")
                                    or rec.get("requested_at")
                                    or rec.get("updated_at")
                                )
                                rid = rec.get("plan_id") or rec.get("review_id") or rec.get("execution_id") or rec.get("request_id")
                                if ts and rid:
                                    existing = role_records_summary.get(ledger_name)
                                    if existing is None or _timestamp_is_newer(ts, existing.get("timestamp")):
                                        role_records_summary[ledger_name] = {
                                            "id": str(rid),
                                            "timestamp": str(ts),
                                        }
                                        latest_role_timestamp = _latest_timestamp_value((latest_role_timestamp, ts))

    # Progress channel history
    progress_summary: Optional[dict[str, Any]] = None
    latest_progress_timestamp: Optional[str] = None
    if runtime_root is not None:
        prog_file = Path(runtime_root) / "progress-channel.json"
        if prog_file.is_file():
            prog_data = read_json(prog_file, {})
            if isinstance(prog_data, dict) and isinstance(prog_data.get("history"), list):
                for item in prog_data["history"]:
                    if not isinstance(item, dict) or str(item.get("project_id") or "") != project_id:
                        continue
                    mstone = item.get("milestone")
                    if mstone in WATCHDOG_MILESTONES:
                        continue
                    dtls = item.get("details") if isinstance(item.get("details"), dict) else {}
                    if dtls.get("source") == "watchdog":
                        continue
                    ts = item.get("timestamp")
                    nid = item.get("notification_id")
                    if ts and nid:
                        if progress_summary is None or _timestamp_is_newer(ts, progress_summary.get("timestamp")):
                            progress_summary = {
                                "id": str(nid),
                                "timestamp": str(ts),
                                "milestone": str(mstone),
                            }
                            latest_progress_timestamp = _latest_timestamp_value((latest_progress_timestamp, ts))

    # Job records (from jobs/index.json if runtime_root is provided)
    job_records_summary: Optional[dict[str, Any]] = None
    latest_job_timestamp: Optional[str] = None
    if runtime_root is not None:
        jobs_index_file = Path(runtime_root) / "jobs" / "index.json"
        if jobs_index_file.is_file():
            jobs_index_data = read_json(jobs_index_file, {})
            if isinstance(jobs_index_data, dict):
                raw_jobs = jobs_index_data.get("jobs", {})
                if isinstance(raw_jobs, dict):
                    for jdata in raw_jobs.values():
                        if not isinstance(jdata, dict) or str(jdata.get("project_id") or "") != project_id:
                            continue
                        jid = str(jdata.get("job_id") or "")
                        if jid.startswith("wd-") or jdata.get("source") == "watchdog" or str(jdata.get("command_ref") or "").startswith("wd-"):
                            continue
                        ts = (
                            jdata.get("finished_at")
                            or jdata.get("started_at")
                            or jdata.get("updated_at")
                            or jdata.get("created_at")
                        )
                        if ts and jid:
                            if job_records_summary is None or _timestamp_is_newer(ts, job_records_summary.get("timestamp")):
                                job_records_summary = {
                                    "id": str(jid),
                                    "timestamp": str(ts),
                                }
                                latest_job_timestamp = _latest_timestamp_value((latest_job_timestamp, ts))

    # Determine latest durable progress timestamp
    last_progress_at = _latest_timestamp_value((safe_last_activity, latest_role_timestamp, latest_progress_timestamp, latest_job_timestamp))

    # Assemble fingerprint payload strictly from allowlisted fields
    sources_summary: dict[str, Any] = {}
    for k in sorted(sources_map.keys()):
        v = sources_map[k]
        if isinstance(v, dict):
            sources_summary[k] = {
                "last_activity_at": v.get("last_activity_at"),
                "path": v.get("path"),
            }

    fingerprint_payload = {
        "git_head": git_head,
        "last_activity_at": safe_last_activity,
        "newest_kind": newest_kind,
        "newest_path": newest_path,
        "sources": sources_summary,
        "changed_entries_considered": changed_considered,
        "role_records": role_records_summary,
        "progress_entry": progress_summary,
        "job_records": job_records_summary,
    }

    progress_fingerprint = build_progress_fingerprint(fingerprint_payload)

    signal_sources = {
        "activity_evidence": "available",
        "activity_evidence_reason": None,
        "git_head": git_head,
        "watchdog_safe_activity_at": safe_last_activity,
        "watchdog_safe_age_seconds": safe.get("age_seconds"),
        "changed_entries_considered": changed_considered,
        "excluded_paths": safe.get("excluded_paths", []),
        "excluded_count": safe.get("excluded_count", 0),
        "sources": sources_map,
        "role_records": role_records_summary,
        "progress_entry": progress_summary,
        "fingerprint_inputs": fingerprint_payload,
    }

    return last_progress_at, progress_fingerprint, signal_sources


def _ready_launch_gap_signals(
    signals: tuple[str | None, str, dict[str, Any]],
) -> tuple[str | None, str, dict[str, Any]]:
    """Return progress signals relevant to an unlaunched READY_TO_RUN task.

    A ready task has no managed Worker whose repository writes can establish
    progress.  In that state an unrelated dirty/untracked file (a copied
    prompt, editor scratch file, or diagnostic artifact) must not indefinitely
    refresh the launch timer.  Such a write remains visible to the normal
    repository-safety guard and can still block a launch; it is simply not
    evidence that the ready task made progress.
    """
    _last_progress_at, _fingerprint, signal_sources = signals
    sources = signal_sources.get("sources") if isinstance(signal_sources.get("sources"), dict) else {}
    agent = sources.get("agent_file") if isinstance(sources.get("agent_file"), dict) else {}
    payload = signal_sources.get("fingerprint_inputs") if isinstance(signal_sources.get("fingerprint_inputs"), dict) else {}
    if not payload:
        return signals
    # Older or reduced adapters may not publish per-source activity.  Their
    # aggregate is still the only available evidence, so retain it rather than
    # silently declaring a ready gap immediately.  The selective behavior is
    # only possible once the adapter explicitly identifies a Git or worker
    # runtime source to exclude.
    source_breakdown_available = any(
        isinstance(sources.get(kind), dict)
        for kind in ("git_changed", "worker_runtime", "agent_file")
    )
    agent_activity_at = agent.get("last_activity_at") if source_breakdown_available else payload.get("last_activity_at")
    role_records = payload.get("role_records") if isinstance(payload.get("role_records"), dict) else {}
    role_timestamps = [
        record.get("timestamp")
        for record in role_records.values()
        if isinstance(record, dict)
    ]
    progress_entry = payload.get("progress_entry") if isinstance(payload.get("progress_entry"), dict) else {}
    job_records = payload.get("job_records") if isinstance(payload.get("job_records"), dict) else {}
    last_progress_at = _latest_timestamp_value((
        agent_activity_at,
        *role_timestamps,
        progress_entry.get("timestamp"),
        job_records.get("timestamp"),
    ))

    # Keep the immutable HEAD and all durable role/progress records, but omit
    # raw worktree and old worker-runtime activity.  Neither can prove progress
    # for a task that has no current managed execution.
    ready_sources = {
        "agent_file": {
            "last_activity_at": agent_activity_at,
            "path": agent.get("path"),
        },
        "git_changed": {"last_activity_at": None, "path": None},
        "worker_runtime": {"last_activity_at": None, "path": None},
    }
    ready_payload = {
        "git_head": payload.get("git_head"),
        "last_activity_at": agent_activity_at,
        "newest_kind": "ready_launch_activity",
        "newest_path": agent.get("path"),
        "sources": ready_sources,
        "changed_entries_considered": 0,
        "role_records": copy.deepcopy(role_records),
        "progress_entry": copy.deepcopy(progress_entry) if progress_entry else None,
        "job_records": copy.deepcopy(job_records) if job_records else None,
    }
    ready_signal_sources = copy.deepcopy(signal_sources)
    ready_signal_sources["ready_launch_gap_activity"] = True
    ready_signal_sources["fingerprint_inputs"] = ready_payload
    return last_progress_at, build_progress_fingerprint(ready_payload), ready_signal_sources


@dataclass(frozen=True)
class StallAssessment:
    monitored: bool
    lifecycle_state: str
    task_id: Optional[str]
    run_key: str
    run_scope_key: str
    no_progress_seconds: Optional[float]
    threshold_seconds: float
    breached: bool
    progress_fingerprint: str
    activity_evidence: str
    active_execution: bool
    recovery_epoch: dict[str, Any] | None
    last_progress_at: str | None


def evaluate_stall(
    snapshot: dict[str, Any],
    policy: dict[str, Any],
    signals: tuple[str | None, str, dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    executor_state: dict[str, Any] | None = None,
    runtime_root: Path | str | None = None,
) -> StallAssessment:
    """Sole consumer of age; evaluates threshold breach against durable signals."""
    last_progress_at, progress_fingerprint, signal_sources = signals
    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
    lifecycle_state = str(snapshot.get("lifecycle_state") or snapshot.get("status") or snapshot.get("state") or "").strip().upper()
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    task_id = telemetry.get("task_id") or snapshot.get("task_id")

    active_execution = _has_active_execution(snapshot, executor_state)
    if lifecycle_state == "READY_TO_RUN" and not active_execution:
        last_progress_at, progress_fingerprint, signal_sources = _ready_launch_gap_signals(signals)
    monitored = lifecycle_state in ACTIVE_LIFECYCLE_STATES and not (
        lifecycle_state == "READY_TO_RUN" and active_execution
    )
    threshold_seconds = resolve_threshold_seconds(policy, lifecycle_state)
    run_key = resolve_run_key(snapshot, executor_state)
    r_scope_key = resolve_run_scope_key(project_id, str(task_id or ""), lifecycle_state, run_key)

    evidence_state = signal_sources.get("activity_evidence", "available")
    no_progress_seconds: Optional[float] = None
    breached = False

    if evidence_state == "available" and last_progress_at is not None:
        eval_now = now or datetime.now(timezone.utc)
        prog_dt = parse_utc(last_progress_at)
        if prog_dt is not None:
            no_progress_seconds = max(0.0, round((eval_now - prog_dt).total_seconds(), 1))
            if monitored and no_progress_seconds >= threshold_seconds:
                breached = True

    return StallAssessment(
        monitored=monitored,
        lifecycle_state=lifecycle_state,
        task_id=task_id,
        run_key=run_key,
        run_scope_key=r_scope_key,
        no_progress_seconds=no_progress_seconds,
        threshold_seconds=threshold_seconds,
        breached=breached,
        progress_fingerprint=progress_fingerprint,
        activity_evidence=evidence_state,
        active_execution=active_execution,
        recovery_epoch=resolve_recovery_epoch(snapshot, executor_state, runtime_root=runtime_root),
        last_progress_at=last_progress_at,
    )


def _is_quarantine_file_readable(path: Path) -> bool:
    """Return True if path is a non-empty, readable regular file."""
    try:
        return path.is_file() and path.stat().st_size > 0 and len(path.read_bytes()) > 0
    except (OSError, IOError):
        return False


class WatchdogCoordinator:
    """Daemon-owned progress watchdog and automatic diagnostics coordinator."""

    def __init__(
        self,
        runtime_root: Path | str,
        *,
        ai_execution_port: Any = None,
        progress_channel: Any = None,
        liveness_probe: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.state_path = self.runtime_root / WATCHDOG_STATE_FILE
        self.ai_execution_port = ai_execution_port
        self.progress_channel = progress_channel
        # FR2B-LIVE-IDENTITY: injectable liveness probe so tests can supply deterministic stubs;
        # production code defaults to the platform is_pid_alive abstraction.
        self._liveness_probe: Callable[[Any], bool] = liveness_probe if liveness_probe is not None else is_pid_alive
        self._lock = threading.RLock()
        self._advance_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._cached_state: dict[str, Any] = {}
        self._preserve_existing_state_file = False
        self._init_state()

    def _init_state(self) -> None:
        with self._lock:
            self._cached_state = self._load_state()
            self._recover_interrupted()
            if not self._preserve_existing_state_file:
                self._save_state(self._cached_state)

    def _load_state(self) -> dict[str, Any]:
        """Load durable state fail-closed with quarantine on corrupt or future version."""
        if not self.state_path.is_file():
            return {
                "version": WATCHDOG_SCHEMA_VERSION,
                "degraded": False,
                "degraded_reason": None,
                "last_tick_error": None,
                "quarantined_projects": {},
                "projects": {},
            }

        try:
            raw_bytes = self.state_path.read_bytes()
            data = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            return self._quarantine_corrupt_state(f"unreadable state JSON: {exc}", raw_bytes if "raw_bytes" in locals() else b"")

        if not isinstance(data, dict):
            return self._quarantine_corrupt_state("state root is not a JSON object", raw_bytes)

        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            return self._quarantine_corrupt_state("invalid schema version type", raw_bytes)
        if version > WATCHDOG_SCHEMA_VERSION:
            return self._quarantine_corrupt_state(f"unsupported schema version {version}", raw_bytes)

        projects = data.get("projects")
        if not isinstance(projects, dict):
            return self._quarantine_corrupt_state("projects root is not a JSON object", raw_bytes)

        quarantined_projects = data.get("quarantined_projects") if isinstance(data.get("quarantined_projects"), dict) else {}
        validated_projects: dict[str, Any] = {}
        newly_quarantined_pids: list[str] = []

        for pid, prow in projects.items():
            if not isinstance(prow, dict) or not isinstance(prow.get("attempts"), dict):
                # Preserve raw row data for diagnostics; store as {"reason": ..., "raw": ...}
                quarantined_projects[pid] = {"reason": "malformed project row structure", "raw": prow}
                newly_quarantined_pids.append(pid)
                continue
            validated_projects[pid] = prow

        loaded = {
            "version": version,
            "degraded": bool(data.get("degraded", False)),
            "degraded_reason": data.get("degraded_reason"),
            "last_tick_error": data.get("last_tick_error"),
            "quarantined_projects": quarantined_projects,
            "projects": validated_projects,
        }

        # Emit OWNER_GATE per design for each newly quarantined project row
        if newly_quarantined_pids and self.progress_channel is not None and hasattr(self.progress_channel, "emit"):
            for pid in newly_quarantined_pids:
                try:
                    self.progress_channel.emit(
                        pid,
                        "OWNER_GATE",
                        occurrence_key=f"quarantined-project:{pid}",
                        message=f"Watchdog project row for {pid!r} was quarantined due to malformed structure",
                        details={"source": "watchdog", "gate": "quarantined-project", "project_id": pid},
                    )
                except Exception:
                    pass

        return loaded

    def _quarantine_corrupt_state(self, reason: str, raw_bytes: bytes) -> dict[str, Any]:
        self._preserve_existing_state_file = True
        stamp = utc_now_iso().replace(":", "-")

        if raw_bytes:
            # Identity is derived directly from the corrupt bytes — deterministic and verifiable.
            file_hash: Optional[str] = hashlib.sha256(raw_bytes).hexdigest()[:16]
            already_quarantined = any(
                q.name.endswith(f"-{file_hash}")
                for q in self.runtime_root.glob("watchdog.json.corrupt-*")
            )
            if not already_quarantined:
                quarantine_file = self.runtime_root / f"watchdog.json.corrupt-{stamp}-{file_hash}"
                try:
                    quarantine_file.write_bytes(raw_bytes)
                except OSError:
                    pass
        else:
            # FR-3: raw_bytes unavailable (e.g. OSError during read).  Attempt to copy the
            # state file and derive identity from the copied artifact's bytes so that
            # clear_degraded can cryptographically verify the artifact later.  If the copy or
            # the read-back fails, no verifiable artifact is produced; corrupt_identity is set
            # to None and clear_degraded will remain degraded (fail closed).
            file_hash = None
            if self.state_path.exists():
                # Use a reason-derived preliminary name; rename to content-hash name after copy.
                reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
                temp_quarantine = self.runtime_root / f"watchdog.json.corrupt-{stamp}-{reason_hash}"
                try:
                    shutil.copy2(self.state_path, temp_quarantine)
                    copied_bytes = temp_quarantine.read_bytes()
                    real_hash = hashlib.sha256(copied_bytes).hexdigest()[:16]
                    # Check for an existing quarantine under the real content hash.
                    already_quarantined = any(
                        q.name.endswith(f"-{real_hash}")
                        for q in self.runtime_root.glob("watchdog.json.corrupt-*")
                        if q != temp_quarantine
                    )
                    if already_quarantined:
                        try:
                            temp_quarantine.unlink()
                        except OSError:
                            pass
                    else:
                        final_quarantine = self.runtime_root / f"watchdog.json.corrupt-{stamp}-{real_hash}"
                        try:
                            temp_quarantine.rename(final_quarantine)
                        except OSError:
                            pass  # leave under temp name; identity still set
                    file_hash = real_hash
                except (OSError, IOError):
                    # Copy or read-back failed — clean up any partial file and leave
                    # file_hash=None so that clear_degraded remains degraded (fail closed).
                    try:
                        temp_quarantine.unlink()
                    except (OSError, NameError):
                        pass

        notification_hash = file_hash or hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
        if self.progress_channel is not None and hasattr(self.progress_channel, "emit"):
            try:
                self.progress_channel.emit(
                    "__controller__",
                    "OWNER_GATE",
                    occurrence_key=f"corrupt-state:{notification_hash}",
                    message=f"Watchdog state corrupt ({reason}); degraded monitor-only mode active",
                    details={"source": "watchdog", "gate": "corrupt-state", "reason": reason, "hash": notification_hash},
                )
            except Exception:
                pass

        return {
            "version": WATCHDOG_SCHEMA_VERSION,
            "degraded": True,
            "degraded_reason": reason,
            # FR-3: Persist the quarantine artifact identity (SHA-256[:16] of artifact bytes)
            # so clear_degraded can cryptographically verify the matching artifact.  None when
            # no verifiable artifact could be produced (fail-closed: clear_degraded blocked).
            "corrupt_identity": file_hash,
            "last_tick_error": None,
            "quarantined_projects": {},
            "projects": {},
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        self._prune_state(state)
        if state.get("degraded") and self._preserve_existing_state_file:
            return
        write_json(self.state_path, state, indent=2)
        self._preserve_existing_state_file = False

    def _prune_state(self, state: dict[str, Any]) -> None:
        state.setdefault("last_tick_error", None)
        projects = state.get("projects")
        if not isinstance(projects, dict):
            return
        for prow in projects.values():
            if not isinstance(prow, dict):
                continue
            attempts = prow.get("attempts")
            if not isinstance(attempts, dict):
                continue
            kept_attempts: dict[str, Any] = {}
            terminal_items: list[tuple[str, dict[str, Any]]] = []
            for att_key, att in attempts.items():
                if not isinstance(att, dict):
                    continue
                state_name = str(att.get("state") or "").lower()
                if state_name in NONTERMINAL_ATTEMPT_STATES:
                    kept_attempts[att_key] = att
                else:
                    # Preserve terminal attempts whose recovery is still in-flight to
                    # prevent pruning from dropping the in-progress reserve/enqueue record
                    # and allowing a duplicate recovery slot for the same run scope.
                    rec = att.get("recovery")
                    if isinstance(rec, dict) and rec.get("state") in ("reserved", "requested"):
                        kept_attempts[att_key] = att
                    else:
                        terminal_items.append((att_key, att))
            terminal_items.sort(key=lambda item: _attempt_sort_key(item[1]), reverse=True)
            for att_key, att in terminal_items[:MAX_TERMINAL_ATTEMPTS_PER_PROJECT]:
                kept_attempts[att_key] = att
            prow["attempts"] = kept_attempts
            keep_run_scopes = {
                str(att.get("run_scope_key"))
                for att in kept_attempts.values()
                if isinstance(att, dict) and att.get("run_scope_key")
            }
            stall = prow.get("stall")
            if isinstance(stall, dict) and stall.get("run_scope_key"):
                keep_run_scopes.add(str(stall.get("run_scope_key")))
            attempt_counts = prow.get("attempt_counts")
            if isinstance(attempt_counts, dict):
                prow["attempt_counts"] = {
                    str(key): value
                    for key, value in attempt_counts.items()
                    if str(key) in keep_run_scopes
                }
            recovery_slots = prow.get("recovery_slots")
            if isinstance(recovery_slots, dict):
                # R3-F6: Retain consumed live-scope recovery budgets even when the
                # corresponding attempt falls off the MAX_TERMINAL_ATTEMPTS_PER_PROJECT
                # pruning window.  A non-null slot means a recovery was already issued for
                # that run scope; removing it would silently allow a duplicate recovery.
                keep_run_scopes.update(
                    str(key)
                    for key, value in recovery_slots.items()
                    if value  # non-null means the slot was consumed
                )
                prow["recovery_slots"] = {
                    str(key): value
                    for key, value in recovery_slots.items()
                    if str(key) in keep_run_scopes
                }

    def _recover_interrupted(self) -> None:
        """Mark in-flight attempts interrupted after process restart."""
        projects = self._cached_state.get("projects") or {}
        now_iso = utc_now_iso()
        for prow in projects.values():
            if not isinstance(prow, dict):
                continue
            attempts = prow.get("attempts") or {}
            for att in attempts.values():
                if isinstance(att, dict) and att.get("state") == "running":
                    att["state"] = "interrupted"
                    att["completed_at"] = now_iso
                    att["reason"] = "process restarted while diagnostic was running"

    def state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._cached_state)

    def project_state(self, project_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            p = (self._cached_state.get("projects") or {}).get(project_id)
            return copy.deepcopy(p) if isinstance(p, dict) else None

    def clear_degraded(self) -> None:
        """Clear degraded mode only after cryptographically verifying the quarantine artifact.

        FR-3: corrupt_identity must be present; the quarantine filename must contain the
        identity as an exact trailing segment (not a substring of a different hash); and the
        SHA-256[:16] of the artifact bytes must match the stored corrupt_identity.  Any
        deviation causes clear_degraded to block and remain degraded (fail closed).
        """
        with self._lock:
            if not self._cached_state.get("degraded"):
                return
            corrupt_identity = self._cached_state.get("corrupt_identity")
            if not corrupt_identity:
                # FR-3: No verifiable identity stored — cannot confirm the artifact; remain
                # degraded.  This covers both absent and None corrupt_identity.
                self._emit_milestone(
                    "__controller__",
                    "OWNER_GATE",
                    occurrence_key="clear-degraded:no-corrupt-identity",
                    details={
                        "source": "watchdog",
                        "gate": "clear-degraded-blocked",
                        "reason": "corrupt_identity_absent",
                        "corrupt_identity": None,
                    },
                )
                return
            # FR-3: Exact segment match — the identity must be the last dash-separated
            # segment of the filename, not merely a substring anywhere in the name.
            matching_files = [
                f for f in self.runtime_root.glob("watchdog.json.corrupt-*")
                if f.name.endswith(f"-{corrupt_identity}")
            ]
            if not matching_files:
                self._emit_milestone(
                    "__controller__",
                    "OWNER_GATE",
                    occurrence_key=f"clear-degraded:no-quarantine:{corrupt_identity}",
                    details={
                        "source": "watchdog",
                        "gate": "clear-degraded-blocked",
                        "reason": "no_matching_quarantine_artifact",
                        "corrupt_identity": corrupt_identity,
                    },
                )
                return
            # FR-3: Content verification — recompute SHA-256[:16] of artifact bytes and
            # compare against stored corrupt_identity.  A readable filename is not enough;
            # the bytes must hash to the stored identity to confirm the correct artifact.
            verified = False
            for f in matching_files:
                try:
                    artifact_bytes = f.read_bytes()
                    if artifact_bytes and hashlib.sha256(artifact_bytes).hexdigest()[:16] == corrupt_identity:
                        verified = True
                        break
                except (OSError, IOError):
                    pass
            if not verified:
                readable = any(_is_quarantine_file_readable(f) for f in matching_files)
                reason = "quarantine_artifact_unreadable" if not readable else "quarantine_artifact_content_mismatch"
                self._emit_milestone(
                    "__controller__",
                    "OWNER_GATE",
                    occurrence_key=f"clear-degraded:unreadable:{corrupt_identity}",
                    details={
                        "source": "watchdog",
                        "gate": "clear-degraded-blocked",
                        "reason": reason,
                        "corrupt_identity": corrupt_identity,
                    },
                )
                return
            self._cached_state["degraded"] = False
            self._cached_state["degraded_reason"] = None
            self._save_state(self._cached_state)

    def record_tick_error(self, exc: Exception) -> None:
        """Persist the latest watchdog tick error for status visibility."""
        with self._lock:
            self._cached_state["last_tick_error"] = str(exc)
            self._save_state(self._cached_state)

    def _reap_overdue_attempts(self, now: datetime) -> None:
        """Fencing: reap any attempt whose deadline_at expired."""
        projects = self._cached_state.get("projects") or {}
        for pid, prow in projects.items():
            if not isinstance(prow, dict):
                continue
            attempts = prow.get("attempts") or {}
            cooldown_min = prow.get("cooldown_minutes", 30)
            for akey, att in attempts.items():
                if not isinstance(att, dict) or att.get("state") != "running":
                    continue
                deadline_dt = parse_utc(att.get("deadline_at"))
                if deadline_dt is not None and now >= deadline_dt:
                    att["state"] = "timed_out"
                    att["diagnosis"] = "unknown"
                    att["confidence"] = 0.0
                    att["reason"] = "diagnostic execution exceeded deadline"
                    att["completed_at"] = now.isoformat()
                    prow["fence_generation"] = int(prow.get("fence_generation", 0)) + 1
                    r_scope = att.get("run_scope_key")
                    if r_scope:
                        counts = prow.setdefault("attempt_counts", {})
                        counts[r_scope] = int(counts.get(r_scope, 0)) + 1
                    prow["last_diagnosis"] = "unknown"
                    cooldown_delta = (now.timestamp() + float(cooldown_min) * 60.0)
                    prow["cooldown_until"] = datetime.fromtimestamp(cooldown_delta, timezone.utc).isoformat()
                    # Release the single-flight slot so the next advance() tick can start a
                    # fresh diagnostic.  The blocked worker thread (if any) will eventually
                    # acquire the lock, find state != "running" and fence_token stale, and
                    # discard its late result — preserving existing late-result fencing.
                    self._threads.pop(pid, None)
                    self._emit_milestone(
                        pid,
                        "DIAGNOSTIC_RESULT",
                        task_id=att.get("task_id"),
                        occurrence_key=f"{akey}:result",
                        details={"source": "watchdog", "diagnosis": "unknown", "reason": att["reason"]},
                    )

    def _reconcile_recoveries(self) -> None:
        """Reconcile pending recoveries against control inbox and history."""
        projects = self._cached_state.get("projects") or {}
        inbox_dir = self.runtime_root / "control" / "inbox"
        history_dir = self.runtime_root / "control" / "history"

        for pid, prow in projects.items():
            if not isinstance(prow, dict):
                continue
            attempts = prow.get("attempts") or {}
            for att in attempts.values():
                if not isinstance(att, dict):
                    continue
                rec = att.get("recovery")
                if not isinstance(rec, dict):
                    continue
                rec_state = rec.get("state")
                if rec_state in ("completed", "blocked", "unresolved", "unknown"):
                    continue

                cid = rec.get("command_id")
                if not cid:
                    continue

                hist_file = history_dir / f"{cid}.json"
                inbox_file = inbox_dir / f"{cid}.json"

                if hist_file.is_file():
                    hdata = read_json(hist_file, {})
                    outcome = hdata.get("state") if isinstance(hdata, dict) else "unknown"
                    if outcome == "accepted":
                        rec["state"] = "completed"
                    elif outcome in ("blocked", "owner_gate"):
                        rec["state"] = "blocked"
                    else:
                        rec["state"] = "unknown"
                    rec["resolved_at"] = utc_now_iso()
                    rec["reason"] = f"reconciled from history: {outcome}"
                    prow["last_recovery_result"] = rec["state"]
                elif inbox_file.is_file():
                    if rec_state == "reserved":
                        rec["state"] = "requested"
                        rec["requested_at"] = utc_now_iso()
                else:
                    # Neither exists!
                    if rec_state == "reserved":
                        rec["state"] = "unresolved"
                        rec["resolved_at"] = utc_now_iso()
                        rec["reason"] = "command was reserved but never enqueued before crash"
                        prow["last_recovery_result"] = "unresolved"
                        self._emit_milestone(
                            pid,
                            "OWNER_GATE",
                            task_id=att.get("task_id"),
                            occurrence_key=f"{att.get('attempt_key')}:recovery-unresolved",
                            details={"source": "watchdog", "gate": "recovery-unresolved", "command_id": cid},
                        )

            # Reconcile execution loss recovery slots
            loss_slots = prow.get("execution_loss_slots") or {}
            for inv_key, slot in loss_slots.items():
                if not isinstance(slot, dict):
                    continue
                s_state = slot.get("state")
                s_phase = slot.get("phase")
                if s_state in ("completed", "blocked", "unresolved") or s_phase in ("completed", "blocked", "unresolved"):
                    continue
                cid = slot.get("command_id")
                if not cid:
                    continue
                hist_file = history_dir / f"{cid}.json"
                inbox_file = inbox_dir / f"{cid}.json"
                if hist_file.is_file():
                    hdata = read_json(hist_file, {})
                    outcome = hdata.get("state") if isinstance(hdata, dict) else "unknown"
                    if outcome == "accepted":
                        slot["state"] = "completed"
                        slot["phase"] = "completed"
                    elif outcome in ("blocked", "owner_gate"):
                        slot["state"] = "blocked"
                        slot["phase"] = "blocked"
                    else:
                        slot["state"] = "unknown"
                        slot["phase"] = "unknown"
                    slot["resolved_at"] = utc_now_iso()
                    slot["reason"] = f"reconciled from history: {outcome}"
                elif inbox_file.is_file():
                    slot["state"] = "requested"
                    slot["phase"] = "requested"
                    if not slot.get("requested_at"):
                        slot["requested_at"] = utc_now_iso()
                else:
                    if s_phase == "reconciled_pending_retry":
                        # Resumes phase with same command ID across daemon restarts / deferred ticks
                        continue
                    if s_state == "reserved" and s_phase == "reserved":
                        slot["state"] = "unresolved"
                        slot["phase"] = "unresolved"
                        slot["resolved_at"] = utc_now_iso()
                        slot["reason"] = "command was reserved but never enqueued before crash"

    @staticmethod
    def _reset_epoch_state(project_row: dict[str, Any]) -> None:
        """Discard watchdog-only state from a conclusively superseded epoch.

        Incrementing the fence invalidates an in-memory diagnostic that began
        under the old epoch.  Lifecycle state is intentionally not touched:
        recovery remains a normal guarded control command.
        """
        project_row["fence_generation"] = int(project_row.get("fence_generation", 0)) + 1
        project_row["attempts"] = {}
        project_row["attempt_counts"] = {}
        project_row["recovery_slots"] = {}
        project_row["cooldown_until"] = None
        project_row["last_diagnosis"] = None
        project_row["last_evidence_hash"] = None
        project_row["last_recovery_result"] = None
        project_row["owner_gate"] = None
        project_row.pop("stall", None)

    def _reconcile_recovery_epoch(
        self,
        project_row: dict[str, Any],
        assessment: StallAssessment,
    ) -> bool:
        """Persist the current epoch and reset only watchdog state on supersession.

        A current OWNER_GATE is authoritative owner/planner state and is never
        cleared here.  Legacy watchdog rows have no epoch binding; the first
        complete authoritative epoch safely supersedes only that watchdog data.
        """
        epoch = assessment.recovery_epoch
        if epoch is None:
            return False
        previous = project_row.get("recovery_epoch")
        previous_id = previous.get("id") if isinstance(previous, dict) else None
        current_id = epoch["id"]
        if previous_id == current_id:
            return False
        if assessment.lifecycle_state == "OWNER_GATE":
            # Do not let a newer incidental observation bypass a genuine
            # current owner decision.  Retain the pending watchdog data too.
            return False
        self._reset_epoch_state(project_row)
        project_row["recovery_epoch"] = copy.deepcopy(epoch)
        return True

    def _emit_milestone(
        self,
        project_id: str,
        milestone: str,
        *,
        task_id: Optional[str] = None,
        occurrence_key: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        if self.progress_channel is None or not hasattr(self.progress_channel, "emit"):
            return
        dtls = dict(details or {})
        dtls["source"] = "watchdog"
        try:
            self.progress_channel.emit(
                project_id,
                milestone,
                task_id=task_id,
                occurrence_key=occurrence_key,
                details=dtls,
            )
        except Exception:
            pass

    def consume_recovery_handoff(
        self,
        project_id: str,
        *,
        epoch_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Atomically inspect, clear, and persist consumption of a recovery handoff record.

        Returns the consumed recovery_handoff dict if matched and cleared, or None.
        """
        with self._lock:
            prow = self._cached_state.get("projects", {}).get(project_id)
            if not isinstance(prow, dict) or not prow.get("recovery_handoff"):
                return None
            handoff = prow["recovery_handoff"]
            handoff_epoch = handoff.get("recovery_epoch_id")
            if epoch_id and handoff_epoch and epoch_id != handoff_epoch:
                return None
            consumed = dict(handoff)
            prow.pop("recovery_handoff", None)
            self._save_state(self._cached_state)
            return consumed

    def advance(
        self,
        config_path: Path | str,
        summary: dict[str, Any],
        *,
        executor: Any = None,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Advance watchdog coordinator tick with strict non-blocking concurrency control."""
        if not self._advance_lock.acquire(blocking=False):
            return [{"skipped": "concurrent-advance"}]

        try:
            return self._advance_under_lock(config_path, summary, executor=executor, now=now)
        finally:
            self._advance_lock.release()

    def _advance_under_lock(
        self,
        config_path: Path | str,
        summary: dict[str, Any],
        *,
        executor: Any = None,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        tick_now = now or datetime.now(timezone.utc)
        now_iso = tick_now.isoformat()
        results: list[dict[str, Any]] = []

        from dev_orchestrator.config import load_projects_config
        try:
            config = load_projects_config(config_path)
        except Exception as exc:
            return [{"error": f"config load failed: {exc}"}]

        cfg_map = {
            str(p.get("project_id") or p.get("id") or ""): p
            for p in (config.get("projects") or [])
            if isinstance(p, dict)
        }

        executor_state: Optional[dict[str, Any]] = None
        if executor is not None and hasattr(executor, "state"):
            try:
                executor_state = executor.state()
            except Exception:
                executor_state = None

        with self._lock:
            self._reap_overdue_attempts(tick_now)
            self._reconcile_recoveries()

            degraded = bool(self._cached_state.get("degraded"))
            quarantined = self._cached_state.get("quarantined_projects") or {}

            projects_state = self._cached_state.setdefault("projects", {})

            for snapshot in summary.get("projects") or []:
                if not isinstance(snapshot, dict):
                    continue
                pid = str(snapshot.get("project_id") or snapshot.get("id") or "")
                if not pid:
                    continue

                pcfg = cfg_map.get(pid, {})
                policy = resolve_watchdog_policy(pcfg)

                prow = projects_state.setdefault(pid, {
                    "last_checked_at": None,
                    "last_progress_at": None,
                    "progress_fingerprint": "",
                    "signal_sources": {},
                    "activity_evidence": "available",
                    "activity_evidence_reason": None,
                    "fence_generation": 0,
                    "cooldown_minutes": policy["cooldown_minutes"],
                    "attempts": {},
                    "attempt_counts": {},
                    "recovery_slots": {},
                    "cooldown_until": None,
                    "last_diagnosis": None,
                    "last_evidence_hash": None,
                    "last_recovery_result": None,
                    "owner_gate": None,
                    "last_error": None,
                })
                prow["last_checked_at"] = now_iso
                prow["cooldown_minutes"] = policy["cooldown_minutes"]
                prow.setdefault("execution_loss", None)
                prow.setdefault("execution_loss_slots", {})
                prow["last_error"] = None

                if degraded or pid in quarantined or not policy["enabled"]:
                    results.append({"project_id": pid, "status": "skipped", "reason": "degraded_or_quarantined_or_disabled"})
                    continue

                # Execution Loss Observation & Evaluation
                loss_summary = None
                if policy.get("execution_loss_detection", True):
                    try:
                        from dev_orchestrator.core.execution_lifecycle import observe_executions
                        loss_summary = observe_executions(
                            self.runtime_root,
                            pid,
                            snapshot,
                            policy,
                            executor=executor,
                            now=tick_now,
                            liveness_probe=self._liveness_probe,
                            ai_execution_port=self.ai_execution_port,
                        )
                        prow["execution_loss"] = loss_summary
                    except Exception as exc:
                        prow["last_error"] = f"execution_loss_observation_failed: {exc}"
                        loss_summary = None

                # Emit execution loss milestones and update slots
                if loss_summary:
                    for f in loss_summary.get("active_findings", []):
                        if f.get("state") not in ("suppressed_live",):
                            inv_k = f.get("invariant_key") or f.get("code")
                            self._emit_milestone(
                                pid,
                                "EXECUTION_LOSS_DETECTED",
                                task_id=f.get("task_id"),
                                occurrence_key=f"{inv_k}:detected",
                                details={
                                    "invariant_key": inv_k,
                                    "code": f.get("code"),
                                    "source_request_id": f.get("source_request_id"),
                                },
                            )
                    for f in loss_summary.get("resolved_findings", []):
                        inv_k = f.get("invariant_key") or f.get("code")
                        self._emit_milestone(
                            pid,
                            "EXECUTION_LOSS_RESOLVED",
                            task_id=f.get("task_id"),
                            occurrence_key=f"{inv_k}:resolved",
                            details={
                                "invariant_key": inv_k,
                                "code": f.get("code"),
                                "reason": f.get("resolved_reason"),
                            },
                        )
                        slots = prow.setdefault("execution_loss_slots", {})
                        if inv_k in slots and slots[inv_k].get("phase") != "completed":
                            slots[inv_k]["phase"] = "completed"
                            slots[inv_k]["state"] = "completed"
                            slots[inv_k]["resolved_at"] = f.get("resolved_at") or utc_now_iso()

                # Trigger execution loss recovery for actionable findings
                if loss_summary and loss_summary.get("actionable_findings"):
                    for finding in loss_summary["actionable_findings"]:
                        f_state = finding.get("state")
                        if f_state in ("actionable_dead", "reconciled_pending_retry"):
                            self._trigger_execution_loss_recovery(
                                pcfg,
                                snapshot,
                                prow,
                                finding,
                                executor=executor,
                                now=tick_now,
                            )

                signals = collect_progress_signals(snapshot, self.runtime_root)
                _last_progress_at, _progress_fingerprint, signal_sources = signals

                if signal_sources.get("activity_evidence") != "available":
                    prow["last_error"] = "activity-evidence-unavailable"
                    results.append({"project_id": pid, "status": "evidence_unavailable", "reason": prow["activity_evidence_reason"]})
                    continue

                if prow.get("last_error") in (None, "activity-evidence-unavailable"):
                    prow["last_error"] = None

                assessment = evaluate_stall(
                    snapshot,
                    policy,
                    signals,
                    now=tick_now,
                    executor_state=executor_state,
                    runtime_root=self.runtime_root,
                )
                # READY_TO_RUN without an execution deliberately uses the
                # launch-gap subset of activity; persist that same evidence
                # rather than reporting a scratch worktree write as progress.
                prow["last_progress_at"] = assessment.last_progress_at
                prow["progress_fingerprint"] = assessment.progress_fingerprint
                prow["signal_sources"] = signal_sources
                prow["activity_evidence"] = assessment.activity_evidence
                prow["activity_evidence_reason"] = signal_sources.get("activity_evidence_reason")
                epoch_advanced = self._reconcile_recovery_epoch(prow, assessment)

                if not assessment.breached:
                    # Not breached; clean stall state
                    prow.pop("stall", None)
                    unresolved = (
                        loss_summary.get("unresolved_invariants", [])
                        if isinstance(loss_summary, dict)
                        else []
                    )
                    status_name = "execution_loss_detected" if unresolved else "ok"
                    results.append({
                        "project_id": pid,
                        "status": status_name,
                        "breached": False,
                        "epoch_advanced": epoch_advanced,
                        "unresolved_invariants": unresolved,
                    })
                    continue

                # Stall detected!
                prow["stall"] = {
                    "detected_at": now_iso,
                    "lifecycle_state": assessment.lifecycle_state,
                    "task_id": assessment.task_id,
                    "run_key": assessment.run_key,
                    "run_scope_key": assessment.run_scope_key,
                    "recovery_epoch_id": (
                        assessment.recovery_epoch.get("id") if assessment.recovery_epoch else None
                    ),
                    "threshold_minutes": round(assessment.threshold_seconds / 60.0, 1),
                    "no_progress_seconds": assessment.no_progress_seconds,
                }

                att_key = resolve_attempt_key(assessment.run_scope_key, assessment.progress_fingerprint)
                attempts = prow.setdefault("attempts", {})
                attempt_counts = prow.setdefault("attempt_counts", {})
                current_run_attempts = attempt_counts.get(assessment.run_scope_key, 0)

                # Guard 1: Deduplication
                if att_key in attempts:
                    existing = attempts[att_key]
                    if existing.get("state") == "completed" and existing.get("diagnosis") is not None:
                        self._check_and_trigger_recovery(pcfg, snapshot, prow, att_key, existing)
                    results.append({"project_id": pid, "status": "deduplicated", "attempt_key": att_key})
                    continue

                # Guard 2: Cooldown
                cooldown_until = parse_utc(prow.get("cooldown_until"))
                if cooldown_until is not None and tick_now < cooldown_until:
                    results.append({"project_id": pid, "status": "cooldown", "cooldown_until": prow["cooldown_until"]})
                    continue

                # Guard 3: Single-flight thread
                active_thread = self._threads.get(pid)
                if active_thread is not None and active_thread.is_alive():
                    results.append({"project_id": pid, "status": "single_flight_active"})
                    continue

                # Guard 4: Max attempts per run
                if current_run_attempts >= policy["max_attempts_per_run"]:
                    from dev_orchestrator.core.execution_intent import get_active_intent
                    active_intent = get_active_intent(self.runtime_root, pid)
                    epoch_id = assessment.recovery_epoch.get("id") if assessment.recovery_epoch else None
                    sh_cfg = pcfg.get("self_healing") if isinstance(pcfg, dict) else None
                    sh_enabled = sh_cfg.get("enabled", True) if isinstance(sh_cfg, dict) else True
                    if sh_enabled and active_intent and (not active_intent.get("recovery_epoch_id") or active_intent.get("recovery_epoch_id") == epoch_id):
                        prow["recovery_handoff"] = {
                            "state": "recovery_exhausted",
                            "reason": f"exhausted max_attempts_per_run ({policy['max_attempts_per_run']})",
                            "attempts": current_run_attempts,
                            "recovery_epoch_id": epoch_id,
                            "handed_off_at": now_iso,
                        }
                        self._emit_milestone(
                            pid,
                            "RECOVERY_HANDOFF",
                            task_id=assessment.task_id,
                            occurrence_key=f"{att_key}:max-attempts-handoff",
                            details={"source": "watchdog", "handoff": "max_attempts_exhausted", "attempts": current_run_attempts, "recovery_epoch_id": epoch_id},
                        )
                        results.append({"project_id": pid, "status": "max_attempts_handed_off"})
                        continue

                    prow["owner_gate"] = {
                        "source": "watchdog",
                        "state": "owner_gate",
                        "recovery_epoch_id": assessment.recovery_epoch.get("id") if assessment.recovery_epoch else None,
                        "reason": f"exhausted max_attempts_per_run ({policy['max_attempts_per_run']})",
                        "triggered_at": now_iso,
                    }
                    self._emit_milestone(
                        pid,
                        "OWNER_GATE",
                        task_id=assessment.task_id,
                        occurrence_key=f"{att_key}:max-attempts",
                        details={"source": "watchdog", "gate": "max_attempts_exhausted", "attempts": current_run_attempts},
                    )
                    results.append({"project_id": pid, "status": "max_attempts_exhausted"})
                    continue

                # Launch diagnostic attempt!
                generation = prow.get("fence_generation", 0)
                fence_token = f"{att_key}:{generation}"
                timeout_sec = policy["diagnostic_timeout_seconds"]
                deadline_at = datetime.fromtimestamp(tick_now.timestamp() + timeout_sec, timezone.utc).isoformat()

                attempt_record = {
                    "attempt_key": att_key,
                    "run_scope_key": assessment.run_scope_key,
                    "task_id": assessment.task_id,
                    "recovery_epoch_id": assessment.recovery_epoch.get("id") if assessment.recovery_epoch else None,
                    "fence_token": fence_token,
                    "state": "running",
                    "started_at": now_iso,
                    "deadline_at": deadline_at,
                    "completed_at": None,
                    "late_discarded_at": None,
                    "late_discarded_diagnosis": None,
                    "diagnosis": None,
                    "confidence": None,
                    "reason": None,
                    "evidence": None,
                    "evidence_hash": None,
                    "recommended_action": None,
                    "owner_gate_required": None,
                    "recovery": None,
                }
                attempts[att_key] = attempt_record

                self._emit_milestone(
                    pid,
                    "STALL_DETECTED",
                    task_id=assessment.task_id,
                    occurrence_key=f"{att_key}:stall",
                    details={"source": "watchdog", "no_progress_seconds": assessment.no_progress_seconds},
                )
                self._emit_milestone(
                    pid,
                    "DIAGNOSTIC_STARTED",
                    task_id=assessment.task_id,
                    occurrence_key=f"{att_key}:diag-start",
                    details={"source": "watchdog", "attempt_key": att_key},
                )

                # Spawn diagnostic thread
                worker_thread = threading.Thread(
                    target=self._run_diagnostic_worker,
                    args=(pcfg, snapshot, assessment, att_key, fence_token, timeout_sec),
                    name=f"watchdog-diag-{pid}",
                    daemon=True,
                )
                self._threads[pid] = worker_thread
                worker_thread.start()

                results.append({"project_id": pid, "status": "attempt_started", "attempt_key": att_key})

            self._cached_state["last_tick_error"] = None
            self._save_state(self._cached_state)

        return results

    def _run_diagnostic_worker(
        self,
        project_config: dict[str, Any],
        snapshot: dict[str, Any],
        assessment: StallAssessment,
        attempt_key: str,
        fence_token: str,
        timeout_seconds: int,
    ) -> None:
        pid = str(
            project_config.get("project_id")
            or project_config.get("id")
            or snapshot.get("project_id")
            or snapshot.get("id")
            or ""
        )
        try:
            evidence = collect_evidence(
                project_config,
                snapshot,
                self.runtime_root,
                ai_execution_port=self.ai_execution_port,
                timeout_seconds=timeout_seconds,
            )
            ev_hash = evidence_hash(evidence)
            diagnosis = classify_evidence(evidence, assessment)
        except Exception as exc:
            evidence = {"error": str(exc)}
            ev_hash = "error"
            diagnosis = Diagnosis(
                code="unknown",
                confidence=0.0,
                reason=f"diagnostic collection error: {exc}",
                recommended_action="inspect logs and require owner intervention",
                owner_gate_required=True,
                evidence=evidence,
                evidence_hash=ev_hash,
            )

        now_iso = utc_now_iso()
        with self._lock:
            prow = (self._cached_state.get("projects") or {}).get(pid)
            if not isinstance(prow, dict):
                return

            att = (prow.get("attempts") or {}).get(attempt_key)
            if not isinstance(att, dict) or att.get("state") != "running" or att.get("fence_token") != fence_token:
                # Late-result fencing: discard!
                if isinstance(att, dict):
                    att["late_discarded_at"] = now_iso
                    att["late_discarded_diagnosis"] = diagnosis.code
                    self._save_state(self._cached_state)
                return

            # Normal completion
            att["state"] = "completed"
            att["completed_at"] = now_iso
            att["diagnosis"] = diagnosis.code
            att["confidence"] = diagnosis.confidence
            att["reason"] = diagnosis.reason
            att["evidence"] = diagnosis.evidence
            att["evidence_hash"] = diagnosis.evidence_hash
            att["recommended_action"] = diagnosis.recommended_action
            att["owner_gate_required"] = diagnosis.owner_gate_required
            # B-INTEGRITY: seal the complete actuation record with an integrity hash so that
            # any tamper of diagnosis, completed_at, attempt_key, run_scope_key, or
            # evidence_hash is detectable before RESERVE at recovery time.
            att["record_integrity_hash"] = compute_record_integrity_hash(att)

            prow["last_diagnosis"] = diagnosis.code
            prow["last_evidence_hash"] = diagnosis.evidence_hash

            # A READY_TO_RUN launch-gap diagnosis is an explicit, high-confidence
            # recommendation to submit the existing guarded ``continue`` path.
            # Submit it while the completed diagnostic is still current, before
            # arming cooldown.  This does not launch a Worker directly:
            # _check_and_trigger_recovery writes the normal two-phase control
            # command, whose consumer revalidates the complete identity (including
            # current owner-gate, pause, repository, and active-execution facts)
            # before it can actuate.  Other diagnoses retain FR-1's deferred
            # actuation because their liveness bindings depend on a fresh tick.
            if diagnosis.code == "ready_to_run_unlaunched":
                self._check_and_trigger_recovery(
                    project_config, snapshot, prow, attempt_key, att,
                )

            # Increment attempt counts for run_scope_key
            counts = prow.setdefault("attempt_counts", {})
            counts[assessment.run_scope_key] = int(counts.get(assessment.run_scope_key, 0)) + 1

            # Arm cooldown
            cooldown_min = prow.get("cooldown_minutes", 30)
            cooldown_delta = datetime.now(timezone.utc).timestamp() + float(cooldown_min) * 60.0
            prow["cooldown_until"] = datetime.fromtimestamp(cooldown_delta, timezone.utc).isoformat()

            self._emit_milestone(
                pid,
                "DIAGNOSTIC_RESULT",
                task_id=assessment.task_id,
                occurrence_key=f"{attempt_key}:result",
                details={
                    "source": "watchdog",
                    "diagnosis": diagnosis.code,
                    "confidence": diagnosis.confidence,
                    "recommended_action": diagnosis.recommended_action,
                    "evidence_hash": diagnosis.evidence_hash,
                },
            )

            # FR-1: diagnoses other than the guarded READY_TO_RUN launch gap defer
            # recovery actuation to the next watchdog advance tick, where it uses
            # the latest projected snapshot.
            self._save_state(self._cached_state)

    def _check_and_trigger_recovery(
        self,
        project_config: dict[str, Any],
        snapshot: dict[str, Any],
        project_row: dict[str, Any],
        attempt_key: str,
        attempt_record: dict[str, Any],
    ) -> None:
        """Run two-phase reserve/enqueue recovery under explicit safety guards."""
        if attempt_record.get("state") != "completed" or attempt_record.get("diagnosis") is None:
            return
        if attempt_record.get("recovery") is not None:
            return  # Already reserved or executed for this attempt

        # An epoch transition invalidates old diagnostics before they can
        # reserve a slot or emit another gate.  Direct unit callers without an
        # epoch retain the compatibility path; durable coordinator records are
        # always bound when authoritative identity is available.
        current_epoch = project_row.get("recovery_epoch")
        current_epoch_id = current_epoch.get("id") if isinstance(current_epoch, dict) else None
        attempt_epoch_id = attempt_record.get("recovery_epoch_id")
        if current_epoch_id and attempt_epoch_id and current_epoch_id != attempt_epoch_id:
            return

        pid = str(project_config.get("project_id") or project_config.get("id") or "")
        policy = resolve_watchdog_policy(project_config)
        if not policy["auto_recovery"]:
            if attempt_record.get("owner_gate_required"):
                self._emit_owner_gate_once(pid, attempt_record, "auto_recovery_disabled")
            return

        diag_code = attempt_record.get("diagnosis")
        if diag_code not in (
            "agent_stalled", "process_dead", "reviewer_failed",
            "plan_reviewer_failed", "planner_failed", "ready_to_run_unlaunched",
        ):
            self._emit_owner_gate_once(pid, attempt_record, f"diagnosis_{diag_code}_requires_owner")
            return

        # FR-2: Validate persisted attempt schema and evidence integrity before any recovery.
        # All checks must pass before RESERVE to prevent acting on corrupted, tampered, or
        # structurally invalid attempt records loaded from disk.
        evidence = attempt_record.get("evidence") if isinstance(attempt_record.get("evidence"), dict) else None
        stored_ev_hash = attempt_record.get("evidence_hash")
        if not evidence:
            self._emit_owner_gate_once(pid, attempt_record, "malformed_attempt_no_evidence")
            return
        if not stored_ev_hash:
            self._emit_owner_gate_once(pid, attempt_record, "malformed_attempt_no_evidence_hash")
            return
        recomputed_ev_hash = evidence_hash(evidence)
        if recomputed_ev_hash != stored_ev_hash:
            # Quarantine the attempt to prevent repeated processing of a corrupted record.
            attempt_record["state"] = "quarantined"
            attempt_record["quarantine_reason"] = "evidence_hash_mismatch"
            self._emit_owner_gate_once(pid, attempt_record, "evidence_hash_mismatch")
            return
        proc_liveness = evidence.get("process_liveness") if isinstance(evidence.get("process_liveness"), dict) else {}
        ev_pid = proc_liveness.get("pid")
        process_alive = proc_liveness.get("process_alive")
        if diag_code in ("agent_stalled", "process_dead"):
            if ev_pid is None:
                self._emit_owner_gate_once(pid, attempt_record, "malformed_attempt_missing_pid")
                return
            if "process_alive" not in proc_liveness:
                self._emit_owner_gate_once(pid, attempt_record, "malformed_attempt_missing_process_alive")
                return

        current_worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
        current_lifecycle = str(
            snapshot.get("lifecycle_state") or snapshot.get("status") or snapshot.get("state") or ""
        ).strip().upper()

        recovery_action = "continue"
        recovery_target: dict[str, Any] = {}
        if diag_code == "ready_to_run_unlaunched":
            # Bind a launch-gap recovery to the same complete, current control
            # identity that the consumer will validate.  READY_TO_RUN alone is
            # insufficient: a current owner gate or pause must remain a hard
            # stop even if a stale projection still advertises READY_TO_RUN.
            from dev_orchestrator.control.surface import project_identity
            identity = project_identity(snapshot, self.runtime_root)
            required_identity = ("project_id", "repo_path", "branch", "head", "task_id")
            if any(not str(identity.get(field) or "").strip() for field in required_identity):
                self._emit_owner_gate_once(pid, attempt_record, "ready_to_run_identity_incomplete")
                return
            if identity.get("gate_id") is not None:
                self._emit_owner_gate_once(pid, attempt_record, "ready_to_run_owner_gate_present")
                return
            if identity.get("paused"):
                self._emit_owner_gate_once(pid, attempt_record, "ready_to_run_project_paused")
                return
            if str(identity.get("lifecycle_state") or "").upper() != "READY_TO_RUN":
                self._emit_owner_gate_once(pid, attempt_record, "ready_to_run_identity_lifecycle_changed")
                return
            if current_lifecycle != "READY_TO_RUN":
                self._emit_owner_gate_once(pid, attempt_record, "ready_to_run_lifecycle_changed")
                return
            if _has_active_execution(snapshot):
                self._emit_owner_gate_once(pid, attempt_record, "ready_to_run_execution_started")
                return
        if diag_code == "reviewer_failed":
            if current_lifecycle != "REVIEW_FAILED":
                self._emit_owner_gate_once(pid, attempt_record, "reviewer_failed_lifecycle_changed")
                return
            from dev_orchestrator.control.reconcile import resolve_retry_candidate
            retry_candidate, retry_reason = resolve_retry_candidate(snapshot, self.runtime_root, project_config)
            if retry_candidate is None:
                self._emit_owner_gate_once(pid, attempt_record, f"reviewer_retry_unavailable: {retry_reason}")
                return
            recovery_action = "retry"
            recovery_target = {"target_id": retry_candidate["target_id"]}

        if diag_code == "plan_reviewer_failed":
            if current_lifecycle != "PLAN_FAILED":
                self._emit_owner_gate_once(pid, attempt_record, "plan_reviewer_failed_lifecycle_changed")
                return
            recovery_action = "continue"
            recovery_target = {}

        if diag_code == "planner_failed":
            if current_lifecycle != "PLAN_FAILED":
                self._emit_owner_gate_once(pid, attempt_record, "planner_failed_lifecycle_changed")
                return
            recovery_action = "continue"
            recovery_target = {}

        if diag_code == "agent_stalled":
            # R3-F1: agent_stalled recovery is only valid when the current snapshot lifecycle
            # is a worker-expected state (EXECUTING / REMEDIATING).  A stale or reused alive
            # PID from a previous run during PLANNING / REVIEWING / REVIEWING_PLAN /
            # APPLYING_PLAN must not trigger recovery and must not enqueue a wd-continue
            # command that would start a second planner cycle.
            if current_lifecycle not in WORKER_EXPECTED_LIFECYCLE_STATES:
                self._emit_owner_gate_once(pid, attempt_record, "agent_stalled_non_worker_lifecycle")
                return
            # FR-2: diagnosis-consistent liveness — agent_stalled requires process_alive=True.
            if process_alive is not True:
                self._emit_owner_gate_once(pid, attempt_record, "evidence_inconsistent_agent_stalled_dead")
                return
            # R3-F1: Verify that the PID in evidence was in an active execution state when
            # evidence was collected (not a stale completed/stopped worker).
            ev_worker_state = str(proc_liveness.get("worker_state") or "").lower()
            if ev_worker_state and ev_worker_state not in ACTIVE_WORKER_STATES:
                self._emit_owner_gate_once(pid, attempt_record, "agent_stalled_worker_not_active")
                return
            # FR2B-LIVE-IDENTITY: require non-empty active current worker identity — an empty
            # or absent worker state is not a safe match for a stalled-worker diagnosis.
            # Checked before PID so that an absent worker dict (no state, no PID) fails here.
            current_worker_state = str(current_worker.get("state") or "").lower()
            if not current_worker_state or current_worker_state not in ACTIVE_WORKER_STATES:
                self._emit_owner_gate_once(pid, attempt_record, "agent_stalled_current_worker_not_active")
                return
            # R3-F2: Re-probe: current snapshot PID must match the evidence PID to ensure
            # we are recovering the same execution, not a different one that reused the slot.
            # FR-2B: Absent current PID is not a safe match — fail closed.
            current_pid = current_worker.get("pid")
            if current_pid is None:
                self._emit_owner_gate_once(pid, attempt_record, "agent_stalled_current_pid_absent")
                return
            if current_pid != ev_pid:
                self._emit_owner_gate_once(pid, attempt_record, "agent_stalled_pid_mismatch")
                return
            # STABLE-IDENTITY: fail closed unless both evidence and current snapshot carry a
            # non-empty, matching started_at.  PID alone cannot distinguish a reused PID
            # (OS recycled the slot for an unrelated process) or a race where a new execution
            # began under the same PID.  Missing started_at in either direction is not a safe
            # match — require both sides to be present and equal.
            ev_started_at = str(proc_liveness.get("started_at") or "").strip()
            current_started_at = str(current_worker.get("started_at") or "").strip()
            if not ev_started_at:
                self._emit_owner_gate_once(pid, attempt_record, "agent_stalled_evidence_started_at_absent")
                return
            if not current_started_at:
                self._emit_owner_gate_once(pid, attempt_record, "agent_stalled_current_started_at_absent")
                return
            if ev_started_at != current_started_at:
                self._emit_owner_gate_once(pid, attempt_record, "agent_stalled_started_at_mismatch")
                return
            try:
                _live_alive: Optional[bool] = self._liveness_probe(current_pid)
            except Exception:
                _live_alive = None
            if _live_alive is not True:
                self._emit_owner_gate_once(pid, attempt_record, "agent_stalled_pid_not_alive_at_recovery")
                return

        if diag_code == "process_dead":
            # Use the actual PID probe from diagnostic evidence, not snapshot.worker.process_alive
            # which is derived from the executor ledger and may be stale.
            if process_alive is not False:
                self._emit_owner_gate_once(pid, attempt_record, "process_alive_ambiguous")
                return
            # R3-F2: Cross-check evidence PID against the current snapshot worker PID.
            # If the PID changed (new execution started after diagnosis), the stored evidence
            # belongs to a different run and must not trigger recovery for the new one.
            # FR-2B: Absent current PID is not a safe match — require stable execution
            # binding; fail closed when the current identity cannot be confirmed.
            current_pid = current_worker.get("pid")
            if current_pid is None:
                self._emit_owner_gate_once(pid, attempt_record, "process_dead_current_pid_absent")
                return
            if current_pid != ev_pid:
                self._emit_owner_gate_once(pid, attempt_record, "process_dead_pid_mismatch")
                return
            # STABLE-IDENTITY: fail closed unless both evidence and current snapshot carry a
            # non-empty, matching started_at.  Missing started_at in either direction is not
            # a safe match — a reused PID or new execution after the dead-process snapshot
            # may share the same PID with a completely different started_at.
            ev_started_at = str(proc_liveness.get("started_at") or "").strip()
            current_started_at = str(current_worker.get("started_at") or "").strip()
            if not ev_started_at:
                self._emit_owner_gate_once(pid, attempt_record, "process_dead_evidence_started_at_absent")
                return
            if not current_started_at:
                self._emit_owner_gate_once(pid, attempt_record, "process_dead_current_started_at_absent")
                return
            if ev_started_at != current_started_at:
                self._emit_owner_gate_once(pid, attempt_record, "process_dead_started_at_mismatch")
                return
            # FR2B-LIVE-IDENTITY: re-probe live liveness at recovery time to independently
            # confirm the process is still dead.  If the process has since come back alive
            # (PID reuse, unexpected restart), the stale evidence must not trigger recovery.
            # If the probe itself is unavailable, fail closed.
            try:
                _live_alive = self._liveness_probe(ev_pid)
            except Exception:
                _live_alive = None
            if _live_alive is not False:
                _pd_reason = (
                    "process_dead_pid_alive_at_recovery" if _live_alive is True
                    else "process_dead_liveness_probe_unavailable"
                )
                self._emit_owner_gate_once(pid, attempt_record, _pd_reason)
                return

        # FR-2A: completed_at must be present, parseable, not materially future-dated, and
        # within the configured evidence/recovery age window.  Fail closed on any deviation
        # to prevent acting on missing, tampered, or stale diagnostic timestamps.
        completed_at_str = attempt_record.get("completed_at")
        if not completed_at_str:
            self._emit_owner_gate_once(pid, attempt_record, "evidence_missing_completed_at")
            return
        completed_dt = parse_utc(completed_at_str)
        if completed_dt is None:
            self._emit_owner_gate_once(pid, attempt_record, "evidence_unparseable_completed_at")
            return
        now_utc = datetime.now(timezone.utc)
        future_delta = (completed_dt - now_utc).total_seconds()
        if future_delta > RECOVERY_COMPLETED_AT_CLOCK_SKEW_S:
            self._emit_owner_gate_once(pid, attempt_record, "evidence_completed_at_future")
            return
        cooldown_min = float(project_row.get("cooldown_minutes") or policy.get("cooldown_minutes") or 30)
        age_seconds = (now_utc - completed_dt).total_seconds()
        if age_seconds > cooldown_min * 60:
            self._emit_owner_gate_once(pid, attempt_record, "evidence_stale_beyond_cooldown")
            return

        # Check repository truth
        repo_path = str(project_config.get("repo_path") or snapshot.get("repo_path") or "")
        truth = read_repository_truth(repo_path)
        if not truth.valid:
            self._emit_owner_gate_once(pid, attempt_record, f"repository_invalid: {truth.error}")
            return
        if truth.dirty:
            self._emit_owner_gate_once(pid, attempt_record, "repository_dirty")
            return

        # Check one-recovery-per-run-scope
        r_scope = attempt_record.get("run_scope_key")
        recovery_slots = project_row.setdefault("recovery_slots", {})
        if recovery_slots.get(r_scope):
            self._emit_owner_gate_once(pid, attempt_record, "recovery_slot_already_consumed")
            return

        # B-INTEGRITY: verify internal key consistency and complete record integrity before
        # RESERVE.  A forged, tampered, or substituted record must never consume a recovery
        # slot.  Fail closed: missing hash → OWNER_GATE; mismatch or key drift → quarantine.
        internal_att_key = attempt_record.get("attempt_key")
        if internal_att_key != attempt_key:
            attempt_record["state"] = "quarantined"
            attempt_record["quarantine_reason"] = "attempt_key_mismatch"
            self._emit_owner_gate_once(pid, attempt_record, "attempt_key_mismatch")
            return
        stored_rih = attempt_record.get("record_integrity_hash")
        if not stored_rih:
            self._emit_owner_gate_once(pid, attempt_record, "malformed_attempt_no_record_hash")
            return
        recomputed_rih = compute_record_integrity_hash(attempt_record)
        if recomputed_rih != stored_rih:
            attempt_record["state"] = "quarantined"
            attempt_record["quarantine_reason"] = "record_integrity_hash_mismatch"
            self._emit_owner_gate_once(pid, attempt_record, "record_integrity_hash_mismatch")
            return

        cid = f"{WATCHDOG_COMMAND_PREFIX}{attempt_key}"
        now_iso = utc_now_iso()

        # 1. RESERVE
        attempt_record["recovery"] = {
            "action": recovery_action,
            "state": "reserved",
            "command_id": cid,
            "reserved_at": now_iso,
            "requested_at": None,
            "resolved_at": None,
            "reason": f"automatic {recovery_action} recovery for {diag_code}",
            "target": copy.deepcopy(recovery_target),
        }
        recovery_slots[r_scope] = cid
        self._save_state(self._cached_state)

        # 2. ENQUEUE
        try:
            from dev_orchestrator.control.surface import project_identity
            from dev_orchestrator.core.control_commands import submit_control_command
            submit_control_command(
                self.runtime_root, pid, recovery_action, command_id=cid,
                expected=project_identity(snapshot, self.runtime_root),
                target=recovery_target,
            )
            attempt_record["recovery"]["state"] = "requested"
            attempt_record["recovery"]["requested_at"] = utc_now_iso()
            self._emit_milestone(
                pid,
                "RECOVERY_STARTED",
                task_id=attempt_record.get("task_id"),
                occurrence_key=f"{attempt_key}:recovery-start",
                details={"source": "watchdog", "command_id": cid, "action": recovery_action},
            )
        except Exception as exc:
            attempt_record["recovery"]["state"] = "blocked"
            attempt_record["recovery"]["resolved_at"] = utc_now_iso()
            attempt_record["recovery"]["reason"] = f"enqueue failed: {exc}"
            self._emit_owner_gate_once(pid, attempt_record, f"recovery_enqueue_failed: {exc}")

    def _emit_owner_gate_once(self, project_id: str, attempt_record: dict[str, Any], reason: str) -> None:
        from dev_orchestrator.core.execution_intent import get_active_intent
        from dev_orchestrator.core.diagnostics import classify_failure_class
        pcfg = None
        cfg_path = self.runtime_root.parent / "config" / "projects.json"
        if cfg_path.is_file():
            try:
                import json
                pdata = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
                for p in pdata.get("projects", []):
                    if isinstance(p, dict) and (p.get("project_id") == project_id or p.get("id") == project_id):
                        pcfg = p
                        break
            except Exception:
                pcfg = None
        sh_cfg = pcfg.get("self_healing") if isinstance(pcfg, dict) else None
        sh_enabled = sh_cfg.get("enabled", True) if isinstance(sh_cfg, dict) else True

        active_intent = get_active_intent(self.runtime_root, project_id)
        if sh_enabled and active_intent:
            epoch_id = attempt_record.get("recovery_epoch_id")
            intent_epoch = active_intent.get("recovery_epoch_id")
            if not intent_epoch or intent_epoch == epoch_id:
                fail_class = classify_failure_class("", reason)
                if fail_class in {"transient_infrastructure", "recoverable_orchestration", "recovery_exhausted"}:
                    att_key = attempt_record.get("attempt_key", "")
                    reason_slug = re.sub(r"[^a-z0-9-]", "-", str(reason or "reason").lower()).strip("-")[:32] or "reason"
                    self._emit_milestone(
                        project_id,
                        "RECOVERY_HANDOFF",
                        task_id=attempt_record.get("task_id"),
                        occurrence_key=f"{att_key}:suppressed-gate:{reason_slug}",
                        details={
                            "source": "watchdog",
                            "gate_suppressed": True,
                            "reason": reason,
                            "failure_class": fail_class,
                            "diagnosis": attempt_record.get("diagnosis"),
                        },
                    )
                    prow = self._cached_state.setdefault("projects", {}).setdefault(project_id, {})
                    prow["recovery_handoff"] = {
                        "state": "recovery_exhausted",
                        "reason": reason,
                        "attempts": attempt_record.get("attempt_key"),
                        "recovery_epoch_id": epoch_id,
                        "handed_off_at": utc_now_iso(),
                    }
                    self._save_state(self._cached_state)
                    return

        att_key = attempt_record.get("attempt_key", "")
        reason_slug = re.sub(r"[^a-z0-9-]", "-", str(reason or "reason").lower()).strip("-")[:32] or "reason"
        self._emit_milestone(
            project_id,
            "OWNER_GATE",
            task_id=attempt_record.get("task_id"),
            occurrence_key=f"{att_key}:gate:{reason_slug}",
            details={
                "source": "watchdog",
                "gate": "recovery-blocked",
                "reason": reason,
                "diagnosis": attempt_record.get("diagnosis"),
                "evidence_hash": attempt_record.get("evidence_hash"),
            },
        )

    def _trigger_execution_loss_recovery(
        self,
        project_config: dict[str, Any],
        snapshot: dict[str, Any],
        project_row: dict[str, Any],
        finding: dict[str, Any],
        *,
        executor: Any = None,
        now: Optional[datetime] = None,
    ) -> None:
        pid = str(snapshot.get("project_id") or snapshot.get("id") or "")
        invariant_key = str(finding.get("invariant_key") or "")
        if not pid or not invariant_key:
            return

        if finding.get("code") == "RUNNING_WITHOUT_PROVIDER_OUTPUT":
            return

        from dev_orchestrator.core.execution_lifecycle import update_finding_state

        policy = resolve_watchdog_policy(project_config)
        if not policy.get("auto_recovery", False):
            return

        if snapshot.get("paused") is True:
            return

        repo_path = str(project_config.get("repo_path") or snapshot.get("repo_path") or "")
        truth = read_repository_truth(repo_path)
        if not truth.valid or truth.dirty:
            self._emit_milestone(
                pid,
                "OWNER_GATE",
                task_id=finding.get("task_id"),
                occurrence_key=f"{invariant_key}:dirty_or_invalid_repo",
                details={
                    "source": "watchdog",
                    "gate": "recovery-blocked",
                    "reason": "repository_dirty_or_invalid",
                },
            )
            return

        launch_anchor = finding.get("launch_anchor") or {}
        expected_head = launch_anchor.get("head") or launch_anchor.get("git_head")
        if expected_head and truth.head and truth.head != expected_head:
            finding["state"] = "escalated"
            update_finding_state(
                self.runtime_root,
                project_id=pid,
                lineage_key=finding.get("lineage_key", ""),
                invariant_key=invariant_key,
                finding_id=finding.get("finding_id"),
                state="escalated",
            )
            self._emit_milestone(
                pid,
                "EXECUTION_LOSS_ESCALATED",
                task_id=finding.get("task_id"),
                occurrence_key=f"{invariant_key}:head-mismatch",
                details={
                    "source": "watchdog",
                    "gate": "head_mismatch",
                    "expected_head": expected_head,
                    "observed_head": truth.head,
                },
            )
            return

        max_recoveries = int(policy.get("execution_loss_max_recoveries", 3))
        backoff_seconds = float(policy.get("execution_loss_backoff_seconds", 30))
        slots = project_row.setdefault("execution_loss_slots", {})
        existing_slot = slots.get(invariant_key)

        eval_now = now or datetime.now(timezone.utc)
        now_iso = eval_now.isoformat()

        recovery_count = int(finding.get("recovery_attempts", 0))
        if recovery_count >= max_recoveries:
            finding["state"] = "escalated"
            update_finding_state(
                self.runtime_root,
                project_id=pid,
                lineage_key=finding.get("lineage_key", ""),
                invariant_key=invariant_key,
                finding_id=finding.get("finding_id"),
                state="escalated",
                recovery_attempts=recovery_count,
            )
            self._emit_milestone(
                pid,
                "EXECUTION_LOSS_ESCALATED",
                task_id=finding.get("task_id"),
                occurrence_key=f"{invariant_key}:max_recoveries_exhausted",
                details={
                    "source": "watchdog",
                    "gate": "max_recoveries_exhausted",
                    "attempts": recovery_count,
                },
            )
            return

        if existing_slot and existing_slot.get("phase") != "reconciled_pending_retry":
            last_req = parse_utc(existing_slot.get("requested_at") or existing_slot.get("reserved_at"))
            if last_req is not None and (eval_now - last_req).total_seconds() < backoff_seconds:
                return

        from dev_orchestrator.core.execution_lifecycle import resolve_execution_liveness
        liveness = resolve_execution_liveness(
            self.runtime_root,
            pid,
            source_request_id=finding.get("source_request_id"),
            execution_id=finding.get("execution_id"),
            engine_handle=finding.get("engine_handle"),
            worker_pid=finding.get("worker_pid"),
            started_at=finding.get("started_at"),
            task_id=finding.get("task_id"),
            snapshot=snapshot,
            executor=executor,
            now=eval_now,
            liveness_probe=self._liveness_probe,
            ai_execution_port=self.ai_execution_port,
        )
        if liveness.get("verdict") != "dead":
            if liveness.get("verdict") == "alive":
                finding["state"] = "suppressed_live"
            else:
                finding["state"] = "unresolved_unknown"
            update_finding_state(
                self.runtime_root,
                project_id=pid,
                lineage_key=finding.get("lineage_key", ""),
                invariant_key=invariant_key,
                finding_id=finding.get("finding_id"),
                state=finding["state"],
            )
            return

        if executor is None or not hasattr(executor, "reconcile_execution_loss"):
            project_row["last_error"] = "execution_reconciliation_unavailable"
            return

        exec_state = executor.state() if hasattr(executor, "state") else {}
        target_srid = finding.get("source_request_id")
        for rec in (exec_state.get("executions") or {}).values():
            if not isinstance(rec, dict):
                continue
            if str(rec.get("project_id") or "") != pid:
                continue
            st = str(rec.get("state") or "").lower()
            if st in {"launching", "running"}:
                if str(rec.get("source_request_id") or "") != str(target_srid or ""):
                    project_row["last_error"] = "duplicate_execution_present"
                    return

        cid = f"{WATCHDOG_COMMAND_PREFIX}xl-{invariant_key}"

        # If already reconciled and pending retry: resume from step 3
        if existing_slot and existing_slot.get("phase") == "reconciled_pending_retry":
            refreshed_executor_state = executor.state() if hasattr(executor, "state") else {}
            if _has_active_execution(snapshot, refreshed_executor_state):
                return
        else:
            # 1. RESERVE
            finding["recovery_attempts"] = recovery_count + 1
            finding["state"] = "recovery_reserved"
            slots[invariant_key] = {
                "invariant_key": invariant_key,
                "command_id": cid,
                "lineage_key": finding.get("lineage_key"),
                "source_request_id": target_srid,
                "phase": "reserved",
                "state": "reserved",
                "reserved_at": now_iso,
                "reconciled_at": None,
                "requested_at": None,
                "resolved_at": None,
                "reason": f"execution loss recovery for {invariant_key}",
            }
            self._save_state(self._cached_state)
            update_finding_state(
                self.runtime_root,
                project_id=pid,
                lineage_key=finding.get("lineage_key", ""),
                invariant_key=invariant_key,
                finding_id=finding.get("finding_id"),
                state="recovery_reserved",
                recovery_attempts=finding["recovery_attempts"],
            )

            # 2. CALL executor.reconcile_execution_loss
            reconcile_res = executor.reconcile_execution_loss(
                target_srid,
                project_id=pid,
                invariant_key=invariant_key,
                command_id=cid,
                expected_anchor=launch_anchor,
                evidence=liveness,
            )
            status = reconcile_res.get("status")
            if status in ("reconciled", "already_reconciled"):
                slots[invariant_key]["phase"] = "reconciled_pending_retry"
                slots[invariant_key]["state"] = "reconciled_pending_retry"
                slots[invariant_key]["reconciled_at"] = utc_now_iso()
                finding["state"] = "reconciled_pending_retry"
                self._save_state(self._cached_state)
                update_finding_state(
                    self.runtime_root,
                    project_id=pid,
                    lineage_key=finding.get("lineage_key", ""),
                    invariant_key=invariant_key,
                    finding_id=finding.get("finding_id"),
                    state="reconciled_pending_retry",
                )
            elif status == "superseded_by_terminal":
                slots[invariant_key]["state"] = "completed"
                slots[invariant_key]["phase"] = "completed"
                slots[invariant_key]["resolved_at"] = utc_now_iso()
                finding["state"] = "resolved"
                self._save_state(self._cached_state)
                update_finding_state(
                    self.runtime_root,
                    project_id=pid,
                    lineage_key=finding.get("lineage_key", ""),
                    invariant_key=invariant_key,
                    finding_id=finding.get("finding_id"),
                    state="resolved",
                    resolved_at=utc_now_iso(),
                    resolved_reason="superseded by terminal execution",
                )
                self._emit_milestone(
                    pid,
                    "EXECUTION_LOSS_RESOLVED",
                    task_id=finding.get("task_id"),
                    occurrence_key=f"{invariant_key}:resolved",
                    details={"invariant_key": invariant_key, "reason": "superseded_by_terminal"},
                )
                return
            elif status == "conflict":
                slots[invariant_key]["state"] = "blocked"
                slots[invariant_key]["phase"] = "blocked"
                finding["state"] = "unresolved_unknown"
                self._save_state(self._cached_state)
                update_finding_state(
                    self.runtime_root,
                    project_id=pid,
                    lineage_key=finding.get("lineage_key", ""),
                    invariant_key=invariant_key,
                    finding_id=finding.get("finding_id"),
                    state="unresolved_unknown",
                )
                return
            else:
                slots[invariant_key]["state"] = "blocked"
                slots[invariant_key]["phase"] = "blocked"
                self._save_state(self._cached_state)
                return

            # 3. Reload executor state and evaluate strict _has_active_execution
            refreshed_executor_state = executor.state() if hasattr(executor, "state") else {}
            if _has_active_execution(snapshot, refreshed_executor_state):
                return

        # 4. ENQUEUE
        try:
            from dev_orchestrator.control.surface import project_identity
            from dev_orchestrator.core.control_commands import submit_control_command
            submit_control_command(
                self.runtime_root,
                pid,
                "continue",
                command_id=cid,
                expected=project_identity(snapshot, self.runtime_root),
                target={},
                source="watchdog",
            )
            slots[invariant_key]["phase"] = "requested"
            slots[invariant_key]["state"] = "requested"
            slots[invariant_key]["requested_at"] = utc_now_iso()
            finding["state"] = "recovering"
            self._save_state(self._cached_state)
            update_finding_state(
                self.runtime_root,
                project_id=pid,
                lineage_key=finding.get("lineage_key", ""),
                invariant_key=invariant_key,
                finding_id=finding.get("finding_id"),
                state="recovering",
            )
            self._emit_milestone(
                pid,
                "EXECUTION_LOSS_RECOVERY_STARTED",
                task_id=finding.get("task_id"),
                occurrence_key=f"{invariant_key}:recovery-start",
                details={"source": "watchdog", "command_id": cid, "action": "continue", "invariant_key": invariant_key},
            )
        except Exception as exc:
            slots[invariant_key]["state"] = "blocked"
            slots[invariant_key]["phase"] = "blocked"
            slots[invariant_key]["resolved_at"] = utc_now_iso()
            slots[invariant_key]["reason"] = f"enqueue failed: {exc}"
            self._save_state(self._cached_state)
