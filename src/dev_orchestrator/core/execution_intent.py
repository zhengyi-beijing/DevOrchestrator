"""Durable execution intent ledger for DevOrchestrator.

Maintains runtime/execution-intent.json (schema_version 1), persisting active
forward execution intent across daemon restarts, transient errors, and
bounded self-recovery actions.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

EXECUTION_INTENT_SCHEMA_VERSION = 1

DEFAULT_MAX_RECOVERY_ACTIONS = 20
DEFAULT_MAX_IDENTICAL_FAILURES = 3
DEFAULT_MAX_LAUNCH_MINUTES = 30
DEFAULT_BACKOFF_SECONDS = 5


def _intent_lock(runtime_root: Path | str) -> InterProcessFileLock:
    return InterProcessFileLock(Path(runtime_root) / "execution-intent.lock")


def compute_recovery_fingerprint(
    project_id: str,
    task_id: Optional[str],
    lifecycle_state: str,
    blocker_code: str,
    git_anchor: Optional[str],
    worker_state: Optional[str] = None,
    broker_state: Optional[str] = None,
) -> dict[str, Any]:
    """Compute normalized recovery fingerprint dictionary with stable hash."""
    data = {
        "project_id": str(project_id or "").strip(),
        "task_id": str(task_id or "").strip() or None,
        "lifecycle_state": str(lifecycle_state or "").strip(),
        "blocker_code": str(blocker_code or "").strip(),
        "git_anchor": str(git_anchor or "").strip() or None,
        "worker_state": str(worker_state or "").strip() or None,
        "broker_state": str(broker_state or "").strip() or None,
    }
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    data["fingerprint_hash"] = digest
    return data


def load_execution_intents(runtime_root: Path | str) -> dict[str, Any]:
    """Load runtime/execution-intent.json or return empty envelope."""
    intent_file = Path(runtime_root) / "execution-intent.json"
    data = read_json(intent_file, None)
    if (
        isinstance(data, dict)
        and data.get("schema_version") == EXECUTION_INTENT_SCHEMA_VERSION
        and isinstance(data.get("intents"), dict)
    ):
        return data
    return {"schema_version": EXECUTION_INTENT_SCHEMA_VERSION, "intents": {}}


def get_active_intent(runtime_root: Path | str, project_id: str) -> Optional[dict[str, Any]]:
    """Retrieve active or pending execution intent for project_id, if any."""
    data = load_execution_intents(runtime_root)
    intent = data.get("intents", {}).get(project_id)
    if isinstance(intent, dict) and intent.get("state") in {"active", "pending"}:
        return copy.deepcopy(intent)
    return None


def record_or_refresh_intent(
    runtime_root: Path | str,
    project_id: str,
    *,
    task_id: Optional[str] = None,
    target_state: str = "EXECUTING",
    command_id: Optional[str] = None,
    source: str = "control",
    control_revision: Optional[str] = None,
    recovery_epoch_id: Optional[str] = None,
    repo_path: Optional[str] = None,
    config_path: Optional[str] = None,
    activation_request_id: Optional[str] = None,
    profile: Optional[str] = None,
    state: str = "active",
) -> dict[str, Any]:
    """Create or refresh a durable execution intent in runtime/execution-intent.json."""
    runtime = Path(runtime_root)
    with _intent_lock(runtime):
        intent_file = runtime / "execution-intent.json"
        data = load_execution_intents(runtime)
        now = utc_now_iso()

        existing = data["intents"].get(project_id)
        if isinstance(existing, dict) and existing.get("state") in {"active", "pending"}:
            intent = existing
            if task_id:
                intent["task_id"] = task_id
            if command_id:
                intent["command_id"] = command_id
            if source:
                intent["source"] = source
            if control_revision:
                intent["control_revision"] = control_revision
            if recovery_epoch_id:
                intent["recovery_epoch_id"] = recovery_epoch_id
            if repo_path:
                intent["repo_path"] = repo_path
            if config_path:
                intent["config_path"] = config_path
            if activation_request_id:
                intent["activation_request_id"] = activation_request_id
            if profile:
                intent["profile"] = profile
            intent["updated_at"] = now
        else:
            intent = {
                "project_id": project_id,
                "task_id": task_id,
                "target_state": target_state,
                "command_id": command_id or f"intent-{project_id}",
                "source": source,
                "control_revision": control_revision,
                "recovery_epoch_id": recovery_epoch_id,
                "created_at": now,
                "updated_at": now,
                "actions_used": 0,
                "repeats_by_fingerprint": {},
                "fingerprints": [],
                "repo_path": repo_path,
                "config_path": config_path,
                "activation_request_id": activation_request_id,
                "profile": profile,
                "state": state,
            }
            data["intents"][project_id] = intent

        write_json(intent_file, data, indent=2)
        return copy.deepcopy(intent)


def record_intent_action(
    runtime_root: Path | str,
    project_id: str,
    fingerprint: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], bool, Optional[str], Optional[str]]:
    """Increment action counter and append fingerprint."""
    runtime = Path(runtime_root)
    with _intent_lock(runtime):
        intent_file = runtime / "execution-intent.json"
        data = load_execution_intents(runtime)
        intent = data.get("intents", {}).get(project_id)
        if not isinstance(intent, dict):
            return {}, False, None, None

        intent["actions_used"] = int(intent.get("actions_used", 0)) + 1
        intent["updated_at"] = utc_now_iso()

        if fingerprint:
            f_hash = fingerprint.get("fingerprint_hash") or "unknown"
            repeats = intent.setdefault("repeats_by_fingerprint", {})
            repeats[f_hash] = int(repeats.get(f_hash, 0)) + 1
            fps = intent.setdefault("fingerprints", [])
            fps.append(fingerprint)
            if len(fps) > 20:
                intent["fingerprints"] = fps[-20:]

        write_json(intent_file, data, indent=2)
        return copy.deepcopy(intent), False, None, None


def check_intent_budgets(
    intent: dict[str, Any],
    config_self_healing: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Check whether execution intent has exhausted its recovery budget or detected livelock.

    Returns (is_exhausted, reason, blocker_code).
    """
    cfg = config_self_healing or {}
    if cfg.get("enabled") is False:
        return (
            True,
            "self-healing disabled by project configuration",
            "SELF_HEALING_DISABLED",
        )

    max_actions = int(cfg.get("max_recovery_actions") or DEFAULT_MAX_RECOVERY_ACTIONS)
    max_repeats = int(cfg.get("max_identical_failures") or DEFAULT_MAX_IDENTICAL_FAILURES)
    max_minutes = int(cfg.get("max_launch_minutes") or DEFAULT_MAX_LAUNCH_MINUTES)

    # 1. Total actions budget
    actions_used = int(intent.get("actions_used", 0))
    if actions_used >= max_actions:
        return (
            True,
            f"exhausted total recovery actions ({actions_used} >= {max_actions})",
            "RECOVERY_BUDGET_EXHAUSTED",
        )

    # 2. Elapsed time budget
    created_at_str = intent.get("created_at")
    if created_at_str:
        try:
            created_dt = parse_utc(created_at_str)
            curr = now or datetime.now(timezone.utc)
            if created_dt and (curr - created_dt).total_seconds() > max_minutes * 60:
                return (
                    True,
                    f"exhausted elapsed recovery time ({max_minutes} minutes)",
                    "RECOVERY_BUDGET_EXHAUSTED",
                )
        except Exception:
            pass

    # 3. Repeated identical failures
    repeats_map = intent.get("repeats_by_fingerprint") or {}
    for f_hash, count in repeats_map.items():
        if count >= max_repeats:
            return (
                True,
                f"exhausted repeated identical failures ({count} >= {max_repeats} for {f_hash})",
                "RECOVERY_BUDGET_EXHAUSTED",
            )

    # 4. Cycle detection (A -> B -> A -> B)
    fingerprints = intent.get("fingerprints") or []
    if len(fingerprints) >= 4:
        hashes = [f.get("fingerprint_hash") for f in fingerprints if isinstance(f, dict)]
        if len(hashes) >= 4:
            if hashes[-1] == hashes[-3] and hashes[-2] == hashes[-4] and hashes[-1] != hashes[-2]:
                return (
                    True,
                    f"livelock cycle detected between {hashes[-1]} and {hashes[-2]}",
                    "RECOVERY_LIVELOCK_DETECTED",
                )

    return False, None, None


