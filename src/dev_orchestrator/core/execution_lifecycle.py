"""Execution lifecycle tracking, durable launch obligations, and execution-loss detection.

Contract: docs/P16_9_EXECUTION_LOSS_CONTRACT.md.
Maintains runtime/execution-lineage.json (schema_version 1), ensuring that every
accepted execution that reaches launch/running has a durable lifecycle outcome:
accepted -> launched/running -> {completed | failed | cancelled | explicitly_reconciled}.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.core.diagnostics import ACTIVE_WORKER_STATES
from dev_orchestrator.platform.process import is_pid_alive
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

LIFECYCLE_SCHEMA_VERSION = 1
LINEAGE_STATE_FILE = "execution-lineage.json"
LINEAGE_LOCK_FILE = "execution-lineage.lock"
LINEAGE_CORRUPT_PREFIX = "execution-lineage.json.corrupt-"

# Invariant failure codes
WORKER_VANISHED_WITHOUT_TERMINAL_STATE = "WORKER_VANISHED_WITHOUT_TERMINAL_STATE"
EXECUTION_RECORD_DISAPPEARED = "EXECUTION_RECORD_DISAPPEARED"
RUNNING_WITHOUT_PROVIDER_OUTPUT = "RUNNING_WITHOUT_PROVIDER_OUTPUT"
INCONSISTENT_ACTIVE_STATE = "INCONSISTENT_ACTIVE_STATE"

# Finding lifecycle states
FINDING_CANDIDATE = "candidate"
FINDING_OPEN = "open"
FINDING_SUPPRESSED_LIVE = "suppressed_live"
FINDING_ACTIONABLE_DEAD = "actionable_dead"
FINDING_RECOVERY_RESERVED = "recovery_reserved"
FINDING_RECONCILED_PENDING_RETRY = "reconciled_pending_retry"
FINDING_RECOVERING = "recovering"
FINDING_UNRESOLVED_UNKNOWN = "unresolved_unknown"
FINDING_ESCALATED = "escalated"
FINDING_RESOLVED = "resolved"

FINDING_ACTIVE_STATES = frozenset({
    FINDING_CANDIDATE,
    FINDING_OPEN,
    FINDING_ACTIONABLE_DEAD,
    FINDING_RECOVERY_RESERVED,
    FINDING_RECONCILED_PENDING_RETRY,
    FINDING_RECOVERING,
    FINDING_UNRESOLVED_UNKNOWN,
    FINDING_ESCALATED,
})

# Liveness verdicts
LIVENESS_ALIVE = "alive"
LIVENESS_DEAD = "dead"
LIVENESS_UNKNOWN = "unknown"

# Terminal outcomes
TERMINAL_OUTCOMES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "explicitly_reconciled",
})


class ObligationPersistError(RuntimeError):
    """Raised when an execution obligation cannot be persisted or verified."""
    pass


def lineage_key_for(project_id: str, source_request_id: str) -> str:
    """Deterministic lineage key for a project and source request pair."""
    raw = f"{project_id}|{source_request_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def invariant_key_for(project_id: str, source_request_id: str) -> str:
    """Deterministic invariant key for deduplication and recovery command IDs."""
    raw = f"inv|{project_id}|{source_request_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def compute_lineage_integrity_hash(record: dict[str, Any]) -> str:
    """Cryptographic hash over canonical lineage fields to detect tampering."""
    anchor = record.get("launch_anchor") or {}
    fields = {
        "lineage_key": str(record.get("lineage_key") or ""),
        "invariant_key": str(record.get("invariant_key") or ""),
        "project_id": str(record.get("project_id") or ""),
        "task_id": str(record.get("task_id") or ""),
        "source_request_id": str(record.get("source_request_id") or ""),
        "branch": str(anchor.get("branch") or ""),
        "head": str(anchor.get("head") or ""),
        "launch_status_hash": str(anchor.get("status_hash") or ""),
        "lifecycle_phase": str(record.get("lifecycle_phase") or ""),
        "terminal_outcome": str(record.get("terminal_outcome") or ""),
        "terminal_at": str(record.get("terminal_at") or ""),
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _lineage_lock(runtime_root: Path | str) -> InterProcessFileLock:
    return InterProcessFileLock(Path(runtime_root) / LINEAGE_LOCK_FILE)


def _quarantine_corrupt_lineage(runtime: Path, reason: str, raw_bytes: bytes) -> dict[str, Any]:
    stamp = utc_now_iso().replace(":", "-")
    file_hash = hashlib.sha256(raw_bytes).hexdigest()[:16] if raw_bytes else "empty"
    quarantine_file = runtime / f"{LINEAGE_CORRUPT_PREFIX}{stamp}-{file_hash}"
    try:
        quarantine_file.write_bytes(raw_bytes)
    except OSError:
        pass
    return {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "degraded": True,
        "degraded_reason": reason,
        "records": {},
    }


def load_execution_lineage(runtime_root: Path | str) -> dict[str, Any]:
    """Load execution lineage store fail-closed with quarantine on corruption or future versions."""
    runtime = Path(runtime_root)
    state_file = runtime / LINEAGE_STATE_FILE
    if not state_file.is_file():
        return {
            "schema_version": LIFECYCLE_SCHEMA_VERSION,
            "degraded": False,
            "degraded_reason": None,
            "records": {},
        }
    try:
        raw_bytes = state_file.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        return _quarantine_corrupt_lineage(runtime, f"unreadable lineage JSON: {exc}", raw_bytes if "raw_bytes" in locals() else b"")

    if not isinstance(data, dict):
        return _quarantine_corrupt_lineage(runtime, "lineage root is not a JSON object", raw_bytes)

    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return _quarantine_corrupt_lineage(runtime, "invalid schema version type", raw_bytes)
    if version > LIFECYCLE_SCHEMA_VERSION:
        return _quarantine_corrupt_lineage(runtime, f"unsupported schema version {version}", raw_bytes)

    records = data.get("records")
    if not isinstance(records, dict):
        return _quarantine_corrupt_lineage(runtime, "records root is not a JSON object", raw_bytes)

    return {
        "schema_version": version,
        "degraded": bool(data.get("degraded", False)),
        "degraded_reason": data.get("degraded_reason"),
        "records": records,
    }


def open_execution_obligation(
    runtime_root: Path | str,
    *,
    project_id: str,
    task_id: str,
    source_request_id: str,
    branch: str = "",
    head: str = "",
    launch_status_hash: Optional[str] = None,
    launch_anchor: Optional[dict[str, Any]] = None,
    control_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    engine: Optional[str] = None,
    backend_handle: Optional[Any] = None,
    worker_identity: Optional[dict[str, Any]] = None,
    recovery_of_lineage_key: Optional[str] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fail-closed pre-actuation barrier: atomically upsert, write, and verify obligation."""
    runtime = Path(runtime_root)
    if launch_anchor:
        branch = str(launch_anchor.get("branch") or branch or "")
        head = str(launch_anchor.get("head") or launch_anchor.get("git_head") or head or "")
        launch_status_hash = launch_anchor.get("status_hash") or launch_status_hash

    with _lineage_lock(runtime):
        data = load_execution_lineage(runtime)
        if data.get("degraded"):
            raise ObligationPersistError(f"execution lineage store is degraded: {data.get('degraded_reason')}")

        lkey = lineage_key_for(project_id, source_request_id)
        inv_key = invariant_key_for(project_id, source_request_id)

        existing = data["records"].get(lkey)
        if isinstance(existing, dict) and existing.get("lifecycle_phase") == "terminal":
            raise ObligationPersistError(
                f"execution obligation {lkey} is already closed as terminal: {existing.get('terminal_outcome')}"
            )

        now_iso = utc_now_iso()
        record = {
            "lineage_key": lkey,
            "invariant_key": inv_key,
            "project_id": project_id,
            "task_id": task_id,
            "source_request_id": source_request_id,
            "control_id": control_id,
            "execution_id": execution_id,
            "engine": engine or "unknown",
            "backend_handle": backend_handle,
            "launch_anchor": {
                "branch": branch,
                "head": head,
                "status_hash": launch_status_hash,
            },
            "worker_identity": copy.deepcopy(worker_identity or {}),
            "provider_output": {
                "provider_output_observed": False,
                "first_output_at": None,
                "last_output_at": None,
                "output_bytes": 0,
            },
            "lifecycle_phase": "obligated",
            "liveness_evidence": {
                "last_probe_at": None,
                "verdict": None,
                "probes": [],
            },
            "recovery_attempts": 0,
            "recovery_of_lineage_key": recovery_of_lineage_key,
            "findings": existing.get("findings", []) if isinstance(existing, dict) else [],
            "terminal_outcome": None,
            "terminal_at": None,
            "created_at": existing.get("created_at") if isinstance(existing, dict) else now_iso,
            "updated_at": now_iso,
            "audit_entries": (existing.get("audit_entries", []) if isinstance(existing, dict) else []) + [
                {
                    "action": "open_obligation",
                    "timestamp": now_iso,
                    "details": {
                        "source_request_id": source_request_id,
                        "project_id": project_id,
                        "task_id": task_id,
                    },
                }
            ],
        }
        record["integrity_hash"] = compute_lineage_integrity_hash(record)
        data["records"][lkey] = record

        state_file = runtime / LINEAGE_STATE_FILE
        try:
            write_json(state_file, data)
        except Exception as exc:
            raise ObligationPersistError(f"failed to write execution obligation: {exc}") from exc

        # Read-back verification
        try:
            verified = read_json(state_file, None)
            if not isinstance(verified, dict) or lkey not in verified.get("records", {}):
                raise ObligationPersistError("verification read-back failed: record missing")
            stored = verified["records"][lkey]
            if stored.get("integrity_hash") != record["integrity_hash"]:
                raise ObligationPersistError("verification read-back failed: integrity hash mismatch")
        except ObligationPersistError:
            raise
        except Exception as exc:
            raise ObligationPersistError(f"verification read-back error: {exc}") from exc

        return copy.deepcopy(record)


