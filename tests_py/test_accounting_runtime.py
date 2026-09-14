from __future__ import annotations

import json
from pathlib import Path

from dev_orchestrator.accounting.runtime import load_accounting_runtime
from dev_orchestrator.core.ai_planner import AIPlannerCoordinator
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.transition_executor import TransitionExecutor


class Truth:
    branch = "feature/test"
    head = "abc"
    dirty = False


def write_config(path: Path, accounting: dict | None) -> None:
    payload = {"projects": []}
    if accounting is not None:
        payload["execution_accounting"] = accounting
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_or_disabled_config_preserves_no_accounting_runtime(tmp_path: Path) -> None:
    config = tmp_path / "projects.json"
    write_config(config, None)
    assert load_accounting_runtime(tmp_path / "runtime", config) is None
    write_config(config, {"enabled": False})
    assert load_accounting_runtime(tmp_path / "runtime", config) is None
    assert not (tmp_path / "runtime" / "execution-accounting").exists()


def test_enabled_runtime_seeds_verified_lesson_and_honors_cap(tmp_path: Path) -> None:
    config = tmp_path / "projects.json"
    write_config(config, {"enabled": True, "prompt_max_chars": 700})
    runtime = load_accounting_runtime(tmp_path / "runtime", config)
    assert runtime is not None
    assert runtime.prompt_max_chars == 700
    assert len(runtime.failure_memory.lessons()) == 1


def test_verified_lessons_can_be_injected_into_all_three_role_prompts(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "projects.json"
    write_config(config, {"enabled": True, "prompt_max_chars": 1000})
    runtime = load_accounting_runtime(tmp_path / "runtime", config)
    assert runtime is not None and runtime.failure_memory is not None
    env = {"os": "windows", "shell": "powershell", "powershell_major": 5}
    block = runtime.failure_memory.prompt_block(env, max_chars=runtime.prompt_max_chars)

    record = {
        "project_id": "p",
        "task_id": "P11b",
        "branch": "feature/test",
        "head": "abc",
        "command_id": "c",
        "next_text": "# P11b\n",
        "failure_memory_block": block,
    }
    assert "seed:p11b:rdc-powershell-5.1" in AIPlannerCoordinator._planner_prompt(record)
    assert "seed:p11b:rdc-powershell-5.1" in AIPlannerCoordinator._review_prompt(
        record,
        {
            "task_id": "P11b",
            "summary": "s",
            "implementation_steps": ["i"],
            "interfaces": ["i"],
            "validation": ["v"],
            "risks": ["r"],
            "out_of_scope": ["o"],
        },
    )
    assert "seed:p11b:rdc-powershell-5.1" in AIReviewerCoordinator._review_prompt(
        "p", "P11b", "worker", Truth(), failure_memory_block=block
    )

    executor = TransitionExecutor(
        tmp_path / "runtime-2",
        accounting=runtime.recorder,
        failure_memory=runtime.failure_memory,
        failure_memory_max_chars=1000,
    )
    captured = {}

    def fake_launch(project, **kwargs):
        captured["prompt"] = kwargs["worker_prompt"]
        return None

    monkeypatch.setattr(executor, "_launch_aibroker", fake_launch)
    executor._launch(
        {
            "project_id": "p",
            "repo_path": str(tmp_path),
            "failure_environment": env,
        },
        source_request_id="source",
        source_kind="decision",
        task_id="P11b",
        source_task_id=None,
        branch="feature/test",
        head="abc",
        worker_prompt="work",
        policy={"engine": "aibroker"},
    )
    assert "seed:p11b:rdc-powershell-5.1" in captured["prompt"]