def set_intent_backoff(
    runtime_root: Path | str,
    project_id: str,
    backoff_until: str,
) -> None:
    """Record backoff_until ISO timestamp for a project's active intent."""
    runtime = Path(runtime_root)
    with _intent_lock(runtime):
        intent_file = runtime / "execution-intent.json"
        data = load_execution_intents(runtime)
        intent = data.get("intents", {}).get(project_id)
        if isinstance(intent, dict):
            intent["backoff_until"] = backoff_until
            intent["updated_at"] = utc_now_iso()
            write_json(intent_file, data, indent=2)


def clear_intent_backoff(
    runtime_root: Path | str,
    project_id: str,
) -> None:
    """Clear backoff_until for a project's active intent."""
    runtime = Path(runtime_root)
    with _intent_lock(runtime):
        intent_file = runtime / "execution-intent.json"
        data = load_execution_intents(runtime)
        intent = data.get("intents", {}).get(project_id)
        if isinstance(intent, dict) and "backoff_until" in intent:
            intent.pop("backoff_until", None)
            intent["updated_at"] = utc_now_iso()
            write_json(intent_file, data, indent=2)


def terminate_intent(
    runtime_root: Path | str,
    project_id: str,
    state: str,
    *,
    failure_class: Optional[str] = None,
    blocker_code: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Mark intent terminal (exhausted, owner_gate, satisfied, stopped)."""
    runtime = Path(runtime_root)
    with _intent_lock(runtime):
        intent_file = runtime / "execution-intent.json"
        data = load_execution_intents(runtime)
        intent = data.get("intents", {}).get(project_id)
        if not isinstance(intent, dict):
            return None
        intent["state"] = state
        intent["updated_at"] = utc_now_iso()
        if failure_class:
            intent["failure_class"] = failure_class
        if blocker_code:
            intent["blocker_code"] = blocker_code
        if reason:
            intent["reason"] = reason
        write_json(intent_file, data, indent=2)
        return copy.deepcopy(intent)
