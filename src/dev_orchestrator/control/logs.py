"""Bounded, paginated read route for authoritative control logs.

Aggregates durable evidence across events.jsonl, runs.jsonl, control audit.jsonl,
and optional accounting events.jsonl with project filtering, opaque cursor pagination,
secret redaction, and explicit source availability reporting.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs

from dev_orchestrator.storage.json_store import parse_utc, utc_now_iso

_SENSITIVE_KEYS = frozenset({
    "api_key", "secret", "password", "token", "access_token", "refresh_token",
    "bearer", "authorization", "private_key", "cookie", "code", "csrf", "token_hash", "code_hash",
    "csrf_token", "service_token",
})
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/-]+", re.IGNORECASE)
_PEM_RE = re.compile(r"-----BEGIN[A-Z\s]+PRIVATE KEY-----.*?-----END[A-Z\s]+PRIVATE KEY-----", re.DOTALL)


def redact_secrets(value: Any) -> Any:
    """Recursively redact secrets, tokens, passwords, and private keys."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, val in value.items():
            key_lower = str(key).lower()
            if key_lower in _SENSITIVE_KEYS or (
                any(marker in key_lower for marker in ("secret", "password", "token", "api_key"))
                and not key_lower.endswith(("_id", "_type", "_name", "_count", "_status", "_state"))
            ):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_secrets(val)
        return result
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        cleaned = _BEARER_RE.sub(r"\1[REDACTED]", value)
        cleaned = _PEM_RE.sub("[REDACTED_PRIVATE_KEY]", cleaned)
        return cleaned
    return value


def _extract_timestamp(record: dict[str, Any], fallback_iso: str) -> str:
    for key in ("timestamp", "occurred_at", "time", "requested_at", "started_at", "finished_at", "created_at"):
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            parsed = parse_utc(val.strip())
            if parsed is not None:
                return val.strip()
    return fallback_iso


def _extract_project_id(record: dict[str, Any]) -> str | None:
    for key in ("project_id", "project", "target_project_id"):
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Check nested fields (e.g. record/expected/data)
    for parent in ("record", "data", "payload", "expected"):
        nested = record.get(parent)
        if isinstance(nested, dict):
            pid = _extract_project_id(nested)
            if pid:
                return pid
    return None


def _extract_field(record: dict[str, Any], field_names: tuple[str, ...]) -> str | None:
    for name in field_names:
        val = record.get(name)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for parent in ("record", "data", "payload", "target"):
        nested = record.get(parent)
        if isinstance(nested, dict):
            v = _extract_field(nested, field_names)
            if v:
                return v
    return None


def _read_jsonl_records(
    path: Path, source_name: str
) -> tuple[list[dict[str, Any]], str, list[str], int]:
    """Read JSONL file lines, returning records, availability, warnings, corrupt count."""
    if not path.is_file():
        return [], "not_found", [], 0
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    corrupt_count = 0
    try:
        mtime_iso = utc_now_iso()
        try:
            mtime_iso = parse_utc(path.stat().st_mtime).isoformat()
        except Exception:
            pass
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f, 1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        ts = _extract_timestamp(data, mtime_iso)
                        pid = _extract_project_id(data)
                        cmd_id = _extract_field(data, ("command_id", "cmd_id", "id"))
                        run_id = _extract_field(data, ("run_id", "run"))
                        records.append({
                            "source": source_name,
                            "timestamp": ts,
                            "project_id": pid,
                            "command_id": cmd_id,
                            "run_id": run_id,
                            "entry_id": f"{source_name}:{idx}",
                            "data": redact_secrets(data),
                        })
                    else:
                        corrupt_count += 1
                except (json.JSONDecodeError, ValueError):
                    corrupt_count += 1
    except OSError as exc:
        return [], "unavailable", [f"Error reading {source_name}: {exc}"], 0

    availability = "available"
    if corrupt_count > 0:
        availability = "degraded"
        warnings.append(f"{source_name} had {corrupt_count} unparseable line(s)")
    return records, availability, warnings, corrupt_count


