"""Frozen data models and contracts for AGY resource pool and routing."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    """Return SHA-256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class QuotaState(Enum):
    """Provider quota state."""
    HEALTHY = "HEALTHY"
    CONSERVE = "CONSERVE"
    LOW = "LOW"
    EXHAUSTED = "EXHAUSTED"
    UNKNOWN = "UNKNOWN"


class RoutingTier(Enum):
    """Tier of resource selected by routing policy."""
    AGY_POOL = "agy_pool"
    HETEROGENEOUS_ESCALATION = "heterogeneous_escalation"
    INDEPENDENT_REVIEWER = "independent_reviewer"
    REJECTED = "rejected"


class PolicyDisposition(Enum):
    """Routing policy recommendation classification."""
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class AccountTelemetry:
    """Real-time availability, quota, cooldown, and utilization telemetry for one account."""
    account_id: str
    provider: str
    available: bool
    quota_state: QuotaState
    cooldown_until: Optional[str] = None
    in_cooldown: bool = False
    cooldown_reason: Optional[str] = None
    active_requests: int = 0
    max_concurrency: int = 1
    total_dispatches: int = 0
    successful_dispatches: int = 0
    failed_dispatches: int = 0
    window_requests: int = 0
    window_limit: int = 60
    window_reset_at: Optional[str] = None
    last_used_at: Optional[str] = None
    last_failure_signature: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "provider": self.provider,
            "available": self.available,
            "quota_state": self.quota_state.value,
            "cooldown_until": self.cooldown_until,
            "in_cooldown": self.in_cooldown,
            "cooldown_reason": self.cooldown_reason,
            "active_requests": self.active_requests,
            "max_concurrency": self.max_concurrency,
            "total_dispatches": self.total_dispatches,
            "successful_dispatches": self.successful_dispatches,
            "failed_dispatches": self.failed_dispatches,
            "window_requests": self.window_requests,
            "window_limit": self.window_limit,
            "window_reset_at": self.window_reset_at,
            "last_used_at": self.last_used_at,
            "last_failure_signature": self.last_failure_signature,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AccountTelemetry:
        qs_val = data.get("quota_state", "UNKNOWN")
        try:
            quota_state = QuotaState(qs_val)
        except ValueError:
            quota_state = QuotaState.UNKNOWN
        return cls(
            account_id=str(data["account_id"]),
            provider=str(data.get("provider", "agy")),
            available=bool(data.get("available", True)),
            quota_state=quota_state,
            cooldown_until=data.get("cooldown_until"),
            in_cooldown=bool(data.get("in_cooldown", False)),
            cooldown_reason=data.get("cooldown_reason"),
            active_requests=int(data.get("active_requests", 0)),
            max_concurrency=int(data.get("max_concurrency", 1)),
            total_dispatches=int(data.get("total_dispatches", 0)),
            successful_dispatches=int(data.get("successful_dispatches", 0)),
            failed_dispatches=int(data.get("failed_dispatches", 0)),
            window_requests=int(data.get("window_requests", 0)),
            window_limit=int(data.get("window_limit", 60)),
            window_reset_at=data.get("window_reset_at"),
            last_used_at=data.get("last_used_at"),
            last_failure_signature=data.get("last_failure_signature"),
        )


