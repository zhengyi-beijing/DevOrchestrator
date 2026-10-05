"""Regression for a live soak that heartbeats forever after final-task review."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

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
