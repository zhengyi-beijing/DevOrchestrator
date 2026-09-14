from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dev_orchestrator.accounting import (
    EventCorruptionError,
    ExecutionEventStore,
)


def event(worker: str, index: int) -> dict:
    return {
        "event_type": "attempt_outcome",
        "occurred_at": "2026-09-14T00:00:00Z",
        "attempt_id": f"{worker}:{index}",
        "outcome": "accepted",
        "project_id": "p",
    }


def test_threads_and_subprocesses_allocate_one_contiguous_sequence(tmp_path: Path) -> None:
    store = ExecutionEventStore(tmp_path)

    def write_thread(worker: int) -> None:
        for index in range(20):
            store.append(event(f"thread-{worker}", index))

    script = (
        "import sys\n"
        "from dev_orchestrator.accounting import ExecutionEventStore\n"
        "root, worker = sys.argv[1], sys.argv[2]\n"
        "s=ExecutionEventStore(root)\n"
        "for i in range(20):\n"
        " s.append({'event_type':'attempt_outcome','occurred_at':'2026-09-14T00:00:00Z',"
        "'attempt_id':f'{worker}:{i}','outcome':'accepted','project_id':'p'})\n"
    )
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(tmp_path), f"process-{i}"], env=env)
        for i in range(3)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(write_thread, i) for i in range(4)]
        for future in futures:
            future.result(timeout=30)
    for process in processes:
        assert process.wait(timeout=30) == 0

    result = store.read()
    assert not result.corruptions
    assert len(result.events) == 140
    assert [row["sequence"] for row in result.events] == list(range(1, 141))
    identities = [row["attempt_id"] for row in result.events]
    assert len(identities) == len(set(identities))


def test_torn_tail_is_bounded_reported_and_explicitly_recoverable(tmp_path: Path) -> None:
    store = ExecutionEventStore(tmp_path, max_corrupt_sample_bytes=64)
    store.append(event("ok", 1))
    with store.path.open("ab") as handle:
        handle.write(b'{"schema_version":1,"sequence":2,"event_type":"attempt_outcome"' + b"x" * 500)

    result = store.read()
    assert result.torn_tail
    assert len(result.events) == 1
    report = result.corruptions[0]
    assert report.sample_truncated
    assert len(bytes.fromhex(report.sample_hex)) == 64
    with pytest.raises(EventCorruptionError):
        store.append(event("blocked", 2))

    quarantine = store.recover_torn_tail()
    assert quarantine is not None and quarantine.read_bytes()
    appended = store.append(event("recovered", 2))
    assert appended["sequence"] == 2
    assert not store.read().corruptions


def test_complete_corrupt_line_is_not_discarded_as_torn_tail(tmp_path: Path) -> None:
    store = ExecutionEventStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b"not-json\n")
    assert not store.read().torn_tail
    with pytest.raises(EventCorruptionError):
        store.recover_torn_tail()


@pytest.mark.parametrize(
    "change",
    [
        {"phase": "not-a-phase"},
        {"role": "not-a-role"},
        {"occurred_at": "not-a-time"},
        {"recorded_at": "not-a-time"},
        {"sequence": True},
    ],
)
def test_complete_schema_invalid_record_blocks_reads_and_appends(
    tmp_path: Path, change: dict
) -> None:
    store = ExecutionEventStore(tmp_path)
    stored = store.append(event("valid", 1))
    stored.update(change)
    store.path.write_text(json.dumps(stored) + "\n", encoding="utf-8")

    result = store.read()
    assert len(result.events) == 0
    assert len(result.corruptions) == 1
    assert not result.torn_tail
    with pytest.raises(EventCorruptionError):
        store.append(event("blocked", 2))


def test_closed_taxonomy_rejects_unknown_event_and_phase(tmp_path: Path) -> None:
    store = ExecutionEventStore(tmp_path)
    with pytest.raises(ValueError):
        store.append({"event_type": "made_up", "occurred_at": "2026-09-14T00:00:00Z"})
    with pytest.raises(ValueError):
        store.append(
            {
                "event_type": "interval_started",
                "occurred_at": "2026-09-14T00:00:00Z",
                "phase": "coding-ish",
                "interval_id": "x",
            }
        )
    with pytest.raises(ValueError, match="must be UTC"):
        store.append(
            {
                "event_type": "attempt_outcome",
                "occurred_at": "2026-09-14T08:00:00+08:00",
                "attempt_id": "offset-time",
                "outcome": "accepted",
            }
        )


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -1.0, True])
def test_failure_recurrence_cost_must_be_finite_non_negative(
    tmp_path: Path, cost: float
) -> None:
    store = ExecutionEventStore(tmp_path)
    with pytest.raises(ValueError):
        store.append(
            {
                "event_type": "failure_recurrence",
                "occurred_at": "2026-09-14T00:00:00Z",
                "metadata": {"fingerprint": "a" * 64, "cost_seconds": cost},
            }
        )


def test_deterministic_event_id_replay_is_idempotent_and_conflicts_fail(tmp_path: Path) -> None:
    store = ExecutionEventStore(tmp_path)
    payload = {**event("one", 1), "event_id": "stable-event"}
    first = store.append(payload)
    second = store.append(payload)
    assert first == second
    assert len(store.read().events) == 1
    with pytest.raises(EventCorruptionError):
        store.append({**event("different", 2), "event_id": "stable-event"})


@pytest.mark.parametrize("relative_path", ["../outside.jsonl", "."])
def test_event_store_path_must_stay_below_runtime_root(
    tmp_path: Path, relative_path: str
) -> None:
    with pytest.raises(ValueError, match="below the runtime root"):
        ExecutionEventStore(tmp_path, relative_path=relative_path)

    with pytest.raises(ValueError, match="below the runtime root"):
        ExecutionEventStore(tmp_path, relative_path=str(tmp_path / "absolute.jsonl"))
