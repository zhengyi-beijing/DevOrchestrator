from __future__ import annotations

from pathlib import Path

import pytest

from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator, _planner_policy
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.dispatcher import dispatch_worker_done_events
from dev_orchestrator.core.transition_executor import TransitionExecutor
from dev_orchestrator.core.workflow_policy import (
    CANONICAL_WORKFLOW_PATH,
    WORKFLOW_POLICY_MAX_CHARS,
    WORKFLOW_POLICY_ROLES,
    inject_workflow_policy,
    workflow_policy_prompt,
)
from tests_py.test_worker_done_dispatcher import make_repo, snap


class Truth:
    branch = "feature/test"
    head = "abc"
    dirty = False


def _plan() -> dict:
    return {
        "task_id": "P11-policy",
        "summary": "harden workflow policy",
        "implementation_steps": ["implement"],
        "interfaces": ["prompt helper"],
        "validation": ["tests"],
        "risks": ["provider discovery"],
        "out_of_scope": ["provider scheduling"],
    }


def _plan_record() -> dict:
    return {
        "project_id": "project",
        "task_id": "P11-policy",
        "branch": "feature/test",
        "head": "abc",
        "next_text": "# P11-policy\n",
    }


def test_role_policy_is_deterministic_bounded_and_complete() -> None:
    assert WORKFLOW_POLICY_ROLES == {
        "planner",
        "plan_reviewer",
        "adjudicator",
        "worker",
        "technical_reviewer",
        "remediator",
    }
    for role in sorted(WORKFLOW_POLICY_ROLES):
        first = workflow_policy_prompt(role)
        assert first == workflow_policy_prompt(role)
        assert len(first) <= WORKFLOW_POLICY_MAX_CHARS
        assert CANONICAL_WORKFLOW_PATH in first
        assert 5 <= len(first.splitlines()) <= 15
        with pytest.raises(ValueError, match="exceeds"):
            workflow_policy_prompt(role, max_chars=len(first) - 1)


def test_role_specific_policy_contracts() -> None:
    planner = workflow_policy_prompt("planner")
    assert "safe and specific enough to execute" in planner
    assert "Do not try to close every local implementation detail" in planner

    reviewer = workflow_policy_prompt("plan_reviewer")
    assert "BLOCKING" in reviewer and "NON_BLOCKING" in reviewer
    assert "NON_BLOCKING findings must not reject or delay implementation" in reviewer
    assert "execution readiness, not formal completeness" in reviewer

    adjudicator = workflow_policy_prompt("adjudicator")
    assert "unresolved BLOCKING delta" in adjudicator
    assert "read-only" in adjudicator
    assert "Do not redesign" in adjudicator
    assert "rewrite the full plan" in adjudicator

    worker = workflow_policy_prompt("worker")
    assert "Resolve NON_BLOCKING details through code" in worker
    assert "implementation and evidence" in worker

    technical = workflow_policy_prompt("technical_reviewer")
    assert "deep correctness review" in technical
    assert "directly to bounded remediation" in technical

    remediator = workflow_policy_prompt("remediator")
    assert "focused tests" in remediator and "regression" in remediator
    assert "Do not rewrite the full plan" in remediator


def test_policy_injection_is_idempotent() -> None:
    once = inject_workflow_policy("Do the task.", "worker")
    assert once == inject_workflow_policy("Do the task.", "worker")
    assert once == inject_workflow_policy(once, "worker")
    assert once.count("[WORKFLOW_POLICY role=worker]") == 1

    fake_marker = "Task text with [WORKFLOW_POLICY role=worker] but no policy."
    repaired = inject_workflow_policy(fake_marker, "worker")
    assert repaired.count("[WORKFLOW_POLICY role=worker]") == 2
    assert repaired.endswith(workflow_policy_prompt("worker"))


def test_planner_and_plan_reviewer_prompts_receive_explicit_policy() -> None:
    planner = AIPlannerCoordinator._planner_prompt(_plan_record())
    reviewer = AIPlannerCoordinator._review_prompt(_plan_record(), _plan())
    assert "[WORKFLOW_POLICY role=planner]" in planner
    assert "implementation detail" in planner
    assert "[WORKFLOW_POLICY role=plan_reviewer]" in reviewer
    assert "BLOCKING" in reviewer and "NON_BLOCKING" in reviewer


def test_technical_reviewer_prompt_receives_deep_review_policy() -> None:
    prompt = AIReviewerCoordinator._review_prompt(
        "project", "P11-policy", "worker-run", Truth()
    )
    assert "[WORKFLOW_POLICY role=technical_reviewer]" in prompt
    assert "deep correctness review" in prompt
    assert "Do not reopen full planning" in prompt


@pytest.mark.parametrize(
    ("source_kind", "expected_role"),
    [("decision", "worker"), ("remediation", "remediator")],
)
def test_worker_launch_uses_role_specific_implementation_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
    expected_role: str,
) -> None:
    executor = TransitionExecutor(tmp_path / "runtime")
    captured: dict[str, str] = {}

    def fake_launch(project: dict, **kwargs: str) -> None:
        captured["prompt"] = kwargs["worker_prompt"]

    monkeypatch.setattr(executor, "_launch_aibroker", fake_launch)
    executor._launch(
        {"project_id": "project", "repo_path": str(tmp_path)},
        source_request_id="source",
        source_kind=source_kind,
        task_id="P11-policy",
        source_task_id=None,
        branch="feature/test",
        head="abc",
        worker_prompt="Implement the task.",
        policy={"engine": "aibroker"},
    )
    assert f"[WORKFLOW_POLICY role={expected_role}]" in captured["prompt"]
    assert "code" in captured["prompt"] and "test" in captured["prompt"]


def test_browser_technical_reviewer_receives_explicit_policy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    runtime = tmp_path / "runtime"
    store = BrowserBridgeStore(runtime / "bridge")
    summary = {"projects": [snap("project", repo, "conversation", "run-1")]}

    assert len(dispatch_worker_done_events(summary, store, runtime)) == 1
    claim = store.claim("chatgpt_web", "conversation")
    assert claim is not None
    assert "[WORKFLOW_POLICY role=technical_reviewer]" in claim.prompt
    assert "deep correctness review" in claim.prompt


def test_plan_remediation_default_is_two_rounds() -> None:
    policy, reason = _planner_policy(
        {
            "execution": {"engine": "aibroker"},
            "ai_roles": {"planner": {"enabled": True}},
        }
    )
    assert reason == ""
    assert policy is not None
    assert policy["max_plan_remediation_rounds"] == 2


def test_provider_native_entries_are_thin_and_reference_canonical_policy() -> None:
    root = Path(__file__).resolve().parents[1]
    canonical = (root / CANONICAL_WORKFLOW_PATH).read_text(encoding="utf-8")
    entries = {
        "AGENTS.md": ("docs/development-workflow.md",),
        "CLAUDE.md": ("AGENTS.md", "docs/development-workflow.md"),
        "GEMINI.md": ("AGENTS.md", "docs/development-workflow.md"),
        ".github/copilot-instructions.md": (
            "AGENTS.md",
            "docs/development-workflow.md",
        ),
    }
    assert "## Context Propagation Contract" in canonical
    assert "## Anti-Patterns" in canonical
    for relative, references in entries.items():
        text = (root / relative).read_text(encoding="utf-8")
        assert all(reference in text for reference in references)
        assert len(text) < 2500
        assert "## Context Propagation Contract" not in text
        assert "## Anti-Patterns" not in text