def read_control_logs(
    runtime_root: Path | str,
    project_id: str | None = None,
    command_id: str | None = None,
    run_id: str | None = None,
    source: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Read, filter, redact and paginate control logs across runtime data stores."""
    runtime = Path(runtime_root)
    limit = max(1, min(100, int(limit)))

    offset = 0
    if cursor:
        try:
            decoded_bytes = base64.urlsafe_b64decode(cursor.strip().encode("ascii"))
            decoded = json.loads(decoded_bytes.decode("utf-8"))
            if isinstance(decoded, dict) and isinstance(decoded.get("offset"), int):
                offset = max(0, decoded["offset"])
            else:
                raise ValueError("cursor missing valid offset")
        except Exception as exc:
            raise ValueError(f"invalid cursor: {exc}") from exc

    # Locate sources, supporting both nested and flat layouts
    events_path = runtime / "history" / "events.jsonl"
    if not events_path.is_file() and (runtime / "events.jsonl").is_file():
        events_path = runtime / "events.jsonl"

    runs_path = runtime / "history" / "runs.jsonl"
    if not runs_path.is_file() and (runtime / "runs.jsonl").is_file():
        runs_path = runtime / "runs.jsonl"

    audit_path = runtime / "control" / "audit.jsonl"
    if not audit_path.is_file() and (runtime / "audit.jsonl").is_file():
        audit_path = runtime / "audit.jsonl"

    acct_path = runtime / "accounting" / "events.jsonl"
    if not acct_path.is_file() and (runtime / "execution-accounting" / "events.jsonl").is_file():
        acct_path = runtime / "execution-accounting" / "events.jsonl"

    source_defs = [
        ("events", events_path),
        ("runs", runs_path),
        ("control_audit", audit_path),
        ("accounting", acct_path),
    ]

    all_records: list[dict[str, Any]] = []
    sources_dict: dict[str, Any] = {}
    sources_list: list[dict[str, Any]] = []
    all_warnings: list[str] = []

    for name, path in source_defs:
        if source and source != name:
            continue
        recs, avail, warns, corrupt_cnt = _read_jsonl_records(path, name)
        src_info = {"name": name, "status": avail, "availability": avail}
        if corrupt_cnt > 0:
            src_info["corrupt_lines_skipped"] = corrupt_cnt
        sources_dict[name] = src_info
        sources_list.append(src_info)
        all_warnings.extend(warns)
        all_records.extend(recs)

    # Filter
    filtered = all_records
    if project_id:
        filtered = [r for r in filtered if r.get("project_id") == project_id]
    if command_id:
        filtered = [r for r in filtered if r.get("command_id") == command_id]
    if run_id:
        filtered = [r for r in filtered if r.get("run_id") == run_id]

    # Sort descending by timestamp, then entry_id
    filtered.sort(key=lambda r: (str(r.get("timestamp") or ""), str(r.get("entry_id") or "")), reverse=True)

    total_matched = len(filtered)
    page_records = filtered[offset: offset + limit]

    has_more = (offset + limit) < total_matched
    next_cursor = None
    if has_more:
        next_payload = {"offset": offset + limit, "project_id": project_id}
        next_cursor = base64.urlsafe_b64encode(json.dumps(next_payload).encode("utf-8")).decode("ascii")

    envelope = {
        "version": 1,
        "schema_version": 1,
        "items": page_records,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "total_matched": total_matched,
        "offset": offset,
        "limit": limit,
        "data": {
            "items": page_records,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total_matched": total_matched,
            "offset": offset,
            "limit": limit,
        },
        "sources": sources_dict,
        "sources_list": sources_list,
        "warnings": all_warnings,
        "observed_at": utc_now_iso(),
    }
    return envelope


def build_control_logs(
    runtime_root: Path | str,
    query_string: str = "",
) -> dict[str, Any]:
    """Derive the bounded paginated /api/v1/control/logs payload from a query string."""
    params = parse_qs(query_string)
    project_id = params.get("project_id", [None])[0]
    if project_id is not None:
        project_id = project_id.strip() or None

    command_id = params.get("command_id", [None])[0]
    if command_id is not None:
        command_id = command_id.strip() or None

    run_id = params.get("run_id", [None])[0]
    if run_id is not None:
        run_id = run_id.strip() or None

    source = params.get("source", [None])[0]
    if source is not None:
        source = source.strip() or None

    raw_limit = params.get("limit", ["50"])[0]
    try:
        limit = max(1, min(100, int(raw_limit)))
    except (ValueError, TypeError):
        limit = 50

    cursor = params.get("cursor", [None])[0]

    return read_control_logs(
        runtime_root=runtime_root,
        project_id=project_id,
        command_id=command_id,
        run_id=run_id,
        source=source,
        limit=limit,
        cursor=cursor,
    )
