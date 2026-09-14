"""Deterministic interval construction and Effective Development Ratio."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .events import PHASES

# Higher values win when intervals overlap.  Retry is intentionally below the
# work it encloses, so a retrying AI call remains AI execution in the exclusive
# wall-clock breakdown while retry_wall_time can still be read from raw events.
DEFAULT_PRECEDENCE: dict[str, int] = {
    "managed_validation": 100,
    "technical_review": 90,
    "plan_review": 80,
    "ai_execution": 70,
    "plan_remediation": 60,
    # An explicit owner gate suspends ordinary planning.  It therefore wins
    # over planning/retry/queue, but not over evidence that work actually ran.
    "owner_wait": 55,
    "planning": 50,
    "retry": 40,
    "queue": 20,
    "idle": 0,
}
PRODUCTIVE_PHASES = frozenset({"ai_execution", "managed_validation"})


def _time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError("timestamp must be an ISO-8601 string or datetime")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AccountedInterval:
    interval_id: str
    phase: str
    start: datetime
    end: datetime
    project_id: str | None = None
    task_id: str | None = None
    role: str | None = None
    request_id: str | None = None
    source_request_id: str | None = None
    attempt_id: str | None = None
    right_censored: bool = False
    clipped_start: bool = False
    clipped_end: bool = False
    attempt_outcome: str | None = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass(frozen=True, slots=True)
class IntervalBuildResult:
    intervals: tuple[AccountedInterval, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AccountingSummary:
    window_start: datetime
    window_end: datetime
    observed_seconds: float
    duration_by_phase: Mapping[str, float]
    accepted_productive_seconds: float
    rejected_attempt_seconds: float
    edr: float
    plan_review_churn: int
    retry_wall_time_seconds: float
    owner_wait_seconds: float
    longest_no_progress_seconds: float
    right_censored_intervals: int
    issues: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "observed_seconds": self.observed_seconds,
            "duration_by_phase": dict(self.duration_by_phase),
            "accepted_productive_seconds": self.accepted_productive_seconds,
            "rejected_attempt_seconds": self.rejected_attempt_seconds,
            "edr": self.edr,
            "plan_review_churn": self.plan_review_churn,
            "retry_wall_time_seconds": self.retry_wall_time_seconds,
            "owner_wait_seconds": self.owner_wait_seconds,
            "longest_no_progress_seconds": self.longest_no_progress_seconds,
            "right_censored_intervals": self.right_censored_intervals,
            "issues": list(self.issues),
        }


def _event_start_kind(event_type: Any) -> bool:
    return event_type in {"interval_started", "owner_gate_opened"}


def _event_end_kind(event_type: Any) -> bool:
    return event_type in {"interval_ended", "owner_gate_closed"}


def build_intervals(
    events: Iterable[Mapping[str, Any]],
    window_start: str | datetime,
    window_end: str | datetime,
    *,
    precedence: Mapping[str, int] | None = None,
    include_idle: bool = True,
) -> IntervalBuildResult:
    """Pair events, clip to a window and resolve overlap into exclusive time.

    Pairing is by explicit interval_id/gate_id only.  Missing ends are
    right-censored at ``window_end``; unmatched ends are reported and ignored.
    """
    start_bound = _time(window_start)
    end_bound = _time(window_end)
    if end_bound <= start_bound:
        raise ValueError("window_end must be after window_start")
    weights = dict(DEFAULT_PRECEDENCE if precedence is None else precedence)
    if set(weights) != set(PHASES):
        raise ValueError("precedence must define every closed phase exactly once")

    materialized = [dict(event) for event in events if isinstance(event, Mapping)]
    materialized.sort(
        key=lambda row: (
            _time(str(row.get("occurred_at"))),
            int(row.get("sequence") or 0),
            str(row.get("event_type") or ""),
        )
    )
    outcomes: dict[str, str] = {}
    for event in materialized:
        if (
            event.get("event_type") == "attempt_outcome"
            and event.get("attempt_id")
            and _time(str(event.get("occurred_at"))) <= end_bound
        ):
            outcomes[str(event["attempt_id"])] = str(event.get("outcome") or "unknown")

    opens: dict[str, dict[str, Any]] = {}
    raw: list[AccountedInterval] = []
    issues: list[str] = []

    def identity(event: Mapping[str, Any]) -> str | None:
        value = event.get("interval_id") or event.get("gate_id")
        return str(value) if isinstance(value, str) and value else None

    def phase_of(event: Mapping[str, Any]) -> str | None:
        if event.get("event_type") in {"owner_gate_opened", "owner_gate_closed"}:
            return "owner_wait"
        value = event.get("phase")
        return str(value) if value in PHASES else None

    def append_interval(opened: Mapping[str, Any], close_time: datetime, censored: bool) -> None:
        interval_id = identity(opened)
        phase = phase_of(opened)
        if interval_id is None or phase is None:
            return
        original_start = _time(str(opened["occurred_at"]))
        if close_time <= original_start:
            issues.append(f"interval {interval_id} ends at or before it starts")
            return
        left = max(original_start, start_bound)
        right = min(close_time, end_bound)
        if right <= left:
            return
        attempt_id = opened.get("attempt_id")
        raw.append(
            AccountedInterval(
                interval_id=interval_id,
                phase=phase,
                start=left,
                end=right,
                project_id=opened.get("project_id"),
                task_id=opened.get("task_id"),
                role=opened.get("role"),
                request_id=opened.get("request_id"),
                source_request_id=opened.get("source_request_id"),
                attempt_id=attempt_id,
                right_censored=censored,
                clipped_start=original_start < start_bound,
                clipped_end=close_time > end_bound,
                attempt_outcome=outcomes.get(str(attempt_id)) if attempt_id else None,
            )
        )

    for event in materialized:
        event_type = event.get("event_type")
        interval_id = identity(event)
        if interval_id is None or not (_event_start_kind(event_type) or _event_end_kind(event_type)):
            continue
        if _event_start_kind(event_type):
            if interval_id in opens:
                issues.append(f"duplicate interval start: {interval_id}")
                continue
            opens[interval_id] = event
            continue
        opened = opens.pop(interval_id, None)
        if opened is None:
            issues.append(f"unmatched interval end: {interval_id}")
            continue
        if phase_of(opened) != phase_of(event):
            issues.append(f"interval phase mismatch: {interval_id}")
            continue
        append_interval(opened, _time(str(event["occurred_at"])), False)

    for interval_id, opened in sorted(opens.items()):
        append_interval(opened, end_bound, True)

    boundaries = {start_bound, end_bound}
    for interval in raw:
        boundaries.add(interval.start)
        boundaries.add(interval.end)
    ordered = sorted(boundaries)
    resolved: list[AccountedInterval] = []
    for left, right in zip(ordered, ordered[1:]):
        if right <= left:
            continue
        active = [item for item in raw if item.start < right and item.end > left]
        if active:
            chosen = max(active, key=lambda item: (weights[item.phase], item.interval_id))
            piece = AccountedInterval(
                interval_id=chosen.interval_id,
                phase=chosen.phase,
                start=left,
                end=right,
                project_id=chosen.project_id,
                task_id=chosen.task_id,
                role=chosen.role,
                request_id=chosen.request_id,
                source_request_id=chosen.source_request_id,
                attempt_id=chosen.attempt_id,
                right_censored=chosen.right_censored,
                clipped_start=chosen.clipped_start and left == start_bound,
                clipped_end=chosen.clipped_end and right == end_bound,
                attempt_outcome=chosen.attempt_outcome,
            )
        elif include_idle:
            piece = AccountedInterval("idle", "idle", left, right)
        else:
            continue
        previous = resolved[-1] if resolved else None
        comparable = (
            previous is not None
            and previous.end == piece.start
            and previous.interval_id == piece.interval_id
            and previous.phase == piece.phase
            and previous.attempt_outcome == piece.attempt_outcome
        )
        if comparable:
            resolved[-1] = AccountedInterval(
                interval_id=previous.interval_id,
                phase=previous.phase,
                start=previous.start,
                end=piece.end,
                project_id=previous.project_id,
                task_id=previous.task_id,
                role=previous.role,
                request_id=previous.request_id,
                source_request_id=previous.source_request_id,
                attempt_id=previous.attempt_id,
                right_censored=previous.right_censored or piece.right_censored,
                clipped_start=previous.clipped_start,
                clipped_end=piece.clipped_end,
                attempt_outcome=previous.attempt_outcome,
            )
        else:
            resolved.append(piece)
    return IntervalBuildResult(tuple(resolved), tuple(issues))


construct_intervals = build_intervals


def summarize_accounting(
    events: Iterable[Mapping[str, Any]],
    window_start: str | datetime,
    window_end: str | datetime,
    *,
    precedence: Mapping[str, int] | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    role: str | None = None,
) -> AccountingSummary:
    materialized = [dict(event) for event in events if isinstance(event, Mapping)]
    for key, expected in (("project_id", project_id), ("task_id", task_id)):
        if expected is not None:
            materialized = [event for event in materialized if event.get(key) == expected]
    if role is not None:
        role_attempts = {
            str(event.get("attempt_id"))
            for event in materialized
            if event.get("role") == role and event.get("attempt_id")
        }
        materialized = [
            event
            for event in materialized
            if event.get("role") == role
            or (
                event.get("event_type") == "attempt_outcome"
                and str(event.get("attempt_id")) in role_attempts
            )
        ]
    built = build_intervals(materialized, window_start, window_end, precedence=precedence)
    start = _time(window_start)
    end = _time(window_end)
    durations = {phase: 0.0 for phase in PHASES}
    productive = 0.0
    rejected = 0.0
    longest_no_progress = 0.0
    current_no_progress = 0.0
    for interval in built.intervals:
        duration = interval.duration_seconds
        durations[interval.phase] += duration
        is_productive = (
            interval.phase in PRODUCTIVE_PHASES and interval.attempt_outcome == "accepted"
        )
        if is_productive:
            productive += duration
            longest_no_progress = max(longest_no_progress, current_no_progress)
            current_no_progress = 0.0
        else:
            current_no_progress += duration
        if interval.phase in PRODUCTIVE_PHASES and interval.attempt_outcome == "rejected":
            rejected += duration
    longest_no_progress = max(longest_no_progress, current_no_progress)
    observed = (end - start).total_seconds()
    churn = sum(
        1
        for event in materialized
        if event.get("event_type") == "attempt_outcome"
        and event.get("outcome") == "rejected"
        and (
            event.get("role") == "plan_reviewer"
            or (isinstance(event.get("metadata"), Mapping) and event["metadata"].get("review_kind") == "plan")
        )
    )
    retry_events = [
        event
        for event in materialized
        if event.get("phase") == "retry" or event.get("event_type") == "attempt_outcome"
    ]
    retry_raw = build_intervals(
        retry_events,
        start,
        end,
        precedence=precedence,
        include_idle=False,
    )
    retry_seconds = sum(
        item.duration_seconds for item in retry_raw.intervals if item.phase == "retry"
    )
    return AccountingSummary(
        window_start=start,
        window_end=end,
        observed_seconds=observed,
        duration_by_phase=durations,
        accepted_productive_seconds=productive,
        rejected_attempt_seconds=rejected,
        edr=productive / observed if observed > 0 else 0.0,
        plan_review_churn=churn,
        retry_wall_time_seconds=retry_seconds,
        owner_wait_seconds=durations["owner_wait"],
        longest_no_progress_seconds=longest_no_progress,
        right_censored_intervals=len(
            {item.interval_id for item in built.intervals if item.right_censored}
        ),
        issues=built.issues,
    )
