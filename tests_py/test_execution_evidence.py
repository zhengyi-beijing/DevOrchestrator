import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from dev_orchestrator.accounting import (
    EventCorruptionError,
    ExecutionEventStore,
    ExecutionRecorder,
    RDCThresholds,
    classify_rdc_evidence,
    import_rdc_evidence,
    plan_rdc_recovery,
    summarize_provider_evidence,
)
from dev_orchestrator.ai import AIBrokerClientConfig, AIBrokerExecutionPort, AIRoleRequest
from dev_orchestrator.cli import main


def ts(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def provider_event(
    request_id: str,
    status: str,
    at: int,
    *,
    resource: str | None,
    provider: str | None,
    account: str | None,
    model: str | None,
    session: str | None,
    started: int | None,
    finished: int | None,
    group: str = "group-1",
    **extra,
) -> dict:
    metadata = {
        "status": status,
        "correlation_group": group,
        "started_at": ts(started) if started is not None else None,
        "finished_at": ts(finished) if finished is not None else None,
        **extra,
    }
    return {
        "event_type": "provider_result_observed",
        "occurred_at": ts(at),
        "request_id": request_id,
        "project_id": "p1",
        "task_id": "P11c",
        "role": "worker",
        "resource_id": resource,
        "provider": provider,
        "account": account,
        "model": model,
        "session_id": session,
        "metadata": {key: value for key, value in metadata.items() if value is not None},
    }


def rdc(
    invocation_id: str,
    project_id: str,
    status: str,
    *,
    occurred: int,
    connection: str | None = None,
    generation: int | None = None,
    submitted: int | None = None,
    started: int | None = None,
    output: int | None = None,
    finished: int | None = None,
    command_bytes: int | None = None,
    command_count: int | None = None,
    **extra,
) -> dict:
    value = {
        "project_id": project_id,
        "invocation_id": invocation_id,
        "status": status,
        "occurred_at": ts(occurred),
        "connection_id": connection,
        "connection_generation": generation,
        "submitted_at": ts(submitted) if submitted is not None else None,
        "started_at": ts(started) if started is not None else None,
        "first_output_at": ts(output) if output is not None else None,
        "finished_at": ts(finished) if finished is not None else None,
        "command_bytes": command_bytes,
        "command_count": command_count,
        **extra,
    }
    return {key: item for key, item in value.items() if item is not None}


def test_provider_context_switch_quota_and_failover_use_explicit_correlation() -> None:
    events = [
        provider_event(
            "r1", "failed", 10, resource="res-a", provider="a", account="one",
            model="m1", session="s1", started=0, finished=10,
            quota_observation={"remaining": 0},
            rate_limit_observation={"kind": "provider_limit", "retry_after_seconds": 5},
        ),
        provider_event(
            "r2", "succeeded", 25, resource="res-b", provider="b", account="two",
            model="m2", session="s2", started=15, finished=25,
        ),
    ]
    summary = summarize_provider_evidence(events)
    data = summary.as_dict()
    assert data["result_count"] == 2
    assert data["resource_switches"] == 1
    assert data["context_switches"] == 1
    assert data["quota_observation_count"] == 1
    assert data["rate_limit_observation_count"] == 1
    assert data["failover_latency_seconds"] == 5
    assert data["failovers"][0]["from_request_id"] == "r1"
    assert data["failovers"][0]["to_request_id"] == "r2"


def test_provider_missing_fields_remain_unknown_and_do_not_create_failover() -> None:
    summary = summarize_provider_evidence([
        provider_event(
            "r1", "failed", 10, resource=None, provider=None, account=None,
            model=None, session=None, started=None, finished=None,
        ),
        provider_event(
            "r2", "succeeded", 20, resource="res", provider="p", account="a",
            model="m", session=None, started=None, finished=None,
        ),
    ])
    assert summary.failovers == ()
    assert summary.continuity["session_id"].unknown == 2
    assert summary.unavailable_fields["first_output_at"] == 2


def test_rdc_import_is_durable_idempotent_and_conflicts_fail_closed(tmp_path: Path) -> None:
    recorder = ExecutionRecorder(ExecutionEventStore(tmp_path))
    rows = [rdc(
        "i1", "p1", "succeeded", occurred=20, connection="c1", generation=2,
        submitted=0, started=2, output=3, finished=20, command_bytes=42, command_count=2,
    )]
    first = import_rdc_evidence(recorder, rows)
    second = import_rdc_evidence(recorder, rows)
    assert first == second
    stored = recorder.store.read()
    assert len(stored.events) == 1
    assert stored.events[0]["event_id"] == "rdc:p1:i1"
    changed = [dict(rows[0], command_bytes=43)]
    with pytest.raises(EventCorruptionError, match="conflicting replay"):
        import_rdc_evidence(recorder, changed)


def test_rdc_cli_imports_json_array_into_configured_ledger(tmp_path: Path, capsys) -> None:
    runtime = tmp_path / "runtime"
    config = tmp_path / "projects.json"
    source = tmp_path / "rdc.json"
    config.write_text(json.dumps({
        "projects": [],
        "execution_accounting": {"enabled": True, "failure_memory": False},
    }), encoding="utf-8")
    source.write_text(json.dumps([
        rdc("i1", "p1", "succeeded", occurred=3, started=0, output=1, finished=3)
    ]), encoding="utf-8")
    assert main([
        "import-rdc-evidence", "--input", str(source),
        "--config", str(config), "--runtime-root", str(runtime),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == 1
    assert ExecutionEventStore(runtime).read().events[0]["invocation_id"] == "i1"


def test_rdc_classifiers_distinguish_expected_contention_modes() -> None:
    rows = [
        rdc("iso-a", "p1", "succeeded", occurred=20, connection="iso-1", generation=1, started=0, output=1, finished=20),
        rdc("iso-b", "p2", "succeeded", occurred=15, connection="iso-2", generation=1, started=5, output=6, finished=15),
        rdc("blocker", "p1", "succeeded", occurred=20, connection="hol", generation=1, started=0, output=1, finished=20, command_bytes=70000),
        rdc("blocked", "p1", "succeeded", occurred=25, connection="hol", generation=1, submitted=2, started=20, output=21, finished=25),
        rdc("starved", "p3", "succeeded", occurred=45, connection="starve", generation=1, submitted=0, started=40, output=41, finished=45),
        rdc("overtake-1", "p3", "succeeded", occurred=10, connection="starve", generation=1, submitted=5, started=5, output=6, finished=10),
        rdc("overtake-2", "p3", "succeeded", occurred=15, connection="starve", generation=1, submitted=10, started=10, output=11, finished=15),
        rdc("dead", "p4", "running", occurred=0, connection="dead", generation=1, started=0),
        rdc("coupled", "p2", "cancelled", occurred=30, connection="shared", generation=1, started=10, finished=30, cancelled_by_project_id="p1"),
        rdc("reconnect", "p1", "failed", occurred=31, connection="shared", generation=2, started=10, finished=31, reconnect_at=ts(25), affected_invocation_ids=["other-project"]),
        rdc("other-project", "p2", "failed", occurred=31, connection="shared", generation=1, started=10, finished=31),
    ]
    result = classify_rdc_evidence(
        rows,
        window_end=ts(300),
        thresholds=RDCThresholds(
            hol_wait_seconds=5,
            starvation_seconds=30,
            deadlock_seconds=120,
            oversized_command_bytes=65536,
            oversized_command_count=32,
        ),
    )
    kinds = {item.kind for item in result.findings}
    assert {
        "isolated_concurrency",
        "head_of_line_blocking",
        "starvation",
        "session_coupling",
        "reconnect_contamination",
        "deadlock",
    } <= kinds


def test_rdc_recovery_targets_are_project_isolated() -> None:
    rows = [
        rdc("p1-running", "p1", "running", occurred=1, started=0),
        rdc("p2-running", "p2", "running", occurred=1, started=0),
        rdc("p1-done", "p1", "succeeded", occurred=2, started=0, finished=2),
    ]
    before = json.dumps(rows, sort_keys=True)
    assert plan_rdc_recovery(rows, "p1", "cancel") == ("p1-running",)
    assert plan_rdc_recovery(rows, "p1", "reconnect") == ("p1-running",)
    assert json.dumps(rows, sort_keys=True) == before


@patch("dev_orchestrator.ai.aibroker_subprocess.subprocess.run")
def test_aibroker_port_records_exact_result_evidence(run, tmp_path: Path) -> None:
    recorder = ExecutionRecorder(ExecutionEventStore(tmp_path / "runtime"))
    port = AIBrokerExecutionPort(
        AIBrokerClientConfig(
            python_executable=Path(sys.executable),
            broker_repo=tmp_path,
            config_path=tmp_path / "resources.yaml",
        ),
        accounting=recorder,
    )
    payload = {
        "dispatch_id": "d1",
        "request_id": "req-1",
        "decision_id": "q1",
        "execution_id": "e1",
        "session_id": "s1",
        "status": "succeeded",
        "output": "OK",
        "error": None,
        "usage": None,
        "usage_source": "unknown",
        "started_at": ts(10),
        "finished_at": ts(20),
        "first_output_at": ts(12),
        "quota_observation": {"remaining_ratio": 0.5},
        "resource_context": {
            "resource_id": "res-1",
            "provider": "provider-1",
            "account": "account-1",
            "model": "model-1",
        },
    }
    run.return_value = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
    request = AIRoleRequest(
        project_id="p1",
        task_run_id="P11c",
        stage_run_id="work",
        role_run_id="role-1",
        request_id="req-1",
        role="worker",
        prompt="work",
        working_directory=tmp_path,
        metadata={"source_request_id": "control-1"},
    )
    port.execute(request)
    event = recorder.store.read().events[0]
    assert event["event_type"] == "provider_result_observed"
    assert event["provider"] == "provider-1"
    assert event["session_id"] == "s1"
    assert event["metadata"]["started_at"] == ts(10)
    assert event["metadata"]["first_output_at"] == ts(12)
    assert event["metadata"]["correlation_group"] == "control-1"
