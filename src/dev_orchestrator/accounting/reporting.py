"""Unified P11 reporting, bottleneck ranking, and quantitative gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from math import isfinite
from typing import Any, Iterable, Mapping

from .evidence import RDCThresholds, classify_rdc_evidence, summarize_provider_evidence
from .events import ExecutionEventStore
from .intervals import build_intervals, summarize_accounting


def _time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("report timestamps must be ISO-8601") from exc
    else:
        raise ValueError("report timestamps must be ISO-8601")
    if parsed.tzinfo is None:
        raise ValueError("report timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def reporting_event_store(runtime_root: Path | str) -> ExecutionEventStore:
    """Resolve the live accounting path recorded by the enabled runtime."""
    runtime = Path(runtime_root)
    manifest_path = runtime / "execution-accounting" / "runtime.json"
    if not manifest_path.exists():
        return ExecutionEventStore(runtime)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid execution accounting runtime manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("invalid execution accounting runtime manifest schema")
    event_path = manifest.get("event_path")
    if not isinstance(event_path, str) or not event_path.strip():
        raise ValueError("execution accounting runtime manifest event_path is invalid")
    return ExecutionEventStore(runtime, relative_path=event_path.strip())


def _nonnegative(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class AcceptanceThresholds:
    minimum_edr: float = 0.25
    maximum_owner_wait_ratio: float = 0.25
    maximum_retry_ratio: float = 0.20
    maximum_context_switches: int = 2
    maximum_failover_latency_seconds: float = 300.0
    maximum_rdc_deadlocks: int = 0
    maximum_cross_project_contamination: int = 0

    def __post_init__(self) -> None:
        for name in (
            "minimum_edr",
            "maximum_owner_wait_ratio",
            "maximum_retry_ratio",
            "maximum_failover_latency_seconds",
        ):
            _nonnegative(name, getattr(self, name))
        for name in (
            "maximum_context_switches",
            "maximum_rdc_deadlocks",
            "maximum_cross_project_contamination",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class AcceptanceGate:
    name: str
    state: str
    actual: float | int | None
    operator: str
    threshold: float | int
    provenance: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "actual": self.actual,
            "operator": self.operator,
            "threshold": self.threshold,
            "provenance": self.provenance,
        }


@dataclass(frozen=True, slots=True)
class Bottleneck:
    kind: str
    lost_seconds: float
    provenance: str
    evidence_ids: tuple[str, ...]
    recommendation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "lost_seconds": self.lost_seconds,
            "provenance": self.provenance,
            "evidence_ids": list(self.evidence_ids),
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True, slots=True)
class P11Report:
    window_start: datetime
    window_end: datetime
    project_id: str | None
    task_id: str | None
    role: str | None
    data_status: Mapping[str, str]
    accounting: Mapping[str, Any]
    provider: Mapping[str, Any]
    rdc: Mapping[str, Any]
    time_breakdown: tuple[Mapping[str, Any], ...]
    hypotheses: Mapping[str, Mapping[str, Any]]
    bottlenecks: tuple[Bottleneck, ...]
    gates: tuple[AcceptanceGate, ...]
    acceptance_status: str
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
                "observed_seconds": (self.window_end - self.window_start).total_seconds(),
            },
            "scope": {
                "project_id": self.project_id,
                "task_id": self.task_id,
                "role": self.role,
            },
            "inference": "disabled",
            "data_status": dict(self.data_status),
            "accounting": dict(self.accounting),
            "provider": dict(self.provider),
            "rdc": dict(self.rdc),
            "time_breakdown": [dict(item) for item in self.time_breakdown],
            "hypotheses": {key: dict(value) for key, value in self.hypotheses.items()},
            "dominant_bottleneck": self.bottlenecks[0].as_dict() if self.bottlenecks else None,
            "bottlenecks": [item.as_dict() for item in self.bottlenecks],
            "acceptance": {
                "status": self.acceptance_status,
                "gates": [gate.as_dict() for gate in self.gates],
            },
            "warnings": list(self.warnings),
        }


def _event_id(event: Mapping[str, Any]) -> str:
    for key in ("event_id", "request_id", "invocation_id", "interval_id", "gate_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    sequence = event.get("sequence")
    return f"sequence:{sequence}" if isinstance(sequence, int) else "unidentified"


def _refs(events: Iterable[Mapping[str, Any]], predicate) -> tuple[str, ...]:
    return tuple(sorted({_event_id(event) for event in events if predicate(event)}))


def _gate(
    name: str,
    actual: float | int | None,
    operator: str,
    threshold: float | int,
    available: bool,
) -> AcceptanceGate:
    if not available or actual is None:
        state = "unavailable"
        provenance = "unavailable"
    else:
        passed = actual >= threshold if operator == ">=" else actual <= threshold
        state = "pass" if passed else "fail"
        provenance = "derived_from_measured"
    return AcceptanceGate(name, state, actual if available else None, operator, threshold, provenance)


def build_p11_report(
    events: Iterable[Mapping[str, Any]],
    window_start: str | datetime,
    window_end: str | datetime,
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    role: str | None = None,
    acceptance_thresholds: AcceptanceThresholds | None = None,
    rdc_thresholds: RDCThresholds | None = None,
) -> P11Report:
    """Build one deterministic report over an explicit UTC window and scope."""
    start = _time(window_start)
    end = _time(window_end)
    if end <= start:
        raise ValueError("window_end must be after window_start")
    materialized = [dict(event) for event in events if isinstance(event, Mapping)]
    project_scoped = [
        event
        for event in materialized
        if (project_id is None or event.get("project_id") == project_id)
        and (task_id is None or event.get("task_id") == task_id)
    ]
    role_scoped = [
        event for event in project_scoped
        if role is None or event.get("role") == role
    ]
    if role is None:
        accounting_scoped = project_scoped
    else:
        role_attempts = {
            str(event.get("attempt_id"))
            for event in project_scoped
            if event.get("role") == role and event.get("attempt_id")
        }
        accounting_scoped = [
            event for event in project_scoped
            if event.get("role") == role
            or (
                event.get("event_type") == "attempt_outcome"
                and str(event.get("attempt_id")) in role_attempts
            )
        ]
    scoped = [
        event
        for event in role_scoped
        if start <= _time(str(event.get("occurred_at"))) <= end
    ]

    accounting_summary = summarize_accounting(
        project_scoped, start, end, role=role
    ).as_dict()
    accounted = build_intervals(accounting_scoped, start, end, include_idle=False)
    accounting_available = bool(accounted.intervals)
    provider_summary = summarize_provider_evidence(scoped).as_dict()
    rdc_summary = classify_rdc_evidence(
        scoped, window_end=end, thresholds=rdc_thresholds
    ).as_dict()
    provider_available = provider_summary["result_count"] > 0
    rdc_available = rdc_summary["invocation_count"] > 0
    statuses = {
        "accounting": "measured" if accounting_available else "unavailable",
        "provider": "measured" if provider_available else "unavailable",
        "rdc": "measured" if rdc_available else "unavailable",
    }
    breakdown: list[dict[str, Any]] = []
    breakdown_keys = sorted(
        {
            (event.get("project_id"), event.get("task_id"), event.get("role"))
            for event in accounting_scoped
            if event.get("event_type") in {"interval_started", "owner_gate_opened"}
        },
        key=lambda item: tuple("" if value is None else str(value) for value in item),
    )
    for breakdown_project, breakdown_task, breakdown_role in breakdown_keys:
        group_events = [
            event
            for event in materialized
            if event.get("project_id") == breakdown_project
            and event.get("task_id") == breakdown_task
        ]
        if breakdown_role is None:
            role_attempts = {
                str(event.get("attempt_id"))
                for event in group_events
                if event.get("role") is None and event.get("attempt_id")
            }
            group_events = [
                event for event in group_events
                if event.get("role") is None
                or (
                    event.get("event_type") == "attempt_outcome"
                    and str(event.get("attempt_id")) in role_attempts
                )
            ]
            row = summarize_accounting(group_events, start, end).as_dict()
        else:
            row = summarize_accounting(group_events, start, end, role=breakdown_role).as_dict()
        group_accounted = build_intervals(group_events, start, end, include_idle=False)
        if not group_accounted.intervals:
            continue
        breakdown.append({
            "project_id": breakdown_project,
            "task_id": breakdown_task,
            "role": breakdown_role,
            "provenance": "derived_from_measured",
            "duration_by_phase": row["duration_by_phase"],
            "accepted_productive_seconds": row["accepted_productive_seconds"],
            "rejected_attempt_seconds": row["rejected_attempt_seconds"],
            "edr": row["edr"],
            "plan_review_churn": row["plan_review_churn"],
            "retry_wall_time_seconds": row["retry_wall_time_seconds"],
            "owner_wait_seconds": row["owner_wait_seconds"],
        })
    warnings: list[str] = []
    for source, status in statuses.items():
        if status == "unavailable":
            warnings.append(f"{source} evidence unavailable for the selected scope")
    for field, count in provider_summary["unavailable_fields"].items():
        if count:
            warnings.append(f"provider.{field} unavailable for {count} result(s)")
    for field, count in rdc_summary["unavailable_fields"].items():
        if count:
            warnings.append(f"rdc.{field} unavailable for {count} invocation(s)")

    provider_events = [
        event for event in scoped if event.get("event_type") == "provider_result_observed"
    ]
    rdc_events = [
        event for event in scoped if event.get("event_type") == "rdc_invocation_observed"
    ]
    explicit_rate_waits: list[float] = []
    quota_refs: list[str] = []
    wait_refs: list[str] = []
    for event in provider_events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        rate = metadata.get("rate_limit_observation")
        quota = metadata.get("quota_observation")
        if isinstance(rate, Mapping) or isinstance(quota, Mapping):
            quota_refs.append(_event_id(event))
        retry_after = rate.get("retry_after_seconds") if isinstance(rate, Mapping) else None
        if (
            not isinstance(retry_after, bool)
            and isinstance(retry_after, (int, float))
            and isfinite(retry_after)
            and retry_after >= 0
        ):
            explicit_rate_waits.append(float(retry_after))
            wait_refs.append(_event_id(event))
    oversized_refs: list[str] = []
    command_complete = bool(rdc_events)
    for event in rdc_events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        command_bytes = metadata.get("command_bytes")
        command_count = metadata.get("command_count")
        bytes_known = isinstance(command_bytes, int) and not isinstance(command_bytes, bool)
        count_known = isinstance(command_count, int) and not isinstance(command_count, bool)
        command_complete = command_complete and bytes_known and count_known
        if (
            bytes_known
            and command_bytes >= rdc_summary["thresholds"]["oversized_command_bytes"]
        ) or (
            count_known
            and command_count >= rdc_summary["thresholds"]["oversized_command_count"]
        ):
            oversized_refs.append(_event_id(event))
    multi_project_contention = [
        item for item in rdc_summary["classifications"]
        if item["kind"] in {"session_coupling", "reconnect_contamination"}
        or (
            len(item["project_ids"]) > 1
            and item["kind"] in {"head_of_line_blocking", "starvation", "deadlock"}
        )
    ]
    cross_project_evidence = any(
        item["kind"] in {
            "isolated_concurrency", "session_coupling", "reconnect_contamination",
        }
        or len(item["project_ids"]) > 1
        for item in rdc_summary["classifications"]
    )
    review_stall_seconds = (
        float(accounting_summary["duration_by_phase"].get("plan_review") or 0.0)
        + float(accounting_summary["duration_by_phase"].get("technical_review") or 0.0)
        + float(accounting_summary["owner_wait_seconds"])
    )
    session_known = (
        provider_available
        and provider_summary["unavailable_fields"]["session_id"] == 0
    )
    hypotheses = {
        "oversized_rdc_commands": {
            "status": "measured" if oversized_refs or command_complete else "unavailable",
            "observed": bool(oversized_refs) if oversized_refs or command_complete else None,
            "count": len(oversized_refs) if oversized_refs or command_complete else None,
            "evidence_ids": sorted(set(oversized_refs)),
        },
        "ai_context_switching": {
            "status": "derived_from_measured" if session_known else "unavailable",
            "observed": bool(provider_summary["context_switches"]) if session_known else None,
            "count": provider_summary["context_switches"] if session_known else None,
            "evidence_ids": sorted(_event_id(event) for event in provider_events) if session_known else [],
        },
        "quota_waits": {
            "status": "derived_from_measured" if wait_refs else "unavailable",
            "observed": sum(explicit_rate_waits) > 0 if wait_refs else None,
            "observation_count": len(quota_refs) if quota_refs else 0,
            "known_wait_seconds": sum(explicit_rate_waits) if wait_refs else None,
            "evidence_ids": sorted(set(wait_refs)),
        },
        "lifecycle_reviewer_stalls": {
            "status": "derived_from_measured" if accounting_available else "unavailable",
            "observed": review_stall_seconds > 0 if accounting_available else None,
            "lost_seconds": review_stall_seconds if accounting_available else None,
            "evidence_ids": sorted({
                item.interval_id for item in accounted.intervals
                if item.phase in {"plan_review", "technical_review", "owner_wait"}
            }),
        },
        "multi_project_contention": {
            "status": "derived_from_measured" if cross_project_evidence else "unavailable",
            "observed": bool(multi_project_contention) if cross_project_evidence else None,
            "finding_count": len(multi_project_contention) if cross_project_evidence else None,
            "evidence_ids": sorted({
                invocation_id
                for item in multi_project_contention
                for invocation_id in item["invocation_ids"]
            }),
        },
    }
    for name, hypothesis in hypotheses.items():
        if hypothesis["status"] == "unavailable":
            warnings.append(f"hypothesis.{name} cannot be evaluated from available evidence")

    candidates: list[Bottleneck] = []
    recommendations = {
        "idle": "Inspect lifecycle gaps and missing progress boundaries before changing routing.",
        "owner_wait": "Reduce or batch owner gates while retaining explicit authorization boundaries.",
        "plan_review": "Inspect rejection evidence and tighten the plan contract where churn repeats.",
        "technical_review": "Inspect reviewer queue/execution evidence and validation scope.",
        "queue": "Inspect dispatch and transport queue ownership for avoidable serialization.",
        "retry": "Remove the evidenced retry cause before increasing retry limits.",
        "rejected_attempt": "Use review evidence to prevent repeated rejected implementation work.",
        "provider_failover": "Inspect explicit quota/rate-limit evidence and continuity before policy changes.",
        "quota_wait": "Use the explicit retry-after/reset evidence before changing provider policy.",
        "rdc_head_of_line": "Split oversized RDC commands or isolate connections after confirming the probe.",
        "rdc_starvation": "Enforce fair project-scoped RDC admission after confirming repeated starvation.",
        "rdc_deadlock": "Terminate only the affected project invocation and preserve other project sessions.",
    }
    durations = accounting_summary["duration_by_phase"]
    if accounting_available:
        for phase in ("idle", "owner_wait", "plan_review", "technical_review", "queue"):
            seconds = float(durations.get(phase) or 0.0)
            if seconds > 0:
                candidates.append(Bottleneck(
                    phase, seconds, "derived_from_measured",
                    tuple(sorted({
                        item.interval_id for item in accounted.intervals if item.phase == phase
                    })),
                    recommendations[phase],
                ))
        retry_seconds = float(accounting_summary["retry_wall_time_seconds"])
        if retry_seconds > 0:
            candidates.append(Bottleneck(
                "retry", retry_seconds, "derived_from_measured",
                tuple(sorted({
                    item.interval_id for item in accounted.intervals if item.phase == "retry"
                })), recommendations["retry"],
            ))
        rejected = float(accounting_summary["rejected_attempt_seconds"])
        if rejected > 0:
            rejected_attempts = {
                item.attempt_id for item in accounted.intervals
                if item.attempt_outcome == "rejected" and item.attempt_id is not None
            }
            candidates.append(Bottleneck(
                "rejected_attempt", rejected, "derived_from_measured",
                _refs(
                    accounting_scoped,
                    lambda event: event.get("event_type") == "attempt_outcome"
                    and event.get("attempt_id") in rejected_attempts,
                ),
                recommendations["rejected_attempt"],
            ))
    failover_seconds = float(provider_summary["failover_latency_seconds"])
    if provider_available and failover_seconds > 0:
        candidates.append(Bottleneck(
            "provider_failover", failover_seconds, "derived_from_measured",
            tuple(sorted({
                request_id
                for row in provider_summary["failovers"]
                for request_id in (row["from_request_id"], row["to_request_id"])
            })),
            recommendations["provider_failover"],
        ))
    quota_wait_seconds = sum(explicit_rate_waits)
    if quota_wait_seconds > 0:
        candidates.append(Bottleneck(
            "quota_wait", quota_wait_seconds, "derived_from_measured",
            tuple(sorted(set(wait_refs))), recommendations["quota_wait"],
        ))
    for finding in rdc_summary["classifications"]:
        evidence = finding["evidence"]
        if finding["kind"] == "head_of_line_blocking":
            kind, seconds = "rdc_head_of_line", float(evidence.get("queue_seconds") or 0.0)
        elif finding["kind"] == "starvation":
            kind, seconds = "rdc_starvation", float(evidence.get("queue_seconds") or 0.0)
        elif finding["kind"] == "deadlock":
            kind, seconds = "rdc_deadlock", float(evidence.get("age_seconds") or 0.0)
        else:
            continue
        if seconds > 0:
            candidates.append(Bottleneck(
                kind, seconds, "derived_from_measured", tuple(finding["invocation_ids"]),
                recommendations[kind],
            ))
    candidates.sort(key=lambda item: (-item.lost_seconds, item.kind, item.evidence_ids))

    limits = acceptance_thresholds or AcceptanceThresholds()
    observed = float(accounting_summary["observed_seconds"])
    owner_ratio = float(accounting_summary["owner_wait_seconds"]) / observed if observed else None
    retry_ratio = float(accounting_summary["retry_wall_time_seconds"]) / observed if observed else None
    provider_session_known = (
        provider_available
        and provider_summary["unavailable_fields"]["session_id"] == 0
    )
    provider_timing_known = (
        provider_available
        and provider_summary["unavailable_fields"]["started_at"] == 0
        and provider_summary["unavailable_fields"]["finished_at"] == 0
    )
    classifications = rdc_summary["classifications"]
    deadlocks = sum(item["kind"] == "deadlock" for item in classifications)
    contamination = sum(
        item["kind"] in {"session_coupling", "reconnect_contamination"}
        for item in classifications
    )
    gates = (
        _gate("minimum_edr", accounting_summary["edr"], ">=", limits.minimum_edr, accounting_available),
        _gate("maximum_owner_wait_ratio", owner_ratio, "<=", limits.maximum_owner_wait_ratio, accounting_available),
        _gate("maximum_retry_ratio", retry_ratio, "<=", limits.maximum_retry_ratio, accounting_available),
        _gate("maximum_context_switches", provider_summary["context_switches"], "<=", limits.maximum_context_switches, provider_session_known),
        _gate("maximum_failover_latency_seconds", failover_seconds, "<=", limits.maximum_failover_latency_seconds, provider_timing_known),
        _gate("maximum_rdc_deadlocks", deadlocks, "<=", limits.maximum_rdc_deadlocks, rdc_available),
        _gate(
            "maximum_cross_project_contamination", contamination, "<=",
            limits.maximum_cross_project_contamination, cross_project_evidence,
        ),
    )
    if any(gate.state == "fail" for gate in gates):
        acceptance = "fail"
    elif any(gate.state == "unavailable" for gate in gates):
        acceptance = "insufficient_evidence"
    else:
        acceptance = "pass"
    return P11Report(
        start,
        end,
        project_id,
        task_id,
        role,
        statuses,
        accounting_summary,
        provider_summary,
        rdc_summary,
        tuple(breakdown),
        hypotheses,
        tuple(candidates),
        gates,
        acceptance,
        tuple(warnings),
    )
