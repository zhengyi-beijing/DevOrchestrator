"""Durable terminal evidence may repair a stale predecessor projection only."""
import copy
from types import SimpleNamespace

import pytest

from dev_orchestrator.core.control_commands import _terminal_settled_predecessor


def ledger():
    return {
        "lifecycle": {"p": {
            "current_task_id": "P1", "lifecycle_state": "COMPLETE",
            "active_owner": None, "owner_gate": None,
        }},
        "executions": {"accepted": {
            "project_id": "p", "task_id": "P1", "state": "settled",
            "outcome": "task_complete",
        }},
    }


def test_terminal_evidence_matches_exact_project_and_predecessor():
    state = ledger()
    executor = SimpleNamespace(state=lambda: copy.deepcopy(state))
    assert _terminal_settled_predecessor(executor, "p", "P1")
    assert not _terminal_settled_predecessor(executor, "other", "P1")
    assert not _terminal_settled_predecessor(executor, "p", "P2")


@pytest.mark.parametrize("fault", ["no_settlement", "wrong_outcome", "wrong_task",
                                  "owner_gate", "active_owner", "active_worker", "non_terminal"])
def test_ambiguous_or_active_predecessor_cannot_use_terminal_fallback(fault):
    state = ledger()
    authority = state["lifecycle"]["p"]
    if fault == "no_settlement":
        state["executions"] = {}
    elif fault == "wrong_outcome":
        state["executions"]["accepted"]["outcome"] = "planning_required"
    elif fault == "wrong_task":
        state["executions"]["accepted"]["task_id"] = "P0"
    elif fault == "owner_gate":
        authority["owner_gate"] = {"code": "UNSAFE"}
    elif fault == "active_owner":
        authority["active_owner"] = {"role": "worker"}
    elif fault == "active_worker":
        state["executions"]["running"] = {"project_id": "p", "task_id": "P1", "state": "running"}
    elif fault == "non_terminal":
        authority["lifecycle_state"] = "EXECUTING"
    assert not _terminal_settled_predecessor(SimpleNamespace(state=lambda: state), "p", "P1")
