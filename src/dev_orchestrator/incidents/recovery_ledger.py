"""Durable recovery-attempt ledger and replay reconciliation.

Persists attempt reservations before effects and terminal outcomes afterwards
exclusively inside IncidentStore.execute_txn, ensuring:
1. Concurrency isolation under the InterProcessFileLock.
2. In-transaction deduplication on deterministic attempt_id.
3. Schema version 1 validation and fail-closed corruption fencing.
4. Restart survival and replay reconciliation without duplicate actuation.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from dev_orchestrator.incidents.store import IncidentStore, load_incident_store
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

RECOVERY_ATTEMPT_SCHEMA_VERSION = 1

RECOVERY_ATTEMPT_STATUSES = frozenset({
    "reserved",
    "actuated",
    "succeeded",
    "failed",
    "abandoned",
    "reconciled",
    "gated",
})


@dataclass(frozen=True)
class RecoveryAttempt:
    """Validated durable record of one recovery attempt."""
    attempt_id: str
    project_id: str
    family_id: str
    incident_id: str
    failure_signature: str
    strategy_key: str
    action: str
    attempt_index: int
    status: str
    reserved_at: str
    actuated_at: str | None = None
    settled_at: str | None = None
    recovery_latency: float | None = None
    effect_kind: str | None = None
    effect_id: str | None = None
    expected_authority_anchor: dict[str, Any] | None = None
    expected_source_anchor: dict[str, Any] | None = None
    owner_gate_id: str | None = None
    reason: str | None = None
    schema_version: int = RECOVERY_ATTEMPT_SCHEMA_VERSION
    reservation_ttl_seconds: float = 300.0
    authorized_effect: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in RECOVERY_ATTEMPT_STATUSES:
            raise ValueError(f"Invalid recovery attempt status '{self.status}'; must be in {RECOVERY_ATTEMPT_STATUSES}")
        if self.schema_version != RECOVERY_ATTEMPT_SCHEMA_VERSION:
            raise ValueError(f"Unsupported recovery attempt schema_version {self.schema_version}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "project_id": self.project_id,
            "family_id": self.family_id,
            "incident_id": self.incident_id,
            "failure_signature": self.failure_signature,
            "strategy_key": self.strategy_key,
            "action": self.action,
            "attempt_index": self.attempt_index,
            "status": self.status,
            "reserved_at": self.reserved_at,
            "actuated_at": self.actuated_at,
            "settled_at": self.settled_at,
            "recovery_latency": self.recovery_latency,
            "effect_kind": self.effect_kind,
            "effect_id": self.effect_id,
            "expected_authority_anchor": dict(self.expected_authority_anchor or {}),
            "expected_source_anchor": dict(self.expected_source_anchor or {}),
            "owner_gate_id": self.owner_gate_id,
            "reason": self.reason,
            "reservation_ttl_seconds": self.reservation_ttl_seconds,
            "authorized_effect": dict(self.authorized_effect or {}),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RecoveryAttempt:
        return cls(
            attempt_id=str(data["attempt_id"]),
            project_id=str(data["project_id"]),
            family_id=str(data["family_id"]),
            incident_id=str(data["incident_id"]),
            failure_signature=str(data["failure_signature"]),
            strategy_key=str(data["strategy_key"]),
            action=str(data["action"]),
            attempt_index=int(data["attempt_index"]),
            status=str(data["status"]),
            reserved_at=str(data["reserved_at"]),
            actuated_at=data.get("actuated_at"),
            settled_at=data.get("settled_at"),
            recovery_latency=float(data["recovery_latency"]) if data.get("recovery_latency") is not None else None,
            effect_kind=data.get("effect_kind"),
            effect_id=data.get("effect_id"),
            expected_authority_anchor=data.get("expected_authority_anchor"),
            expected_source_anchor=data.get("expected_source_anchor"),
            owner_gate_id=data.get("owner_gate_id"),
            reason=data.get("reason"),
            schema_version=int(data.get("schema_version", RECOVERY_ATTEMPT_SCHEMA_VERSION)),
            reservation_ttl_seconds=float(data.get("reservation_ttl_seconds", 300.0)),
            authorized_effect=data.get("authorized_effect"),
        )


def validate_recovery_attempt(record: Mapping[str, Any]) -> None:
    """Validate recovery attempt record against schema version 1."""
    if not isinstance(record, Mapping):
        raise ValueError("Recovery attempt record must be a mapping")
    ver = record.get("schema_version", RECOVERY_ATTEMPT_SCHEMA_VERSION)
    if ver != RECOVERY_ATTEMPT_SCHEMA_VERSION:
        raise ValueError(f"Unsupported recovery attempt schema_version {ver}")
    for required in ("attempt_id", "project_id", "family_id", "incident_id", "strategy_key", "action", "status"):
        if not record.get(required):
            raise ValueError(f"Recovery attempt record missing required field: {required}")
    status = record.get("status")
    if status not in RECOVERY_ATTEMPT_STATUSES:
        raise ValueError(f"Invalid recovery attempt status '{status}'")


def normalize_recovery_attempt(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize recovery attempt record to a clean dictionary."""
    validate_recovery_attempt(record)
    return RecoveryAttempt.from_dict(record).to_dict()


