"""Append-only reliability run registry and daemon boot ledger."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.storage.json_store import parse_utc, utc_now_iso

RUN_SCHEMA_VERSION = 1
RUN_REGISTRY_SUBDIR = "reliability"
RUN_REGISTRY_FILE = "runs.jsonl"
DAEMON_BOOTS_FILE = "daemon-boots.jsonl"


def _run_registry_path(runtime_root: Path | str) -> Path:
    return Path(runtime_root) / RUN_REGISTRY_SUBDIR / RUN_REGISTRY_FILE


def _daemon_boots_path(runtime_root: Path | str) -> Path:
    return Path(runtime_root) / DAEMON_BOOTS_FILE


def _lock_path(runtime_root: Path | str) -> Path:
    return Path(runtime_root) / RUN_REGISTRY_SUBDIR / "runs.lock"


def record_daemon_boot(
    runtime_root: Path | str,
    pid: int,
    listen_address: str,
    port: int,
    started_at: str | None = None,
    boot_id: str | None = None,
) -> dict[str, Any]:
    """Append one durable daemon boot identity record to daemon-boots.jsonl."""
    boots_path = _daemon_boots_path(runtime_root)
    boots_path.parent.mkdir(parents=True, exist_ok=True)
    assigned_boot_id = boot_id or f"boot-{uuid4().hex[:12]}"
    record = {
        "schema_version": RUN_SCHEMA_VERSION,
        "boot_id": assigned_boot_id,
        "pid": pid,
        "listen_address": f"{listen_address}:{port}",
        "started_at": started_at or utc_now_iso(),
    }
    with InterProcessFileLock(boots_path.with_suffix(".lock")):
        with boots_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    return record


def get_daemon_boots(runtime_root: Path | str) -> list[dict[str, Any]]:
    """Read all durable daemon boot records in chronological order."""
    boots_path = _daemon_boots_path(runtime_root)
    if not boots_path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        with boots_path.open("r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if line_str:
                    try:
                        data = json.loads(line_str)
                        if isinstance(data, dict):
                            records.append(data)
                    except Exception:
                        pass
    except OSError:
        return []
    return records


def open_reliability_run(
    runtime_root: Path | str,
    project_id: str,
    opener_source: str = "direct_call",
    boot_id: str | None = None,
) -> dict[str, Any]:
    """Open an explicit append-only reliability run."""
    reg_path = _run_registry_path(runtime_root)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(runtime_root)

    run_id = f"run-{uuid4().hex[:16]}"
    now_iso = utc_now_iso()
    mono = time.monotonic()

    active_boot_id = boot_id
    if not active_boot_id:
        boots = get_daemon_boots(runtime_root)
        if boots:
            active_boot_id = boots[-1].get("boot_id")

    record: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "project_id": str(project_id),
        "opened_at": now_iso,
        "closed_at": None,
        "monotonic_anchor_opened": mono,
        "monotonic_anchor_closed": None,
        "boot_id_at_open": active_boot_id,
        "opener_source": opener_source,
        "closer_source": None,
    }

    with InterProcessFileLock(lock_path):
        with reg_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    return record


def close_reliability_run(
    runtime_root: Path | str,
    run_id: str,
    closer_source: str = "direct_call",
) -> dict[str, Any]:
    """Close an active reliability run."""
    reg_path = _run_registry_path(runtime_root)
    lock_path = _lock_path(runtime_root)

    closed_record: dict[str, Any] | None = None
    with InterProcessFileLock(lock_path):
        records = []
        if reg_path.is_file():
            with reg_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line_str = line.strip()
                    if line_str:
                        records.append(json.loads(line_str))

        for rec in records:
            if rec.get("run_id") == run_id:
                if rec.get("closed_at") is None:
                    rec["closed_at"] = utc_now_iso()
                    rec["monotonic_anchor_closed"] = time.monotonic()
                    rec["closer_source"] = closer_source
                closed_record = rec
                break

        if closed_record is None:
            raise KeyError(f"Reliability run {run_id!r} not found in registry")

        # Atomic rewrite of registry file
        tmp_path = reg_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        os.replace(tmp_path, reg_path)

    return closed_record


def get_reliability_run(runtime_root: Path | str, run_id: str) -> dict[str, Any] | None:
    """Retrieve a reliability run by ID."""
    reg_path = _run_registry_path(runtime_root)
    if not reg_path.is_file():
        return None
    try:
        with reg_path.open("r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if line_str:
                    try:
                        rec = json.loads(line_str)
                        if rec.get("run_id") == run_id:
                            return rec
                    except Exception:
                        pass
    except OSError:
        return None
    return None


def list_reliability_runs(
    runtime_root: Path | str,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """List reliability runs, optionally filtered by project_id."""
    reg_path = _run_registry_path(runtime_root)
    if not reg_path.is_file():
        return []
    runs: list[dict[str, Any]] = []
    try:
        with reg_path.open("r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if line_str:
                    try:
                        rec = json.loads(line_str)
                        if project_id is None or rec.get("project_id") == project_id:
                            runs.append(rec)
                    except Exception:
                        pass
    except OSError:
        return []
    return runs
