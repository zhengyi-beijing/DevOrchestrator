"""Bounded, sanitized observability logging for machine transport operations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from dev_orchestrator.jobs.logs import BoundedNDJSONLog
from dev_orchestrator.storage.json_store import utc_now_iso

MAX_TRANSPORT_LOG_LINES = 5000
MAX_TRANSPORT_LOG_BYTES = 5 * 1024 * 1024  # 5 MiB


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
        "operation": operation,
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
    logger = BoundedNDJSONLog(
        log_file,
        max_job_bytes=MAX_TRANSPORT_LOG_BYTES,
        max_line_bytes=4096,
        head_lines=500,
        tail_lines=MAX_TRANSPORT_LOG_LINES,
    )
    logger.append(record)
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
        return {"items": [], "total_lines": 0, "cursor": 0, "next_cursor": None}

    logger = BoundedNDJSONLog(log_file)
    return logger.read_paginated(cursor=cursor, limit=limit)