@dataclass(frozen=True, slots=True)
class PoolResource:
    """One schedulable model resource backed by an account."""
    resource_id: str
    account_id: str
    provider: str
    model: str
    roles: frozenset[str]
    qualities: frozenset[str] = frozenset({"economy", "balanced", "high"})
    cost_per_million_tokens: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "account_id": self.account_id,
            "provider": self.provider,
            "model": self.model,
            "roles": sorted(list(self.roles)),
            "qualities": sorted(list(self.qualities)),
            "cost_per_million_tokens": self.cost_per_million_tokens,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PoolResource:
        return cls(
            resource_id=str(data["resource_id"]),
            account_id=str(data["account_id"]),
            provider=str(data["provider"]),
            model=str(data["model"]),
            roles=frozenset(data.get("roles", ())),
            qualities=frozenset(data.get("qualities", ("economy", "balanced", "high"))),
            cost_per_million_tokens=float(data.get("cost_per_million_tokens", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class PoolAccount:
    """Configuration and static metadata for a pool account."""
    account_id: str
    provider: str = "agy"
    auth_mode: str = "windows-credential-slot"
    profile_ref: str = ""
    max_concurrency: int = 1
    window_limit: int = 60
    window_duration_seconds: float = 900.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "provider": self.provider,
            "auth_mode": self.auth_mode,
            "profile_ref": self.profile_ref,
            "max_concurrency": self.max_concurrency,
            "window_limit": self.window_limit,
            "window_duration_seconds": self.window_duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class PoolTelemetry:
    """Aggregate snapshot of the entire resource pool at a point in time."""
    timestamp: str
    total_accounts: int
    available_accounts: int
    in_cooldown_accounts: int
    exhausted_accounts: int
    active_requests: int
    accounts: dict[str, AccountTelemetry]
    resources: tuple[PoolResource, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "total_accounts": self.total_accounts,
            "available_accounts": self.available_accounts,
            "in_cooldown_accounts": self.in_cooldown_accounts,
            "exhausted_accounts": self.exhausted_accounts,
            "active_requests": self.active_requests,
            "accounts": {k: v.to_dict() for k, v in self.accounts.items()},
            "resources": [r.to_dict() for r in self.resources],
        }


@dataclass(frozen=True, slots=True)
class FailureSignature:
    """Normalized semantic signature of an execution failure."""
    category: str
    error_class: str
    root_cause_hint: str
    signature_hash: str

    @classmethod
    def from_error(cls, error: str | None, category: str = "generic") -> FailureSignature:
        err_str = (error or "").strip()
        # Extract basic error class if present
        first_line = err_str.splitlines()[0] if err_str else "UnknownError"
        if ":" in first_line:
            err_class = first_line.split(":")[0].strip()
            hint = first_line.split(":", 1)[1].strip()[:100]
        else:
            err_class = first_line[:50]
            hint = first_line[:100]

        # Normalized components for stable hashing
        norm_key = f"{category.lower()}|{err_class.lower()}|{hint.lower()}"
        sig_hash = sha256_text(norm_key)[:16]
        return cls(
            category=category,
            error_class=err_class,
            root_cause_hint=hint,
            signature_hash=sig_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "error_class": self.error_class,
            "root_cause_hint": self.root_cause_hint,
            "signature_hash": self.signature_hash,
        }


@dataclass(frozen=True, slots=True)
class RoutingCandidate:
    """Per-resource evaluation result during routing."""
    resource_id: str
    provider: str
    account_id: str
    eligible: bool
    score: int
    tier: RoutingTier
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "provider": self.provider,
            "account_id": self.account_id,
            "eligible": self.eligible,
            "score": self.score,
            "tier": self.tier.value,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class RoutingRecommendation:
    """Machine-readable routing decision outcome."""
    selected_resource_id: Optional[str]
    routing_tier: RoutingTier
    account_id: Optional[str]
    provider: Optional[str]
    model: Optional[str]
    is_escalated: bool
    escalation_reason: Optional[str]
    same_failure_prevented: bool
    independence_enforced: bool
    candidates: tuple[RoutingCandidate, ...]
    telemetry_snapshot: dict[str, Any]
    policy_disposition: PolicyDisposition
    decision_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_resource_id": self.selected_resource_id,
            "routing_tier": self.routing_tier.value,
            "account_id": self.account_id,
            "provider": self.provider,
            "model": self.model,
            "is_escalated": self.is_escalated,
            "escalation_reason": self.escalation_reason,
            "same_failure_prevented": self.same_failure_prevented,
            "independence_enforced": self.independence_enforced,
            "candidates": [c.to_dict() for c in self.candidates],
            "telemetry_snapshot": dict(self.telemetry_snapshot),
            "policy_disposition": self.policy_disposition.value,
            "decision_reason": self.decision_reason,
        }


@dataclass(frozen=True, slots=True)
class RoutingDecisionRecord:
    """Durable record of one routing decision for auditing."""
    record_id: str
    request_id: str
    project_id: str
    role: str
    task_id: str
    recommendation: RoutingRecommendation
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "request_id": self.request_id,
            "project_id": self.project_id,
            "role": self.role,
            "task_id": self.task_id,
            "recommendation": self.recommendation.to_dict(),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class RoutingPolicyConfig:
    """Configuration settings for AGY-first routing policy."""
    preferred_pool: str = "agy"
    max_concurrency_per_account: int = 1
    default_cooldown_seconds: float = 300.0
    rate_limit_cooldown_seconds: float = 900.0
    sliding_window_seconds: float = 900.0
    sliding_window_max_requests: int = 60
    escalation_fallback_resources: tuple[str, ...] = (
        "codex/default/gpt-5.6-sol",
        "claude/default/opus",
        "chatgpt/default/sol",
    )
    independent_reviewer_resources: tuple[str, ...] = (
        "claude/default/opus",
        "codex/default/gpt-5.6-sol",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "preferred_pool": self.preferred_pool,
            "max_concurrency_per_account": self.max_concurrency_per_account,
            "default_cooldown_seconds": self.default_cooldown_seconds,
            "rate_limit_cooldown_seconds": self.rate_limit_cooldown_seconds,
            "sliding_window_seconds": self.sliding_window_seconds,
            "sliding_window_max_requests": self.sliding_window_max_requests,
            "escalation_fallback_resources": list(self.escalation_fallback_resources),
            "independent_reviewer_resources": list(self.independent_reviewer_resources),
        }
