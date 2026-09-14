"""Durable append-only execution event storage.

Each append is serialized by both a process-local lock and a one-byte OS file
lock.  Sequence allocation and the durable append happen while that lock is
held, which makes the sequence the evidence ordering across threads and
processes.  A malformed/torn tail is never skipped: reads report it and writes
fail until the caller explicitly recovers it.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

PHASES = frozenset(
    {
        "planning",
        "plan_review",
        "plan_remediation",
        "queue",
        "ai_execution",
        "retry",
        "managed_validation",
        "technical_review",
        "owner_wait",
        "idle",
    }
)
ROLES = frozenset(
    {
        "planner",
        "plan_reviewer",
        "worker",
        "remediation_worker",
        "reviewer",
        "validator",
        "owner",
        "control",
        "system",
    }
)
EVENT_TYPES = frozenset(
    {
        "interval_started",
        "interval_ended",
        "attempt_outcome",
        "owner_gate_opened",
        "owner_gate_closed",
        "failure_recurrence",
    }
)
OUTCOMES = frozenset({"accepted", "rejected", "failed", "cancelled", "unknown"})
_SCHEMA_VERSION = 1
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


class EventWriteError(RuntimeError):
    """An accounting event could not be durably written."""


class EventCorruptionError(EventWriteError):
    """Appending is unsafe because existing evidence is corrupt."""


@dataclass(frozen=True, slots=True)
class CorruptionReport:
    offset: int
    line_number: int
    reason: str
    sample_hex: str
    byte_count: int
    sample_truncated: bool
    torn_tail: bool


@dataclass(frozen=True, slots=True)
class EventReadResult:
    events: tuple[dict[str, Any], ...]
    corruptions: tuple[CorruptionReport, ...]
    last_sequence: int

    @property
    def torn_tail(self) -> bool:
        return any(item.torn_tail for item in self.corruptions)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class InterProcessFileLock:
    """Small stdlib-only blocking lock that works on Windows and POSIX."""

    def __init__(self, path: Path | str, *, timeout_seconds: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = float(timeout_seconds)
        self._handle: Any = None
        self._thread_lock = _thread_lock(self.path)

    def __enter__(self) -> "InterProcessFileLock":
        deadline = time.monotonic() + self.timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock.acquire()
        try:
            self._handle = self.path.open("a+b")
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            while True:
                try:
                    self._handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out acquiring accounting lock {self.path}")
                    time.sleep(0.01)
        except BaseException:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            self._thread_lock.release()
            raise

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self._handle is not None:
                self._handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                self._handle.close()
        finally:
            self._handle = None
            self._thread_lock.release()


def _validate_timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank UTC timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be UTC")
    return text


def _nonblank_optional(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank when present")
    return value.strip()


def _validated_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise TypeError("event must be a mapping")
    payload = dict(event)
    event_type = payload.get("event_type")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"event_type must be one of {sorted(EVENT_TYPES)}")
    payload["occurred_at"] = _validate_timestamp(payload.get("occurred_at"), "occurred_at")
    phase = payload.get("phase")
    if phase is not None and phase not in PHASES:
        raise ValueError(f"phase must be one of {sorted(PHASES)}")
    role = payload.get("role")
    if role is not None and role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    for key in (
        "project_id",
        "task_id",
        "request_id",
        "source_request_id",
        "dispatch_id",
        "decision_id",
        "execution_id",
        "session_id",
        "stage_run_id",
        "role_run_id",
        "resource_id",
        "provider",
        "account",
        "model",
        "interval_id",
        "attempt_id",
        "gate_id",
        "event_id",
    ):
        if key in payload:
            value = _nonblank_optional(payload[key], key)
            if value is None:
                payload.pop(key, None)
            else:
                payload[key] = value
    if event_type in {"interval_started", "interval_ended"}:
        if phase is None or "interval_id" not in payload:
            raise ValueError("interval events require phase and interval_id")
    if event_type in {"owner_gate_opened", "owner_gate_closed"}:
        payload["phase"] = "owner_wait"
        if "gate_id" not in payload:
            raise ValueError("owner gate events require gate_id")
        payload.setdefault("interval_id", payload["gate_id"])
    if event_type == "attempt_outcome":
        if "attempt_id" not in payload:
            raise ValueError("attempt_outcome requires attempt_id")
        if payload.get("outcome") not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
    if "metadata" in payload and not isinstance(payload["metadata"], Mapping):
        raise ValueError("metadata must be an object")
    if event_type == "failure_recurrence":
        metadata = payload.get("metadata")
        fingerprint = metadata.get("fingerprint") if isinstance(metadata, Mapping) else None
        cost = metadata.get("cost_seconds") if isinstance(metadata, Mapping) else None
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(ch not in "0123456789abcdef" for ch in fingerprint)
        ):
            raise ValueError("failure_recurrence requires a lowercase SHA-256 fingerprint")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ValueError("failure_recurrence cost_seconds must be a number")
        if not math.isfinite(float(cost)) or float(cost) < 0:
            raise ValueError("failure_recurrence cost_seconds must be finite and non-negative")
    payload.pop("sequence", None)
    payload.pop("recorded_at", None)
    payload.pop("schema_version", None)
    return payload


def _validate_stored_record(record: Mapping[str, Any], expected_sequence: int) -> None:
    """Validate a durable row with the same contract used by writers."""
    sequence = record.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise ValueError("event sequence is not an integer")
    if sequence != expected_sequence:
        raise ValueError("event sequence is not contiguous")
    if record.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported event schema version")
    _validate_timestamp(record.get("recorded_at"), "recorded_at")
    _validated_payload(record)


class ExecutionEventStore:
    """One append-only JSONL ledger below a runtime root."""

    def __init__(
        self,
        runtime_root: Path | str,
        *,
        relative_path: str = "execution-accounting/events.jsonl",
        max_corrupt_sample_bytes: int = 4096,
        lock_timeout_seconds: float = 30.0,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or relative.drive
            or ".." in relative.parts
            or relative in {Path(""), Path(".")}
        ):
            raise ValueError("execution event path must name a file below the runtime root")
        self.path = self.runtime_root / relative
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.max_corrupt_sample_bytes = max(64, int(max_corrupt_sample_bytes))
        self.lock_timeout_seconds = float(lock_timeout_seconds)

    def _locked(self) -> InterProcessFileLock:
        return InterProcessFileLock(self.lock_path, timeout_seconds=self.lock_timeout_seconds)

    def read(self) -> EventReadResult:
        with self._locked():
            return self._read_unlocked()

    def _read_unlocked(self) -> EventReadResult:
        try:
            data = self.path.read_bytes() if self.path.exists() else b""
        except OSError as exc:
            raise EventWriteError(f"cannot read execution event store: {exc}") from exc
        events: list[dict[str, Any]] = []
        corruptions: list[CorruptionReport] = []
        offset = 0
        lines = data.splitlines(keepends=True)
        for index, raw in enumerate(lines, 1):
            has_newline = raw.endswith(b"\n")
            content = raw[:-1]
            if content.endswith(b"\r"):
                content = content[:-1]
            reason: str | None = None
            decoded: Any = None
            try:
                decoded = json.loads(content.decode("utf-8"))
                if not isinstance(decoded, dict):
                    reason = "event record is not an object"
                else:
                    _validate_stored_record(decoded, len(events) + 1)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                reason = f"invalid JSON event: {exc}"
            if not has_newline and reason is None:
                reason = "event store has a non-terminated trailing record"
            if reason is not None:
                sample = raw[: self.max_corrupt_sample_bytes]
                corruptions.append(
                    CorruptionReport(
                        offset=offset,
                        line_number=index,
                        reason=reason,
                        sample_hex=sample.hex(),
                        byte_count=len(raw),
                        sample_truncated=len(raw) > len(sample),
                        torn_tail=index == len(lines) and not has_newline,
                    )
                )
                break
            events.append(decoded)
            offset += len(raw)
        return EventReadResult(tuple(events), tuple(corruptions), len(events))

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = _validated_payload(event)
        try:
            with self._locked():
                current = self._read_unlocked()
                if current.corruptions:
                    first = current.corruptions[0]
                    raise EventCorruptionError(
                        f"execution event store corrupt at byte {first.offset}: {first.reason}"
                    )
                event_id = payload.get("event_id")
                if event_id is not None:
                    for existing in current.events:
                        if existing.get("event_id") != event_id:
                            continue
                        comparable = dict(existing)
                        for key in ("schema_version", "sequence", "recorded_at"):
                            comparable.pop(key, None)
                        if comparable != payload:
                            raise EventCorruptionError(
                                f"conflicting replay for execution event_id {event_id}"
                            )
                        return existing
                record = {
                    "schema_version": _SCHEMA_VERSION,
                    "sequence": current.last_sequence + 1,
                    "recorded_at": _utc_now_iso(),
                    **payload,
                }
                encoded = (
                    json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("ab", buffering=0) as handle:
                    remaining = memoryview(encoded)
                    while remaining:
                        written = handle.write(remaining)
                        if written is None or written <= 0:
                            raise OSError("short write while appending execution event")
                        remaining = remaining[written:]
                    handle.flush()
                    os.fsync(handle.fileno())
                return record
        except (EventWriteError, EventCorruptionError):
            raise
        except (OSError, TimeoutError) as exc:
            raise EventWriteError(f"cannot append execution event: {exc}") from exc

    def append_many(self, events: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
        return tuple(self.append(event) for event in events)

    def recover_torn_tail(self) -> Path | None:
        """Quarantine and truncate only an incomplete final record.

        Complete malformed lines are not recoverable because discarding them
        would hide durable evidence corruption.
        """
        try:
            with self._locked():
                result = self._read_unlocked()
                if not result.corruptions:
                    return None
                if len(result.corruptions) != 1 or not result.corruptions[0].torn_tail:
                    raise EventCorruptionError("event corruption is not a recoverable torn tail")
                data = self.path.read_bytes()
                boundary = data.rfind(b"\n") + 1
                tail = data[boundary:]
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                quarantine = self.path.with_name(self.path.name + f".torn-{stamp}.bin")
                quarantine.write_bytes(tail)
                with self.path.open("r+b") as handle:
                    handle.truncate(boundary)
                    handle.flush()
                    os.fsync(handle.fileno())
                return quarantine
        except (EventWriteError, EventCorruptionError):
            raise
        except (OSError, TimeoutError) as exc:
            raise EventWriteError(f"cannot recover execution event tail: {exc}") from exc


class ExecutionRecorder:
    """Typed convenience facade; it never suppresses store failures."""

    def __init__(self, store: ExecutionEventStore) -> None:
        self.store = store

    @staticmethod
    def now() -> str:
        return _utc_now_iso()

    @staticmethod
    def _correlation(**values: Any) -> dict[str, Any]:
        return {key: value for key, value in values.items() if value is not None}

    def start_interval(
        self,
        phase: str,
        interval_id: str,
        *,
        occurred_at: str | None = None,
        **correlation: Any,
    ) -> dict[str, Any]:
        return self.store.append(
            {
                "event_type": "interval_started",
                "phase": phase,
                "interval_id": interval_id,
                "occurred_at": occurred_at or self.now(),
                **self._correlation(**correlation),
            }
        )

    def end_interval(
        self,
        phase: str,
        interval_id: str,
        *,
        occurred_at: str | None = None,
        outcome: str | None = None,
        **correlation: Any,
    ) -> dict[str, Any]:
        payload = {
            "event_type": "interval_ended",
            "phase": phase,
            "interval_id": interval_id,
            "occurred_at": occurred_at or self.now(),
            **self._correlation(**correlation),
        }
        if outcome is not None:
            if outcome not in OUTCOMES:
                raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
            payload["outcome"] = outcome
        return self.store.append(payload)

    @contextlib.contextmanager
    def interval(self, phase: str, interval_id: str, **correlation: Any):
        self.start_interval(phase, interval_id, **correlation)
        outcome = "unknown"
        try:
            yield
            outcome = "accepted"
        except BaseException:
            outcome = "failed"
            raise
        finally:
            self.end_interval(phase, interval_id, outcome=outcome, **correlation)

    def record_attempt_outcome(
        self,
        attempt_id: str,
        outcome: str,
        *,
        occurred_at: str | None = None,
        **correlation: Any,
    ) -> dict[str, Any]:
        return self.store.append(
            {
                "event_type": "attempt_outcome",
                "attempt_id": attempt_id,
                "outcome": outcome,
                "occurred_at": occurred_at or self.now(),
                **self._correlation(**correlation),
            }
        )

    def open_owner_gate(
        self, gate_id: str, *, occurred_at: str | None = None, **correlation: Any
    ) -> dict[str, Any]:
        return self.store.append(
            {
                "event_type": "owner_gate_opened",
                "gate_id": gate_id,
                "occurred_at": occurred_at or self.now(),
                **self._correlation(**correlation),
            }
        )

    def close_owner_gate(
        self, gate_id: str, *, occurred_at: str | None = None, **correlation: Any
    ) -> dict[str, Any]:
        return self.store.append(
            {
                "event_type": "owner_gate_closed",
                "gate_id": gate_id,
                "occurred_at": occurred_at or self.now(),
                **self._correlation(**correlation),
            }
        )

    def record_failure_recurrence(
        self,
        fingerprint: str,
        cost_seconds: float,
        *,
        occurred_at: str | None = None,
        **correlation: Any,
    ) -> dict[str, Any]:
        if (
            isinstance(cost_seconds, bool)
            or not math.isfinite(float(cost_seconds))
            or float(cost_seconds) < 0
        ):
            raise ValueError("cost_seconds must be finite and non-negative")
        return self.store.append(
            {
                "event_type": "failure_recurrence",
                "occurred_at": occurred_at or self.now(),
                "metadata": {"fingerprint": fingerprint, "cost_seconds": float(cost_seconds)},
                **self._correlation(**correlation),
            }
        )
