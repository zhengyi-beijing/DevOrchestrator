"""Bounded append-only NDJSON log writer with head/tail retention and secret redaction."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.control.logs import redact_secrets
from dev_orchestrator.storage.json_store import utc_now_iso

DEFAULT_MAX_LINE_BYTES = 4096
DEFAULT_MAX_JOB_BYTES = 2 * 1024 * 1024
DEFAULT_HEAD_LINES = 1000
DEFAULT_TAIL_LINES = 1000


class BoundedNDJSONLog:
    """Append-only bounded NDJSON log with head+tail retention and secret redaction."""

    def __init__(
        self,
        log_path: Path | str,
        *,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        max_job_bytes: int = DEFAULT_MAX_JOB_BYTES,
        head_lines: int = DEFAULT_HEAD_LINES,
        tail_lines: int = DEFAULT_TAIL_LINES,
    ) -> None:
        self.path = Path(log_path)
        self.max_line_bytes = max(16, max_line_bytes)
        self.max_job_bytes = max(128, max_job_bytes)
        self.head_lines = max(1, head_lines)
        self.tail_lines = max(1, tail_lines)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        text: str,
        stream: str = "stdout",
        timestamp: Optional[str] = None,
    ) -> dict[str, Any]:
        """Append one log line with per-line capping and compaction if over max_job_bytes."""
        ts = timestamp or utc_now_iso()
        raw_bytes = text.encode("utf-8", errors="replace")
        truncated_bytes = 0
        if len(raw_bytes) > self.max_line_bytes:
            truncated_bytes = len(raw_bytes) - self.max_line_bytes
            text = raw_bytes[: self.max_line_bytes].decode("utf-8", errors="ignore")

        entry = {
            "ts": ts,
            "stream": stream,
            "text": text,
        }
        if truncated_bytes > 0:
            entry["truncated_bytes"] = truncated_bytes

        line_str = json.dumps(entry, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line_str)
            handle.flush()
            os.fsync(handle.fileno())

        # Check compaction
        try:
            size = self.path.stat().st_size
            if size > self.max_job_bytes:
                self._compact()
        except OSError:
            pass

        return entry

    def _compact(self) -> None:
        """Compact file to head_lines + marker + tail_lines."""
        lines = self._read_raw_lines()
        total = len(lines)
        keep_count = self.head_lines + self.tail_lines
        if total <= keep_count:
            return

        head = lines[: self.head_lines]
        tail = lines[-self.tail_lines :]
        dropped_lines = total - keep_count
        dropped_bytes = sum(len(l.encode("utf-8")) for l in lines[self.head_lines : -self.tail_lines])

        marker_entry = {
            "ts": utc_now_iso(),
            "marker": "truncated",
            "stream": "system",
            "truncated_lines": dropped_lines,
            "truncated_bytes": dropped_bytes,
            "text": f"[... truncated {dropped_lines} lines ({dropped_bytes} bytes) ...]",
        }
        marker_line = json.dumps(marker_entry, ensure_ascii=False) + "\n"

        fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp-compact-", suffix=".ndjson", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                for line in head:
                    handle.write(line.strip() + "\n")
                handle.write(marker_line)
                for line in tail:
                    handle.write(line.strip() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    def _read_raw_lines(self) -> list[str]:
        if not self.path.is_file():
            return []
        try:
            with self.path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                return [line for line in handle if line.strip()]
        except OSError:
            return []

    def read_paginated(self, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        """Read paginated records with torn-tail tolerance and secret redaction."""
        raw_lines = self._read_raw_lines()
        parsed: list[dict[str, Any]] = []
        accum_truncated_lines = 0
        accum_truncated_bytes = 0

        for line in raw_lines:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    if item.get("marker") == "truncated":
                        accum_truncated_lines += int(item.get("truncated_lines") or 0)
                        accum_truncated_bytes += int(item.get("truncated_bytes") or 0)
                    elif item.get("truncated_bytes"):
                        accum_truncated_bytes += int(item.get("truncated_bytes") or 0)
                    parsed.append(item)
            except (json.JSONDecodeError, ValueError):
                # Torn tail or unparseable line: skip safely
                continue

        total_lines = len(parsed)
        safe_cursor = max(0, int(cursor))
        safe_limit = max(1, min(500, int(limit)))

        sliced = parsed[safe_cursor : safe_cursor + safe_limit]
        redacted = [redact_secrets(row) for row in sliced]
        next_cursor = safe_cursor + len(redacted)

        return {
            "lines": redacted,
            "cursor": next_cursor,
            "total_lines": total_lines,
            "has_more": next_cursor < total_lines,
            "truncated_lines": accum_truncated_lines,
            "truncated_bytes": accum_truncated_bytes,
        }
