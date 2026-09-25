"""Web Sol failover state machine, durable intent store, and reconciliation engine."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.websol_health import WebSolAvailability, WebSolHealthStore
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now, utc_now_iso, write_json

FAILOVER_FILE = "websol-failover.json"
FAILOVER_SCHEMA_VERSION = 1
DEFAULT_MAX_FAILOVER_ATTEMPTS = 3


class FailoverState(str, Enum):
    INTENT = "intent"
    WITHDRAWAL_REQUESTED = "withdrawal_requested"
    REVOCATION_PENDING = "revocation_pending"
    WITHDRAWN = "withdrawn"
    REVIEW_SUBMITTED = "review_submitted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class FailoverDecision(str, Enum):
    NONE = "none"
    FAILOVER_DIRECT = "failover_direct"
    REQUEST_WITHDRAWAL = "request_withdrawal"
    AWAIT_REVOCATION = "await_revocation"
    SUBMIT_REVIEW = "submit_review"
    CANCEL_FAILOVER = "cancel_failover"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class FailoverRecord:
    project_id: str
    run_id: str
    request_id: str
    nonce: str
    adapter: str
    binding_id: str
    state: str  # FailoverState value
    reason: str
    attempts: int = 0
    cancel_deadline: Optional[str] = None
    review_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


def _failover_key(project_id: str, run_id: str) -> str:
    return f"{project_id.strip()}:{run_id.strip()}"


def _as_utc(moment: Optional[datetime]) -> datetime:
    val = moment or utc_now()
    if val.tzinfo is None:
        val = val.replace(tzinfo=timezone.utc)
    return val.astimezone(timezone.utc)


DEFAULT_PREPARED_FAILOVER_GRACE_SECONDS = 300.0


def evaluate_failover_decision(
    *,
    occurrence_state: str,
    health_availability: str,
    bridge_state: Optional[str] = None,
    cancel_requested: bool = False,
    cancel_deadline_passed: bool = False,
    attempts: int = 0,
    max_attempts: int = DEFAULT_MAX_FAILOVER_ATTEMPTS,
    prepared_age: Optional[float] = None,
    prepared_grace_seconds: float = DEFAULT_PREPARED_FAILOVER_GRACE_SECONDS,
) -> tuple[FailoverDecision, str]:
    """Pure decision function for occurrence-level failover."""
    if attempts >= max_attempts:
        return FailoverDecision.EXHAUSTED, "max_failover_attempts_exhausted"

    if occurrence_state == "prepared":
        # Work prepared but not submitted to bridge
        if prepared_age is not None and prepared_age < prepared_grace_seconds:
            return FailoverDecision.NONE, "prepared_within_grace_period"
        if health_availability in (
            WebSolAvailability.OFFLINE.value,
            WebSolAvailability.PAIRING_REQUIRED.value,
            WebSolAvailability.PROBE_FAILED.value,
        ):
            return FailoverDecision.FAILOVER_DIRECT, f"unhealthy_resource_{health_availability.lower()}"
        return FailoverDecision.NONE, "resource_not_failing"

    if occurrence_state == "submitted":
        # Work submitted to bridge
        if bridge_state == "responded":
            return FailoverDecision.CANCEL_FAILOVER, "response_already_received"
        if bridge_state == "withdrawn":
            return FailoverDecision.SUBMIT_REVIEW, "claim_withdrawn"
        if cancel_requested:
            if cancel_deadline_passed:
                return FailoverDecision.REQUEST_WITHDRAWAL, "cancellation_deadline_expired"
            return FailoverDecision.AWAIT_REVOCATION, "cancellation_pending"

        # Not yet requested cancellation:
        if (
            health_availability in (
                WebSolAvailability.OFFLINE.value,
                WebSolAvailability.PAIRING_REQUIRED.value,
                WebSolAvailability.PROBE_FAILED.value,
            )
            or bridge_state == "expired"
        ):
            return FailoverDecision.REQUEST_WITHDRAWAL, "resource_unhealthy_or_claim_expired"

        return FailoverDecision.NONE, "resource_healthy"

    return FailoverDecision.NONE, "occurrence_not_eligible"


class WebSolFailoverStore:
    """Thread- and process-safe persistent store for Web Sol failover intents."""

    def __init__(self, runtime_root: Path | str) -> None:
        self.runtime_root = Path(runtime_root)
        self.failover_path = self.runtime_root / FAILOVER_FILE
        self.lock_path = self.runtime_root / "websol-failover.lock"
        self._lock = threading.RLock()

    def _empty_payload(self) -> dict[str, Any]:
        return {
            "version": FAILOVER_SCHEMA_VERSION,
            "records": {},
        }

    def _load_data(self) -> dict[str, Any]:
        if not self.failover_path.exists():
            return self._empty_payload()
        try:
            val = read_json(self.failover_path, None)
            if not isinstance(val, dict) or val.get("version") != FAILOVER_SCHEMA_VERSION:
                return self._empty_payload()
            val.setdefault("records", {})
            return val
        except Exception:
            return self._empty_payload()

    def get(self, project_id: str, run_id: str) -> Optional[FailoverRecord]:
        key = _failover_key(project_id, run_id)
        with self._lock:
            data = self._load_data()
            records = data.get("records") or {}
            raw = records.get(key)
            if not isinstance(raw, dict):
                return None
            return FailoverRecord(
                project_id=str(raw["project_id"]),
                run_id=str(raw["run_id"]),
                request_id=str(raw["request_id"]),
                nonce=str(raw["nonce"]),
                adapter=str(raw["adapter"]),
                binding_id=str(raw["binding_id"]),
                state=str(raw["state"]),
                reason=str(raw.get("reason", "")),
                attempts=int(raw.get("attempts", 0)),
                cancel_deadline=raw.get("cancel_deadline"),
                review_id=raw.get("review_id"),
                created_at=str(raw.get("created_at", "")),
                updated_at=str(raw.get("updated_at", "")),
            )

    def put(self, record: FailoverRecord) -> None:
        key = _failover_key(record.project_id, record.run_id)
        with InterProcessFileLock(self.lock_path):
            data = self._load_data()
            records = data.setdefault("records", {})
            records[key] = asdict(record)
            write_json(self.failover_path, data, indent=2)

    def list_all(self) -> list[FailoverRecord]:
        with self._lock:
            data = self._load_data()
            records = data.get("records") or {}
            results = []
            for raw in records.values():
                if not isinstance(raw, dict):
                    continue
                results.append(
                    FailoverRecord(
                        project_id=str(raw["project_id"]),
                        run_id=str(raw["run_id"]),
                        request_id=str(raw["request_id"]),
                        nonce=str(raw["nonce"]),
                        adapter=str(raw["adapter"]),
                        binding_id=str(raw["binding_id"]),
                        state=str(raw["state"]),
                        reason=str(raw.get("reason", "")),
                        attempts=int(raw.get("attempts", 0)),
                        cancel_deadline=raw.get("cancel_deadline"),
                        review_id=raw.get("review_id"),
                        created_at=str(raw.get("created_at", "")),
                        updated_at=str(raw.get("updated_at", "")),
                    )
                )
            return sorted(results, key=lambda x: (x.project_id, x.run_id))


class FailoverEngine:
    """Reconciles failover intents across bridge withdrawal and direct reviewer submission."""

    def __init__(
        self,
        *,
        failover_store: WebSolFailoverStore,
        bridge_store: BrowserBridgeStore,
        reviewer_coordinator: Any,
        dispatcher_ledger_path: Path | str,
        health_store: WebSolHealthStore,
        max_attempts: int = DEFAULT_MAX_FAILOVER_ATTEMPTS,
    ) -> None:
        self.store = failover_store
        self.bridge_store = bridge_store
        self.reviewer = reviewer_coordinator
        self.dispatcher_ledger_path = Path(dispatcher_ledger_path)
        self.health_store = health_store
        self.max_attempts = max_attempts

    def reconcile(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> list[FailoverRecord]:
        """Advance all active failover intents idempotently."""
        moment = _as_utc(now)
        now_iso = moment.isoformat()

        # Load dispatcher ledger to see active prepared or submitted occurrences
        if not self.dispatcher_ledger_path.exists():
            return []
        ledger = read_json(self.dispatcher_ledger_path, None)
        if not isinstance(ledger, dict) or not isinstance(ledger.get("worker_done"), dict):
            return []

        modified_records: list[FailoverRecord] = []
        worker_done = ledger["worker_done"]

        for project_id, p_info in worker_done.items():
            if not isinstance(p_info, dict):
                continue
            occurrences = p_info.get("occurrences")
            if not isinstance(occurrences, dict):
                continue

            for run_id, occ in occurrences.items():
                if not isinstance(occ, dict):
                    continue

                occ_state = occ.get("state")
                f_state = occ.get("failover_state")
                # Skip already completed or permanently finalized failovers
                if f_state in (FailoverState.COMPLETED.value, FailoverState.UNAVAILABLE.value, FailoverState.CANCELLED.value):
                    continue

                adapter = str(occ.get("adapter") or "chatgpt_web")
                binding_id = str(occ.get("binding_id") or "")
                req_id = str(occ.get("request_id") or "")
                nonce = str(occ.get("nonce") or "")

                # Load current health
                h = self.health_store.get(project_id, adapter, binding_id, now=moment)
                h_avail = h.availability if h else WebSolAvailability.OFFLINE.value

                # Load existing failover record if any
                f_rec = self.store.get(project_id, run_id)
                attempts = f_rec.attempts if f_rec else 0

                # Check bridge state if submitted
                bridge_state = None
                cancel_requested = False
                cancel_deadline_passed = False

                if occ_state == "submitted":
                    with self.bridge_store._lock:
                        q = self.bridge_store._load_queue(adapter, binding_id)
                        brec = q.get(req_id)
                        if brec:
                            b_st = brec.get("state")
                            if b_st == "responded":
                                bridge_state = "responded"
                            elif b_st == "withdrawn":
                                bridge_state = "withdrawn"
                            else:
                                bridge_state = b_st
                            if brec.get("cancel_requested_at"):
                                cancel_requested = True
                                c_dead = parse_utc(brec.get("cancel_deadline"))
                                if c_dead and moment >= c_dead:
                                    cancel_deadline_passed = True
                        else:
                            bridge_state = "withdrawn"

                prepared_age = None
                if occ_state == "prepared":
                    prep_iso = occ.get("prepared_at")
                    prep_time = parse_utc(prep_iso) if prep_iso else None
                    if prep_time is not None:
                        prepared_age = (moment - prep_time).total_seconds()

                decision, d_reason = evaluate_failover_decision(
                    occurrence_state=occ_state,
                    health_availability=h_avail,
                    bridge_state=bridge_state,
                    cancel_requested=cancel_requested,
                    cancel_deadline_passed=cancel_deadline_passed,
                    attempts=attempts,
                    max_attempts=self.max_attempts,
                    prepared_age=prepared_age,
                )

                if decision == FailoverDecision.NONE:
                    continue

                if decision == FailoverDecision.EXHAUSTED:
                    new_rec = FailoverRecord(
                        project_id=project_id,
                        run_id=run_id,
                        request_id=req_id,
                        nonce=nonce,
                        adapter=adapter,
                        binding_id=binding_id,
                        state=FailoverState.UNAVAILABLE.value,
                        reason="max_failover_attempts_exhausted",
                        attempts=attempts,
                        created_at=f_rec.created_at if f_rec else now_iso,
                        updated_at=now_iso,
                    )
                    self.store.put(new_rec)
                    occ["failover_state"] = FailoverState.UNAVAILABLE.value
                    occ["failover_reason"] = "max_failover_attempts_exhausted"
                    write_json(self.dispatcher_ledger_path, ledger, indent=2)
                    modified_records.append(new_rec)
                    continue

                if decision == FailoverDecision.CANCEL_FAILOVER:
                    new_rec = FailoverRecord(
                        project_id=project_id,
                        run_id=run_id,
                        request_id=req_id,
                        nonce=nonce,
                        adapter=adapter,
                        binding_id=binding_id,
                        state=FailoverState.CANCELLED.value,
                        reason="response_received",
                        attempts=attempts,
                        created_at=f_rec.created_at if f_rec else now_iso,
                        updated_at=now_iso,
                    )
                    self.store.put(new_rec)
                    occ["failover_state"] = FailoverState.CANCELLED.value
                    occ["failover_reason"] = "response_received"
                    write_json(self.dispatcher_ledger_path, ledger, indent=2)
                    modified_records.append(new_rec)
                    continue

                if decision == FailoverDecision.FAILOVER_DIRECT:
                    # Submit direct failover review for prepared work
                    rev_id = None
                    if hasattr(self.reviewer, "submit_failover_review"):
                        rev_id = self.reviewer.submit_failover_review(
                            project_id=project_id,
                            run_id=run_id,
                            failover_record_id=f"{project_id}:{run_id}",
                        )
                    if rev_id is None:
                        continue
                    new_rec = FailoverRecord(
                        project_id=project_id,
                        run_id=run_id,
                        request_id=req_id,
                        nonce=nonce,
                        adapter=adapter,
                        binding_id=binding_id,
                        state=FailoverState.REVIEW_SUBMITTED.value,
                        reason=d_reason,
                        attempts=attempts + 1,
                        review_id=rev_id,
                        created_at=f_rec.created_at if f_rec else now_iso,
                        updated_at=now_iso,
                    )
                    self.store.put(new_rec)
                    occ["failover_state"] = new_rec.state
                    occ["failover_reason"] = d_reason
                    occ["failover_decided_at"] = now_iso
                    occ["failover_record_id"] = f"{project_id}:{run_id}"
                    write_json(self.dispatcher_ledger_path, ledger, indent=2)
                    modified_records.append(new_rec)
                    continue

                if decision == FailoverDecision.REQUEST_WITHDRAWAL:
                    w_res = self.bridge_store.withdraw(adapter, binding_id, req_id, nonce, d_reason, now=moment)
                    if w_res.outcome == "responded":
                        new_rec = FailoverRecord(
                            project_id=project_id,
                            run_id=run_id,
                            request_id=req_id,
                            nonce=nonce,
                            adapter=adapter,
                            binding_id=binding_id,
                            state=FailoverState.CANCELLED.value,
                            reason="response_received_during_withdrawal",
                            attempts=attempts,
                            created_at=f_rec.created_at if f_rec else now_iso,
                            updated_at=now_iso,
                        )
                    elif w_res.outcome == "revocation_pending":
                        new_rec = FailoverRecord(
                            project_id=project_id,
                            run_id=run_id,
                            request_id=req_id,
                            nonce=nonce,
                            adapter=adapter,
                            binding_id=binding_id,
                            state=FailoverState.REVOCATION_PENDING.value,
                            reason=d_reason,
                            attempts=attempts + 1,
                            cancel_deadline=w_res.cancel_deadline,
                            created_at=f_rec.created_at if f_rec else now_iso,
                            updated_at=now_iso,
                        )
                    else:  # withdrawn or missing
                        rev_id = None
                        if hasattr(self.reviewer, "submit_failover_review"):
                            rev_id = self.reviewer.submit_failover_review(
                                project_id=project_id,
                                run_id=run_id,
                                failover_record_id=f"{project_id}:{run_id}",
                            )
                        new_rec = FailoverRecord(
                            project_id=project_id,
                            run_id=run_id,
                            request_id=req_id,
                            nonce=nonce,
                            adapter=adapter,
                            binding_id=binding_id,
                            state=FailoverState.REVIEW_SUBMITTED.value if rev_id else FailoverState.WITHDRAWN.value,
                            reason=d_reason,
                            attempts=attempts + 1,
                            review_id=rev_id,
                            created_at=f_rec.created_at if f_rec else now_iso,
                            updated_at=now_iso,
                        )

                    self.store.put(new_rec)
                    occ["failover_state"] = new_rec.state
                    occ["failover_reason"] = d_reason
                    occ["failover_decided_at"] = now_iso
                    occ["failover_record_id"] = f"{project_id}:{run_id}"
                    write_json(self.dispatcher_ledger_path, ledger, indent=2)
                    modified_records.append(new_rec)
                    continue

                if decision == FailoverDecision.SUBMIT_REVIEW:
                    rev_id = None
                    if hasattr(self.reviewer, "submit_failover_review"):
                        rev_id = self.reviewer.submit_failover_review(
                            project_id=project_id,
                            run_id=run_id,
                            failover_record_id=f"{project_id}:{run_id}",
                        )
                    new_rec = FailoverRecord(
                        project_id=project_id,
                        run_id=run_id,
                        request_id=req_id,
                        nonce=nonce,
                        adapter=adapter,
                        binding_id=binding_id,
                        state=FailoverState.REVIEW_SUBMITTED.value if rev_id else FailoverState.WITHDRAWN.value,
                        reason=d_reason,
                        attempts=attempts + 1,
                        review_id=rev_id,
                        created_at=f_rec.created_at if f_rec else now_iso,
                        updated_at=now_iso,
                    )
                    self.store.put(new_rec)
                    occ["failover_state"] = new_rec.state
                    occ["failover_reason"] = d_reason
                    occ["failover_decided_at"] = now_iso
                    occ["failover_record_id"] = f"{project_id}:{run_id}"
                    write_json(self.dispatcher_ledger_path, ledger, indent=2)
                    modified_records.append(new_rec)
                    continue

        return modified_records
