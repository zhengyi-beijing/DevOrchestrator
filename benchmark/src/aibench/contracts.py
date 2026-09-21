"""Contract types, schema definitions, and frozen models for aibench."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROLE_CLASSES: tuple[str, ...] = ("planner", "reviewer", "worker", "debugger")
TRACK_A = "A"
TRACK_B = "B"
TRACK_NAMES: tuple[str, ...] = (TRACK_A, TRACK_B)

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_UNAVAILABLE = "unavailable"
TRIAL_STATUSES: frozenset[str] = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_TIMED_OUT, STATUS_UNAVAILABLE})

DECISION_PROMOTE = "PROMOTE"
DECISION_NO_PROMOTE = "NO_PROMOTE"

REASON_CAPABILITY_UNSUPPORTED = "capability_unsupported"
REASON_EVIDENCE_GATE_FAILED = "evidence_gate_failed"
REASON_QUALITY_GATE_FAILED = "quality_gate_failed"
REASON_BENEFIT_GATE_FAILED = "benefit_gate_failed"
REASON_PRIVACY_GATE_FAILED = "privacy_gate_failed"
REASON_STALENESS_GATE_FAILED = "staleness_gate_failed"
REASON_COST_GATE_FAILED = "cost_gate_failed"
REASON_FALLBACK_GATE_FAILED = "fallback_gate_failed"

GATE_NAMES: tuple[str, ...] = (
    "capability_gate",
    "evidence_gate",
    "quality_gate",
    "benefit_gate",
    "privacy_gate",
    "staleness_gate",
    "cost_gate",
    "fallback_gate",
)

DEFAULT_PROMOTION_THRESHOLDS: dict[str, Any] = {
    "min_distinct_resources": 3,
    "required_roles": list(ROLE_CLASSES),
    "max_false_findings_ratio": 0.20,
    "max_correctness_drop": -0.05,  # Track B delta >= -0.05 (non-inferior)
    "min_correctness_retention": 1.0,  # Legacy multiplicative retention fallback
    "min_completion_rate": 0.95,  # Fallback gate requires >= 0.95 completion rate
    "min_benefit_metrics": 1,  # at least 1 of (wall_time, tool_calls, tokens) has statistically supported advantage
    "max_stale_error_rate": 0.15,
    "max_index_time_seconds": 60.0,
    "max_index_size_mb": 50.0,
    "require_privacy_verified": True,
    "require_fallback_verified": True,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    snapshot_id: str
    source: str
    registry_digest: str
    timestamp: str
    resources: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "registry_digest": self.registry_digest,
            "timestamp": self.timestamp,
            "resources": list(self.resources),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResourceSnapshot:
        return cls(
            snapshot_id=str(data["snapshot_id"]),
            source=str(data["source"]),
            registry_digest=str(data["registry_digest"]),
            timestamp=str(data["timestamp"]),
            resources=tuple(dict(r) for r in data["resources"]),
        )


@dataclass(frozen=True, slots=True)
class FrozenPrompt:
    task_id: str
    prompt_bytes: bytes
    prompt_hash: str
    prompt_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "prompt_hash": self.prompt_hash,
            "prompt_text": self.prompt_text,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FrozenPrompt:
        text = str(data["prompt_text"])
        pbytes = text.encode("utf-8")
        phash = sha256_bytes(pbytes)
        return cls(
            task_id=str(data["task_id"]),
            prompt_bytes=pbytes,
            prompt_hash=phash,
            prompt_text=text,
        )


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    task_id: str
    role: str
    title: str
    description: str
    prompt_template: str
    timeout_seconds: float
    response_schema: dict[str, Any]
    rubric_ref: str
    retrieval_sensitive: bool
    ground_truth: dict[str, Any]

    def __post_init__(self) -> None:
        if self.role not in ROLE_CLASSES:
            raise ValueError(f"invalid task role: {self.role!r}, must be one of {ROLE_CLASSES}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "title": self.title,
            "description": self.description,
            "prompt_template": self.prompt_template,
            "timeout_seconds": self.timeout_seconds,
            "response_schema": dict(self.response_schema),
            "rubric_ref": self.rubric_ref,
            "retrieval_sensitive": self.retrieval_sensitive,
            "ground_truth": dict(self.ground_truth),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BenchmarkTask:
        return cls(
            task_id=str(data["task_id"]),
            role=str(data["role"]),
            title=str(data["title"]),
            description=str(data["description"]),
            prompt_template=str(data["prompt_template"]),
            timeout_seconds=float(data["timeout_seconds"]),
            response_schema=dict(data.get("response_schema", {})),
            rubric_ref=str(data["rubric_ref"]),
            retrieval_sensitive=bool(data.get("retrieval_sensitive", False)),
            ground_truth=dict(data.get("ground_truth", {})),
        )


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    path: str
    start_line: int
    end_line: int
    score: float
    mode: str = "zvec"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "score": self.score,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RetrievalHit:
        return cls(
            path=str(data["path"]),
            start_line=int(data["start_line"]),
            end_line=int(data["end_line"]),
            score=float(data["score"]),
            mode=str(data.get("mode", "zvec")),
        )


@dataclass(frozen=True, slots=True)
class BrokerAttempt:
    request_id: str
    dispatch_id: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    session_id: str | None = None
    resource_id: str | None = None
    provider: str | None = None
    account: str | None = None
    model: str | None = None
    status: str = "failed"
    output: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    usage_source: str = "unknown"
    started_at: str | None = None
    finished_at: str | None = None
    first_output_at: str | None = None
    quota_observation: dict[str, Any] | None = None
    rate_limit_observation: dict[str, Any] | None = None
    failure_classification: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "dispatch_id": self.dispatch_id,
            "decision_id": self.decision_id,
            "execution_id": self.execution_id,
            "session_id": self.session_id,
            "resource_id": self.resource_id,
            "provider": self.provider,
            "account": self.account,
            "model": self.model,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "usage": dict(self.usage) if self.usage else None,
            "usage_source": self.usage_source,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "first_output_at": self.first_output_at,
            "quota_observation": dict(self.quota_observation) if self.quota_observation else None,
            "rate_limit_observation": dict(self.rate_limit_observation) if self.rate_limit_observation else None,
            "failure_classification": self.failure_classification,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BrokerAttempt:
        return cls(
            request_id=str(data["request_id"]),
            dispatch_id=data.get("dispatch_id"),
            decision_id=data.get("decision_id"),
            execution_id=data.get("execution_id"),
            session_id=data.get("session_id"),
            resource_id=data.get("resource_id"),
            provider=data.get("provider"),
            account=data.get("account"),
            model=data.get("model"),
            status=str(data.get("status", "failed")),
            output=data.get("output"),
            error=data.get("error"),
            usage=dict(data["usage"]) if data.get("usage") else None,
            usage_source=str(data.get("usage_source", "unknown")),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            first_output_at=data.get("first_output_at"),
            quota_observation=dict(data["quota_observation"]) if data.get("quota_observation") else None,
            rate_limit_observation=dict(data["rate_limit_observation"]) if data.get("rate_limit_observation") else None,
            failure_classification=data.get("failure_classification"),
        )


@dataclass(frozen=True, slots=True)
class TrialCell:
    trial_id: str
    task_id: str
    role: str
    target_resource_id: str
    track: str
    repeat_index: int
    pair_id: str
    pair_order: int
    prompt_hash: str
    corpus_revision: str
    timeout_seconds: float

    def __post_init__(self) -> None:
        if self.track not in TRACK_NAMES:
            raise ValueError(f"invalid track: {self.track!r}")
        if self.role not in ROLE_CLASSES:
            raise ValueError(f"invalid role: {self.role!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "task_id": self.task_id,
            "role": self.role,
            "target_resource_id": self.target_resource_id,
            "track": self.track,
            "repeat_index": self.repeat_index,
            "pair_id": self.pair_id,
            "pair_order": self.pair_order,
            "prompt_hash": self.prompt_hash,
            "corpus_revision": self.corpus_revision,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrialCell:
        return cls(
            trial_id=str(data["trial_id"]),
            task_id=str(data["task_id"]),
            role=str(data["role"]),
            target_resource_id=str(data["target_resource_id"]),
            track=str(data["track"]),
            repeat_index=int(data["repeat_index"]),
            pair_id=str(data["pair_id"]),
            pair_order=int(data["pair_order"]),
            prompt_hash=str(data["prompt_hash"]),
            corpus_revision=str(data["corpus_revision"]),
            timeout_seconds=float(data["timeout_seconds"]),
        )


@dataclass(frozen=True, slots=True)
class TrialScore:
    trial_id: str
    correctness: float
    cited_spans_valid: int
    cited_spans_invalid: int
    required_findings_met: int
    required_findings_total: int
    false_findings: int
    patch_valid: bool | None = None
    tests_passed: int | None = None
    tests_failed: int | None = None
    regression_rate: float | None = None
    raw_score: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "correctness": self.correctness,
            "cited_spans_valid": self.cited_spans_valid,
            "cited_spans_invalid": self.cited_spans_invalid,
            "required_findings_met": self.required_findings_met,
            "required_findings_total": self.required_findings_total,
            "false_findings": self.false_findings,
            "patch_valid": self.patch_valid,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "regression_rate": self.regression_rate,
            "raw_score": self.raw_score,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrialScore:
        return cls(
            trial_id=str(data["trial_id"]),
            correctness=float(data["correctness"]),
            cited_spans_valid=int(data["cited_spans_valid"]),
            cited_spans_invalid=int(data["cited_spans_invalid"]),
            required_findings_met=int(data["required_findings_met"]),
            required_findings_total=int(data["required_findings_total"]),
            false_findings=int(data["false_findings"]),
            patch_valid=data.get("patch_valid"),
            tests_passed=data.get("tests_passed"),
            tests_failed=data.get("tests_failed"),
            regression_rate=data.get("regression_rate"),
            raw_score=float(data.get("raw_score", 0.0)),
            details=dict(data.get("details", {})),
        )


@dataclass(frozen=True, slots=True)
class MetricCoverage:
    observed_shell_tool_calls: int = 0
    zvec_calls: int = 0
    provider_reported_tool_calls: int = 0
    total_complete_tool_calls: int | None = None
    reported_tokens: int | None = None
    wall_time_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_shell_tool_calls": self.observed_shell_tool_calls,
            "zvec_calls": self.zvec_calls,
            "provider_reported_tool_calls": self.provider_reported_tool_calls,
            "total_complete_tool_calls": self.total_complete_tool_calls,
            "reported_tokens": self.reported_tokens,
            "wall_time_seconds": self.wall_time_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MetricCoverage:
        return cls(
            observed_shell_tool_calls=int(data.get("observed_shell_tool_calls", 0)),
            zvec_calls=int(data.get("zvec_calls", 0)),
            provider_reported_tool_calls=int(data.get("provider_reported_tool_calls", 0)),
            total_complete_tool_calls=data.get("total_complete_tool_calls"),
            reported_tokens=data.get("reported_tokens"),
            wall_time_seconds=float(data.get("wall_time_seconds", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class TrialRecord:
    trial_id: str
    plan_id: str
    cell: TrialCell
    broker_attempt: BrokerAttempt
    score: TrialScore
    metrics: MetricCoverage
    status: str
    retrieval_applied: bool
    finished_at: str

    def __post_init__(self) -> None:
        if self.status not in TRIAL_STATUSES:
            raise ValueError(f"invalid trial status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "plan_id": self.plan_id,
            "cell": self.cell.to_dict(),
            "broker_attempt": self.broker_attempt.to_dict(),
            "score": self.score.to_dict(),
            "metrics": self.metrics.to_dict(),
            "status": self.status,
            "retrieval_applied": self.retrieval_applied,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrialRecord:
        return cls(
            trial_id=str(data["trial_id"]),
            plan_id=str(data["plan_id"]),
            cell=TrialCell.from_dict(data["cell"]),
            broker_attempt=BrokerAttempt.from_dict(data["broker_attempt"]),
            score=TrialScore.from_dict(data["score"]),
            metrics=MetricCoverage.from_dict(data["metrics"]),
            status=str(data["status"]),
            retrieval_applied=bool(data["retrieval_applied"]),
            finished_at=str(data["finished_at"]),
        )


@dataclass(frozen=True, slots=True)
class TrialPlan:
    plan_id: str
    created_at: str
    corpus_manifest_hash: str
    rubric_digest: str
    config_hash: str
    registry_digest: str
    selected_resources: tuple[str, ...]
    cells: tuple[TrialCell, ...]
    repeats: int
    seed: int
    max_dispatches: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "corpus_manifest_hash": self.corpus_manifest_hash,
            "rubric_digest": self.rubric_digest,
            "config_hash": self.config_hash,
            "registry_digest": self.registry_digest,
            "selected_resources": list(self.selected_resources),
            "cells": [c.to_dict() for c in self.cells],
            "repeats": self.repeats,
            "seed": self.seed,
            "max_dispatches": self.max_dispatches,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrialPlan:
        return cls(
            plan_id=str(data["plan_id"]),
            created_at=str(data["created_at"]),
            corpus_manifest_hash=str(data["corpus_manifest_hash"]),
            rubric_digest=str(data["rubric_digest"]),
            config_hash=str(data["config_hash"]),
            registry_digest=str(data["registry_digest"]),
            selected_resources=tuple(str(r) for r in data["selected_resources"]),
            cells=tuple(TrialCell.from_dict(c) for c in data["cells"]),
            repeats=int(data["repeats"]),
            seed=int(data["seed"]),
            max_dispatches=int(data["max_dispatches"]),
        )


@dataclass(frozen=True, slots=True)
class StalenessProbeResult:
    probe_id: str
    latency_ms: float
    index_time_seconds: float
    index_size_bytes: int
    stale_false_hits: int
    stale_misses: int
    stale_error_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "latency_ms": self.latency_ms,
            "index_time_seconds": self.index_time_seconds,
            "index_size_bytes": self.index_size_bytes,
            "stale_false_hits": self.stale_false_hits,
            "stale_misses": self.stale_misses,
            "stale_error_rate": self.stale_error_rate,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StalenessProbeResult:
        return cls(
            probe_id=str(data["probe_id"]),
            latency_ms=float(data["latency_ms"]),
            index_time_seconds=float(data["index_time_seconds"]),
            index_size_bytes=int(data["index_size_bytes"]),
            stale_false_hits=int(data["stale_false_hits"]),
            stale_misses=int(data["stale_misses"]),
            stale_error_rate=float(data["stale_error_rate"]),
        )


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    plan_id: str
    created_at: str
    total_trials: int
    completed_trials: int
    track_a_summary: dict[str, Any]
    track_b_summary: dict[str, Any]
    comparison: dict[str, Any]
    resource_breakdown: dict[str, Any]
    role_breakdown: dict[str, Any]
    quota_consumed: dict[str, Any]
    staleness_metrics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "total_trials": self.total_trials,
            "completed_trials": self.completed_trials,
            "track_a_summary": dict(self.track_a_summary),
            "track_b_summary": dict(self.track_b_summary),
            "comparison": dict(self.comparison),
            "resource_breakdown": dict(self.resource_breakdown),
            "role_breakdown": dict(self.role_breakdown),
            "quota_consumed": dict(self.quota_consumed),
            "staleness_metrics": dict(self.staleness_metrics) if self.staleness_metrics else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunSummary:
        return cls(
            run_id=str(data["run_id"]),
            plan_id=str(data["plan_id"]),
            created_at=str(data["created_at"]),
            total_trials=int(data["total_trials"]),
            completed_trials=int(data["completed_trials"]),
            track_a_summary=dict(data.get("track_a_summary", {})),
            track_b_summary=dict(data.get("track_b_summary", {})),
            comparison=dict(data.get("comparison", {})),
            resource_breakdown=dict(data.get("resource_breakdown", {})),
            role_breakdown=dict(data.get("role_breakdown", {})),
            quota_consumed=dict(data.get("quota_consumed", {})),
            staleness_metrics=dict(data["staleness_metrics"]) if data.get("staleness_metrics") else None,
        )


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    decision: str
    reasons: tuple[str, ...]
    failed_gates: tuple[str, ...]
    gate_results: dict[str, dict[str, Any]]
    run_id: str
    plan_id: str
    timestamp: str

    def __post_init__(self) -> None:
        if self.decision not in {DECISION_PROMOTE, DECISION_NO_PROMOTE}:
            raise ValueError(f"invalid promotion decision: {self.decision!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "failed_gates": list(self.failed_gates),
            "gate_results": dict(self.gate_results),
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PromotionDecision:
        return cls(
            decision=str(data["decision"]),
            reasons=tuple(str(r) for r in data.get("reasons", ())),
            failed_gates=tuple(str(g) for g in data.get("failed_gates", ())),
            gate_results=dict(data.get("gate_results", {})),
            run_id=str(data["run_id"]),
            plan_id=str(data["plan_id"]),
            timestamp=str(data["timestamp"]),
        )


@dataclass(frozen=True, slots=True)
class ContainmentConfig:
    scratch_root: str
    queue_root: str
    dedicated_sid: str
    scheduled_task_name: str
    broker_config_path: str
    zvec_path: str
    firewall_rule_name: str
    protected_roots: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scratch_root": self.scratch_root,
            "queue_root": self.queue_root,
            "dedicated_sid": self.dedicated_sid,
            "scheduled_task_name": self.scheduled_task_name,
            "broker_config_path": self.broker_config_path,
            "zvec_path": self.zvec_path,
            "firewall_rule_name": self.firewall_rule_name,
            "protected_roots": list(self.protected_roots),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ContainmentConfig:
        return cls(
            scratch_root=str(data["scratch_root"]),
            queue_root=str(data["queue_root"]),
            dedicated_sid=str(data["dedicated_sid"]),
            scheduled_task_name=str(data["scheduled_task_name"]),
            broker_config_path=str(data["broker_config_path"]),
            zvec_path=str(data["zvec_path"]),
            firewall_rule_name=str(data["firewall_rule_name"]),
            protected_roots=tuple(str(r) for r in data.get("protected_roots", ())),
        )


@dataclass(frozen=True, slots=True)
class ContainmentAuditResult:
    sid_match: bool
    deny_acl_verified: bool
    scratch_verified: bool
    registry_verified: bool
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sid_match": self.sid_match,
            "deny_acl_verified": self.deny_acl_verified,
            "scratch_verified": self.scratch_verified,
            "registry_verified": self.registry_verified,
            "passed": self.passed,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ContainmentAuditResult:
        return cls(
            sid_match=bool(data["sid_match"]),
            deny_acl_verified=bool(data["deny_acl_verified"]),
            scratch_verified=bool(data["scratch_verified"]),
            registry_verified=bool(data["registry_verified"]),
            passed=bool(data["passed"]),
            details=dict(data.get("details", {})),
        )


@dataclass(frozen=True, slots=True)
class ZvecProbeResult:
    supported: bool
    executable_path: str | None = None
    version: str | None = None
    executable_hash: str | None = None
    local_only_verified: bool = False
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "executable_path": self.executable_path,
            "version": self.version,
            "executable_hash": self.executable_hash,
            "local_only_verified": self.local_only_verified,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ZvecProbeResult:
        return cls(
            supported=bool(data["supported"]),
            executable_path=data.get("executable_path"),
            version=data.get("version"),
            executable_hash=data.get("executable_hash"),
            local_only_verified=bool(data.get("local_only_verified", False)),
            error_message=data.get("error_message"),
        )
