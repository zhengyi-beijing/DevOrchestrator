"""Structured, predicate-matched failure memory with bounded prompt injection."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from dev_orchestrator.storage.json_store import read_json, write_json

from .events import ExecutionRecorder, InterProcessFileLock

VERIFICATION_STATES = frozenset({"unverified", "verified", "superseded"})
_STATE_VERSION = 1
_SEED_PROVENANCE = "seed:p11b:rdc-powershell-5.1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def lesson_fingerprint(
    environment: Mapping[str, Any],
    symptom: str,
    root_cause: str,
    preferred_action: str,
    avoided_action: str,
) -> str:
    payload = {
        "environment": dict(environment),
        "symptom": symptom.strip(),
        "root_cause": root_cause.strip(),
        "preferred_action": preferred_action.strip(),
        "avoided_action": avoided_action.strip(),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FailureLesson:
    fingerprint: str
    environment: Mapping[str, Any]
    symptom: str
    root_cause: str
    preferred_action: str
    avoided_action: str
    confidence: float
    verification_state: str
    provenance: str
    created_at: str
    updated_at: str
    recurrence_count: int = 0
    recurrence_cost_seconds: float = 0.0
    last_recurred_at: str | None = None

    def __post_init__(self) -> None:
        if len(self.fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in self.fingerprint):
            raise ValueError("fingerprint must be lowercase SHA-256")
        if not isinstance(self.environment, Mapping) or not self.environment:
            raise ValueError("environment predicates must be a non-empty object")
        for name in ("symptom", "root_cause", "preferred_action", "avoided_action", "provenance"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonblank")
        if isinstance(self.confidence, bool) or not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.verification_state not in VERIFICATION_STATES:
            raise ValueError("invalid verification_state")
        if self.recurrence_count < 0:
            raise ValueError("recurrence count cannot be negative")
        if not math.isfinite(float(self.recurrence_cost_seconds)) or self.recurrence_cost_seconds < 0:
            raise ValueError("recurrence cost must be finite and non-negative")


def environment_for_project(project: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return observed/configured prompt predicates without provider inference."""
    configured = project.get("failure_environment") if isinstance(project, Mapping) else None
    if isinstance(configured, Mapping):
        return {str(key): value for key, value in configured.items()}
    result: dict[str, Any] = {
        "os": "windows" if os.name == "nt" else platform.system().casefold(),
    }
    execution = project.get("execution") if isinstance(project, Mapping) else None
    if isinstance(execution, Mapping):
        if isinstance(execution.get("shell"), str) and execution["shell"].strip():
            result["shell"] = execution["shell"].strip()
        if isinstance(execution.get("powershell_major"), int):
            result["powershell_major"] = execution["powershell_major"]
    elif os.name == "nt":
        result["shell"] = "powershell"
        result["powershell_major"] = 5
    if os.name == "nt" and "shell" not in result:
        result["shell"] = "powershell"
        result["powershell_major"] = 5
    return result


def _value_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, list):
        return any(_value_matches(item, actual) for item in expected)
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.casefold() == actual.casefold()
    return expected == actual


