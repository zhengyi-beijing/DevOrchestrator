"""Atomic JSON/JSONL storage and tolerant UTC time helpers.

All DevOrchestrator-owned runtime files are written here. Writes are
atomic (temp file in the target directory followed by ``os.replace``) so a
reader never observes a partially written projection.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

PathLike = Union[str, os.PathLike]


def utc_now() -> datetime:
    """Current aware UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with explicit offset."""
    return utc_now().isoformat()


def parse_utc(value: Any) -> Optional[datetime]:
    """Tolerantly parse an ISO-8601 timestamp into an aware UTC datetime.

    Accepts ``Z`` suffixes, explicit numeric offsets and naive timestamps
    (treated as UTC). Returns ``None`` when the value cannot be parsed or is
    blank. Extra fractional-second digits are tolerated by Python 3.11+.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ensure_dir(path: PathLike) -> Path:
    """Create the directory (and parents) for ``path`` if needed."""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_json(path: PathLike, value: Any, indent: Optional[int] = None) -> None:
    """Atomically write ``value`` as UTF-8 JSON to ``path``."""
    target = Path(path)
    ensure_dir(target.parent)
    text = json.dumps(value, indent=indent, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".tmp-", suffix=".json", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json(path: PathLike, fallback: Any = None) -> Any:
    """Read JSON from ``path``; return ``fallback`` when missing or invalid."""
    target = Path(path)
    try:
        with open(target, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


def read_text_strict(path: PathLike) -> str:
    """Read a file as UTF-8, raising when it is missing or not decodable."""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write_text(path: PathLike, text: str) -> None:
    """Atomically write a plain-text file (used for PID files)."""
    target = Path(path)
    ensure_dir(target.parent)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".tmp-", suffix=".txt", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_pid(path: PathLike) -> Optional[int]:
    """Read a PID file, returning ``None`` when absent/invalid/<=0."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def append_jsonl(path: PathLike, value: Any) -> None:
    """Append one compact JSON object line to a JSONL history file."""
    target = Path(path)
    ensure_dir(target.parent)
    with open(target, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def read_jsonl(path: PathLike) -> list:
    """Read all parseable JSON objects from a JSONL file, in file order."""
    target = Path(path)
    rows: list = []
    try:
        with open(target, "r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rows.append(json.loads(stripped))
                except ValueError:
                    continue
    except OSError:
        return rows
    return rows


def read_last_jsonl(path: PathLike, limit: Optional[int] = None) -> list:
    """Read the most recent ``limit`` (clamped to 1..100, default 20) entries.

    Returns entries in file order (oldest of the tail first), matching the
    PowerShell reference ``Select-Object -Last`` semantics.
    """
    target = Path(path)
    if limit is None:
        safe_limit = 20
    else:
        try:
            safe_limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            safe_limit = 20
    try:
        with open(target, "r", encoding="utf-8-sig", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    tail = [line.strip() for line in lines if line.strip()][-safe_limit:]
    rows: list = []
    for line in tail:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows
