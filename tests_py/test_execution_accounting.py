from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dev_orchestrator.accounting import build_intervals, summarize_accounting


def ts(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def start(interval_id: str, phase: str, at: int, *, attempt: str | None = None, role: str | None = None) -> dict:
    row = {
        "event_type": "interval_started",
        "interval_id": interval_id,
        "phase": phase,
        "occurred_at": ts(at),
    }
    if attempt:
        row["attempt_id"] = attempt
    if role:
        row["role"] = role
    return row


def end(interval_id: str, phase: str, at: int) -> dict:
    return {
        "event_type": "interval_ended",
        "interval_id": interval_id,
        "phase": phase,
        "occurred_at": ts(at),
    }


def outcome(attempt: str, value: str, at: int, **extra: object) -> dict:
    return {
        "event_type": "attempt_outcome",
        "attempt_id": attempt,
        "outcome": value,
        "occurred_at": ts(at),
        **extra,
    }


def test_clipping_right_censoring_overlap_precedence_and_no_double_counting() -> None:
    events = [
        start("plan", "planning", 0),
        end("plan", "planning", 30),
        start("review", "plan_review", 10),
        end("review", "plan_review", 20),
        start("gate", "owner_wait", 25),
    ]
    built = build_intervals(events, ts(5), ts(40))
    assert [(row.phase, row.duration_seconds) for row in built.intervals] == [
        ("planning", 5.0),
        ("plan_review", 10.0),
        ("planning", 5.0),
        ("owner_wait", 15.0),
    ]
    assert sum(item.duration_seconds for item in built.intervals) == 35.0
    assert built.intervals[0].clipped_start
    assert built.intervals[-1].right_censored


def test_edr_counts_only_accepted_worker_and_validation_attempts() -> None:
    events = [
        start("worker-a", "ai_execution", 0, attempt="a", role="worker"),
        end("worker-a", "ai_execution", 10),
        outcome("a", "rejected", 12),
        start("worker-b", "ai_execution", 20, attempt="b", role="remediation_worker"),
        end("worker-b", "ai_execution", 35),
        start("validation-b", "managed_validation", 35, attempt="b", role="validator"),
        end("validation-b", "managed_validation", 40),
        outcome("b", "accepted", 45),
    ]
    summary = summarize_accounting(events, ts(0), ts(50))
    assert summary.accepted_productive_seconds == 20.0
    assert summary.rejected_attempt_seconds == 10.0
    assert summary.edr == pytest.approx(0.4)
    assert summary.longest_no_progress_seconds == 20.0


def test_owner_gate_requires_explicit_id_and_unmatched_gate_is_censored() -> None:
    events = [
        {"event_type": "owner_gate_opened", "gate_id": "g1", "occurred_at": ts(5)},
        {"event_type": "owner_gate_closed", "gate_id": "other", "occurred_at": ts(8)},
    ]
    built = build_intervals(events, ts(0), ts(20))
    owner = [row for row in built.intervals if row.phase == "owner_wait"]
    assert len(owner) == 1
    assert owner[0].duration_seconds == 15.0
    assert owner[0].right_censored
    assert "unmatched interval end: other" in built.issues


def test_plan_reject_remediation_fixture_reports_churn_retry_and_accepted_work() -> None:
    events = [
        start("planning-1", "planning", 0), end("planning-1", "planning", 5),
        start("review-1", "plan_review", 5), end("review-1", "plan_review", 8),
        outcome("plan-1", "rejected", 8, role="plan_reviewer", metadata={"review_kind": "plan"}),
        start("remediate", "plan_remediation", 8), end("remediate", "plan_remediation", 13),
        start("review-2", "plan_review", 13), end("review-2", "plan_review", 15),
        start("retry", "retry", 15), end("retry", "retry", 17),
        start("worker", "ai_execution", 17, attempt="worker"), end("worker", "ai_execution", 27),
        start("tech", "technical_review", 27), end("tech", "technical_review", 30),
        outcome("worker", "accepted", 30),
    ]
    summary = summarize_accounting(events, ts(0), ts(30))
    assert summary.plan_review_churn == 1
    assert summary.retry_wall_time_seconds == 2.0
    assert summary.accepted_productive_seconds == 10.0
    assert summary.edr == pytest.approx(1 / 3)
