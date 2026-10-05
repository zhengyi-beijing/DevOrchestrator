from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dev_orchestrator.control.command_store import (
    ControlCommandConflictError,
    canonical_request,
    EXPECTED_IDENTITY_FIELDS,
)
from dev_orchestrator.control.surface import project_identity
from dev_orchestrator.core.control_commands import ControlCommandCoordinator
from dev_orchestrator.core.execution_lifecycle import (
    lineage_key_for,
    load_execution_lineage,
    observe_executions,
    open_execution_obligation,
)
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.soak.permit import create_soak_permit, load_soak_permit, save_soak_permit
from dev_orchestrator.storage.json_store import write_json
from test_transition_executor import valid_project


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "canonical"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "agent").mkdir()
    (repo / "agent" / "next.md").write_text("# P20.4\n\nStatus: **READY_TO_RUN**\n", encoding="utf-8")
    (repo / "agent" / "execution-state.json").write_text(
        json.dumps({"schema_version": 1, "execution_state": "ready_to_run", "task_id": "P20.4"}),
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("runtime/\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _permit(root: Path, repo: Path, runtime: Path, run_id: str, project_id: str):
    base = root / run_id
    disp_repo = base / "repo"
    disp_rt = base / "runtime"
    disp_repo.mkdir(parents=True)
    disp_rt.mkdir(parents=True)
    marker = disp_repo / ".devorch-disposable-soak"
    marker.write_text(json.dumps({"run_id": run_id, "project_id": project_id}), encoding="utf-8")
    permit = create_soak_permit(
        run_id=run_id,
        soak_project_id=project_id,
        disposable_repo_path=disp_repo,
        disposable_runtime_root=disp_rt,
        canonical_repo_path=repo,
        canonical_runtime_root=runtime,
        allowed_fault_ids=["F01_MALFORMED_PLANNER"],
    )
    permit_path = base / "permit.json"
    save_soak_permit(permit_path, permit)
    return permit, permit_path


def _fixture(tmp_path: Path, *, old_status: str = "FAILED"):
    repo = _repo(tmp_path)
    runtime = repo / "runtime"
    runtime.mkdir()
    old, old_path = _permit(tmp_path, repo, runtime, "old-run", "old-project")
    new, new_path = _permit(tmp_path, repo, runtime, "new-run", "new-project")
    old_run_path = Path(old.disposable_runtime_root) / "run-old-run.json"
    write_json(old_run_path, {
        "run_id": "old-run",
        "project_id": "old-project",
        "status": old_status,
        "started_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-01T06:00:00+00:00",
        "failure_reason": "fixture-failure" if old_status == "FAILED" else None,
        "segments": [],
    })
    old_bytes = old_run_path.read_bytes()
    head = _git(repo, "rev-parse", "HEAD")
    old_source = "worker-old"
    ledger = {
        "version": 2,
        "lifecycle": {
            "devorchestrator": {
                "schema_version": 1,
                "project_id": "devorchestrator",
                "generation": 14,
                "current_task_id": "P20.4",
                "source_task_id": "P20.3",
                "lifecycle_state": "OWNER_GATE",
                "active_owner": None,
                "owner_gate": {
                    "gate_id": "soak-" + old_source,
                    "code": "SOAK_EVIDENCE_UNRESOLVED",
                    "task_id": "P20.4",
                    "reason": "owned_soak_job_terminal_or_deadline_expired: failed",
                    "required_owner_decision": "explicit recovery required",
                },
            }
        },
        "executions": {
            old_source: {
                "project_id": "devorchestrator",
                "source_request_id": old_source,
                "source_kind": "control",
                "task_id": "P20.4",
                "repo_path": str(repo),
                "branch": "master",
                "head": head,
                "engine": "aibroker",
                "started_at": "2026-10-01T00:00:00+00:00",
                "state": "recovery_required",
                "reason": "owned_soak_job_terminal_or_deadline_expired: failed",
                "recovery_safe_retry": False,
                "external_execution_binding": {
                    "binding_kind": "p20_soak_run",
                    "job_id": "job-old",
                    "permit_path": str(old_path),
                    "permit_fingerprint": old.fingerprint,
                    "soak_run_id": old.run_id,
                    "soak_project_id": old.soak_project_id,
                },
            }
        },
        "transitions": {},
    }
    write_json(runtime / "transition-executor.json", ledger)
    write_json(runtime / "p20-soak-bootstrap.json", {
        "run_id": old.run_id,
        "permit_path": str(old_path),
        "permit_fingerprint": old.fingerprint,
        "prepared_head": head,
        "prepared_at": "2026-10-01T00:00:00+00:00",
        "source": "p20_local_cli",
    })
    project = {"project_id": "devorchestrator", "repo_path": str(repo)}
    return repo, runtime, project, old, old_path, new, new_path, old_run_path, old_bytes


def _old_terminal_job(executor: TransitionExecutor):
    state = executor.state()
    old = state["executions"]["worker-old"]
    binding = old["external_execution_binding"]
    permit = load_soak_permit(binding["permit_path"])
    assert permit is not None
    return SimpleNamespace(
        state="failed",
        project_id="devorchestrator",
        task_id="P20.4",
        source_request_id="worker-old",
        kind="soak",
        command_ref="p20_soak_supervisor",
        transport="local",
        parameters_digest=permit.fingerprint,
        idempotency_key=f"soak:{permit.run_id}:worker-old",
    )


def _authorize(executor: TransitionExecutor, project: dict, permit_path: Path, command_id: str):
    executor.owner_store.set_paused(
        "devorchestrator", True, command_id="fixture-pause", action="pause", reason="test"
    )
    with patch(
        "dev_orchestrator.jobs.service.JobService.status",
        return_value=_old_terminal_job(executor),
    ):
        return executor.authorize_soak_recovery(
            project, str(permit_path), command_id=command_id
        )


def test_soak_recover_action_is_local_only():
    req = {
        "schema_version": 1,
        "command_id": "recover",
        "project_id": "devorchestrator",
        "action": "soak_recover",
        "expected": {key: None for key in EXPECTED_IDENTITY_FIELDS},
        "target": {
            "permit_path": "new-permit.json",
            "permit_fingerprint": "f" * 64,
        },
    }
    try:
        canonical_request(req)
    except ValueError:
        pass
    else:
        raise AssertionError("remote canonical request unexpectedly accepted soak_recover")
    assert canonical_request(req, local=True)["action"] == "soak_recover"


def test_owner_recovery_refuses_old_run_that_is_still_running(tmp_path):
    _, runtime, project, _, _, _, new_path, _, _ = _fixture(tmp_path, old_status="RUNNING")
    executor = TransitionExecutor(runtime)
    result = _authorize(executor, project, new_path, "owner-recover-running")
    assert result["status"] == "rejected"
    assert result["reason"] == "previous_soak_run_not_terminal_failed"
    auth = executor.state()["lifecycle"]["devorchestrator"]
    assert auth["lifecycle_state"] == "OWNER_GATE"
    assert auth["owner_gate"]["code"] == "SOAK_EVIDENCE_UNRESOLVED"


def test_owner_recovery_refuses_reusing_failed_run_identity(tmp_path):
    _, runtime, project, _, old_path, _, _, _, _ = _fixture(tmp_path)
    executor = TransitionExecutor(runtime)
    result = _authorize(executor, project, old_path, "owner-recover-same")
    assert result["status"] == "rejected"
    assert result["reason"] == "replacement_soak_identity_must_be_new"


def test_owner_recovery_preserves_failed_run_and_prepares_new_measurement(tmp_path):
    repo, runtime, project, old, _, new, new_path, old_run_path, old_bytes = _fixture(tmp_path)
    executor = TransitionExecutor(runtime)
    result = _authorize(executor, project, new_path, "owner-recover-new")
    assert result["status"] == "authorized"
    assert result["previous_run_id"] == old.run_id
    assert result["replacement_run_id"] == new.run_id
    assert result["old_run_sha256"] == hashlib.sha256(old_bytes).hexdigest()

    state = executor.state()
    auth = state["lifecycle"]["devorchestrator"]
    assert auth["current_task_id"] == "P20.4"
    assert auth["lifecycle_state"] == "READY_TO_RUN"
    assert auth["owner_gate"] is None
    assert auth["active_owner"] is None
    recovery = auth["soak_recovery_authorization"]
    assert recovery["previous_run_id"] == old.run_id
    assert recovery["replacement_run_id"] == new.run_id
    assert recovery["command_id"] == "owner-recover-new"

    old_row = state["executions"]["worker-old"]
    assert old_row["state"] == "recovery_required"
    assert old_row["reason"] == "owned_soak_job_terminal_or_deadline_expired: failed"
    assert old_row["owner_recovery_authorized"] is True
    assert old_run_path.read_bytes() == old_bytes

    bootstrap = json.loads((runtime / "p20-soak-bootstrap.json").read_text(encoding="utf-8"))
    assert bootstrap["run_id"] == new.run_id
    assert bootstrap["permit_fingerprint"] == new.fingerprint
    assert bootstrap["prepared_head"] == _git(repo, "rev-parse", "HEAD")
    archived = runtime / "soak-bootstrap-history" / "old-run.json"
    assert archived.is_file()
    assert json.loads(archived.read_text(encoding="utf-8"))["run_id"] == old.run_id

    # Authorization itself never launches a worker or a durable soak job.
    assert not any(
        isinstance(row, dict) and row.get("state") in {"launching", "running"}
        for row in state["executions"].values()
    )


def test_owner_recovery_replays_after_bootstrap_publish_before_ledger_save(tmp_path):
    repo, runtime, project, old, _, new, new_path, old_run_path, old_bytes = _fixture(tmp_path)
    executor = TransitionExecutor(runtime)
    executor.owner_store.set_paused(
        "devorchestrator", True, command_id="fixture-pause", action="pause", reason="test"
    )
    with patch(
        "dev_orchestrator.jobs.service.JobService.status",
        return_value=_old_terminal_job(executor),
    ), patch.object(executor, "_save_ledger", side_effect=RuntimeError("crash-after-bootstrap")):
        try:
            executor.authorize_soak_recovery(
                project, str(new_path), command_id="owner-recover-crash"
            )
        except RuntimeError as exc:
            assert "crash-after-bootstrap" in str(exc)
        else:
            raise AssertionError("simulated crash did not interrupt ledger persistence")

    bootstrap = json.loads((runtime / "p20-soak-bootstrap.json").read_text(encoding="utf-8"))
    assert bootstrap["run_id"] == new.run_id
    assert bootstrap["replacement_of_run_id"] == old.run_id
    still_gated = TransitionExecutor(runtime).state()["lifecycle"]["devorchestrator"]
    assert still_gated["owner_gate"]["code"] == "SOAK_EVIDENCE_UNRESOLVED"

    restarted = TransitionExecutor(runtime)
    with patch(
        "dev_orchestrator.jobs.service.JobService.status",
        return_value=_old_terminal_job(restarted),
    ):
        replay = restarted.authorize_soak_recovery(
            project, str(new_path), command_id="owner-recover-crash"
        )
    assert replay["status"] == "authorized"
    assert replay["replacement_run_id"] == new.run_id
    assert old_run_path.read_bytes() == old_bytes
    auth = restarted.state()["lifecycle"]["devorchestrator"]
    assert auth["owner_gate"] is None
    assert auth["soak_recovery_authorization"]["command_id"] == "owner-recover-crash"


def test_authorized_failed_soak_allows_new_start_control_without_retrying_old_execution(tmp_path):
    repo, runtime, project, old, _, new, new_path, old_run_path, old_bytes = _fixture(tmp_path)
    executor = TransitionExecutor(runtime)
    result = _authorize(executor, project, new_path, "owner-recover-launch")
    assert result["status"] == "authorized"

    executor.owner_store.set_paused(
        "devorchestrator", False, command_id="fixture-resume", action="resume"
    )
    full_project = {
        **valid_project(),
        "project_id": "devorchestrator",
        "repo_path": str(repo),
    }
    full_project["execution"]["bootstrap"] = {
        "request_id": "fixture-bootstrap",
        "task_id": "P20.4",
    }
    snapshot = {
        "project_id": "devorchestrator",
        "repo_path": str(repo),
        "state": "READY_TO_RUN",
        "worker": {"kind": "none", "state": "not_started", "process_alive": False},
        "telemetry": {"task_id": "P20.4"},
        "next_title": "P20.4 bounded task",
    }
    launch = SimpleNamespace(task_id="P20.4", backend_id="fixture")
    with patch.object(executor, "_launch", return_value=launch) as mocked_launch:
        observed = executor.start_control(
            full_project, snapshot, "owner-fresh-p20-4-execution"
        )
    assert observed is launch
    kwargs = mocked_launch.call_args.kwargs
    assert kwargs["source_request_id"] == "owner-fresh-p20-4-execution"
    assert kwargs["lineage"]["recovery_of"] == "worker-old"
    assert kwargs["lineage"]["previous_soak_run_id"] == old.run_id
    assert kwargs["lineage"]["replacement_soak_run_id"] == new.run_id

    old_row = executor.state()["executions"]["worker-old"]
    assert old_row["state"] == "recovery_required"
    assert old_row["recovery_safe_retry"] is False
    assert old_run_path.read_bytes() == old_bytes


def test_owner_recovery_rejects_tampered_previous_permit(tmp_path):
    _, runtime, project, _, old_path, _, new_path, _, _ = _fixture(tmp_path)
    raw = json.loads(old_path.read_text(encoding="utf-8"))
    raw["fingerprint"] = "0" * 64
    write_json(old_path, raw)
    executor = TransitionExecutor(runtime)
    executor.owner_store.set_paused(
        "devorchestrator", True, command_id="fixture-pause", action="pause", reason="test"
    )
    result = executor.authorize_soak_recovery(
        project, str(new_path), command_id="owner-recover-tampered"
    )
    assert result["status"] == "rejected"
    assert result["reason"].startswith("previous_soak_permit_invalid:")
    auth = executor.state()["lifecycle"]["devorchestrator"]
    assert auth["owner_gate"]["code"] == "SOAK_EVIDENCE_UNRESOLVED"


def test_owner_recovery_rejects_replacement_run_that_already_exists(tmp_path):
    _, runtime, project, _, _, new, new_path, _, _ = _fixture(tmp_path)
    write_json(
        Path(new.disposable_runtime_root) / f"run-{new.run_id}.json",
        {
            "run_id": new.run_id,
            "project_id": new.soak_project_id,
            "status": "FAILED",
        },
    )
    executor = TransitionExecutor(runtime)
    result = _authorize(executor, project, new_path, "owner-recover-existing-run")
    assert result["status"] == "rejected"
    assert result["reason"] == "replacement_soak_run_already_exists"
    assert executor.state()["lifecycle"]["devorchestrator"]["owner_gate"] is not None


def test_control_command_replay_recovers_committed_authorization_before_stale_identity(tmp_path):
    repo, runtime, project, _, _, new, new_path, _, _ = _fixture(tmp_path)
    executor = TransitionExecutor(runtime)
    executor.owner_store.set_paused(
        "devorchestrator", True, command_id="fixture-pause", action="pause", reason="test"
    )
    snapshot = {
        "project_id": "devorchestrator",
        "repo_path": str(repo),
        "state": "OWNER_GATE",
        "telemetry": {"task_id": "P20.4"},
        "worker": {"kind": "none", "state": "not_started", "process_alive": False},
    }
    record = {
        "version": 1,
        "state": "pending",
        "command_id": "owner-recover-replay",
        "project_id": "devorchestrator",
        "action": "soak_recover",
        "source": "p20_local_cli",
        "target": {
            "permit_path": str(new_path),
            "permit_fingerprint": new.fingerprint,
        },
        "expected": project_identity(snapshot, runtime),
    }
    coordinator = ControlCommandCoordinator(runtime)
    job = _old_terminal_job(executor)
    with patch("dev_orchestrator.jobs.service.JobService.status", return_value=job):
        first = coordinator._consume_one(
            record,
            {"devorchestrator": project},
            {"devorchestrator": snapshot},
            executor,
        )
        second = coordinator._consume_one(
            record,
            {"devorchestrator": project},
            {"devorchestrator": snapshot},
            executor,
        )
    assert first["state"] == "accepted"
    assert second["state"] == "accepted"
    assert second["replayed_committed_authorization"] is True
    assert second["replacement_run_id"] == first["replacement_run_id"]


def test_owner_recovery_terminally_settles_old_execution_lineage(tmp_path):
    repo, runtime, project, _, _, _, new_path, _, _ = _fixture(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    open_execution_obligation(
        runtime,
        project_id="devorchestrator",
        task_id="P20.4",
        source_request_id="worker-old",
        branch="master",
        head=head,
        engine="aibroker",
        backend_handle="broker-old",
    )
    executor = TransitionExecutor(runtime)
    result = _authorize(executor, project, new_path, "owner-recover-lineage")
    assert result["status"] == "authorized"

    lineage = load_execution_lineage(runtime)
    key = lineage_key_for("devorchestrator", "worker-old")
    record = lineage["records"][key]
    assert record["lifecycle_phase"] == "terminal"
    assert record["terminal_outcome"] == "failed"
    assert any(
        item.get("action") == "close_lineage_record"
        and item.get("outcome") == "failed"
        for item in record.get("audit_entries", [])
    )

    observed = observe_executions(
        runtime,
        snapshot={"project_id": "devorchestrator", "state": "READY_TO_RUN"},
        executor_state=executor.state(),
    )
    assert observed["unresolved_invariants"] == []
    auth = executor.state()["lifecycle"]["devorchestrator"]["soak_recovery_authorization"]
    assert auth["previous_lineage_key"] == key


def test_authorized_fresh_start_links_recovery_to_old_lineage(tmp_path):
    repo, runtime, project, _, _, _, new_path, _, _ = _fixture(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    old_lineage = open_execution_obligation(
        runtime,
        project_id="devorchestrator",
        task_id="P20.4",
        source_request_id="worker-old",
        branch="master",
        head=head,
        engine="aibroker",
    )
    executor = TransitionExecutor(runtime)
    result = _authorize(executor, project, new_path, "owner-recover-lineage-link")
    assert result["status"] == "authorized"

    executor.owner_store.set_paused(
        "devorchestrator", False, command_id="fixture-resume-lineage", action="resume"
    )
    full_project = {
        **valid_project(),
        "project_id": "devorchestrator",
        "repo_path": str(repo),
    }
    full_project["execution"]["bootstrap"] = {
        "request_id": "fixture-bootstrap",
        "task_id": "P20.4",
    }
    snapshot = {
        "project_id": "devorchestrator",
        "repo_path": str(repo),
        "state": "READY_TO_RUN",
        "worker": {"kind": "none", "state": "not_started", "process_alive": False},
        "telemetry": {"task_id": "P20.4"},
        "next_title": "P20.4 bounded task",
    }
    launch = SimpleNamespace(task_id="P20.4", backend_id="fixture")
    with patch.object(executor, "_launch", return_value=launch) as mocked_launch:
        observed = executor.start_control(
            full_project, snapshot, "owner-fresh-p20-4-lineage-execution"
        )
    assert observed is launch
    lineage = mocked_launch.call_args.kwargs["lineage"]
    assert lineage["recovery_of"] == "worker-old"
    assert lineage["recovery_of_lineage_key"] == old_lineage["lineage_key"]


def test_soak_recover_cli_rejects_same_command_id_with_different_permit(tmp_path, monkeypatch):
    from dev_orchestrator import cli as cli_module

    repo, runtime, _, _, _, _, new_path, _, _ = _fixture(tmp_path)
    _, other_path = _permit(tmp_path, repo, runtime, "other-run", "other-project")
    (runtime / "projects").mkdir(parents=True, exist_ok=True)
    write_json(runtime / "projects" / "devorchestrator.json", {"project_id": "devorchestrator"})

    identity = {key: None for key in EXPECTED_IDENTITY_FIELDS}
    identity["project_id"] = "devorchestrator"
    monkeypatch.setattr(cli_module, "REPO_ROOT", repo)
    monkeypatch.setattr(
        "dev_orchestrator.control.surface.project_control_view",
        lambda snapshot, runtime_root: {"control_identity": identity},
    )

    args = SimpleNamespace(
        permit=str(new_path),
        runtime_root=str(runtime),
        command_id="owner-recover-cli-conflict",
    )
    assert cli_module.cmd_soak_recover(args) == 0

    conflict = SimpleNamespace(
        permit=str(other_path),
        runtime_root=str(runtime),
        command_id="owner-recover-cli-conflict",
    )
    with pytest.raises(ControlCommandConflictError):
        cli_module.cmd_soak_recover(conflict)


def test_control_recovery_rejects_permit_changed_after_submission(tmp_path):
    repo, runtime, project, _, _, new, new_path, _, _ = _fixture(tmp_path)
    executor = TransitionExecutor(runtime)
    executor.owner_store.set_paused(
        "devorchestrator", True, command_id="fixture-pause", action="pause", reason="test"
    )
    snapshot = {
        "project_id": "devorchestrator",
        "repo_path": str(repo),
        "state": "OWNER_GATE",
        "telemetry": {"task_id": "P20.4"},
        "worker": {"kind": "none", "state": "not_started", "process_alive": False},
    }
    submitted_fp = new.fingerprint
    record = {
        "version": 1,
        "state": "pending",
        "command_id": "owner-recover-permit-toctou",
        "project_id": "devorchestrator",
        "action": "soak_recover",
        "source": "p20_local_cli",
        "target": {
            "permit_path": str(new_path),
            "permit_fingerprint": submitted_fp,
        },
        "expected": project_identity(snapshot, runtime),
    }

    replacement, _ = _permit(tmp_path, repo, runtime, "other-run-toctou", "other-project-toctou")
    save_soak_permit(new_path, replacement)

    coordinator = ControlCommandCoordinator(runtime)
    with patch(
        "dev_orchestrator.jobs.service.JobService.status",
        return_value=_old_terminal_job(executor),
    ):
        outcome = coordinator._consume_one(
            record,
            {"devorchestrator": project},
            {"devorchestrator": snapshot},
            executor,
        )
    assert outcome["state"] == "blocked"
    assert outcome["reason"] == "submitted soak permit fingerprint no longer matches"
    authority = executor.state()["lifecycle"]["devorchestrator"]
    assert authority["owner_gate"]["code"] == "SOAK_EVIDENCE_UNRESOLVED"
    assert authority.get("soak_recovery_authorization") is None


def test_authorized_recovery_blocks_new_worker_if_head_changes_before_continue(tmp_path):
    repo, runtime, project, _, _, _, new_path, _, old_bytes = _fixture(tmp_path)
    executor = TransitionExecutor(runtime)
    result = _authorize(executor, project, new_path, "owner-recover-head-fence")
    assert result["status"] == "authorized"
    authorized_head = result["prepared_head"]

    (repo / "post-authorization-change.txt").write_text("changed", encoding="utf-8")
    _git(repo, "add", "post-authorization-change.txt")
    _git(repo, "commit", "-m", "change after soak recovery authorization")
    assert _git(repo, "rev-parse", "HEAD") != authorized_head

    executor.owner_store.set_paused(
        "devorchestrator", False, command_id="fixture-resume-head-fence", action="resume"
    )
    full_project = {
        **valid_project(),
        "project_id": "devorchestrator",
        "repo_path": str(repo),
    }
    full_project["execution"]["bootstrap"] = {
        "request_id": "fixture-bootstrap",
        "task_id": "P20.4",
    }
    snapshot = {
        "project_id": "devorchestrator",
        "repo_path": str(repo),
        "state": "READY_TO_RUN",
        "worker": {"kind": "none", "state": "not_started", "process_alive": False},
        "telemetry": {"task_id": "P20.4"},
        "next_title": "P20.4 bounded task",
    }

    with patch.object(executor, "_launch") as mocked_launch:
        observed = executor.start_control(
            full_project, snapshot, "owner-fresh-p20-4-after-head-change"
        )
    assert observed is None
    mocked_launch.assert_not_called()
    blocked = executor.state()["executions"]["owner-fresh-p20-4-after-head-change"]
    assert blocked["state"] == "blocked"
    assert blocked["reason"] == "owner-authorized P20.4 soak recovery HEAD changed"
    old_run = Path(
        load_soak_permit(
            executor.state()["executions"]["worker-old"]["external_execution_binding"]["permit_path"]
        ).disposable_runtime_root
    ) / "run-old-run.json"
    assert old_run.read_bytes() == old_bytes


def test_recovery_after_corrective_commit_preserves_old_execution_lineage(tmp_path):
    repo, runtime, project, _, _, _, new_path, _, _ = _fixture(tmp_path)
    old_head = _git(repo, "rev-parse", "HEAD")
    old_lineage = open_execution_obligation(
        runtime,
        project_id="devorchestrator",
        task_id="P20.4",
        source_request_id="worker-old",
        branch="master",
        head=old_head,
        engine="aibroker",
    )

    (repo / "corrective-fix.txt").write_text("fix failed soak", encoding="utf-8")
    _git(repo, "add", "corrective-fix.txt")
    _git(repo, "commit", "-m", "fix before owner recovery")
    new_head = _git(repo, "rev-parse", "HEAD")
    assert new_head != old_head

    executor = TransitionExecutor(runtime)
    result = _authorize(executor, project, new_path, "owner-recover-after-fix")
    assert result["status"] == "authorized"
    assert result["prepared_head"] == new_head

    executor.owner_store.set_paused(
        "devorchestrator", False, command_id="fixture-resume-after-fix", action="resume"
    )
    full_project = {
        **valid_project(),
        "project_id": "devorchestrator",
        "repo_path": str(repo),
    }
    full_project["execution"]["bootstrap"] = {
        "request_id": "fixture-bootstrap",
        "task_id": "P20.4",
    }
    snapshot = {
        "project_id": "devorchestrator",
        "repo_path": str(repo),
        "state": "READY_TO_RUN",
        "worker": {"kind": "none", "state": "not_started", "process_alive": False},
        "telemetry": {"task_id": "P20.4"},
        "next_title": "P20.4 bounded task",
    }
    launch = SimpleNamespace(task_id="P20.4", backend_id="fixture")
    with patch.object(executor, "_launch", return_value=launch) as mocked_launch:
        observed = executor.start_control(
            full_project, snapshot, "owner-fresh-p20-4-after-fix"
        )
    assert observed is launch
    lineage = mocked_launch.call_args.kwargs["lineage"]
    assert lineage["recovery_of"] == "worker-old"
    assert lineage["recovery_of_lineage_key"] == old_lineage["lineage_key"]
    assert lineage["previous_soak_run_id"] == "old-run"
    assert lineage["replacement_soak_run_id"] == "new-run"
