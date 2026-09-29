"""Bounded, sanitized observability logging for machine transport operations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from dev_orchestrator.control.logs import redact_secrets
from dev_orchestrator.storage.json_store import utc_now_iso

MAX_TRANSPORT_LOG_LINES = 5000
MAX_TRANSPORT_LOG_BYTES = 5 * 1024 * 1024  # 5 MiB


def _compact_transport_log(log_file: Path) -> None:
    try:
        if not log_file.is_file():
            return
        lines: list[str] = []
        with log_file.open("r", encoding="utf-8-sig", errors="replace") as handle:
            lines = [l for l in handle if l.strip()]
        if len(lines) <= MAX_TRANSPORT_LOG_LINES:
            return
        head_count = 500
        tail_count = max(0, MAX_TRANSPORT_LOG_LINES - head_count)
        head = lines[:head_count]
        tail = lines[-tail_count:] if tail_count > 0 else []
        marker_entry = {
            "timestamp": utc_now_iso(),
            "operation": "log_compaction",
            "status": "compacted",
            "truncated_lines": len(lines) - len(head) - len(tail),
        }
        marker_line = json.dumps(marker_entry, ensure_ascii=False) + "\n"

        fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp-compact-", suffix=".ndjson", dir=str(log_file.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                for l in head:
                    handle.write(l.strip() + "\n")
                handle.write(marker_line)
                for l in tail:
                    handle.write(l.strip() + "\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.replace(tmp_name, log_file)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    except Exception:
        pass


def normalize_failure_fingerprint(error: Optional[str]) -> Optional[str]:
    """Derive stable normalized failure fingerprint without timestamps, PIDs, or volatile paths."""
    if not error or not error.strip():
        return None
    cleaned = error.strip().lower()
    # Normalize common volatile error strings
    if "connection timed out" in cleaned or "timeout" in cleaned:
        return "network_timeout"
    if "connection refused" in cleaned:
        return "connection_refused"
    if "host key" in cleaned:
        return "host_key_verification_failed"
    if "precondition_failed" in cleaned:
        return "write_precondition_failed"
    if "hardware" in cleaned:
        return "hardware_rejected"
    if "policy_pin" in cleaned:
        return "policy_pin_mismatch"
    if "parameter" in cleaned:
        return "parameter_validation_failed"
    if "corrupt" in cleaned:
        return "digest_corruption"
    # Fallback to normalized SHA-256 of first 120 chars
    short_err = cleaned[:120]
    return "fp-" + hashlib.sha256(short_err.encode("utf-8")).hexdigest()[:16]


def log_transport_operation(
    runtime_root: Path | str,
    *,
    operation_id: str,
    operation: str,
    host_id: str,
    selected_transport: str,
    status: str,
    request_id: Optional[str] = None,
    project_id: Optional[str] = None,
    command_ref: Optional[str] = None,
    parameters_names: Optional[list[str]] = None,
    parameters_digest: Optional[str] = None,
    execution_policy_digest: Optional[str] = None,
    resolution_digest: Optional[str] = None,
    candidate_reasons: Optional[Mapping[str, str]] = None,
    capability_id: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    exit_code: Optional[int] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    """Append one bounded, sanitized transport operation record.
    
    Strictly excludes content bytes, Base64 data, parameter values, private keys,
    tokens, and capability secrets.
    """
    rt = Path(runtime_root)
    logs_dir = rt / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "transport-operations.ndjson"

    record = {
        "timestamp": utc_now_iso(),
        "operation_id": operation_id,
        "request_id": request_id,
        "operation": operation,
        "project_id": project_id,
        "command_ref": command_ref,
        "parameters_names": sorted(parameters_names or []),
        "parameters_digest": parameters_digest,
        "execution_policy_digest": execution_policy_digest,
        "resolution_digest": resolution_digest,
        "host_id": host_id,
        "selected_transport": selected_transport,
        "candidate_reasons": dict(candidate_reasons or {}),
        "capability_id": capability_id,
        "duration_seconds": round(duration_seconds, 4) if duration_seconds is not None else None,
        "exit_code": exit_code,
        "status": status,
        "failure_fingerprint": normalize_failure_fingerprint(error),
    }

    # Bounded file write
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with log_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass

    try:
        if log_file.stat().st_size > MAX_TRANSPORT_LOG_BYTES:
            _compact_transport_log(log_file)
    except OSError:
        pass

    return record


def read_transport_operations(
    runtime_root: Path | str,
    *,
    cursor: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Read paginated recent transport operations."""
    rt = Path(runtime_root)
    log_file = rt / "logs" / "transport-operations.ndjson"
    if not log_file.is_file():
        return {
            "items": [],
            "operations": [],
            "count": 0,
            "total_lines": 0,
            "cursor": 0,
            "next_cursor": None,
        }

    records: list[dict[str, Any]] = []
    try:
        with log_file.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    item = json.loads(line_str)
                    if isinstance(item, dict):
                        records.append(item)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

    total = len(records)
    safe_cursor = max(0, int(cursor))
    safe_limit = max(1, min(500, int(limit)))
    sliced = records[safe_cursor : safe_cursor + safe_limit]
    redacted = [redact_secrets(row) for row in sliced]
    next_cursor = safe_cursor + len(redacted) if safe_cursor + len(redacted) < total else None

    return {
        "items": redacted,
        "operations": redacted,
        "count": len(redacted),
        "total_lines": total,
        "cursor": safe_cursor,
        "next_cursor": next_cursor,
    }
