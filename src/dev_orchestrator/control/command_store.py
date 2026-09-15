"""Atomic, idempotent command inbox and always-on control audit."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

CONTROL_SCHEMA_VERSION = 1
CONTROL_ACTIONS = frozenset({
    "continue", "pause", "resume", "stop", "retry", "reconcile",
    "approve_owner_gate", "bind_conversation", "unbind_conversation",
    "rebind_conversation",
})
EXPECTED_IDENTITY_FIELDS = (
    "revision", "project_id", "repo_path", "branch", "head", "dirty",
    "status_hash", "task_id", "lifecycle_state", "gate_id", "paused",
    "binding_state", "binding_id", "binding_adapter",
)


class ControlCommandConflictError(RuntimeError):
    """A command id was reused with different canonical input."""


class ControlCommandCorruptionError(RuntimeError):
    """A durable command record exists but cannot be read safely."""


def safe_command_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 128:
        return None
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    return text if all(ch in allowed for ch in text) else None


def _nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank")
    return value.strip()


def canonical_request(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {"schema_version", "command_id", "project_id", "action", "expected", "target"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError("unknown control request fields: " + ", ".join(unknown))
    if value.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise ValueError("unsupported control schema_version")
    expected = value.get("expected", {})
    target = value.get("target", {})
    if not isinstance(expected, dict):
        raise ValueError("expected must be an object")
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    action = _nonblank(value.get("action"), "action")
    if action not in CONTROL_ACTIONS:
        raise ValueError("unsupported control action")
    expected_allowed = set(EXPECTED_IDENTITY_FIELDS)
    expected_unknown = sorted(set(expected) - expected_allowed)
    if expected_unknown:
        raise ValueError("unknown expected fields: " + ", ".join(expected_unknown))
    expected_missing = [key for key in EXPECTED_IDENTITY_FIELDS if key not in expected]
    if expected_missing:
        raise ValueError("expected identity missing fields: " + ", ".join(expected_missing))
    target_allowed = {
        "continue": {"gate_id"},
        "pause": set(), "resume": set(), "stop": set(),
        "retry": {"target_id"}, "reconcile": {"target_id"},
        "approve_owner_gate": {"gate_id"},
        "bind_conversation": {"adapter", "binding_id"},
        "unbind_conversation": set(),
        "rebind_conversation": {"adapter", "binding_id"},
    }[action]
    target_unknown = sorted(set(target) - target_allowed)
    if target_unknown:
        raise ValueError("unknown target fields for action: " + ", ".join(target_unknown))
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "command_id": _nonblank(value.get("command_id"), "command_id"),
        "project_id": _nonblank(value.get("project_id"), "project_id"),
        "action": action,
        "expected": expected,
        "target": target,
    }


def request_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(canonical_request(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ControlCommandStore:
    """Serialize command submission across local threads and processes."""

    def __init__(self, runtime_root: Path | str) -> None:
        self.runtime_root = Path(runtime_root)
        self.root = self.runtime_root / "control"
        self.inbox = self.root / "inbox"
        self.history = self.root / "history"
        self.audit_path = self.root / "audit.jsonl"
        self.health_path = self.root / "health.json"
        self.quarantine_dir = self.root / "quarantine"
        self.lock_path = self.root / "commands.lock"

    def _read_existing(self, command_id: str) -> dict[str, Any] | None:
        for path in (self.history / f"{command_id}.json", self.inbox / f"{command_id}.json"):
            if not path.exists():
                continue
            value = read_json(path, None)
            if not isinstance(value, dict):
                raise ControlCommandCorruptionError(f"unreadable control record: {path.name}")
            return value
        return None

    @staticmethod
    def _existing_hash(value: dict[str, Any]) -> str | None:
        stored = value.get("request_hash")
        if isinstance(stored, str) and stored:
            return stored
        try:
            return request_hash(value)
        except ValueError:
            return None

    def submit(self, value: dict[str, Any], *, source: str) -> dict[str, Any]:
        canonical = canonical_request(value)
        command_id = safe_command_id(canonical["command_id"])
        if command_id is None:
            raise ValueError("invalid command_id")
        digest = request_hash(canonical)
        with InterProcessFileLock(self.lock_path):
            existing = self._read_existing(command_id)
            if existing is not None:
                if self._existing_hash(existing) != digest:
                    raise ControlCommandConflictError("command_id already belongs to different input")
                return existing
            record = {
                "version": 1,
                **canonical,
                "request_hash": digest,
                "source": _nonblank(source, "source"),
                "state": "pending",
                "requested_at": utc_now_iso(),
            }
            write_json(self.inbox / f"{command_id}.json", record, indent=2)
            self._append_audit_unlocked("request_accepted", record)
            return record

    def get(self, command_id: str) -> dict[str, Any] | None:
        safe = safe_command_id(command_id)
        if safe is None:
            raise ValueError("invalid command_id")
        return self._read_existing(safe)

    def pending_paths(self) -> list[Path]:
        if not self.inbox.is_dir():
            return []
        return sorted(self.inbox.glob("*.json"), key=lambda item: item.name)

    def settle(self, inbox_path: Path, outcome: dict[str, Any]) -> dict[str, Any]:
        command_id = safe_command_id(outcome.get("command_id"))
        if command_id is None:
            raise ValueError("cannot settle invalid command_id")
        with InterProcessFileLock(self.lock_path):
            self._repair_audit_unlocked()
            existing = read_json(self.history / f"{command_id}.json", None)
            terminal = existing if isinstance(existing, dict) else outcome
            if not isinstance(existing, dict):
                write_json(self.history / f"{command_id}.json", terminal, indent=2)
            if not self._audit_has_unlocked("command_settled", terminal):
                self._append_audit_unlocked("command_settled", terminal)
            inbox_path.unlink(missing_ok=True)
            return terminal

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(100, int(limit)))
        rows: list[dict[str, Any]] = []
        for directory in (self.history, self.inbox):
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                value = read_json(path, None)
                if isinstance(value, dict):
                    rows.append(value)
        rows.sort(key=lambda row: str(row.get("processed_at") or row.get("requested_at") or ""))
        return rows[-safe_limit:]

    def latest_for_project(self, project_id: str) -> dict[str, Any] | None:
        rows = [row for row in self.recent(100) if row.get("project_id") == project_id]
        return rows[-1] if rows else None

    def audit(self, event: str, value: dict[str, Any]) -> None:
        with InterProcessFileLock(self.lock_path):
            self._append_audit_unlocked(event, value)

    def quarantine(self, path: Path, reason: str) -> dict[str, Any]:
        """Preserve an unreadable queue record and publish degraded health."""
        with InterProcessFileLock(self.lock_path):
            return self._quarantine_unlocked(path, reason)

    def repair_corruption(self) -> list[dict[str, Any]]:
        """Quarantine unreadable inbox/history records and repair a torn audit."""
        outcomes: list[dict[str, Any]] = []
        with InterProcessFileLock(self.lock_path):
            self._repair_audit_unlocked()
            for directory in (self.inbox, self.history):
                if not directory.is_dir():
                    continue
                for path in sorted(directory.glob("*.json")):
                    if not isinstance(read_json(path, None), dict):
                        outcomes.append(self._quarantine_unlocked(
                            path, f"unreadable control record in {directory.name}"
                        ))
        return outcomes

    def health(self) -> dict[str, Any]:
        persisted = read_json(self.health_path, {})
        result = dict(persisted) if isinstance(persisted, dict) else {}
        corrupt: list[str] = []
        for directory in (self.inbox, self.history):
            if directory.is_dir():
                for path in directory.glob("*.json"):
                    if not isinstance(read_json(path, None), dict):
                        corrupt.append(str(path.relative_to(self.root)))
        if self.audit_path.is_file() and not self._audit_is_valid():
            corrupt.append(self.audit_path.name)
        result.setdefault("schema_version", 1)
        result["degraded"] = bool(result.get("degraded") or corrupt)
        result["detected_corruption"] = corrupt
        result["quarantined"] = sorted(path.name for path in self.quarantine_dir.glob("*") if path.is_file()) if self.quarantine_dir.is_dir() else []
        return result

    def _audit_is_valid(self) -> bool:
        try:
            raw = self.audit_path.read_bytes()
        except OSError:
            return False
        if raw and not raw.endswith(b"\n"):
            return False
        try:
            return all(isinstance(json.loads(line), dict) for line in raw.decode("utf-8").splitlines())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False

    def _mark_degraded_unlocked(self, reason: str, quarantined_file: str) -> None:
        current = read_json(self.health_path, {})
        value = dict(current) if isinstance(current, dict) else {}
        entries = list(value.get("events") or [])[-49:]
        entries.append({"occurred_at": utc_now_iso(), "reason": reason, "quarantined_file": quarantined_file})
        write_json(self.health_path, {"schema_version": 1, "degraded": True, "events": entries}, indent=2)

    def _quarantine_unlocked(self, path: Path, reason: str) -> dict[str, Any]:
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        destination = self.quarantine_dir / f"{path.name}.corrupt-{uuid4().hex}"
        try:
            path.replace(destination)
        except OSError as exc:
            reason = f"{reason}; quarantine failed: {exc}"
            destination = path
        self._mark_degraded_unlocked(reason, destination.name)
        outcome = {
            "version": 1, "command_id": "corrupt-" + uuid4().hex,
            "project_id": None, "action": None, "state": "failed",
            "reason": reason, "processed_at": utc_now_iso(),
            "quarantined_file": destination.name,
        }
        self._append_audit_unlocked("corruption_quarantined", outcome)
        return outcome

    def _repair_audit_unlocked(self) -> None:
        if not self.audit_path.exists() or self._audit_is_valid():
            return
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        destination = self.quarantine_dir / f"audit.jsonl.corrupt-{uuid4().hex}"
        try:
            self.audit_path.replace(destination)
        except OSError as exc:
            self._mark_degraded_unlocked(
                f"corrupt or torn control audit; quarantine failed: {exc}",
                self.audit_path.name,
            )
            return
        self._mark_degraded_unlocked("corrupt or torn control audit", destination.name)

    def _audit_has_unlocked(self, event: str, value: dict[str, Any]) -> bool:
        if not self.audit_path.is_file() or not self._audit_is_valid():
            return False
        command_id = value.get("command_id")
        request_digest = value.get("request_hash")
        try:
            with self.audit_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    if (
                        row.get("event") == event
                        and row.get("command_id") == command_id
                        and row.get("request_hash") == request_digest
                    ):
                        return True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return False

    def _append_audit_unlocked(self, event: str, value: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._repair_audit_unlocked()
        row = {
            "schema_version": 1,
            "event": event,
            "occurred_at": utc_now_iso(),
            "command_id": value.get("command_id"),
            "project_id": value.get("project_id"),
            "action": value.get("action"),
            "state": value.get("state"),
            "request_hash": value.get("request_hash"),
            "reason": value.get("reason"),
            "effect": value.get("effect"),
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
