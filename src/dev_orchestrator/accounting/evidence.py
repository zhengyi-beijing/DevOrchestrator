"""Provider/context and RDC evidence analysis for P11.

All calculations consume explicit durable facts.  Missing timestamps, resource
identity, quota observations, or first-output evidence stay unavailable; this
module never manufactures them from error text or wall-clock guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Iterable, Mapping

from .events import ExecutionRecorder

_PROVIDER_STATUSES = {"succeeded", "failed", "cancelled", "no_candidate"}
_RDC_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled", "unknown"}
_DIMENSIONS = ("resource_id", "provider", "account", "model", "session_id")


def _time(value: str | datetime, name: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _optional_time(value: Any, name: str) -> datetime | None:
    return None if value is None else _time(value, name)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _nonblank(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank")
    return value.strip()


def _optional_nonnegative_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _metadata(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("metadata")
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class DimensionContinuity:
    hits: int = 0
    switches: int = 0
    unknown: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"hits": self.hits, "switches": self.switches, "unknown": self.unknown}


@dataclass(frozen=True, slots=True)
class FailoverEvidence:
    correlation_group: str
    from_request_id: str
    to_request_id: str
    from_resource_id: str | None
    to_resource_id: str | None
    latency_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "correlation_group": self.correlation_group,
            "from_request_id": self.from_request_id,
            "to_request_id": self.to_request_id,
            "from_resource_id": self.from_resource_id,
            "to_resource_id": self.to_resource_id,
            "latency_seconds": self.latency_seconds,
        }


@dataclass(frozen=True, slots=True)
class ProviderEvidenceSummary:
    result_count: int
    continuity: Mapping[str, DimensionContinuity]
    failovers: tuple[FailoverEvidence, ...]
    quota_observation_count: int
    rate_limit_observation_count: int
    unavailable_fields: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "result_count": self.result_count,
            "continuity": {key: value.as_dict() for key, value in self.continuity.items()},
            "resource_switches": self.continuity["resource_id"].switches,
            "context_hits": self.continuity["session_id"].hits,
            "context_switches": self.continuity["session_id"].switches,
            "failovers": [item.as_dict() for item in self.failovers],
            "failover_latency_seconds": sum(item.latency_seconds for item in self.failovers),
            "quota_observation_count": self.quota_observation_count,
            "rate_limit_observation_count": self.rate_limit_observation_count,
            "unavailable_fields": dict(self.unavailable_fields),
        }


def _provider_group(event: Mapping[str, Any]) -> str:
    metadata = _metadata(event)
    explicit = metadata.get("correlation_group")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    source = event.get("source_request_id")
    if isinstance(source, str) and source.strip():
        return source.strip()
    return "|".join(str(event.get(key) or "?") for key in ("project_id", "task_id", "role"))


def summarize_provider_evidence(
    events: Iterable[Mapping[str, Any]],
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    role: str | None = None,
) -> ProviderEvidenceSummary:
    """Summarize exact Broker results and correlated retry/failover evidence."""
    rows = [
        dict(event)
        for event in events
        if isinstance(event, Mapping)
        and event.get("event_type") == "provider_result_observed"
        and (project_id is None or event.get("project_id") == project_id)
        and (task_id is None or event.get("task_id") == task_id)
        and (role is None or event.get("role") == role)
    ]
    rows.sort(
        key=lambda event: (
            _time(str(event.get("occurred_at")), "occurred_at"),
            int(event.get("sequence") or 0),
            str(event.get("request_id") or ""),
        )
    )
    counters = {name: {"hits": 0, "switches": 0, "unknown": 0} for name in _DIMENSIONS}
    unavailable = {name: 0 for name in (*_DIMENSIONS, "started_at", "finished_at", "first_output_at")}
    prior_by_group: dict[str, Mapping[str, Any]] = {}
    failovers: list[FailoverEvidence] = []
    quota_count = 0
    rate_count = 0

    for event in rows:
        metadata = _metadata(event)
        group = _provider_group(event)
        prior = prior_by_group.get(group)
        prior_metadata = _metadata(prior) if prior is not None else {}
        explicit_previous = metadata.get("previous_resource_context")
        previous_resource = explicit_previous if isinstance(explicit_previous, Mapping) else None
        for dimension in _DIMENSIONS:
            current = event.get(dimension)
            if current is None:
                unavailable[dimension] += 1
            if dimension == "session_id":
                previous = metadata.get("previous_session_id")
                if previous is None and prior is not None:
                    previous = prior.get("session_id")
            else:
                previous = previous_resource.get(dimension) if previous_resource is not None else None
                if previous is None and prior is not None:
                    previous = prior.get(dimension)
            if current is None or previous is None:
                counters[dimension]["unknown"] += 1
            elif current == previous:
                counters[dimension]["hits"] += 1
            else:
                counters[dimension]["switches"] += 1

        for name in ("started_at", "finished_at", "first_output_at"):
            if metadata.get(name) is None:
                unavailable[name] += 1
        if isinstance(metadata.get("quota_observation"), Mapping):
            quota_count += 1
        if isinstance(metadata.get("rate_limit_observation"), Mapping):
            rate_count += 1

        if prior is not None and prior_metadata.get("status") in {"failed", "no_candidate"}:
            prior_resource_id = prior.get("resource_id")
            current_resource_id = event.get("resource_id")
            prior_finished = _optional_time(prior_metadata.get("finished_at"), "finished_at")
            current_started = _optional_time(metadata.get("started_at"), "started_at")
            if (
                prior_resource_id is not None
                and current_resource_id is not None
                and prior_resource_id != current_resource_id
                and prior_finished is not None
                and current_started is not None
                and current_started >= prior_finished
            ):
                failovers.append(
                    FailoverEvidence(
                        group,
                        str(prior.get("request_id")),
                        str(event.get("request_id")),
                        str(prior_resource_id),
                        str(current_resource_id),
                        (current_started - prior_finished).total_seconds(),
                    )
                )
        prior_by_group[group] = event

    return ProviderEvidenceSummary(
        len(rows),
        {name: DimensionContinuity(**counts) for name, counts in counters.items()},
        tuple(failovers),
        quota_count,
        rate_count,
        unavailable,
    )


@dataclass(frozen=True, slots=True)
class RDCInvocationEvidence:
    project_id: str
    invocation_id: str
    status: str
    occurred_at: datetime
    request_id: str | None = None
    connection_id: str | None = None
    connection_generation: int | None = None
    session_id: str | None = None
    command_bytes: int | None = None
    command_count: int | None = None
    submitted_at: datetime | None = None
    started_at: datetime | None = None
    first_output_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancelled_by_project_id: str | None = None
    reconnect_at: datetime | None = None
    affected_invocation_ids: tuple[str, ...] = ()

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None or self.finished_at < self.started_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def first_output_seconds(self) -> float | None:
        if self.started_at is None or self.first_output_at is None or self.first_output_at < self.started_at:
            return None
        return (self.first_output_at - self.started_at).total_seconds()

    @property
    def queue_seconds(self) -> float | None:
        if self.submitted_at is None or self.started_at is None or self.started_at < self.submitted_at:
            return None
        return (self.started_at - self.submitted_at).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "invocation_id": self.invocation_id,
            "request_id": self.request_id,
            "status": self.status,
            "occurred_at": _iso(self.occurred_at),
            "connection_id": self.connection_id,
            "connection_generation": self.connection_generation,
            "session_id": self.session_id,
            "command_bytes": self.command_bytes,
            "command_count": self.command_count,
            "submitted_at": _iso(self.submitted_at),
            "started_at": _iso(self.started_at),
            "first_output_at": _iso(self.first_output_at),
            "finished_at": _iso(self.finished_at),
            "duration_seconds": self.duration_seconds,
            "first_output_seconds": self.first_output_seconds,
            "queue_seconds": self.queue_seconds,
            "cancel_requested_at": _iso(self.cancel_requested_at),
            "cancelled_by_project_id": self.cancelled_by_project_id,
            "reconnect_at": _iso(self.reconnect_at),
            "affected_invocation_ids": list(self.affected_invocation_ids),
        }


def normalize_rdc_observation(value: Mapping[str, Any]) -> RDCInvocationEvidence:
    """Validate one normalized RDC row or durable RDC event."""
    if not isinstance(value, Mapping):
        raise TypeError("RDC observation must be a mapping")
    metadata = _metadata(value)
    source = {**metadata, **{key: item for key, item in value.items() if key != "metadata"}}
    project_id = _nonblank(source.get("project_id"), "project_id")
    invocation_id = _nonblank(source.get("invocation_id"), "invocation_id")
    status = _nonblank(source.get("status"), "status")
    if status not in _RDC_STATUSES:
        raise ValueError(f"status must be one of {sorted(_RDC_STATUSES)}")
    occurred = source.get("occurred_at") or source.get("finished_at") or source.get("started_at")
    occurred_at = _time(occurred, "occurred_at")
    affected_raw = source.get("affected_invocation_ids") or []
    if not isinstance(affected_raw, (list, tuple)):
        raise ValueError("affected_invocation_ids must be an array")
    affected = tuple(_nonblank(item, "affected_invocation_id") for item in affected_raw)
    result = RDCInvocationEvidence(
        project_id=str(project_id),
        invocation_id=str(invocation_id),
        status=str(status),
        occurred_at=occurred_at,
        request_id=_nonblank(source.get("request_id"), "request_id", optional=True),
        connection_id=_nonblank(source.get("connection_id"), "connection_id", optional=True),
        connection_generation=_optional_nonnegative_int(source.get("connection_generation"), "connection_generation"),
        session_id=_nonblank(source.get("session_id"), "session_id", optional=True),
        command_bytes=_optional_nonnegative_int(source.get("command_bytes"), "command_bytes"),
        command_count=_optional_nonnegative_int(source.get("command_count"), "command_count"),
        submitted_at=_optional_time(source.get("submitted_at"), "submitted_at"),
        started_at=_optional_time(source.get("started_at"), "started_at"),
        first_output_at=_optional_time(source.get("first_output_at"), "first_output_at"),
        finished_at=_optional_time(source.get("finished_at"), "finished_at"),
        cancel_requested_at=_optional_time(source.get("cancel_requested_at"), "cancel_requested_at"),
        cancelled_by_project_id=_nonblank(source.get("cancelled_by_project_id"), "cancelled_by_project_id", optional=True),
        reconnect_at=_optional_time(source.get("reconnect_at"), "reconnect_at"),
        affected_invocation_ids=affected,
    )
    if result.finished_at is not None and result.started_at is not None and result.finished_at < result.started_at:
        raise ValueError("finished_at must not precede started_at")
    if result.first_output_at is not None and result.started_at is not None and result.first_output_at < result.started_at:
        raise ValueError("first_output_at must not precede started_at")
    return result


def import_rdc_evidence(
    recorder: ExecutionRecorder,
    observations: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Append normalized RDC rows with deterministic replay identities."""
    normalized = [normalize_rdc_observation(item) for item in observations]
    normalized.sort(key=lambda item: (item.occurred_at, item.project_id, item.invocation_id))
    records: list[dict[str, Any]] = []
    for item in normalized:
        payload = item.as_dict()
        for key in ("project_id", "invocation_id", "request_id", "occurred_at", "status"):
            payload.pop(key, None)
        payload = {key: value for key, value in payload.items() if value is not None}
        records.append(
            recorder.record_rdc_invocation(
                item.project_id,
                item.invocation_id,
                status=item.status,
                occurred_at=_iso(item.occurred_at),
                event_id=f"rdc:{item.project_id}:{item.invocation_id}",
                request_id=item.request_id,
                connection_id=item.connection_id,
                session_id=item.session_id,
                metadata=payload,
            )
        )
    return tuple(records)


