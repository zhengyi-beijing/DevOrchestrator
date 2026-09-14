from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path

import pytest

from dev_orchestrator.accounting import ExecutionEventStore, ExecutionRecorder
from dev_orchestrator.accounting.failure_memory import (
    FailureLesson,
    FailureMemory,
    lesson_fingerprint,
)


def make_lesson(environment: dict, label: str, *, verified: str = "verified") -> FailureLesson:
    symptom = f"symptom {label}"
    root = f"root {label}"
    preferred = f"prefer {label}"
    avoided = f"avoid {label}"
    fingerprint = lesson_fingerprint(environment, symptom, root, preferred, avoided)
    now = datetime.now(timezone.utc).isoformat()
    return FailureLesson(
        fingerprint,
        environment,
        symptom,
        root,
        preferred,
        avoided,
        0.9,
        verified,
        f"test:{label}",
        now,
        now,
    )


def test_deterministic_predicate_matching_and_verified_filter(tmp_path: Path) -> None:
    memory = FailureMemory(tmp_path, seed_verified_lessons=False)
    specific = memory.upsert(make_lesson({"os": "windows", "shell": "powershell"}, "specific"))
    generic = memory.upsert(make_lesson({"os": "windows"}, "generic"))
    memory.upsert(make_lesson({"os": "windows"}, "draft", verified="unverified"))
    matched = memory.matching({"os": "Windows", "shell": "PowerShell", "extra": True})
    assert [item.fingerprint for item in matched] == [specific.fingerprint, generic.fingerprint]
    assert memory.matching({"os": "linux", "shell": "powershell"}) == ()


def test_seeded_powershell_lesson_is_injected_with_provenance_and_cap(tmp_path: Path) -> None:
    memory = FailureMemory(tmp_path)
    block = memory.prompt_block(
        {"os": "windows", "shell": "powershell", "powershell_major": 5},
        max_chars=1000,
    )
    assert "&& or ||" in block
    assert "seed:p11b:rdc-powershell-5.1" in block
    assert "cmd.exe" in block
    assert len(block) <= 1000
    assert memory.prompt_block({"os": "linux"}, max_chars=1000) == ""


def test_size_cap_is_hard_and_selection_is_deterministic(tmp_path: Path) -> None:
    memory = FailureMemory(tmp_path, seed_verified_lessons=False)
    for index in range(5):
        memory.upsert(make_lesson({"os": "windows"}, "x" * (50 + index)))
    first = memory.prompt_block({"os": "windows"}, max_chars=400)
    second = memory.prompt_block({"os": "windows"}, max_chars=400)
    assert first == second
    assert len(first) <= 400


def test_recurrence_updates_memory_and_records_cost_event(tmp_path: Path) -> None:
    recorder = ExecutionRecorder(ExecutionEventStore(tmp_path))
    memory = FailureMemory(tmp_path, recorder=recorder, seed_verified_lessons=False)
    lesson = memory.upsert(make_lesson({"os": "windows"}, "repeat"))
    updated = memory.record_recurrence(
        lesson.fingerprint,
        12.5,
        evidence="same parser error",
        project_id="p",
        task_id="t",
    )
    assert updated.recurrence_count == 1
    assert updated.recurrence_cost_seconds == 12.5
    events = recorder.store.read().events
    assert events[-1]["event_type"] == "failure_recurrence"
    assert events[-1]["metadata"]["cost_seconds"] == 12.5


def test_seeded_known_parser_failure_records_recurrence(tmp_path: Path) -> None:
    recorder = ExecutionRecorder(ExecutionEventStore(tmp_path))
    memory = FailureMemory(tmp_path, recorder=recorder)
    matched = memory.record_matching_recurrences(
        {"os": "windows", "shell": "powershell", "powershell_major": 5},
        "ParserError: The token '&&' is not a valid statement separator in this version.",
        3.0,
        project_id="p",
    )
    assert len(matched) == 1
    assert matched[0].recurrence_count == 1


def test_recurrence_rejects_non_finite_cost(tmp_path: Path) -> None:
    memory = FailureMemory(tmp_path)
    lesson = memory.matching(
        {"os": "windows", "shell": "powershell", "powershell_major": 5}
    )[0]
    for cost in (math.nan, math.inf, -1.0):
        with pytest.raises(ValueError, match="finite and non-negative"):
            memory.record_recurrence(lesson.fingerprint, cost)
