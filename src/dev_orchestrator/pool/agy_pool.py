"""Shared quota/time-window constrained AGY resource pool implementation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Mapping, Optional, Sequence

from .models import (
    AccountTelemetry,
    PoolAccount,
    PoolResource,
    PoolTelemetry,
    QuotaState,
    utc_now_iso,
)

_DEFAULT_AGY_ACCOUNTS: tuple[PoolAccount, ...] = (
    PoolAccount(account_id="agy-1", provider="agy", profile_ref="agy-1", max_concurrency=1, window_limit=60, window_duration_seconds=900.0),
    PoolAccount(account_id="agy-2", provider="agy", profile_ref="agy-2", max_concurrency=1, window_limit=60, window_duration_seconds=900.0),
    PoolAccount(account_id="agy-3", provider="agy", profile_ref="agy-3", max_concurrency=1, window_limit=60, window_duration_seconds=900.0),
)

_DEFAULT_ALL_ROLES = frozenset({"planner", "worker", "remediator", "debugger", "evidence_packaging", "reviewer"})
_DEFAULT_QUALITIES = frozenset({"economy", "balanced", "high"})

_DEFAULT_RESOURCES: tuple[PoolResource, ...] = (
    PoolResource(resource_id="agy/agy-1/gemini-3.8-flash-high", account_id="agy-1", provider="agy", model="gemini-3.8-flash-high", roles=_DEFAULT_ALL_ROLES, qualities=_DEFAULT_QUALITIES, cost_per_million_tokens=0.0),
    PoolResource(resource_id="agy/agy-2/gemini-3.8-flash-high", account_id="agy-2", provider="agy", model="gemini-3.8-flash-high", roles=_DEFAULT_ALL_ROLES, qualities=_DEFAULT_QUALITIES, cost_per_million_tokens=0.0),
    PoolResource(resource_id="agy/agy-3/gemini-3.8-flash-high", account_id="agy-3", provider="agy", model="gemini-3.8-flash-high", roles=_DEFAULT_ALL_ROLES, qualities=_DEFAULT_QUALITIES, cost_per_million_tokens=0.0),
    PoolResource(resource_id="agy/agy-1/claude-opus-4-6-thinking", account_id="agy-1", provider="agy", model="claude-opus-4-6-thinking", roles=frozenset({"reviewer", "adjudicator"}), qualities=frozenset({"high"}), cost_per_million_tokens=0.0),
    PoolResource(resource_id="agy/agy-2/claude-opus-4-6-thinking", account_id="agy-2", provider="agy", model="claude-opus-4-6-thinking", roles=frozenset({"reviewer", "adjudicator"}), qualities=frozenset({"high"}), cost_per_million_tokens=0.0),
    PoolResource(resource_id="agy/agy-3/claude-opus-4-6-thinking", account_id="agy-3", provider="agy", model="claude-opus-4-6-thinking", roles=frozenset({"reviewer", "adjudicator"}), qualities=frozenset({"high"}), cost_per_million_tokens=0.0),
)


class AGYResourcePool:
    """Manages the 3 AGY accounts as a shared schedulable compute pool."""

    def __init__(
        self,
        accounts: Optional[Sequence[PoolAccount]] = None,
        resources: Optional[Sequence[PoolResource]] = None,
        default_cooldown_seconds: float = 300.0,
    ) -> None:
        self._lock = RLock()
        self._accounts_cfg: dict[str, PoolAccount] = {
            a.account_id: a for a in (accounts if accounts is not None else _DEFAULT_AGY_ACCOUNTS)
        }
        self._resources: list[PoolResource] = list(resources if resources is not None else _DEFAULT_RESOURCES)
        self._default_cooldown_seconds = default_cooldown_seconds

        now = datetime.now(timezone.utc)
        self._telemetry: dict[str, dict[str, Any]] = {}
        for acc_id, acc in self._accounts_cfg.items():
            reset_at = (now + timedelta(seconds=acc.window_duration_seconds)).isoformat()
            self._telemetry[acc_id] = {
                "account_id": acc_id,
                "provider": acc.provider,
                "available": True,
                "quota_state": QuotaState.HEALTHY,
                "cooldown_until": None,
                "in_cooldown": False,
                "cooldown_reason": None,
                "active_requests": 0,
                "max_concurrency": acc.max_concurrency,
                "total_dispatches": 0,
                "successful_dispatches": 0,
                "failed_dispatches": 0,
                "window_requests": 0,
                "window_limit": acc.window_limit,
                "window_reset_at": reset_at,
                "last_used_at": None,
                "last_failure_signature": None,
            }

    @property
    def account_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._accounts_cfg.keys()))

    def _sync_window_and_cooldown(self, acc_id: str, dt_now: datetime) -> None:
        entry = self._telemetry[acc_id]
        # Check cooldown expiration
        if entry["in_cooldown"] and entry["cooldown_until"]:
            try:
                until_dt = datetime.fromisoformat(entry["cooldown_until"].replace("Z", "+00:00"))
                if dt_now >= until_dt:
                    entry["in_cooldown"] = False
                    entry["cooldown_until"] = None
                    entry["cooldown_reason"] = None
                    if entry["quota_state"] == QuotaState.EXHAUSTED:
                        entry["quota_state"] = QuotaState.HEALTHY
            except Exception:
                entry["in_cooldown"] = False

        # Check sliding window expiration
        if entry["window_reset_at"]:
            try:
                reset_dt = datetime.fromisoformat(entry["window_reset_at"].replace("Z", "+00:00"))
                if dt_now >= reset_dt:
                    entry["window_requests"] = 0
                    acc = self._accounts_cfg[acc_id]
                    entry["window_reset_at"] = (dt_now + timedelta(seconds=acc.window_duration_seconds)).isoformat()
                    if entry["quota_state"] == QuotaState.EXHAUSTED and not entry["in_cooldown"]:
                        entry["quota_state"] = QuotaState.HEALTHY
            except Exception:
                pass

    def telemetry(self, now: Optional[datetime] = None) -> PoolTelemetry:
        """Return aggregate telemetry snapshot for the entire pool."""
        dt_now = now if now is not None else datetime.now(timezone.utc)
        with self._lock:
            acc_snapshots: dict[str, AccountTelemetry] = {}
            avail_cnt = 0
            cool_cnt = 0
            exh_cnt = 0
            act_cnt = 0

            for acc_id in sorted(self._accounts_cfg.keys()):
                self._sync_window_and_cooldown(acc_id, dt_now)
                entry = self._telemetry[acc_id]
                t = AccountTelemetry(
                    account_id=entry["account_id"],
                    provider=entry["provider"],
                    available=entry["available"],
                    quota_state=entry["quota_state"],
                    cooldown_until=entry["cooldown_until"],
                    in_cooldown=entry["in_cooldown"],
                    cooldown_reason=entry["cooldown_reason"],
                    active_requests=entry["active_requests"],
                    max_concurrency=entry["max_concurrency"],
                    total_dispatches=entry["total_dispatches"],
                    successful_dispatches=entry["successful_dispatches"],
                    failed_dispatches=entry["failed_dispatches"],
                    window_requests=entry["window_requests"],
                    window_limit=entry["window_limit"],
                    window_reset_at=entry["window_reset_at"],
                    last_used_at=entry["last_used_at"],
                    last_failure_signature=entry["last_failure_signature"],
                )
                acc_snapshots[acc_id] = t
                if t.available and not t.in_cooldown and t.quota_state != QuotaState.EXHAUSTED:
                    avail_cnt += 1
                if t.in_cooldown:
                    cool_cnt += 1
                if t.quota_state == QuotaState.EXHAUSTED:
                    exh_cnt += 1
                act_cnt += t.active_requests

            return PoolTelemetry(
                timestamp=dt_now.isoformat(),
                total_accounts=len(self._accounts_cfg),
                available_accounts=avail_cnt,
                in_cooldown_accounts=cool_cnt,
                exhausted_accounts=exh_cnt,
                active_requests=act_cnt,
                accounts=acc_snapshots,
                resources=tuple(self._resources),
            )

    def acquire(
        self,
        role: str,
        quality: str = "balanced",
        excluded_accounts: Optional[Sequence[str] | set[str]] = None,
        preferred_model: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> tuple[Optional[PoolResource], str]:
        """Acquire an eligible pool resource preferring parallel utilization across accounts."""
        dt_now = now if now is not None else datetime.now(timezone.utc)
        excluded_set = set(excluded_accounts) if excluded_accounts else set()

        with self._lock:
            # Sync all accounts first
            for acc_id in self._accounts_cfg:
                self._sync_window_and_cooldown(acc_id, dt_now)

            rejection_reasons: list[str] = []
            candidates: list[tuple[int, str, str, PoolResource]] = []

            for res in self._resources:
                if role not in res.roles:
                    continue
                if quality not in res.qualities:
                    continue
                if preferred_model and res.model != preferred_model:
                    continue

                acc_id = res.account_id
                if acc_id in excluded_set:
                    rejection_reasons.append(f"{acc_id} excluded by request")
                    continue

                entry = self._telemetry.get(acc_id)
                if not entry or not entry["available"]:
                    rejection_reasons.append(f"{acc_id} unavailable")
                    continue
                if entry["in_cooldown"]:
                    rejection_reasons.append(f"{acc_id} in cooldown ({entry.get('cooldown_reason')})")
                    continue
                if entry["quota_state"] == QuotaState.EXHAUSTED:
                    rejection_reasons.append(f"{acc_id} quota EXHAUSTED")
                    continue
                if entry["active_requests"] >= entry["max_concurrency"]:
                    rejection_reasons.append(f"{acc_id} max concurrency reached ({entry['active_requests']}/{entry['max_concurrency']})")
                    continue
                if entry["window_requests"] >= entry["window_limit"]:
                    rejection_reasons.append(f"{acc_id} window limit reached ({entry['window_requests']}/{entry['window_limit']})")
                    continue

                # Candidate is eligible!
                # Prioritize:
                # 1. Least active requests (parallel scheduling preference!)
                # 2. Least recently used (LRU)
                # 3. Deterministic account id
                last_used = entry.get("last_used_at") or ""
                candidates.append((entry["active_requests"], last_used, acc_id, res))

            if not candidates:
                if not rejection_reasons:
                    return None, f"no pool resource registered for role {role!r} quality {quality!r}"
                return None, "; ".join(rejection_reasons)

            # Pick best candidate
            candidates.sort(key=lambda c: (c[0], c[1], c[2]))
            chosen_res = candidates[0][3]
            acc_entry = self._telemetry[chosen_res.account_id]
            acc_entry["active_requests"] += 1
            acc_entry["total_dispatches"] += 1
            acc_entry["window_requests"] += 1
            acc_entry["last_used_at"] = dt_now.isoformat()

            # Dynamic quota degradation on high utilization
            ratio = acc_entry["window_requests"] / float(acc_entry["window_limit"])
            if ratio >= 1.0:
                acc_entry["quota_state"] = QuotaState.EXHAUSTED
            elif ratio >= 0.85:
                acc_entry["quota_state"] = QuotaState.LOW
            elif ratio >= 0.65:
                acc_entry["quota_state"] = QuotaState.CONSERVE

            return chosen_res, f"acquired {chosen_res.resource_id} on {chosen_res.account_id} (active: {acc_entry['active_requests']})"

    def release(
        self,
        resource_id: str,
        success: bool,
        failure_classification: Optional[str] = None,
        failure_signature: Optional[str] = None,
        cooldown_seconds: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Release an acquired resource and record outcome telemetry."""
        dt_now = now if now is not None else datetime.now(timezone.utc)
        with self._lock:
            # Find account for resource
            target_res = next((r for r in self._resources if r.resource_id == resource_id), None)
            if not target_res:
                return

            acc_id = target_res.account_id
            entry = self._telemetry.get(acc_id)
            if not entry:
                return

            if entry["active_requests"] > 0:
                entry["active_requests"] -= 1

            if success:
                entry["successful_dispatches"] += 1
            else:
                entry["failed_dispatches"] += 1
                if failure_signature:
                    entry["last_failure_signature"] = failure_signature

                # Cooldown trigger on explicit provider/quota failure
                if failure_classification in ("quota_exhausted", "rate_limited", "provider_temporarily_unavailable"):
                    sec = cooldown_seconds if cooldown_seconds is not None else self._default_cooldown_seconds
                    entry["in_cooldown"] = True
                    entry["cooldown_until"] = (dt_now + timedelta(seconds=sec)).isoformat()
                    entry["cooldown_reason"] = failure_classification
                    if failure_classification == "quota_exhausted":
                        entry["quota_state"] = QuotaState.EXHAUSTED
                    elif failure_classification == "rate_limited":
                        entry["quota_state"] = QuotaState.LOW

    def trigger_cooldown(
        self,
        account_id: str,
        duration_seconds: float,
        reason: str,
        now: Optional[datetime] = None,
    ) -> None:
        """Explicitly arm cooldown for an account."""
        dt_now = now if now is not None else datetime.now(timezone.utc)
        with self._lock:
            entry = self._telemetry.get(account_id)
            if entry:
                entry["in_cooldown"] = True
                entry["cooldown_until"] = (dt_now + timedelta(seconds=duration_seconds)).isoformat()
                entry["cooldown_reason"] = reason

    def clear_cooldown(self, account_id: str) -> None:
        """Explicitly clear cooldown for an account."""
        with self._lock:
            entry = self._telemetry.get(account_id)
            if entry:
                entry["in_cooldown"] = False
                entry["cooldown_until"] = None
                entry["cooldown_reason"] = None
                if entry["quota_state"] == QuotaState.EXHAUSTED:
                    entry["quota_state"] = QuotaState.HEALTHY

    def set_quota_state(self, account_id: str, state: QuotaState) -> None:
        """Explicitly set quota state for an account."""
        with self._lock:
            entry = self._telemetry.get(account_id)
            if entry:
                entry["quota_state"] = state
