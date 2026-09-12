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
    DIAGNOSIS_CODES,
    Diagnosis,
    classify_evidence,
    collect_evidence,
    evidence_hash,
)
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

WATCHDOG_SCHEMA_VERSION = 1
WATCHDOG_STATE_FILE = "watchdog.json"
WATCHDOG_COMMAND_PREFIX = "wd-"
MAX_TERMINAL_ATTEMPTS_PER_PROJECT = 20
NONTERMINAL_ATTEMPT_STATES = frozenset({"running", "reserved", "requested"})

ACTIVE_LIFECYCLE_STATES = frozenset({
    "PLANNING",
    "REVIEWING_PLAN",
    "APPLYING_PLAN",
    "EXECUTING",
    "REVIEWING",
    "REMEDIATING",
})

LIFECYCLE_OVERRIDE_FAMILY = {
    "PLANNING": "PLANNING",
    "REVIEWING_PLAN": "PLANNING",
    "APPLYING_PLAN": "PLANNING",
    "EXECUTING": "EXECUTING",
    "REVIEWING": "REVIEWING",
    "REMEDIATING": "REMEDIATING",
}

WATCHDOG_MILESTONES = frozenset({
    "STALL_DETECTED",
    "DIAGNOSTIC_STARTED",
    "DIAGNOSTIC_RESULT",
    "RECOVERY_STARTED",
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
        }
    return {
        "enabled": cfg.get("enabled", True) if isinstance(cfg.get("enabled"), bool) else True,
        "no_progress_threshold_minutes": cfg.get("no_progress_threshold_minutes", 15),
        "lifecycle_overrides": dict(cfg.get("lifecycle_overrides") or {}),
        "cooldown_minutes": cfg.get("cooldown_minutes", 30),
        "max_attempts_per_run": cfg.get("max_attempts_per_run", 3),
        "diagnostic_timeout_seconds": cfg.get("diagnostic_timeout_seconds", 120),
        "auto_recovery": cfg.get("auto_recovery", False) if isinstance(cfg.get("auto_recovery"), bool) else False,
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
                    if rec_state in ("completed", "failed", "cancelled", "handoff", "blocked"):
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

    # Determine latest durable progress timestamp
    last_progress_at = _latest_timestamp_value((safe_last_activity, latest_role_timestamp, latest_progress_timestamp))

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


def evaluate_stall(
    snapshot: dict[str, Any],
    policy: dict[str, Any],
    signals: tuple[str | None, str, dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    executor_state: dict[str, Any] | None = None,
) -> StallAssessment:
    """Sole consumer of age; evaluates threshold breach against durable signals."""
    last_progress_at, progress_fingerprint, signal_sources = signals
    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
    lifecycle_state = str(snapshot.get("lifecycle_state") or snapshot.get("status") or snapshot.get("state") or "").strip().upper()
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    task_id = telemetry.get("task_id") or snapshot.get("task_id")

    monitored = lifecycle_state in ACTIVE_LIFECYCLE_STATES
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
    )


class WatchdogCoordinator:
    """Daemon-owned progress watchdog and automatic diagnostics coordinator."""

    def __init__(
        self,
        runtime_root: Path | str,
        *,
        ai_execution_port: Any = None,
        progress_channel: Any = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.state_path = self.runtime_root / WATCHDOG_STATE_FILE
        self.ai_execution_port = ai_execution_port
        self.progress_channel = progress_channel
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

        for pid, prow in projects.items():
            if not isinstance(prow, dict) or not isinstance(prow.get("attempts"), dict):
                quarantined_projects[pid] = "malformed project row structure"
                continue
            validated_projects[pid] = prow

        return {
            "version": version,
            "degraded": bool(data.get("degraded", False)),
            "degraded_reason": data.get("degraded_reason"),
            "last_tick_error": data.get("last_tick_error"),
            "quarantined_projects": quarantined_projects,
            "projects": validated_projects,
        }

    def _quarantine_corrupt_state(self, reason: str, raw_bytes: bytes) -> dict[str, Any]:
        stamp = utc_now_iso().replace(":", "-")
        quarantine_file = self.runtime_root / f"watchdog.json.corrupt-{stamp}"
        self._preserve_existing_state_file = True
        try:
            if raw_bytes:
                quarantine_file.write_bytes(raw_bytes)
            elif self.state_path.exists():
                shutil.copy2(self.state_path, quarantine_file)
        except OSError:
            pass

        file_hash = hashlib.sha256(raw_bytes).hexdigest()[:16]
        if self.progress_channel is not None and hasattr(self.progress_channel, "emit"):
            try:
                self.progress_channel.emit(
                    "__controller__",
                    "OWNER_GATE",
                    occurrence_key=f"corrupt-state:{file_hash}",
                    message=f"Watchdog state corrupt ({reason}); degraded monitor-only mode active",
                    details={"source": "watchdog", "gate": "corrupt-state", "reason": reason, "hash": file_hash},
                )
            except Exception:
                pass

        return {
            "version": WATCHDOG_SCHEMA_VERSION,
            "degraded": True,
            "degraded_reason": reason,
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
        """Explicitly clear degraded mode and persist a fresh valid state."""
        with self._lock:
            if self._cached_state.get("degraded"):
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
            # Re-read state in case of out-of-band edit
            if self._cached_state.get("degraded") and not self._cached_state.get("degraded_reason"):
                pass
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

                if degraded or pid in quarantined or not policy["enabled"]:
                    results.append({"project_id": pid, "status": "skipped", "reason": "degraded_or_quarantined_or_disabled"})
                    continue

                signals = collect_progress_signals(snapshot, self.runtime_root)
                last_progress_at, progress_fingerprint, signal_sources = signals

                prow["last_progress_at"] = last_progress_at
                prow["progress_fingerprint"] = progress_fingerprint
                prow["signal_sources"] = signal_sources
                prow["activity_evidence"] = signal_sources.get("activity_evidence", "available")
                prow["activity_evidence_reason"] = signal_sources.get("activity_evidence_reason")

                if signal_sources.get("activity_evidence") != "available":
                    prow["last_error"] = "activity-evidence-unavailable"
                    results.append({"project_id": pid, "status": "evidence_unavailable", "reason": prow["activity_evidence_reason"]})
                    continue

                prow["last_error"] = None

                assessment = evaluate_stall(
                    snapshot,
                    policy,
                    signals,
                    now=tick_now,
                    executor_state=executor_state,
                )

                if not assessment.breached:
                    # Not breached; clean stall state
                    prow.pop("stall", None)
                    results.append({"project_id": pid, "status": "ok", "breached": False})
                    continue

                # Stall detected!
                prow["stall"] = {
                    "detected_at": now_iso,
                    "lifecycle_state": assessment.lifecycle_state,
                    "task_id": assessment.task_id,
                    "run_key": assessment.run_key,
                    "run_scope_key": assessment.run_scope_key,
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
                    prow["owner_gate"] = {
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

            prow["last_diagnosis"] = diagnosis.code
            prow["last_evidence_hash"] = diagnosis.evidence_hash

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

            # Check and trigger safe automatic recovery if applicable
            self._check_and_trigger_recovery(project_config, snapshot, prow, attempt_key, att)
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

        pid = str(project_config.get("project_id") or project_config.get("id") or "")
        policy = resolve_watchdog_policy(project_config)
        if not policy["auto_recovery"]:
            if attempt_record.get("owner_gate_required"):
                self._emit_owner_gate_once(pid, attempt_record, "auto_recovery_disabled")
            return

        diag_code = attempt_record.get("diagnosis")
        if diag_code not in ("agent_stalled", "process_dead"):
            self._emit_owner_gate_once(pid, attempt_record, f"diagnosis_{diag_code}_requires_owner")
            return

        if diag_code == "process_dead":
            worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
            if worker.get("process_alive") is not False:
                self._emit_owner_gate_once(pid, attempt_record, "process_alive_ambiguous")
                return

        # Check repository truth
        repo_path = str(project_config.get("repo_path") or snapshot.get("repo_path") or "")
        truth = read_repository_truth(repo_path)
        if not truth.valid:
            self._emit_owner_gate_once(pid, attempt_record, f"repository_invalid: {truth.error}")
            return

        # Check one-recovery-per-run-scope
        r_scope = attempt_record.get("run_scope_key")
        recovery_slots = project_row.setdefault("recovery_slots", {})
        if recovery_slots.get(r_scope):
            self._emit_owner_gate_once(pid, attempt_record, "recovery_slot_already_consumed")
            return

        cid = f"{WATCHDOG_COMMAND_PREFIX}{attempt_key}"
        now_iso = utc_now_iso()

        # 1. RESERVE
        attempt_record["recovery"] = {
            "action": "continue",
            "state": "reserved",
            "command_id": cid,
            "reserved_at": now_iso,
            "requested_at": None,
            "resolved_at": None,
            "reason": f"automatic recovery for {diag_code}",
        }
        recovery_slots[r_scope] = cid
        self._save_state(self._cached_state)

        # 2. ENQUEUE
        try:
            from dev_orchestrator.core.control_commands import submit_control_command
            submit_control_command(self.runtime_root, pid, "continue", command_id=cid)
            attempt_record["recovery"]["state"] = "requested"
            attempt_record["recovery"]["requested_at"] = utc_now_iso()
            self._emit_milestone(
                pid,
                "RECOVERY_STARTED",
                task_id=attempt_record.get("task_id"),
                occurrence_key=f"{attempt_key}:recovery-start",
                details={"source": "watchdog", "command_id": cid, "action": "continue"},
            )
        except Exception as exc:
            attempt_record["recovery"]["state"] = "blocked"
            attempt_record["recovery"]["resolved_at"] = utc_now_iso()
            attempt_record["recovery"]["reason"] = f"enqueue failed: {exc}"
            self._emit_owner_gate_once(pid, attempt_record, f"recovery_enqueue_failed: {exc}")

    def _emit_owner_gate_once(self, project_id: str, attempt_record: dict[str, Any], reason: str) -> None:
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
