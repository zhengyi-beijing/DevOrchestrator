"""Regression for a live soak that heartbeats forever after final-task review."""

from __future__ import annotations

import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest

from dev_orchestrator.soak.evidence import primary_terminal_blocker
from dev_orchestrator.soak.permit import create_soak_permit
from dev_orchestrator.soak.template import generate_soak_project
from dev_orchestrator.soak.timeline import (
    load_soak_run,
    settle_stopped_soak_run,
)
from dev_orchestrator.storage.json_store import write_json


def _permit(tmp_path: Path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    return create_soak_permit(
        "stall-run",
        "stall-project",
        tmp_path / "repo",
        tmp_path / "runtime",
        canonical,
        canonical / "runtime",
    )


def test_generated_final_task_declares_terminal_settlement(tmp_path):
    permit = _permit(tmp_path)
    generate_soak_project(permit)
    final_spec = (Path(permit.disposable_repo_path) / "agent/staged/P204.5.md").read_text(
        encoding="utf-8"
    )
    assert "Successor: none (end of roadmap)" in final_spec
    assert "accepted review returns NEXT" in final_spec
    assert "TBD / owner decision required" not in final_spec


def test_primary_terminal_owner_gate_is_a_typed_blocker(tmp_path):
    permit = _permit(tmp_path)
    runtime = Path(permit.disposable_runtime_root)
    runtime.mkdir(parents=True)
    write_json(runtime / "soak-project-manifest.json", {
        "task_ids": [f"P204.{number}" for number in range(1, 6)]
    })
    write_json(runtime / "transition-executor.json", {
        "lifecycle": {permit.soak_project_id: {
            "current_task_id": "P204.5",
            "lifecycle_state": "REVIEWING",
            "active_owner": {"role": "review_obligation", "id": "review-final"},
            "owner_gate": None,
        }},
        "executions": {},
    })
    write_json(runtime / "ai-reviewer.json", {"reviews": {"review-final": {
        "review_id": "review-final",
        "project_id": permit.soak_project_id,
        "task_id": "P204.5",
        "state": "completed",
        "decision": "owner_gate",
        "next_action": "stop",
        "completed_at": "2026-10-05T17:49:31+00:00",
        "reason": "accepted implementation but no successor was advertised",
    }}})

    blocker = primary_terminal_blocker(permit)
    assert blocker["code"] == "PRIMARY_FINAL_REVIEW_OWNER_GATE"
    assert blocker["task_id"] == "P204.5"
    assert blocker["review_id"] == "review-final"
    assert blocker["decision"] == "owner_gate"


def test_older_owner_gate_does_not_override_newer_accepted_review(tmp_path):
    permit = _permit(tmp_path)
    runtime = Path(permit.disposable_runtime_root)
    runtime.mkdir(parents=True)
    write_json(runtime / "soak-project-manifest.json", {"task_ids": ["P204.5"]})
    write_json(runtime / "transition-executor.json", {
        "lifecycle": {permit.soak_project_id: {
            "current_task_id": "P204.5", "lifecycle_state": "REVIEWING",
            "active_owner": {"role": "review_obligation", "id": "review-new"},
            "owner_gate": None,
        }},
        "executions": {},
    })
    write_json(runtime / "ai-reviewer.json", {"reviews": {
        "review-old": {
            "project_id": permit.soak_project_id, "task_id": "P204.5",
            "state": "completed", "decision": "owner_gate", "next_action": "stop",
            "completed_at": "2026-10-05T17:49:31+00:00",
        },
        "review-new": {
            "project_id": permit.soak_project_id, "task_id": "P204.5",
            "state": "completed", "decision": "next", "next_action": "next_task",
            "completed_at": "2026-10-05T17:50:31+00:00",
        },
    }})
    assert primary_terminal_blocker(permit) is None


def test_stopped_owned_job_settlement_is_exact_and_idempotent(tmp_path):
    permit = _permit(tmp_path)
    runtime = Path(permit.disposable_runtime_root)
    runtime.mkdir(parents=True)
    run = {
        "run_id": permit.run_id,
        "project_id": permit.soak_project_id,
        "status": "RUNNING",
        "started_at": "2026-10-05T17:05:10+00:00",
        "updated_at": "2026-10-05T23:31:46+00:00",
        "supervisor_pid": 999999,
        "supervisor_create_time": 123.5,
        "segments": [{"boot_id": "boot", "start_mono": 10.0, "end_mono": 20.0}],
        "completed_tasks": ["P204.1"],
        "fault_dispositions": {"F01": {"evidence_verified": True}},
    }
    write_json(runtime / f"run-{permit.run_id}.json", run)
    job = {
        "job_id": "job-exact",
        "state": "cancelled",
        "kind": "soak",
        "project_id": "devorchestrator",
        "command_ref": "p20_soak_supervisor",
        "source_request_id": "worker-exact",
        "parameters_digest": permit.fingerprint,
        "heartbeat": {"child_pid": 999999, "child_create_time": 123.5},
    }

    settled = settle_stopped_soak_run(permit, job)
    repeated = settle_stopped_soak_run(permit, job)
    assert settled == repeated
    assert settled["status"] == "FAILED"
    assert settled["failure_reason"] == "owned_soak_job_terminal:cancelled"
    assert settled["settlement_job_id"] == "job-exact"
    assert settled["started_at"] == run["started_at"]
    assert settled["segments"] == run["segments"]
    assert settled["completed_tasks"] == run["completed_tasks"]
    assert settled["fault_dispositions"] == run["fault_dispositions"]
    assert load_soak_run(runtime, permit.run_id) == settled


def test_stopped_settlement_refuses_live_original_process(tmp_path):
    permit = _permit(tmp_path)
    runtime = Path(permit.disposable_runtime_root)
    runtime.mkdir(parents=True)
    create_time = psutil.Process(os.getpid()).create_time()
    write_json(runtime / f"run-{permit.run_id}.json", {
        "run_id": permit.run_id,
        "project_id": permit.soak_project_id,
        "status": "RUNNING",
        "supervisor_pid": os.getpid(),
        "supervisor_create_time": create_time,
        "segments": [],
    })
    job = {
        "job_id": "job-live",
        "state": "cancelled",
        "kind": "soak",
        "project_id": "devorchestrator",
        "command_ref": "p20_soak_supervisor",
        "parameters_digest": permit.fingerprint,
        "heartbeat": {"child_pid": os.getpid(), "child_create_time": create_time},
    }
    with pytest.raises(RuntimeError, match="original supervisor is still alive"):
        settle_stopped_soak_run(permit, job)


def test_finished_fault_driver_with_pending_evidence_is_a_typed_blocker(tmp_path):
    from dev_orchestrator.soak.live_faults import LiveFaultDriver

    driver = LiveFaultDriver.__new__(LiveFaultDriver)
    driver.lock = threading.Lock()
    driver.state = {
        "driver_state": "finished",
        "finished_at": "2026-10-06T09:00:00+00:00",
        "dispositions": {"F07": {"evidence_verified": True}},
        "pending": ["F01", "F02"],
        "errors": {"F02": "RuntimeError"},
    }

    blocker = driver.terminal_failure()
    assert blocker == {
        "code": "FAULT_DRIVER_EXHAUSTED_WITH_PENDING_EVIDENCE",
        "pending_faults": ["F01", "F02"],
        "errors": {"F02": "RuntimeError"},
        "finished_at": "2026-10-06T09:00:00+00:00",
    }
    driver.state["pending"] = []
    assert driver.terminal_failure() is None


def test_fault_driver_records_terminal_state_when_case_list_is_exhausted(tmp_path):
    from dev_orchestrator.soak.live_faults import LiveFaultDriver
    from dev_orchestrator.storage.json_store import read_json

    driver = LiveFaultDriver.__new__(LiveFaultDriver)
    driver.permit = SimpleNamespace(run_id="run", allowed_fault_ids=("UNKNOWN",))
    driver.path = tmp_path / "fault-driver.json"
    driver.lock = threading.Lock()
    driver.stop_event = threading.Event()
    driver.state = {
        "run_id": "run",
        "driver_state": "running",
        "started_at": "2026-10-06T08:00:00+00:00",
        "finished_at": None,
        "dispositions": {},
        "pending": ["UNKNOWN"],
        "errors": {},
    }

    driver._drive()

    persisted = read_json(driver.path, {})
    assert persisted["driver_state"] == "finished"
    assert persisted["finished_at"]
    assert persisted["pending"] == ["UNKNOWN"]
    assert driver.terminal_failure()["code"] == "FAULT_DRIVER_EXHAUSTED_WITH_PENDING_EVIDENCE"


def test_supervisor_fails_immediately_when_fault_driver_is_terminal(tmp_path):
    from dev_orchestrator.soak import supervisor_run

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    permit = SimpleNamespace(
        run_id="terminal-driver-run",
        soak_project_id="terminal-driver-project",
        disposable_runtime_root=str(runtime),
        canonical_repo_path=str(tmp_path / "canonical"),
        canonical_runtime_root=str(tmp_path / "canonical-runtime"),
        allowed_fault_ids=("F01",),
    )
    state = {
        "run_id": permit.run_id,
        "task_evidence": {},
        "completed_tasks": [],
        "fault_dispositions": {},
        "pending_faults": ["F01"],
        "cpf_convergences": {},
        "hard_zeros": {},
        "metric_coverage": {},
    }
    blocker = {
        "code": "FAULT_DRIVER_EXHAUSTED_WITH_PENDING_EVIDENCE",
        "pending_faults": ["F01"],
        "errors": {},
        "finished_at": "2026-10-06T09:00:00+00:00",
    }

    class TerminalDriver:
        def __init__(self, *_args):
            pass

        def advance(self):
            return {}, ["F01"]

        def terminal_failure(self):
            return blocker

        def close(self):
            pass

    events = []
    with (
        patch.object(supervisor_run, "load_soak_permit", return_value=permit),
        patch.object(supervisor_run, "validate_soak_permit", return_value=(True, None)),
        patch.object(supervisor_run, "load_soak_run", return_value=None),
        patch.object(supervisor_run, "read_json", return_value={"initial_control_id": "initial"}),
        patch("dev_orchestrator.control.command_store.ControlCommandStore.get",
              return_value={"state": "accepted"}),
        patch.object(supervisor_run, "primary_active", return_value={"task_id": "P204.1"}),
        patch.object(supervisor_run, "open_soak_run", return_value=state),
        patch.object(supervisor_run, "read_repository_head", return_value="abc123"),
        patch("dev_orchestrator.incidents.reliability_runs.open_reliability_run",
              return_value={"run_id": "reliability"}),
        patch("dev_orchestrator.incidents.reliability_runs.close_reliability_run"),
        patch("dev_orchestrator.soak.evidence.start_cpf_verification", return_value="cpf-job"),
        patch("dev_orchestrator.soak.evidence.cpf_proofs", return_value={}),
        patch("dev_orchestrator.soak.live_faults.LiveFaultDriver", TerminalDriver),
        patch.object(supervisor_run, "accepted_tasks", return_value={}),
        patch.object(supervisor_run, "measured_metrics", return_value=({}, {}, {})),
        patch.object(supervisor_run, "checkpoint_soak_run"),
        patch.object(supervisor_run, "append_soak_event", side_effect=lambda _r, _i, row: events.append(row)),
        patch("dev_orchestrator.soak.cleanup.stop_owned_daemon"),
    ):
        exit_code = supervisor_run.run_soak_supervisor("permit.json")

    assert exit_code == 7
    assert state["status"] == "FAILED"
    assert state["failure_reason"] == (
        "fault_driver_terminal:FAULT_DRIVER_EXHAUSTED_WITH_PENDING_EVIDENCE"
    )
    assert state["fault_driver_blocker"] == blocker
    assert any(row["event_class"] == "fault_driver_terminal_blocker" for row in events)