def record_execution_observation(
    runtime_root: Path | str,
    *,
    project_id: str,
    source_request_id: str,
    changes: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> Optional[dict[str, Any]]:
    """Hooked into TransitionExecutor._update_record to refresh handles and close obligations."""
    runtime = Path(runtime_root)
    with _lineage_lock(runtime):
        data = load_execution_lineage(runtime)
        if data.get("degraded"):
            return None
        lkey = lineage_key_for(project_id, source_request_id)
        record = data.get("records", {}).get(lkey)
        if not isinstance(record, dict):
            return None

        now_iso = utc_now_iso()
        obs = dict(changes or {})
        obs.update(kwargs)

        if "broker_request_id" in obs:
            record["backend_handle"] = obs["broker_request_id"]
        if "pid" in obs:
            record.setdefault("worker_identity", {})["pid"] = obs["pid"]
        if "worker_pid" in obs:
            record["worker_pid"] = obs["worker_pid"]
            record.setdefault("worker_identity", {})["pid"] = obs["worker_pid"]
        if "started_at" in obs:
            record.setdefault("worker_identity", {})["started_at"] = obs["started_at"]
        if "provider_output_observed" in obs:
            pout = record.setdefault("provider_output", {})
            pout["provider_output_observed"] = bool(obs["provider_output_observed"])
            if obs.get("first_output_at"):
                pout["first_output_at"] = obs["first_output_at"]
            pout["last_output_at"] = now_iso
        if "execution_id" in obs:
            record["execution_id"] = obs["execution_id"]
        if "lifecycle_phase" in obs:
            record["lifecycle_phase"] = obs["lifecycle_phase"]

        st = obs.get("state")
        if st:
            if st == "running":
                record["lifecycle_phase"] = "running"
            elif st in TERMINAL_OUTCOMES:
                record["lifecycle_phase"] = "terminal"
                record["terminal_outcome"] = st
                record["terminal_at"] = obs.get("completed_at") or now_iso
                record.setdefault("audit_entries", []).append({
                    "action": "close_obligation",
                    "timestamp": now_iso,
                    "outcome": st,
                    "reason": obs.get("reason"),
                })

        record["updated_at"] = now_iso
        record["integrity_hash"] = compute_lineage_integrity_hash(record)
        write_json(runtime / LINEAGE_STATE_FILE, data)
        return copy.deepcopy(record)


def close_lineage_record(
    runtime_root: Path | str,
    project_id: str,
    source_request_id: str,
    *,
    terminal_outcome: Optional[str] = None,
    outcome: Optional[str] = None,
    reason: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Explicitly close a lineage obligation with a terminal outcome and audit record."""
    final_outcome = terminal_outcome or outcome or "explicitly_reconciled"
    runtime = Path(runtime_root)
    with _lineage_lock(runtime):
        data = load_execution_lineage(runtime)
        if data.get("degraded"):
            return None
        lkey = lineage_key_for(project_id, source_request_id)
        record = data.get("records", {}).get(lkey)
        if not isinstance(record, dict):
            return None
        now_iso = utc_now_iso()
        record["lifecycle_phase"] = "terminal"
        record["terminal_outcome"] = final_outcome
        record["terminal_at"] = completed_at or now_iso
        record.setdefault("audit_entries", []).append({
            "action": "close_lineage_record",
            "timestamp": now_iso,
            "outcome": terminal_outcome,
            "reason": reason,
        })
        record["updated_at"] = now_iso
        record["integrity_hash"] = compute_lineage_integrity_hash(record)
        write_json(runtime / LINEAGE_STATE_FILE, data)
        return copy.deepcopy(record)


def resolve_execution_liveness(
    target: Any,
    *args: Any,
    project_id: Optional[str] = None,
    source_request_id: Optional[str] = None,
    snapshot: Optional[dict[str, Any]] = None,
    executor: Any = None,
    executor_state: Optional[dict[str, Any]] = None,
    ai_execution_port: Any = None,
    liveness_probe: Optional[Callable[[Any], bool]] = None,
    now: Optional[datetime] = None,
    acceptance_grace_seconds: float = 30.0,
    live_proof_freshness_seconds: float = 30.0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Aggregate every available exact-lineage probe without early exit.

    Alive requires an exact live broker fact, a live PID with matching started_at,
    or fresh provider output advance.
    Dead requires conclusive death proof (terminal broker status, dead PID, or expired
    no-handle grace) AND absence of all live proof.
    """
    if isinstance(target, dict):
        lineage_record = target
    else:
        runtime = Path(target)
        if args and isinstance(args[0], str) and not project_id:
            project_id = args[0]
        data = load_execution_lineage(runtime)
        srid = source_request_id or kwargs.get("source_request_id")
        pid = project_id or (snapshot.get("project_id") if isinstance(snapshot, dict) else None)
        lkey = lineage_key_for(str(pid or ""), str(srid or ""))
        lineage_record = data.get("records", {}).get(lkey) or {
            "project_id": pid,
            "source_request_id": srid,
            "worker_pid": kwargs.get("worker_pid"),
            "backend_handle": kwargs.get("engine_handle"),
            "started_at": kwargs.get("started_at"),
            "created_at": kwargs.get("created_at"),
        }

    if executor is not None and executor_state is None and hasattr(executor, "state"):
        try:
            executor_state = executor.state()
        except Exception:
            executor_state = None

    now_dt = now or datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    source_request_id = lineage_record.get("source_request_id") or source_request_id

    has_live_proof = False
    conclusive_death_proof = False
    death_reasons: list[str] = []
    live_reasons: list[str] = []
    probes: list[dict[str, Any]] = []

    # Probe 1: Ledger claim
    exec_row = None
    if isinstance(executor_state, dict) and isinstance(executor_state.get("executions"), dict):
        exec_row = executor_state["executions"].get(source_request_id)
    ledger_state = exec_row.get("state") if isinstance(exec_row, dict) else None
    probes.append({
        "probe": "ledger_claim",
        "timestamp": now_iso,
        "present": exec_row is not None,
        "state": ledger_state,
        "source_request_id": source_request_id,
    })

    # Probe 2: Exact broker request status
    broker_request_id = (
        lineage_record.get("backend_handle")
        or kwargs.get("engine_handle")
        or (exec_row.get("broker_request_id") if isinstance(exec_row, dict) else None)
    )
    engine = lineage_record.get("engine") or (exec_row.get("engine") if isinstance(exec_row, dict) else None)
    broker_probe: dict[str, Any] = {
        "probe": "broker_status",
        "timestamp": now_iso,
        "request_id": broker_request_id,
        "checked": False,
        "fact": None,
        "status": None,
    }
    if engine == "aibroker" or broker_request_id:
        if ai_execution_port is not None and hasattr(ai_execution_port, "status") and broker_request_id:
            try:
                fact = ai_execution_port.status(str(broker_request_id))
                broker_probe["checked"] = True
                broker_probe["fact"] = fact
                if isinstance(fact, dict):
                    b_status = fact.get("status")
                    broker_probe["status"] = b_status
                    if b_status in {"running", "starting"}:
                        has_live_proof = True
                        live_reasons.append(f"broker reports live status {b_status}")
                    elif b_status in {"succeeded", "failed", "cancelled", "not_found", "unknown"}:
                        conclusive_death_proof = True
                        death_reasons.append(f"broker reports terminal/not_found status {b_status}")
                elif fact is None:
                    broker_probe["status"] = "not_found"
                    conclusive_death_proof = True
                    death_reasons.append("broker reports execution not_found")
            except Exception as exc:
                broker_probe["error"] = str(exc)
    probes.append(broker_probe)

    # Probe 3: PID plus started_at identity
    pid_probe: dict[str, Any] = {
        "probe": "pid_liveness",
        "timestamp": now_iso,
        "pid": None,
        "started_at": None,
        "alive": None,
        "identity_matched": False,
    }
    worker_id = lineage_record.get("worker_identity") or {}
    pid = worker_id.get("pid") or lineage_record.get("worker_pid")
    started_at = worker_id.get("started_at") or lineage_record.get("started_at")
    if pid is None and isinstance(exec_row, dict):
        pid = exec_row.get("pid")
        started_at = started_at or exec_row.get("started_at")
    if pid is None and isinstance(snapshot, dict):
        snap_worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
        if str(snap_worker.get("state") or "").lower() in ACTIVE_WORKER_STATES:
            pid = snap_worker.get("pid")
            started_at = started_at or snap_worker.get("started_at")

    if pid is not None:
        pid_probe["pid"] = pid
        pid_probe["started_at"] = started_at
        probe_fn = liveness_probe if liveness_probe is not None else is_pid_alive
        try:
            is_alive = probe_fn(pid)
            pid_probe["alive"] = bool(is_alive)
        except Exception as exc:
            pid_probe["error"] = str(exc)
            is_alive = None

        if is_alive is True:
            snap_worker = snapshot.get("worker") if isinstance(snapshot, dict) and isinstance(snapshot.get("worker"), dict) else {}
            snap_pid = snap_worker.get("pid")
            snap_started_at = snap_worker.get("started_at")
            if snap_pid == pid and snap_started_at and started_at and snap_started_at == started_at:
                pid_probe["identity_matched"] = True
                has_live_proof = True
                live_reasons.append(f"PID {pid} is alive with matching started_at {started_at}")
            elif not started_at or not snap_started_at:
                pid_probe["identity_matched"] = False
            elif snap_pid == pid and snap_started_at != started_at:
                pid_probe["identity_matched"] = False
            else:
                pid_probe["identity_matched"] = True
                has_live_proof = True
                live_reasons.append(f"PID {pid} is alive")
        elif is_alive is False:
            conclusive_death_proof = True
            death_reasons.append(f"PID {pid} is dead")
    probes.append(pid_probe)

    # Probe 4: Fresh provider-output advance
    pout_probe: dict[str, Any] = {
        "probe": "provider_output",
        "timestamp": now_iso,
        "observed": False,
        "first_output_at": None,
        "fresh": False,
    }
    pout = lineage_record.get("provider_output") or {}
    if pout.get("provider_output_observed"):
        pout_probe["observed"] = True
        pout_probe["first_output_at"] = pout.get("first_output_at")
        last_out_str = pout.get("last_output_at") or pout.get("first_output_at")
        last_out_dt = parse_utc(last_out_str)
        if last_out_dt is not None:
            delta = (now_dt - last_out_dt).total_seconds()
            if delta <= live_proof_freshness_seconds:
                pout_probe["fresh"] = True
                has_live_proof = True
                live_reasons.append(f"fresh provider output {delta:.1f}s ago")
    probes.append(pout_probe)

    # Probe 5: Project-level active claims
    claims_probe: dict[str, Any] = {
        "probe": "project_claims",
        "timestamp": now_iso,
        "worker_state": None,
        "active_roles": [],
    }
    if isinstance(snapshot, dict):
        snap_worker = snapshot.get("worker") if isinstance(snapshot.get("worker"), dict) else {}
        claims_probe["worker_state"] = snap_worker.get("state")
        claims_probe["active_roles"] = list(snapshot.get("active_roles") or [])
    probes.append(claims_probe)

    # Grace for no-handle cases
    if not broker_request_id and pid is None:
        created_at_str = lineage_record.get("created_at")
        created_dt = parse_utc(created_at_str)
        if created_dt is not None:
            elapsed = (now_dt - created_dt).total_seconds()
            if elapsed > acceptance_grace_seconds:
                conclusive_death_proof = True
                death_reasons.append(f"acceptance grace expired ({elapsed:.1f}s > {acceptance_grace_seconds}s) with no handle")

    if has_live_proof:
        verdict = LIVENESS_ALIVE
        reason = "; ".join(live_reasons)
    elif conclusive_death_proof and not has_live_proof:
        verdict = LIVENESS_DEAD
        reason = "; ".join(death_reasons)
    else:
        verdict = LIVENESS_UNKNOWN
        reason = "inconclusive liveness probes; no live proof and no conclusive death proof"

    return {
        "verdict": verdict,
        "reason": reason,
        "timestamp": now_iso,
        "probes": probes,
        "evidence_hash": hashlib.sha256(json.dumps(probes, sort_keys=True).encode("utf-8")).hexdigest()[:16],
    }


def observe_executions(
    runtime_root: Path | str,
    *args: Any,
    snapshot: Optional[dict[str, Any]] = None,
    executor: Any = None,
    executor_state: Optional[dict[str, Any]] = None,
    ai_execution_port: Any = None,
    liveness_probe: Optional[Callable[[Any], bool]] = None,
    policy: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Reconcile lineage obligations, detect execution loss, probe liveness, and advance findings."""
    runtime = Path(runtime_root)
    # Parse positional arguments if provided
    # Common calling patterns:
    # 1. observe_executions(runtime, snapshot, executor_state)
    # 2. observe_executions(runtime, pid, snapshot, policy, executor=executor)
    # 3. observe_executions(runtime, snapshot=snapshot, executor_state=...)
    for a in args:
        if isinstance(a, str) and not snapshot:
            pass  # pid argument
        elif isinstance(a, dict) and snapshot is None:
            snapshot = a
        elif isinstance(a, dict) and policy is None and "execution_loss_detection" in a:
            policy = a
        elif isinstance(a, dict) and executor_state is None:
            executor_state = a

    if snapshot is None:
        snapshot = {}

    if executor is not None and executor_state is None and hasattr(executor, "state"):
        try:
            executor_state = executor.state()
        except Exception:
            executor_state = None

    project_id = str(snapshot.get("project_id") or snapshot.get("id") or "")
    now_dt = now or datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    with _lineage_lock(runtime):
        data = load_execution_lineage(runtime)
        if data.get("degraded"):
            return {
                "degraded": True,
                "degraded_reason": data.get("degraded_reason"),
                "findings": [],
                "unresolved_invariants": ["LINEAGE_STORE_DEGRADED"],
                "records": {},
            }

        # Step A: Adopt active rows from executor_state or sync terminal rows
        if isinstance(executor_state, dict) and isinstance(executor_state.get("executions"), dict):
            for rec in executor_state["executions"].values():
                if not isinstance(rec, dict) or str(rec.get("project_id") or "") != project_id:
                    continue
                src_id = str(rec.get("source_request_id") or "")
                if not src_id:
                    continue
                st = str(rec.get("state") or "").lower()
                lkey = lineage_key_for(project_id, src_id)
                inv_key = invariant_key_for(project_id, src_id)

                if lkey not in data["records"]:
                    if st in {"launching", "running"}:
                        data["records"][lkey] = {
                            "lineage_key": lkey,
                            "invariant_key": inv_key,
                            "project_id": project_id,
                            "task_id": rec.get("task_id"),
                            "source_request_id": src_id,
                            "control_id": rec.get("control_id"),
                            "execution_id": rec.get("execution_id"),
                            "engine": rec.get("engine", "unknown"),
                            "backend_handle": rec.get("broker_request_id") or rec.get("backend_id"),
                            "launch_anchor": {
                                "branch": rec.get("branch"),
                                "head": rec.get("head"),
                                "status_hash": rec.get("launch_status_hash"),
                            },
                            "worker_identity": {
                                "pid": rec.get("pid"),
                                "started_at": rec.get("started_at"),
                            },
                            "provider_output": {
                                "provider_output_observed": bool(rec.get("provider_output_observed")),
                                "first_output_at": rec.get("first_output_at"),
                                "last_output_at": rec.get("first_output_at"),
                                "output_bytes": 0,
                            },
                            "lifecycle_phase": st,
                            "liveness_evidence": {"last_probe_at": None, "verdict": None, "probes": []},
                            "recovery_attempts": 0,
                            "recovery_of_lineage_key": None,
                            "findings": [],
                            "terminal_outcome": None,
                            "terminal_at": None,
                            "created_at": rec.get("started_at") or now_iso,
                            "updated_at": now_iso,
                            "audit_entries": [{"action": "adopted_from_executor_row", "timestamp": now_iso}],
                        }
                        data["records"][lkey]["integrity_hash"] = compute_lineage_integrity_hash(data["records"][lkey])
                else:
                    lineage_rec = data["records"][lkey]
                    if st in TERMINAL_OUTCOMES:
                        if lineage_rec.get("lifecycle_phase") != "terminal":
                            lineage_rec["lifecycle_phase"] = "terminal"
                            lineage_rec["terminal_outcome"] = st
                            lineage_rec["terminal_at"] = rec.get("completed_at") or now_iso
                            lineage_rec.setdefault("audit_entries", []).append({
                                "action": "closed_from_executor_terminal_row",
                                "timestamp": now_iso,
                                "outcome": st,
                            })
                            lineage_rec["integrity_hash"] = compute_lineage_integrity_hash(lineage_rec)

        # Step B: Evaluate all project lineage records
        project_records = [r for r in data["records"].values() if str(r.get("project_id") or "") == project_id]

        for lrec in project_records:
            rep_lineage = next(
                (r for r in project_records if r.get("recovery_of_lineage_key") == lrec["lineage_key"]),
                None
            )
            for finding in lrec.get("findings", []):
                if finding.get("state") != FINDING_RESOLVED:
                    if rep_lineage and rep_lineage.get("lifecycle_phase") == "terminal":
                        finding["state"] = FINDING_RESOLVED
                        finding["resolved_at"] = now_iso
                        finding["resolved_reason"] = (
                            f"replacement execution {rep_lineage.get('source_request_id')} reached terminal outcome {rep_lineage.get('terminal_outcome')}"
                        )
                    elif lrec.get("lifecycle_phase") == "terminal" and lrec.get("terminal_outcome") in {"completed", "failed", "cancelled"}:
                        finding["state"] = FINDING_RESOLVED
                        finding["resolved_at"] = now_iso
                        finding["resolved_reason"] = (
                            f"original execution reached authoritative terminal outcome {lrec.get('terminal_outcome')}"
                        )

            if lrec.get("lifecycle_phase") == "terminal":
                continue

            src_id = lrec.get("source_request_id")
            exec_rec = None
            if isinstance(executor_state, dict) and isinstance(executor_state.get("executions"), dict):
                exec_rec = executor_state["executions"].get(src_id)

            detected_code = None
            detected_reason = None

            # Invariant 1: Execution record disappeared from executor ledger
            if exec_rec is None:
                detected_code = EXECUTION_RECORD_DISAPPEARED
                detected_reason = f"execution record {src_id} disappeared from transition-executor.json without terminal state"

            # Invariant 2: Worker vanished without terminal state (e.g. project returned to READY_TO_RUN / IDLE)
            elif (
                str(snapshot.get("state") or snapshot.get("lifecycle_state") or "").upper() in {"READY_TO_RUN", "IDLE"}
                and not snapshot.get("active_roles")
                and str((snapshot.get("worker") or {}).get("state") or "").lower() not in ACTIVE_WORKER_STATES
                and str((snapshot.get("broker_execution") or {}).get("state") or "").lower() not in {"launching", "running"}
            ):
                detected_code = WORKER_VANISHED_WITHOUT_TERMINAL_STATE
                detected_reason = f"project returned to READY_TO_RUN with no active execution, but execution {src_id} has no terminal state"

            # Invariant 3: Running without provider output beyond grace
            elif (
                str(exec_rec.get("state") or "").lower() in {"launching", "running"}
                and not lrec.get("provider_output", {}).get("provider_output_observed")
                and not exec_rec.get("provider_output_observed")
            ):
                grace = float(policy.get("provider_output_grace_seconds", 120.0)) if policy else 120.0
                started_at_str = lrec.get("worker_identity", {}).get("started_at") or exec_rec.get("started_at") or lrec.get("created_at")
                started_dt = parse_utc(started_at_str)
                if started_dt and (now_dt - started_dt).total_seconds() > grace:
                    detected_code = RUNNING_WITHOUT_PROVIDER_OUTPUT
                    detected_reason = f"running without provider output beyond grace ({grace}s)"

            # Invariant 4: Inconsistent active state
            elif bool(snapshot.get("active_roles")) and exec_rec is None:
                detected_code = INCONSISTENT_ACTIVE_STATE
                detected_reason = "active roles claim running worker but active executor execution is absent"

            if detected_code:
                existing_finding = next(
                    (f for f in lrec.get("findings", []) if f.get("code") == detected_code and f.get("state") != FINDING_RESOLVED),
                    None
                )
                if existing_finding is None:
                    existing_finding = {
                        "finding_id": f"find-{lrec['invariant_key']}-{len(lrec.get('findings', []))}",
                        "invariant_key": lrec["invariant_key"],
                        "lineage_key": lrec["lineage_key"],
                        "project_id": project_id,
                        "task_id": lrec.get("task_id"),
                        "source_request_id": src_id,
                        "code": detected_code,
                        "classification": detected_code,
                        "state": FINDING_CANDIDATE,
                        "detected_at": now_iso,
                        "confirmations": 0,
                        "launch_anchor": copy.deepcopy(lrec.get("launch_anchor")),
                        "details": {
                            "project_id": project_id,
                            "task_id": lrec.get("task_id"),
                            "source_request_id": src_id,
                            "launch_anchor": copy.deepcopy(lrec.get("launch_anchor")),
                            "last_known_worker_state": copy.deepcopy(lrec.get("worker_identity")),
                            "provider_output_observed": lrec.get("provider_output", {}).get("provider_output_observed"),
                            "expected_terminal_states": sorted(list(TERMINAL_OUTCOMES)),
                            "observed_state": snapshot.get("state") or snapshot.get("lifecycle_state"),
                            "reason": detected_reason,
                        },
                    }
                    lrec.setdefault("findings", []).append(existing_finding)

                # Probe liveness without early exit
                liveness = resolve_execution_liveness(
                    lrec,
                    snapshot=snapshot,
                    executor_state=executor_state,
                    ai_execution_port=ai_execution_port,
                    liveness_probe=liveness_probe,
                    now=now_dt,
                    acceptance_grace_seconds=float(policy.get("execution_acceptance_grace_seconds", 30.0)) if policy else 30.0,
                    live_proof_freshness_seconds=float(policy.get("live_proof_freshness_seconds", 30.0)) if policy else 30.0,
                )
                lrec["liveness_evidence"] = liveness
                existing_finding["details"]["liveness"] = liveness

                verdict = liveness["verdict"]
                if existing_finding["state"] in {FINDING_CANDIDATE, FINDING_OPEN, FINDING_SUPPRESSED_LIVE, FINDING_ACTIONABLE_DEAD, FINDING_UNRESOLVED_UNKNOWN}:
                    if verdict == LIVENESS_ALIVE:
                        existing_finding["state"] = FINDING_SUPPRESSED_LIVE
                        existing_finding["confirmations"] = 0
                    elif verdict == LIVENESS_DEAD:
                        existing_finding["confirmations"] = int(existing_finding.get("confirmations", 0)) + 1
                        confirm_target = int(policy.get("execution_loss_confirmations", 2)) if policy else 2
                        if existing_finding["confirmations"] >= confirm_target:
                            existing_finding["state"] = FINDING_ACTIONABLE_DEAD
                        else:
                            existing_finding["state"] = FINDING_OPEN
                    else:  # LIVENESS_UNKNOWN
                        existing_finding["state"] = FINDING_UNRESOLVED_UNKNOWN
                        det_dt = parse_utc(existing_finding.get("detected_at"))
                        esc_minutes = float(policy.get("execution_loss_unknown_escalation_minutes", 15.0)) if policy else 15.0
                        if det_dt and (now_dt - det_dt).total_seconds() > esc_minutes * 60:
                            existing_finding["state"] = FINDING_ESCALATED

                lrec["updated_at"] = now_iso
                lrec["integrity_hash"] = compute_lineage_integrity_hash(lrec)

        write_json(runtime / LINEAGE_STATE_FILE, data)

        active_findings: list[dict[str, Any]] = []
        unresolved_invariants: list[str] = []
        for r in project_records:
            for f in r.get("findings", []):
                if f.get("state") != FINDING_RESOLVED:
                    active_findings.append(copy.deepcopy(f))
                    if f.get("state") != FINDING_SUPPRESSED_LIVE:
                        inv = f.get("invariant_key") or f.get("code")
                        if inv:
                            unresolved_invariants.append(inv)

        actionable_findings = [
            f for f in active_findings
            if f.get("state") in {FINDING_ACTIONABLE_DEAD, FINDING_RECONCILED_PENDING_RETRY}
        ]
        status = "loss_detected" if unresolved_invariants else "ok"
        findings_map: dict[str, Any] = {}
        for f in active_findings:
            key = f.get("invariant_key") or f.get("code") or f.get("finding_id")
            if key:
                findings_map[key] = f

        return {
            "status": status,
            "degraded": False,
            "findings": findings_map,
            "active_findings": active_findings,
            "actionable_findings": actionable_findings,
            "unresolved_invariants": sorted(list(set(unresolved_invariants))),
            "lineage_records_count": len(project_records),
        }


def detect_execution_loss(lineage_data: dict[str, Any], project_id: str) -> tuple[bool, list[dict[str, Any]], Optional[dict[str, Any]]]:
    """Return (has_loss, active_findings, top_actionable_finding) for project_id."""
    records = lineage_data.get("records") or {}
    active_findings: list[dict[str, Any]] = []
    top_actionable: Optional[dict[str, Any]] = None

    for rec in records.values():
        if str(rec.get("project_id") or "") != project_id:
            continue
        for f in rec.get("findings", []):
            if f.get("state") in FINDING_ACTIVE_STATES:
                active_findings.append(f)
                if f.get("state") in {FINDING_ACTIONABLE_DEAD, FINDING_RECONCILED_PENDING_RETRY} and top_actionable is None:
                    top_actionable = f

    has_loss = any(f.get("state") != FINDING_SUPPRESSED_LIVE for f in active_findings)
    return has_loss, active_findings, top_actionable