def resolve_recovery_strategy_key(
    incident: Mapping[str, Any] | None = None,
    *,
    role: str | None = None,
    failure_class: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    context_revision: str | None = None,
    repair_plan_id: str | None = None,
) -> str:
    """Compute normalized strategy identity.

    Includes: role, failure_class, provider, model, context_revision, repair_plan_id.
    Explicitly excludes: AGY account, resource IDs, request IDs, timestamps, absolute paths.
    """
    inc = incident if isinstance(incident, Mapping) else {}

    eff_role = str(role or inc.get("role") or "unknown").strip().lower()
    eff_fclass = str(failure_class or inc.get("failure_class") or "unknown").strip().lower()
    eff_provider = str(provider or inc.get("provider") or "unknown").strip().lower()
    eff_model = str(model or inc.get("model") or "unknown").strip().lower()
    eff_crev = str(context_revision or inc.get("context_revision") or "base").strip()
    eff_repair = str(repair_plan_id or inc.get("repair_plan_id") or "none").strip()

    canonical_obj = {
        "context_revision": eff_crev,
        "failure_class": eff_fclass,
        "model": eff_model,
        "provider": eff_provider,
        "repair_plan_id": eff_repair,
        "role": eff_role,
    }
    raw = json.dumps(canonical_obj, sort_keys=True, separators=(",", ":"))
    return f"strat-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def reserve_recovery_attempt(
    runtime_root: Path | str,
    project_id: str,
    decision: Any,
    *,
    config: dict[str, Any] | None = None,
    effect_id: str | None = None,
    effect_kind: str | None = None,
) -> dict[str, Any]:
    """Reserve a recovery attempt record inside IncidentStore under lock.

    Idempotent: if attempt_id is already present in store, returns the existing record
    without bumping store revision.
    """
    runtime = Path(runtime_root)
    store = IncidentStore(runtime)
    from dev_orchestrator.incidents.policy import resolve_incident_policy
    reservation_ttl = resolve_incident_policy(config)["recovery_attempt_reservation_ttl_seconds"]

    attempt_id = getattr(decision, "attempt_id", None) or decision.get("attempt_id")
    strategy_key = getattr(decision, "strategy_key", None) or decision.get("strategy_key")
    action = getattr(decision, "action", None) or decision.get("action")
    auth_anchor = getattr(decision, "expected_authority_anchor", None) or decision.get("expected_authority_anchor", {})
    src_anchor = getattr(decision, "expected_source_anchor", None) or decision.get("expected_source_anchor", {})
    eff_dict = getattr(decision, "authorized_effect", None) or decision.get("authorized_effect", {})
    reason = getattr(decision, "reason", None) or decision.get("reason")
    incident_id = str(src_anchor.get("incident_id") or "")
    family_id = str(src_anchor.get("family_id") or "")
    failure_sig = str(src_anchor.get("failure_signature") or "")

    existing_record: dict[str, Any] = {}

    def _mutator(index: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        nonlocal existing_record
        recovery_attempts = index.setdefault("recovery_attempts", {})
        if attempt_id in recovery_attempts:
            existing_record = dict(recovery_attempts[attempt_id])
            # Idempotent short circuit: unchanged index, no side files
            return index, {}

        scoped_prior_attempts = [
            att for att in recovery_attempts.values()
            if isinstance(att, dict) and (
                (family_id and att.get("family_id") == family_id)
                or (incident_id and att.get("incident_id") == incident_id)
            )
        ]
        scoped_prior_count = len(scoped_prior_attempts)
        attempt_index = scoped_prior_count + 1

        attempt_record = RecoveryAttempt(
            attempt_id=attempt_id,
            project_id=project_id,
            family_id=family_id,
            incident_id=incident_id,
            failure_signature=failure_sig,
            strategy_key=strategy_key,
            action=action,
            attempt_index=attempt_index,
            status="reserved",
            reserved_at=utc_now_iso(),
            effect_kind=effect_kind or eff_dict.get("effect_kind"),
            authorized_effect=eff_dict,
            effect_id=effect_id or eff_dict.get("target_id") or eff_dict.get("target_resource_id") or eff_dict.get("effect_id"),
            expected_authority_anchor=auth_anchor,
            expected_source_anchor=src_anchor,
            reason=reason,
            reservation_ttl_seconds=reservation_ttl,
        ).to_dict()

        recovery_attempts[attempt_id] = attempt_record
        existing_record = dict(attempt_record)

        # Prepare side file
        side_files = {
            f"recovery-attempts/{attempt_id}.json": json.dumps(attempt_record, indent=2),
        }

        # Update family / packet if exists
        fams = index.get("families") or {}
        if family_id in fams and isinstance(fams[family_id], dict):
            fam = fams[family_id]
            fam["recovery_strategy"] = strategy_key
            fam["recovery_attempt"] = attempt_record["attempt_index"]
            fam["previous_attempts"] = scoped_prior_count

        return index, side_files

    store.execute_txn("reserve_recovery_attempt", _mutator)
    return existing_record


def record_recovery_outcome(
    runtime_root: Path | str,
    project_id: str,
    attempt_id: str,
    status: str,
    *,
    effect_id: str | None = None,
    recovery_latency: float | None = None,
    reason: str | None = None,
    owner_gate_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transition a reserved recovery attempt to a terminal outcome under lock.

    Idempotent: re-recording the same terminal status is a no-op with zero revision bump.
    """
    runtime = Path(runtime_root)
    store = IncidentStore(runtime)
    settled_record: dict[str, Any] = {}

    def _mutator(index: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        nonlocal settled_record
        recovery_attempts = index.setdefault("recovery_attempts", {})
        if attempt_id not in recovery_attempts:
            # Fall back to side file if not in index
            side_path = runtime / "incident-packets" / "recovery-attempts" / f"{attempt_id}.json"
            if side_path.is_file():
                loaded = read_json(side_path, None)
                if isinstance(loaded, dict):
                    recovery_attempts[attempt_id] = loaded

        if attempt_id not in recovery_attempts:
            raise KeyError(f"Recovery attempt '{attempt_id}' not found in store")

        rec = recovery_attempts[attempt_id]
        if rec.get("status") == status and (effect_id is None or rec.get("effect_id") == effect_id):
            settled_record = dict(rec)
            # Idempotent: unchanged
            return index, {}

        rec["status"] = status
        now_ts = utc_now_iso()
        if status == "actuated":
            rec["actuated_at"] = now_ts
        else:
            rec["settled_at"] = now_ts

        if effect_id is not None:
            rec["effect_id"] = effect_id
        if recovery_latency is not None:
            rec["recovery_latency"] = float(recovery_latency)
        if reason is not None:
            rec["reason"] = reason
        if owner_gate_id is not None:
            rec["owner_gate_id"] = owner_gate_id

        settled_record = dict(rec)
        side_files = {
            f"recovery-attempts/{attempt_id}.json": json.dumps(rec, indent=2),
        }

        # Update family / packet disposition
        fam_id = rec.get("family_id")
        fams = index.get("families") or {}
        if fam_id and fam_id in fams and isinstance(fams[fam_id], dict):
            fam = fams[fam_id]
            fam["recovery_result"] = status
            if recovery_latency is not None:
                fam["recovery_latency"] = float(recovery_latency)
            if owner_gate_id is not None:
                fam["owner_gate_id"] = owner_gate_id
            if status in {"succeeded", "reconciled"}:
                fam["terminal_disposition"] = "resolved"

        return index, side_files

    store.execute_txn("record_recovery_outcome", _mutator)
    return settled_record


def load_recovery_attempts(
    runtime_root: Path | str,
    project_id: str,
    *,
    family_id: str | None = None,
    incident_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load recovery attempts for project, optionally filtered by family or incident."""
    runtime = Path(runtime_root)
    store = load_incident_store(runtime)
    results: list[dict[str, Any]] = []

    attempts_dict = store.index.get("recovery_attempts") or {}
    for att in attempts_dict.values():
        if not isinstance(att, dict):
            continue
        if str(att.get("project_id")) != project_id:
            continue
        if family_id and str(att.get("family_id")) != family_id:
            continue
        if incident_id and str(att.get("incident_id")) != incident_id:
            continue
        results.append(dict(att))

    # Also check side files if index was unpopulated or compacted
    attempts_dir = runtime / "incident-packets" / "recovery-attempts"
    if attempts_dir.is_dir():
        for p in attempts_dir.glob("*.json"):
            loaded = read_json(p, None)
            if isinstance(loaded, dict) and str(loaded.get("project_id")) == project_id:
                aid = str(loaded.get("attempt_id") or "")
                if not any(r.get("attempt_id") == aid for r in results):
                    if family_id and str(loaded.get("family_id")) != family_id:
                        continue
                    if incident_id and str(loaded.get("incident_id")) != incident_id:
                        continue
                    results.append(dict(loaded))

    results.sort(key=lambda r: (int(r.get("attempt_index", 0)), str(r.get("reserved_at", ""))))
    return results


def recovery_budget_state(
    attempts: Sequence[dict[str, Any]],
    budgets: Mapping[str, Any],
    *,
    family_id: str | None = None,
    family_attempts: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute budget consumption and remaining attempts from durable attempts."""
    max_same = int(budgets.get("max_same_strategy_retries", 1))
    max_incident = int(budgets.get("max_recoveries_per_incident", 3))
    max_family = int(budgets.get("max_recovery_attempts_per_family", 5))

    total_attempts = len(attempts)
    strategy_counts: dict[str, int] = {}
    for att in attempts:
        sk = str(att.get("strategy_key") or "unknown")
        strategy_counts[sk] = strategy_counts.get(sk, 0) + 1

    if family_attempts is not None:
        family_count = len(family_attempts)
    elif family_id:
        family_count = sum(1 for att in attempts if str(att.get("family_id") or "") == family_id)
    else:
        family_count = total_attempts

    return {
        "total_attempts": total_attempts,
        "max_recoveries_per_incident": max_incident,
        "incident_remaining": max(0, max_incident - total_attempts),
        "family_remaining": max(0, max_family - family_count),
        "strategy_counts": strategy_counts,
        "max_same_strategy_retries": max_same,
    }


def reconcile_recovery_attempts(
    runtime_root: Path | str,
    project_id: str | None = None,
    *,
    executor: Any = None,
    planner: Any = None,
    reviewer: Any = None,
    jobs: Any = None,
    now: Any = None,
    reservation_ttl_seconds: float = 300.0,
) -> list[dict[str, Any]]:
    """Restart and replay reconciliation for reserved and actuated recovery attempts.

    Binds to single existing effect if present.
    Marks abandoned on proven absence for unactuated reservations.
    Emits durable actionable OWNER_GATE on crash ambiguity.
    Never launches a duplicate Worker, job, or handoff.
    """
    runtime = Path(runtime_root)

    if project_id is None:
        # Reconcile across all projects present in recovery store
        store = load_incident_store(runtime)
        attempts_dict = store.index.get("recovery_attempts") or {}
        project_ids: set[str] = {
            str(att.get("project_id"))
            for att in attempts_dict.values()
            if isinstance(att, dict) and att.get("project_id")
        }
        attempts_dir = runtime / "incident-packets" / "recovery-attempts"
        if attempts_dir.is_dir():
            for p in attempts_dir.glob("*.json"):
                loaded = read_json(p, None)
                if isinstance(loaded, dict) and loaded.get("project_id"):
                    project_ids.add(str(loaded["project_id"]))
        if not project_ids:
            pdir = runtime / "projects"
            if pdir.is_dir():
                for p in pdir.glob("*.json"):
                    project_ids.add(p.stem)
        reconciled_all: list[dict[str, Any]] = []
        for pid in sorted(project_ids):
            reconciled_all.extend(
                reconcile_recovery_attempts(
                    runtime_root,
                    pid,
                    executor=executor,
                    planner=planner,
                    reviewer=reviewer,
                    jobs=jobs,
                    now=now,
                    reservation_ttl_seconds=reservation_ttl_seconds,
                )
            )
        return reconciled_all

    attempts = load_recovery_attempts(runtime, project_id)
    reconciled_results: list[dict[str, Any]] = []

    # Check available effect sources
    transition_file = runtime / "transition-executor.json"
    trans_data = read_json(transition_file, {}) if transition_file.is_file() else {}
    handoffs_dict = trans_data.get("handoffs") if isinstance(trans_data, dict) else {}

    jobs_file = runtime / "jobs" / "index.json"
    jobs_data = read_json(jobs_file, {}) if jobs_file.is_file() else {}
    jobs_dict = jobs_data.get("jobs") if isinstance(jobs_data, dict) else {}

    lineage_file = runtime / "execution-lineage.json"
    lineage_data = None
    lineage_corrupt = False
    if lineage_file.is_file():
        try:
            lineage_data = read_json(lineage_file, None)
            if not isinstance(lineage_data, dict):
                lineage_corrupt = True
        except Exception:
            lineage_corrupt = True

    for att in attempts:
        status = att.get("status")
        if status == "reconciled":
            reconciled_results.append(dict(att))
            continue
        if status not in {"reserved", "actuated"}:
            continue

        aid = str(att["attempt_id"])
        eff_kind = str(att.get("effect_kind") or "")
        eff_id = str(att.get("effect_id") or "")
        reservation_expired = True
        if status == "reserved":
            try:
                observed = now if isinstance(now, datetime) else datetime.fromisoformat(now.replace("Z", "+00:00")) if isinstance(now, str) else datetime.now(timezone.utc)
                reserved = datetime.fromisoformat(str(att["reserved_at"]).replace("Z", "+00:00"))
                if reserved.tzinfo is None or observed.tzinfo is None:
                    raise ValueError("reservation timestamp must have a timezone")
                ttl = float(att.get("reservation_ttl_seconds", reservation_ttl_seconds))
                reservation_expired = (observed - reserved).total_seconds() >= ttl
            except (KeyError, TypeError, ValueError):
                # Unproven age cannot authorize abandonment or a replacement.
                reservation_expired = False

        # 1. Handoff effect reconciliation (CPF-02, CPF-03, CPF-07)
        if eff_kind == "handoff" or "handoff" in eff_kind:
            found_handoff = None
            if isinstance(handoffs_dict, dict) and eff_id:
                for h_key, h_val in handoffs_dict.items():
                    if h_key == eff_id or (
                        isinstance(h_val, dict)
                        and (
                            h_val.get("handoff_id") == eff_id
                            or h_val.get("target_id") == eff_id
                            or h_val.get("task_id") == eff_id
                        )
                    ):
                        found_handoff = h_key
                        break
            if found_handoff:
                updated = record_recovery_outcome(
                    runtime, project_id, aid, "reconciled", effect_id=found_handoff, reason="bound_to_replayed_handoff"
                )
                reconciled_results.append(updated)
            elif status == "reserved":
                if not reservation_expired:
                    reconciled_results.append(dict(att))
                    continue
                updated = record_recovery_outcome(
                    runtime, project_id, aid, "abandoned", reason="handoff_proven_absent"
                )
                reconciled_results.append(updated)
            else:
                from dev_orchestrator.control.owner_store import OwnerControlStore
                gate_id = f"gate-handoff-ambig-{aid[:12]}"
                OwnerControlStore(runtime).record_gate(
                    project_id=project_id,
                    gate_id=gate_id,
                    reason=f"CRASH_AMBIGUITY: actuated handoff attempt {aid} (effect {eff_id}) not found",
                    active=True,
                )
                updated = record_recovery_outcome(
                    runtime, project_id, aid, "gated", owner_gate_id=gate_id, reason="crash_ambiguity_actuated_handoff_missing"
                )
                reconciled_results.append(updated)
            continue

        # 2. Durable job effect reconciliation
        if eff_kind in {"durable_job", "job"}:
            if eff_id and isinstance(jobs_dict, dict) and eff_id in jobs_dict:
                updated = record_recovery_outcome(
                    runtime, project_id, aid, "reconciled", effect_id=eff_id, reason="bound_to_existing_durable_job"
                )
                reconciled_results.append(updated)
            elif status == "reserved":
                if not reservation_expired:
                    reconciled_results.append(dict(att))
                    continue
                updated = record_recovery_outcome(
                    runtime, project_id, aid, "abandoned", reason="durable_job_proven_absent"
                )
                reconciled_results.append(updated)
            else:
                from dev_orchestrator.control.owner_store import OwnerControlStore
                gate_id = f"gate-job-ambig-{aid[:12]}"
                OwnerControlStore(runtime).record_gate(
                    project_id=project_id,
                    gate_id=gate_id,
                    reason=f"CRASH_AMBIGUITY: actuated job attempt {aid} (job {eff_id}) not found",
                    active=True,
                )
                updated = record_recovery_outcome(
                    runtime, project_id, aid, "gated", owner_gate_id=gate_id, reason="crash_ambiguity_actuated_job_missing"
                )
                reconciled_results.append(updated)
            continue

        # 3. Control command / continue reconciliation (e.g. watchdog attempts)
        from dev_orchestrator.control.command_store import ControlCommandStore
        cmd_store = ControlCommandStore(runtime)
        found_cmd = cmd_store.get(eff_id) if eff_id else None
        if found_cmd is not None:
            updated = record_recovery_outcome(
                runtime, project_id, aid, "reconciled", effect_id=eff_id, reason="bound_to_existing_control_command"
            )
            reconciled_results.append(updated)
            continue

        # 4. Worker execution / runtime reconciliation (CPF-01, CPF-07 crash ambiguity)
        if lineage_corrupt:
            # Crash ambiguity: fail closed to OWNER_GATE
            from dev_orchestrator.control.owner_store import OwnerControlStore
            gate_id = f"gate-ambig-{aid[:12]}"
            OwnerControlStore(runtime).record_gate(
                project_id=project_id,
                gate_id=gate_id,
                reason=f"CRASH_AMBIGUITY: recovery attempt {aid} has corrupt lineage evidence",
                active=True,
            )
            updated = record_recovery_outcome(
                runtime, project_id, aid, "gated", owner_gate_id=gate_id, reason="crash_ambiguity_corrupt_lineage"
            )
            reconciled_results.append(updated)
            continue

        # Check existing execution in lineage
        found_exec = None
        if isinstance(lineage_data, dict):
            records = lineage_data.get("records") or {}
            for r_key, r_val in records.items():
                if eff_id and (
                    r_key == eff_id
                    or r_key == f"{project_id}:{eff_id}"
                    or (
                        isinstance(r_val, dict)
                        and (
                            r_val.get("source_request_id") == eff_id
                            or r_val.get("command_id") == eff_id
                            or r_val.get("request_id") == eff_id
                            or r_val.get("execution_id") == eff_id
                        )
                    )
                ):
                    found_exec = r_key
                    break

        if found_exec:
            updated = record_recovery_outcome(
                runtime, project_id, aid, "reconciled", effect_id=found_exec, reason="bound_to_existing_execution"
            )
            reconciled_results.append(updated)
        elif status == "reserved":
            if not reservation_expired:
                reconciled_results.append(dict(att))
                continue
            updated = record_recovery_outcome(
                runtime, project_id, aid, "abandoned", reason="effect_proven_absent"
            )
            reconciled_results.append(updated)
        else:
            from dev_orchestrator.control.owner_store import OwnerControlStore
            gate_id = f"gate-exec-ambig-{aid[:12]}"
            OwnerControlStore(runtime).record_gate(
                project_id=project_id,
                gate_id=gate_id,
                reason=f"CRASH_AMBIGUITY: actuated execution attempt {aid} (effect {eff_id}) not found in lineage",
                active=True,
            )
            updated = record_recovery_outcome(
                runtime, project_id, aid, "gated", owner_gate_id=gate_id, reason="crash_ambiguity_actuated_execution_missing"
            )
            reconciled_results.append(updated)

    return reconciled_results