class FailureMemory:
    def __init__(
        self,
        runtime_root: Path | str,
        *,
        recorder: ExecutionRecorder | None = None,
        seed_verified_lessons: bool = True,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.path = self.runtime_root / "execution-accounting" / "failure-memory.json"
        self.lock_path = self.path.with_suffix(".lock")
        self.recorder = recorder
        if seed_verified_lessons:
            self.ensure_seeded()

    def _load(self) -> dict[str, Any]:
        raw = read_json(self.path, None)
        if raw is None and self.path.exists():
            raise RuntimeError("failure-memory store is unreadable or invalid JSON")
        if raw is not None and not isinstance(raw, dict):
            raise RuntimeError("failure-memory store must be an object")
        lessons = raw.get("lessons") if isinstance(raw, dict) else None
        if raw is not None and (
            raw.get("version") != _STATE_VERSION or not isinstance(lessons, dict)
        ):
            raise RuntimeError("failure-memory store version or lessons map is invalid")
        return {
            "version": _STATE_VERSION,
            "lessons": lessons if isinstance(lessons, dict) else {},
        }

    def _save(self, state: dict[str, Any]) -> None:
        write_json(self.path, state, indent=2)

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> FailureLesson:
        fields = {
            "fingerprint",
            "environment",
            "symptom",
            "root_cause",
            "preferred_action",
            "avoided_action",
            "confidence",
            "verification_state",
            "provenance",
            "created_at",
            "updated_at",
            "recurrence_count",
            "recurrence_cost_seconds",
            "last_recurred_at",
        }
        values = {key: row.get(key) for key in fields}
        values["recurrence_count"] = int(values.get("recurrence_count") or 0)
        values["recurrence_cost_seconds"] = float(values.get("recurrence_cost_seconds") or 0.0)
        return FailureLesson(**values)

    def ensure_seeded(self) -> FailureLesson:
        environment = {
            "os": "windows",
            "shell": ["powershell", "powershell5.1", "windows_powershell"],
            "powershell_major": 5,
        }
        symptom = "A Windows PowerShell 5.1 command fails when it contains && or ||."
        root_cause = "Windows PowerShell 5.1 does not implement the && and || pipeline-chain operators."
        preferred = "Use PowerShell-safe sequencing and explicit exit-code checks, or invoke cmd.exe when cmd syntax is required."
        avoided = "Do not place && or || directly in a Windows PowerShell 5.1 command."
        fingerprint = lesson_fingerprint(environment, symptom, root_cause, preferred, avoided)
        now = _now()
        lesson = FailureLesson(
            fingerprint=fingerprint,
            environment=environment,
            symptom=symptom,
            root_cause=root_cause,
            preferred_action=preferred,
            avoided_action=avoided,
            confidence=1.0,
            verification_state="verified",
            provenance=_SEED_PROVENANCE,
            created_at=now,
            updated_at=now,
        )
        return self.upsert(lesson, preserve_recurrence=True)

    def upsert(self, lesson: FailureLesson, *, preserve_recurrence: bool = True) -> FailureLesson:
        expected = lesson_fingerprint(
            lesson.environment,
            lesson.symptom,
            lesson.root_cause,
            lesson.preferred_action,
            lesson.avoided_action,
        )
        if lesson.fingerprint != expected:
            raise ValueError("lesson fingerprint does not match its immutable content")
        with InterProcessFileLock(self.lock_path):
            state = self._load()
            existing = state["lessons"].get(lesson.fingerprint)
            row = asdict(lesson)
            if preserve_recurrence and isinstance(existing, Mapping):
                for key in ("created_at", "recurrence_count", "recurrence_cost_seconds", "last_recurred_at"):
                    row[key] = existing.get(key, row.get(key))
                stable_keys = {
                    "fingerprint",
                    "environment",
                    "symptom",
                    "root_cause",
                    "preferred_action",
                    "avoided_action",
                    "confidence",
                    "verification_state",
                    "provenance",
                    "created_at",
                    "recurrence_count",
                    "recurrence_cost_seconds",
                    "last_recurred_at",
                }
                if all(existing.get(key) == row.get(key) for key in stable_keys):
                    return self._from_row({**existing, "fingerprint": lesson.fingerprint})
            state["lessons"][lesson.fingerprint] = row
            self._save(state)
            return self._from_row(row)

    def lessons(self) -> tuple[FailureLesson, ...]:
        with InterProcessFileLock(self.lock_path):
            state = self._load()
        rows: list[FailureLesson] = []
        for fingerprint, row in state["lessons"].items():
            if not isinstance(row, Mapping):
                raise RuntimeError(f"failure-memory lesson {fingerprint!r} is invalid")
            try:
                rows.append(self._from_row({**row, "fingerprint": fingerprint}))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"failure-memory lesson {fingerprint!r} is invalid: {exc}") from exc
        return tuple(sorted(rows, key=lambda item: item.fingerprint))

    def matching(
        self, environment: Mapping[str, Any], *, verified_only: bool = True
    ) -> tuple[FailureLesson, ...]:
        matches = []
        for lesson in self.lessons():
            if verified_only and lesson.verification_state != "verified":
                continue
            if all(key in environment and _value_matches(expected, environment[key]) for key, expected in lesson.environment.items()):
                matches.append(lesson)
        return tuple(sorted(matches, key=lambda item: (-len(item.environment), item.fingerprint)))

    def prompt_block(self, environment: Mapping[str, Any], *, max_chars: int = 2000) -> str:
        if max_chars < 128:
            raise ValueError("max_chars must be at least 128")
        header = "[VERIFIED_FAILURE_MEMORY]\nApply matching lessons; preserve their provenance."
        selected = header
        for lesson in self.matching(environment):
            entry = (
                "\n\n"
                f"- fingerprint: {lesson.fingerprint}\n"
                f"  provenance: {lesson.provenance}\n"
                f"  symptom: {lesson.symptom}\n"
                f"  root cause: {lesson.root_cause}\n"
                f"  preferred action: {lesson.preferred_action}\n"
                f"  avoided action: {lesson.avoided_action}"
            )
            if len(selected) + len(entry) > max_chars:
                continue
            selected += entry
        return selected if selected != header else ""

    def inject_prompt(
        self, prompt: str, environment: Mapping[str, Any], *, max_chars: int = 2000
    ) -> str:
        block = self.prompt_block(environment, max_chars=max_chars)
        return prompt if not block else prompt.rstrip() + "\n\n" + block

    def record_recurrence(
        self,
        fingerprint: str,
        cost_seconds: float,
        *,
        evidence: str | None = None,
        **correlation: Any,
    ) -> FailureLesson:
        if (
            isinstance(cost_seconds, bool)
            or not math.isfinite(float(cost_seconds))
            or float(cost_seconds) < 0
        ):
            raise ValueError("cost_seconds must be finite and non-negative")
        with InterProcessFileLock(self.lock_path):
            state = self._load()
            row = state["lessons"].get(fingerprint)
            if not isinstance(row, Mapping):
                raise KeyError(f"unknown failure lesson: {fingerprint}")
            updated = dict(row)
            updated["recurrence_count"] = int(updated.get("recurrence_count") or 0) + 1
            updated["recurrence_cost_seconds"] = float(updated.get("recurrence_cost_seconds") or 0.0) + float(cost_seconds)
            updated["last_recurred_at"] = _now()
            updated["updated_at"] = updated["last_recurred_at"]
            if evidence is not None:
                updated["last_recurrence_evidence"] = str(evidence)[:2000]
            state["lessons"][fingerprint] = updated
            self._save(state)
            lesson = self._from_row({**updated, "fingerprint": fingerprint})
        if self.recorder is not None:
            self.recorder.record_failure_recurrence(
                fingerprint, float(cost_seconds), **correlation
            )
        return lesson

    def record_matching_recurrences(
        self,
        environment: Mapping[str, Any],
        symptom_text: str,
        cost_seconds: float,
        **correlation: Any,
    ) -> tuple[FailureLesson, ...]:
        """Record verified lessons whose known symptom is present.

        General lessons use a literal symptom match.  The seeded PowerShell
        lesson additionally recognizes the actual parser wording while still
        requiring its environment predicates to match.
        """
        haystack = str(symptom_text or "").casefold()
        recorded: list[FailureLesson] = []
        for lesson in self.matching(environment):
            symptom_match = lesson.symptom.casefold() in haystack
            if lesson.provenance == _SEED_PROVENANCE:
                operator = "&&" in haystack or "||" in haystack
                parser = any(
                    marker in haystack
                    for marker in (
                        "not a valid statement separator",
                        "unexpected token",
                        "parsererror",
                        "无法识别",
                    )
                )
                symptom_match = symptom_match or (operator and parser)
            if symptom_match:
                recorded.append(
                    self.record_recurrence(
                        lesson.fingerprint,
                        cost_seconds,
                        evidence=symptom_text,
                        **correlation,
                    )
                )
        return tuple(recorded)
