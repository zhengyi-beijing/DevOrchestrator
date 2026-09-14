"""Opt-in runtime wiring for P11 execution accounting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_orchestrator.storage.json_store import write_json

from .events import ExecutionEventStore, ExecutionRecorder
from .failure_memory import FailureMemory


@dataclass(frozen=True, slots=True)
class AccountingRuntime:
    recorder: ExecutionRecorder
    failure_memory: FailureMemory | None
    prompt_max_chars: int


@dataclass(frozen=True, slots=True)
class AccountingSettings:
    event_path: str
    failure_memory: bool
    prompt_max_chars: int


def load_accounting_settings(config_path: Path | str) -> AccountingSettings | None:
    """Validate the top-level ``execution_accounting`` opt-in without writes.

    Missing configuration is deliberately disabled, preserving all historical
    orchestration and prompt behaviour.
    """
    path = Path(config_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot load execution accounting config from {path}: {exc}") from exc
    config = raw.get("execution_accounting") if isinstance(raw, dict) else None
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ValueError("execution_accounting must be an object")
    unknown = set(config) - {"enabled", "failure_memory", "prompt_max_chars", "event_path"}
    if unknown:
        raise ValueError(f"execution_accounting has unknown keys: {sorted(unknown)}")
    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("execution_accounting.enabled must be a boolean")
    if not enabled:
        return None
    max_chars = config.get("prompt_max_chars", 2000)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 128 <= max_chars <= 10000:
        raise ValueError("execution_accounting.prompt_max_chars must be an integer from 128 to 10000")
    event_path = config.get("event_path", "execution-accounting/events.jsonl")
    if not isinstance(event_path, str) or not event_path.strip():
        raise ValueError("execution_accounting.event_path must be nonblank")
    event_path = event_path.strip()
    rel = Path(event_path)
    if rel.is_absolute() or rel.drive or ".." in rel.parts or rel in {Path(""), Path(".")}:
        raise ValueError("execution_accounting.event_path must stay below the runtime root")
    if rel.as_posix().casefold() == "execution-accounting/runtime.json":
        raise ValueError("execution_accounting.event_path is reserved for reporting metadata")
    failure_enabled = config.get("failure_memory", True)
    if not isinstance(failure_enabled, bool):
        raise ValueError("execution_accounting.failure_memory must be a boolean")
    return AccountingSettings(event_path.replace("\\", "/"), failure_enabled, max_chars)


def load_accounting_runtime(
    runtime_root: Path | str, config_path: Path | str
) -> AccountingRuntime | None:
    """Create the enabled accounting runtime and publish its reporting path."""
    settings = load_accounting_settings(config_path)
    if settings is None:
        return None
    recorder = ExecutionRecorder(
        ExecutionEventStore(runtime_root, relative_path=settings.event_path)
    )
    write_json(
        Path(runtime_root) / "execution-accounting" / "runtime.json",
        {"schema_version": 1, "event_path": settings.event_path},
        indent=2,
    )
    memory = FailureMemory(runtime_root, recorder=recorder) if settings.failure_memory else None
    return AccountingRuntime(recorder, memory, settings.prompt_max_chars)