@dataclass(frozen=True, slots=True)
class RDCThresholds:
    hol_wait_seconds: float = 5.0
    starvation_seconds: float = 30.0
    deadlock_seconds: float = 120.0
    oversized_command_bytes: int = 65536
    oversized_command_count: int = 32

    def __post_init__(self) -> None:
        for name in ("hol_wait_seconds", "starvation_seconds", "deadlock_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        for name in ("oversized_command_bytes", "oversized_command_count"):
            _optional_nonnegative_int(getattr(self, name), name)

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class RDCFinding:
    kind: str
    project_ids: tuple[str, ...]
    invocation_ids: tuple[str, ...]
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "project_ids": list(self.project_ids),
            "invocation_ids": list(self.invocation_ids),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class RDCAnalysis:
    invocation_count: int
    findings: tuple[RDCFinding, ...]
    project_metrics: Mapping[str, Mapping[str, Any]]
    unavailable_fields: Mapping[str, int]
    thresholds: RDCThresholds

    def as_dict(self) -> dict[str, Any]:
        return {
            "invocation_count": self.invocation_count,
            "classifications": [item.as_dict() for item in self.findings],
            "project_metrics": {key: dict(value) for key, value in self.project_metrics.items()},
            "unavailable_fields": dict(self.unavailable_fields),
            "thresholds": self.thresholds.as_dict(),
        }


def _same_connection(left: RDCInvocationEvidence, right: RDCInvocationEvidence) -> bool:
    return (
        left.connection_id is not None
        and left.connection_id == right.connection_id
        and left.connection_generation is not None
        and left.connection_generation == right.connection_generation
    )


def _overlap(left: RDCInvocationEvidence, right: RDCInvocationEvidence) -> bool:
    if None in (left.started_at, left.finished_at, right.started_at, right.finished_at):
        return False
    return bool(left.started_at < right.finished_at and right.started_at < left.finished_at)


def classify_rdc_evidence(
    events: Iterable[Mapping[str, Any]],
    *,
    window_end: str | datetime,
    thresholds: RDCThresholds | None = None,
    project_id: str | None = None,
) -> RDCAnalysis:
    """Classify RDC contention using only normalized, explicit evidence."""
    limits = thresholds or RDCThresholds()
    rows = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("event_type") not in (None, "rdc_invocation_observed"):
            continue
        try:
            row = normalize_rdc_observation(event)
        except (TypeError, ValueError):
            continue
        if project_id is None or row.project_id == project_id:
            rows.append(row)
    rows.sort(key=lambda item: (item.occurred_at, item.project_id, item.invocation_id))
    end = _time(window_end, "window_end")
    by_id = {item.invocation_id: item for item in rows}
    findings: list[RDCFinding] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def add(kind: str, members: Iterable[RDCInvocationEvidence], **evidence: Any) -> None:
        selected = tuple(members)
        ids = tuple(sorted(item.invocation_id for item in selected))
        key = (kind, ids)
        if key in seen:
            return
        seen.add(key)
        findings.append(
            RDCFinding(kind, tuple(sorted({item.project_id for item in selected})), ids, evidence)
        )

    for item in rows:
        age = (end - item.started_at).total_seconds() if item.started_at is not None else None
        if (
            item.started_at is not None
            and item.finished_at is None
            and item.first_output_at is None
            and age is not None
            and age >= limits.deadlock_seconds
            and item.status in {"running", "unknown"}
        ):
            add("deadlock", (item,), age_seconds=age)

        queue_seconds = item.queue_seconds
        blockers = [
            other
            for other in rows
            if other.invocation_id != item.invocation_id
            and _same_connection(item, other)
            and item.submitted_at is not None
            and item.started_at is not None
            and other.started_at is not None
            and other.finished_at is not None
            and other.started_at <= item.submitted_at < other.finished_at
            and other.finished_at <= item.started_at
            and (
                (other.command_bytes or 0) >= limits.oversized_command_bytes
                or (other.command_count or 0) >= limits.oversized_command_count
            )
        ]
        if queue_seconds is not None and queue_seconds >= limits.hol_wait_seconds and blockers:
            blocker = blockers[0]
            add("head_of_line_blocking", (blocker, item), queue_seconds=queue_seconds)

        overtakers = [
            other
            for other in rows
            if other.invocation_id != item.invocation_id
            and _same_connection(item, other)
            and item.submitted_at is not None
            and item.started_at is not None
            and other.submitted_at is not None
            and other.started_at is not None
            and item.submitted_at < other.submitted_at
            and other.started_at < item.started_at
        ]
        if queue_seconds is not None and queue_seconds >= limits.starvation_seconds and len(overtakers) >= 2:
            add("starvation", (item, *overtakers), queue_seconds=queue_seconds, overtaken_by=len(overtakers))

        if item.cancelled_by_project_id and item.cancelled_by_project_id != item.project_id:
            add("session_coupling", (item,), cancelled_by_project_id=item.cancelled_by_project_id)
        if item.reconnect_at is not None and item.affected_invocation_ids:
            affected = [by_id[value] for value in item.affected_invocation_ids if value in by_id]
            cross_project = [value for value in affected if value.project_id != item.project_id]
            if cross_project:
                add("reconnect_contamination", (item, *cross_project), reconnect_at=_iso(item.reconnect_at))

    contaminated_ids = {
        invocation_id
        for finding in findings
        if finding.kind in {"session_coupling", "reconnect_contamination"}
        for invocation_id in finding.invocation_ids
    }
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if (
                left.project_id != right.project_id
                and _overlap(left, right)
                and left.invocation_id not in contaminated_ids
                and right.invocation_id not in contaminated_ids
                and left.status == right.status == "succeeded"
            ):
                add("isolated_concurrency", (left, right))

    metrics: dict[str, dict[str, Any]] = {}
    for project in sorted({item.project_id for item in rows}):
        project_rows = [item for item in rows if item.project_id == project]
        durations = [item.duration_seconds for item in project_rows if item.duration_seconds is not None]
        first_outputs = [item.first_output_seconds for item in project_rows if item.first_output_seconds is not None]
        metrics[project] = {
            "invocations": len(project_rows),
            "known_duration_seconds": sum(durations),
            "known_first_output_count": len(first_outputs),
            "max_first_output_seconds": max(first_outputs) if first_outputs else None,
            "known_command_bytes": sum(item.command_bytes or 0 for item in project_rows if item.command_bytes is not None),
            "known_command_count": sum(item.command_count or 0 for item in project_rows if item.command_count is not None),
        }
    unavailable = {
        name: sum(getattr(item, name) is None for item in rows)
        for name in (
            "command_bytes", "command_count", "started_at", "finished_at",
            "first_output_at", "connection_id", "connection_generation",
        )
    }
    findings.sort(key=lambda item: (item.kind, item.project_ids, item.invocation_ids))
    return RDCAnalysis(len(rows), tuple(findings), metrics, unavailable, limits)


def plan_rdc_recovery(
    events: Iterable[Mapping[str, Any]], project_id: str, action: str
) -> tuple[str, ...]:
    """Return project-isolated recovery targets; this function never mutates evidence."""
    _nonblank(project_id, "project_id")
    if action not in {"cancel", "reconnect"}:
        raise ValueError("action must be cancel or reconnect")
    targets: list[str] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("event_type") not in (None, "rdc_invocation_observed"):
            continue
        try:
            item = normalize_rdc_observation(event)
        except (TypeError, ValueError):
            continue
        if item.project_id == project_id and item.status in {"queued", "running", "unknown"}:
            targets.append(item.invocation_id)
    return tuple(sorted(set(targets)))
