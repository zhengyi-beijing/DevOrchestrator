"""Behavioral probes for the real automatic harvest -> typed candidate path."""
import json

import pytest

from dev_orchestrator.incidents.capture import capture_incident
from dev_orchestrator.incidents.harvesting import harvest_tick
from dev_orchestrator.incidents.regression_candidate import (
    resolve_regression_candidate, validate_regression_candidate,
    verify_regression_candidate,
)
from dev_orchestrator.incidents.store import (
    IncidentCapacityExceededError, load_incident_store,
)


def capture(runtime, event="event-1", failure="planner_timeout", config=None):
    return capture_incident(
        runtime, "probe-project", "probe-task", failure,
        {"failure_class": failure, "stage": "planning", "role": "planner"},
        {"request_id": event, "Authorization": "Bearer secret-value"},
        event, config=config,
    )


def test_automatic_capture_creates_linked_typed_candidate_without_python(tmp_path):
    family = capture(tmp_path)
    candidate = resolve_regression_candidate(tmp_path, family["candidate_id"])
    assert validate_regression_candidate(candidate) == []
    assert candidate["project_id"] == "probe-project"
    assert candidate["source_family_id"] == family["family_id"]
    assert candidate["source_failure_signature"] == family["failure_signature"]
    assert candidate["source_incident_ids"] == [family["packet_id"]]
    assert candidate["verification_status"] == "proposed"
    assert candidate["replayability"] in {"unknown", "non_replayable"}
    assert not list(tmp_path.rglob("*.py"))
    assert "secret-value" not in "\n".join(
        p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json")
    )


def test_harvest_tick_enters_the_typed_candidate_path(tmp_path):
    (tmp_path / "ai-planner.json").write_text(json.dumps({"plans": {
        "plan-1": {"project_id": "probe-project", "task_id": "probe-task",
                   "state": "failed", "planner_last_failure": "planner timed out"}
    }}), encoding="utf-8")
    harvest_tick(tmp_path, config={"projects": [{"project_id": "probe-project"}]})
    store = load_incident_store(tmp_path)
    assert len(store.index["families"]) == 1
    family = next(iter(store.index["families"].values()))
    candidate = resolve_regression_candidate(tmp_path, family["candidate_id"])
    assert candidate["source_family_id"] == family["family_id"]
    assert len(store.index["regression_candidates"]) == 1


def test_repeat_restart_and_replay_preserve_one_candidate(tmp_path):
    first = capture(tmp_path)
    original = resolve_regression_candidate(tmp_path, first["candidate_id"])
    revision = load_incident_store(tmp_path).index["revision"]
    assert capture(tmp_path)["candidate_id"] == first["candidate_id"]
    assert load_incident_store(tmp_path).index["revision"] == revision
    second = capture(tmp_path, event="event-2")
    assert second["candidate_id"] == first["candidate_id"]
    assert second["recurrence_count"] == 2
    assert resolve_regression_candidate(tmp_path, first["candidate_id"]) == original
    assert len(load_incident_store(tmp_path).index["regression_candidates"]) == 1


def test_unbound_automatic_candidate_cannot_become_verified(tmp_path):
    family = capture(tmp_path)
    result = verify_regression_candidate(tmp_path, family["candidate_id"])
    assert result["verification_status"] == "rejected"
    assert result["rejection_reason"]
    assert resolve_regression_candidate(tmp_path, family["candidate_id"])["verification_status"] == "rejected"


def test_automatic_candidate_capacity_is_checked_before_family_commit(tmp_path):
    policy = {"incidents": {"max_regression_candidates": 1}}
    capture(tmp_path, config=policy)
    revision = load_incident_store(tmp_path).index["revision"]
    with pytest.raises(IncidentCapacityExceededError):
        capture(tmp_path, event="event-2", failure="malformed_ai_output", config=policy)
    store = load_incident_store(tmp_path)
    assert store.index["revision"] == revision
    assert len(store.index["families"]) == 1
    assert len(store.index["regression_candidates"]) == 1


def test_disabled_auto_synthesis_creates_no_candidate(tmp_path):
    family = capture(tmp_path, config={"incidents": {"auto_synthesis": False}})
    assert family["candidate_id"] is None
    assert not list((tmp_path / "incident-packets" / "candidates").rglob("regression_candidate.json"))


def test_explicit_legacy_mode_retains_legacy_generator(tmp_path):
    family = capture(tmp_path, config={"incidents": {"typed_replay_candidates": False}})
    assert list(tmp_path.rglob("test_candidate_*.py"))
    assert not (tmp_path / "incident-packets" / "candidates" / family["candidate_id"] / "regression_candidate.json").exists()


def test_legacy_recurrence_adds_typed_proposal_without_rewriting_evidence(tmp_path):
    family = capture(tmp_path, config={"incidents": {"typed_replay_candidates": False}})
    historical = {p: p.read_bytes() for p in tmp_path.rglob("*")
                  if p.is_file() and (p.suffix == ".py" or p.name == "manifest.json")}
    repeated = capture(tmp_path, event="event-2")
    assert repeated["candidate_id"] == family["candidate_id"]
    candidate = resolve_regression_candidate(tmp_path, family["candidate_id"])
    assert candidate["replayability"] == "unknown"
    assert {p: p.read_bytes() for p in historical} == historical


def test_restart_reconciles_family_packet_and_candidate_as_one_transaction(tmp_path, monkeypatch):
    from dev_orchestrator.incidents import store as store_module
    original_replace = store_module.os.replace

    def fail_index_commit(source, destination):
        if str(destination).endswith("index.json"):
            raise OSError("injected crash after side files, before index commit")
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "replace", fail_index_commit)
        with pytest.raises(OSError, match="injected crash"):
            capture(tmp_path)
    store = load_incident_store(tmp_path)
    store.reconcile_journal()
    family = next(iter(store.index["families"].values()))
    assert resolve_regression_candidate(tmp_path, family["candidate_id"])["source_incident_ids"] == [family["packet_id"]]
    revision = store.index["revision"]
    capture(tmp_path)
    assert load_incident_store(tmp_path).index["revision"] == revision


def test_recurrence_preserves_durable_rejection(tmp_path):
    family = capture(tmp_path)
    rejected = verify_regression_candidate(tmp_path, family["candidate_id"])
    capture(tmp_path, event="event-2")
    assert resolve_regression_candidate(tmp_path, family["candidate_id"]) == rejected
