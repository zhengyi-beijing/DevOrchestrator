from datetime import datetime, timezone

import json

from dev_orchestrator.accounting import AcceptanceThresholds, ExecutionEventStore, build_p11_report
from dev_orchestrator.cli import main


def ts(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def start(interval: str, phase: str, at: int, *, attempt: str | None = None) -> dict:
    return {
        "event_type": "interval_started",
        "event_id": "start:" + interval,
        "occurred_at": ts(at),
        "interval_id": interval,
        "phase": phase,
        "project_id": "devorchestrator",
        "task_id": "P11d",
        "role": "worker" if phase == "ai_execution" else "system",
        **({"attempt_id": attempt} if attempt else {}),
    }


def end(interval: str, phase: str, at: int) -> dict:
    return {
        "event_type": "interval_ended",
        "event_id": "end:" + interval,
        "occurred_at": ts(at),
        "interval_id": interval,
        "phase": phase,
        "project_id": "devorchestrator",
        "task_id": "P11d",
        "role": "worker" if phase == "ai_execution" else "system",
    }


def representative_events() -> list[dict]:
    return [
        start("work", "ai_execution", 0, attempt="attempt-1"),
        end("work", "ai_execution", 30),
        start("validation", "managed_validation", 30, attempt="attempt-1"),
        end("validation", "managed_validation", 40),
        start("review", "technical_review", 40),
        end("review", "technical_review", 50),
        {
            "event_type": "attempt_outcome", "event_id": "outcome:attempt-1",
            "occurred_at": ts(50), "attempt_id": "attempt-1", "outcome": "accepted",
            "project_id": "devorchestrator", "task_id": "P11d", "role": "reviewer",
        },
        {
            "event_type": "owner_gate_opened", "event_id": "gate:start", "gate_id": "gate-1",
            "occurred_at": ts(50), "project_id": "devorchestrator", "task_id": "P11d", "role": "owner",
        },
        {
            "event_type": "owner_gate_closed", "event_id": "gate:end", "gate_id": "gate-1",
            "occurred_at": ts(80), "project_id": "devorchestrator", "task_id": "P11d", "role": "owner",
        },
        {
            "event_type": "provider_result_observed", "event_id": "provider:r1",
            "occurred_at": ts(5), "request_id": "provider-r1", "project_id": "devorchestrator",
            "task_id": "P11d", "role": "worker", "resource_id": "res-a", "provider": "a",
            "account": "one", "model": "m1", "session_id": "s1",
            "metadata": {
                "status": "failed", "correlation_group": "attempts",
                "started_at": ts(0), "finished_at": ts(5),
                "rate_limit_observation": {
                    "kind": "provider_limit", "retry_after_seconds": 7,
                },
            },
        },
        {
            "event_type": "provider_result_observed", "event_id": "provider:r2",
            "occurred_at": ts(30), "request_id": "provider-r2", "project_id": "devorchestrator",
            "task_id": "P11d", "role": "worker", "resource_id": "res-b", "provider": "b",
            "account": "two", "model": "m2", "session_id": "s2",
            "metadata": {"status": "succeeded", "correlation_group": "attempts", "started_at": ts(10), "finished_at": ts(30)},
        },
        {
            "event_type": "rdc_invocation_observed", "event_id": "rdc:devorchestrator:dead",
            "occurred_at": ts(200), "project_id": "devorchestrator", "invocation_id": "dead",
            "task_id": "P11d", "role": "system", "connection_id": "rdc-1", "metadata": {
                "status": "running", "started_at": ts(0), "connection_generation": 1,
                "command_bytes": 100, "command_count": 1,
            },
        },
    ]


def test_report_combines_accounting_provider_rdc_and_ranks_dominant_bottleneck() -> None:
    report = build_p11_report(
        representative_events(), ts(0), ts(200),
        project_id="devorchestrator", task_id="P11d",
    ).as_dict()
    assert report["inference"] == "disabled"
    assert report["data_status"] == {
        "accounting": "measured", "provider": "measured", "rdc": "measured"
    }
    assert report["accounting"]["accepted_productive_seconds"] == 40
    assert report["accounting"]["edr"] == 0.2
    assert {
        (row["project_id"], row["task_id"], row["role"])
        for row in report["time_breakdown"]
    } == {
        ("devorchestrator", "P11d", "owner"),
        ("devorchestrator", "P11d", "system"),
        ("devorchestrator", "P11d", "worker"),
    }
    worker = next(row for row in report["time_breakdown"] if row["role"] == "worker")
    assert worker["accepted_productive_seconds"] == 30
    assert worker["edr"] == 0.15
    assert report["provider"]["context_switches"] == 1
    assert report["provider"]["failover_latency_seconds"] == 5
    assert report["rdc"]["classifications"][0]["kind"] == "deadlock"
    assert report["hypotheses"]["oversized_rdc_commands"]["observed"] is False
    assert report["hypotheses"]["ai_context_switching"]["observed"] is True
    assert report["hypotheses"]["quota_waits"]["known_wait_seconds"] == 7
    assert report["hypotheses"]["lifecycle_reviewer_stalls"]["lost_seconds"] == 40
    assert report["hypotheses"]["multi_project_contention"]["observed"] is None
    cross_project_gate = next(
        gate for gate in report["acceptance"]["gates"]
        if gate["name"] == "maximum_cross_project_contamination"
    )
    assert cross_project_gate["state"] == "unavailable"
    assert report["dominant_bottleneck"]["kind"] == "rdc_deadlock"
    assert report["dominant_bottleneck"]["evidence_ids"] == ["dead"]
    assert report["acceptance"]["status"] == "fail"


def test_report_does_not_turn_absent_sources_into_measured_zeroes() -> None:
    report = build_p11_report([], ts(0), ts(10)).as_dict()
    assert report["data_status"] == {
        "accounting": "unavailable", "provider": "unavailable", "rdc": "unavailable"
    }
    assert report["inference"] == "disabled"
    assert report["dominant_bottleneck"] is None
    assert report["time_breakdown"] == []
    assert report["acceptance"]["status"] == "insufficient_evidence"
    assert {gate["state"] for gate in report["acceptance"]["gates"]} == {"unavailable"}
    assert len(report["warnings"]) == 8
    assert all(item["status"] == "unavailable" for item in report["hypotheses"].values())


def test_report_window_excludes_provider_rdc_and_churn_outside_window() -> None:
    events = [
        start("old", "technical_review", -20),
        end("old", "technical_review", -10),
        {
            "event_type": "attempt_outcome", "occurred_at": ts(-10),
            "attempt_id": "old", "outcome": "rejected", "role": "plan_reviewer",
            "project_id": "devorchestrator", "task_id": "P11d",
        },
        {
            "event_type": "provider_result_observed", "occurred_at": ts(-5),
            "request_id": "old-provider", "project_id": "devorchestrator",
            "task_id": "P11d", "role": "worker", "metadata": {"status": "failed"},
        },
        {
            "event_type": "rdc_invocation_observed", "occurred_at": ts(15),
            "project_id": "devorchestrator", "invocation_id": "future-rdc",
            "metadata": {"status": "running", "started_at": ts(0)},
        },
    ]
    report = build_p11_report(events, ts(0), ts(10)).as_dict()
    assert report["data_status"] == {
        "accounting": "unavailable", "provider": "unavailable", "rdc": "unavailable"
    }
    assert report["accounting"]["plan_review_churn"] == 0
    assert report["provider"]["result_count"] == 0
    assert report["rdc"]["invocation_count"] == 0


def test_quota_observation_without_explicit_wait_does_not_invent_time_loss() -> None:
    event = {
        "event_type": "provider_result_observed", "occurred_at": ts(5),
        "request_id": "quota-only", "project_id": "devorchestrator",
        "task_id": "P11d", "role": "worker", "session_id": "s",
        "metadata": {"status": "failed", "quota_observation": {"remaining": 0}},
    }
    hypothesis = build_p11_report([event], ts(0), ts(10)).as_dict()["hypotheses"]["quota_waits"]
    assert hypothesis == {
        "status": "unavailable", "observed": None, "observation_count": 1,
        "known_wait_seconds": None, "evidence_ids": [],
    }


def test_partial_rdc_command_metrics_do_not_disprove_oversized_hypothesis() -> None:
    event = {
        "event_type": "rdc_invocation_observed", "occurred_at": ts(5),
        "project_id": "devorchestrator", "invocation_id": "partial-command",
        "metadata": {"status": "succeeded", "command_bytes": 100},
    }
    hypothesis = build_p11_report([event], ts(0), ts(10)).as_dict()["hypotheses"]["oversized_rdc_commands"]
    assert hypothesis["status"] == "unavailable"
    assert hypothesis["observed"] is None


def test_quantitative_gates_pass_with_complete_evidence() -> None:
    events = [
        start("work", "ai_execution", 0, attempt="ok"),
        end("work", "ai_execution", 80),
        {
            "event_type": "attempt_outcome", "occurred_at": ts(80),
            "attempt_id": "ok", "outcome": "accepted", "project_id": "devorchestrator",
            "task_id": "P11d", "role": "reviewer",
        },
        {
            "event_type": "provider_result_observed", "occurred_at": ts(80),
            "request_id": "r1", "project_id": "devorchestrator", "task_id": "P11d",
            "role": "worker", "resource_id": "res", "provider": "p", "account": "a",
            "model": "m", "session_id": "s", "metadata": {
                "status": "succeeded", "correlation_group": "g",
                "previous_session_id": "s", "started_at": ts(0), "finished_at": ts(80),
            },
        },
        {
            "event_type": "rdc_invocation_observed", "occurred_at": ts(20),
            "project_id": "devorchestrator", "invocation_id": "i1", "connection_id": "c",
            "metadata": {"status": "succeeded", "connection_generation": 1,
                         "started_at": ts(10), "first_output_at": ts(11), "finished_at": ts(20)},
        },
        {
            "event_type": "rdc_invocation_observed", "occurred_at": ts(20),
            "project_id": "isolated-peer", "invocation_id": "i2", "connection_id": "other",
            "metadata": {"status": "succeeded", "connection_generation": 1,
                         "started_at": ts(12), "first_output_at": ts(13), "finished_at": ts(19)},
        },
    ]
    report = build_p11_report(
        events, ts(0), ts(100),
        acceptance_thresholds=AcceptanceThresholds(minimum_edr=0.75),
    ).as_dict()
    assert report["acceptance"]["status"] == "pass"
    assert {gate["state"] for gate in report["acceptance"]["gates"]} == {"pass"}
    assert report["hypotheses"]["multi_project_contention"]["observed"] is False


def test_scope_filter_excludes_other_projects() -> None:
    events = representative_events() + [
        {
            "event_type": "rdc_invocation_observed", "occurred_at": ts(0),
            "project_id": "other", "invocation_id": "other-dead",
            "metadata": {"status": "running", "started_at": ts(0)},
        }
    ]
    report = build_p11_report(events, ts(0), ts(200), project_id="devorchestrator").as_dict()
    assert report["rdc"]["invocation_count"] == 1
    assert "other-dead" not in str(report)


def test_role_scope_keeps_linked_review_outcome_for_worker_edr() -> None:
    report = build_p11_report(
        representative_events(), ts(0), ts(200),
        project_id="devorchestrator", task_id="P11d", role="worker",
    ).as_dict()
    assert report["accounting"]["accepted_productive_seconds"] == 30
    assert report["accounting"]["edr"] == 0.15
    assert report["provider"]["result_count"] == 2
    assert report["rdc"]["invocation_count"] == 0


def test_representative_devorchestrator_cli_run_emits_complete_machine_report(tmp_path, capsys) -> None:
    runtime = tmp_path / "runtime"
    store = ExecutionEventStore(runtime)
    for event in representative_events():
        store.append(event)
    assert main([
        "execution-report",
        "--runtime-root", str(runtime),
        "--window-start", ts(0),
        "--window-end", ts(200),
        "--project-id", "devorchestrator",
        "--task-id", "P11d",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["accounting"]["edr"] == 0.2
    assert payload["dominant_bottleneck"]["evidence_ids"]
    assert payload["acceptance"]["status"] == "fail"
    assert main([
        "execution-report",
        "--runtime-root", str(runtime),
        "--window-start", ts(0),
        "--window-end", ts(200),
        "--fail-on-gate",
    ]) == 1


def test_cli_config_reads_custom_ledger_without_mutating_runtime(tmp_path, capsys) -> None:
    runtime = tmp_path / "runtime"
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({
        "execution_accounting": {
            "enabled": True,
            "event_path": "custom/evidence.jsonl",
        }
    }), encoding="utf-8")
    store = ExecutionEventStore(runtime, relative_path="custom/evidence.jsonl")
    for event in representative_events():
        store.append(event)
    manifest = runtime / "execution-accounting" / "runtime.json"
    assert not manifest.exists()
    assert main([
        "execution-report",
        "--config", str(config),
        "--runtime-root", str(runtime),
        "--window-start", ts(0),
        "--window-end", ts(200),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"]["result_count"] == 2
    assert not manifest.exists()
