"""WebBridgeAdapter request store and execution boundary.

Provides a durable request store keyed by adapter_request_id and canonical request hash.
Guarantees idempotent replays for identical requests, conflicts on changed bodies,
quarantine-as-degraded on corrupt records, and freshness enforcement.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.control.adapter import ControlAdapterClient
from dev_orchestrator.control.command_store import safe_command_id
from dev_orchestrator.control.logs import redact_secrets
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json

WEB_BRIDGE_SCHEMA_VERSION = 1
WEB_BRIDGE_OPERATIONS = frozenset({"status", "logs", "submit_control", "command_status"})
DEFAULT_FRESHNESS_WINDOW_SECONDS = 300.0  # 5 minutes


class WebBridgeError(RuntimeError):
    """Base error for WebBridge operations."""


class WebBridgeConflictError(WebBridgeError):
    """adapter_request_id was reused with a different request payload."""


class WebBridgeCorruptionError(WebBridgeError):
    """Stored WebBridge request record was unreadable or corrupt."""


class WebBridgeFreshnessError(WebBridgeError):
    """Request timestamp is missing, expired, or in the future."""


def canonical_web_bridge_request(req: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a WebBridge request into canonical representation."""
    if not isinstance(req, dict):
        raise ValueError("WebBridge request must be a JSON object")

    allowed_top = {
        "schema_version", "adapter_request_id", "operation", "issued_at",
        "binding", "project_id", "expected_revision", "payload",
    }
    unknown = sorted(set(req) - allowed_top)
    if unknown:
        raise ValueError(f"unknown WebBridge request fields: {', '.join(unknown)}")

    if req.get("schema_version") != WEB_BRIDGE_SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: expected {WEB_BRIDGE_SCHEMA_VERSION}")

    adapter_request_id = req.get("adapter_request_id")
    if not isinstance(adapter_request_id, str) or not adapter_request_id.strip():
        raise ValueError("adapter_request_id must be a nonblank string")
    adapter_request_id = adapter_request_id.strip()

    operation = req.get("operation")
    if operation not in WEB_BRIDGE_OPERATIONS:
        raise ValueError(f"unsupported operation: {operation!r}")

    issued_at = req.get("issued_at")
    if not isinstance(issued_at, str) or not issued_at.strip():
        raise ValueError("issued_at must be a valid ISO 8601 UTC timestamp")
    dt = parse_utc(issued_at.strip())
    if dt is None:
        raise ValueError("issued_at is not a valid UTC timestamp")

    binding = req.get("binding")
    if not isinstance(binding, dict):
        raise ValueError("binding must be an object with adapter and binding_id")
    b_adapter = binding.get("adapter")
    b_id = binding.get("binding_id")
    if not isinstance(b_adapter, str) or not b_adapter.strip() or not isinstance(b_id, str) or not b_id.strip():
        raise ValueError("binding requires nonblank 'adapter' and 'binding_id'")

    project_id = req.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id must be a nonblank string")

    expected_rev = req.get("expected_revision")
    if expected_rev is not None and (not isinstance(expected_rev, str) or not expected_rev.strip()):
        raise ValueError("expected_revision must be nonblank when present")

    payload = req.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    return {
        "schema_version": WEB_BRIDGE_SCHEMA_VERSION,
        "adapter_request_id": adapter_request_id,
        "operation": operation,
        "issued_at": issued_at.strip(),
        "binding": {
            "adapter": b_adapter.strip(),
            "binding_id": b_id.strip(),
        },
        "project_id": project_id.strip(),
        "expected_revision": expected_rev.strip() if expected_rev else None,
        "payload": payload,
    }


def web_bridge_request_hash(canonical_req: dict[str, Any]) -> str:
    """Deterministic hash of canonical WebBridge request."""
    raw = json.dumps(canonical_req, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class WebBridgeRequestStore:
    """Durable request store for WebBridge requests and responses."""

    def __init__(self, runtime_root: Path | str) -> None:
        self.runtime_root = Path(runtime_root)
        self.root = self.runtime_root / "control"
        self.requests_dir = self.root / "web_bridge_requests"
        self.quarantine_dir = self.root / "quarantine"
        self.lock_path = self.root / "web_bridge.lock"
        self.health_path = self.root / "health.json"
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

    def _validate_freshness(self, issued_at: str, window_seconds: float) -> None:
        dt = parse_utc(issued_at)
        if dt is None:
            raise WebBridgeFreshnessError("invalid issued_at timestamp")
        now_dt = datetime.now(timezone.utc)
        age = (now_dt - dt).total_seconds()
        if age > window_seconds:
            raise WebBridgeFreshnessError(f"request timestamp expired (age {round(age, 1)}s > {window_seconds}s)")
        if age < 0.0:
            raise WebBridgeFreshnessError(f"request timestamp is in the future ({round(-age, 1)}s ahead)")

    def _quarantine(self, path: Path, reason: str) -> None:
        stamp = utc_now_iso().replace(":", "-")
        target = self.quarantine_dir / f"wb_{path.name}.corrupt-{stamp}"
        try:
            path.rename(target)
        except OSError:
            pass
        # Publish degraded health
        health = read_json(self.health_path, {})
        if not isinstance(health, dict):
            health = {}
        health["degraded"] = True
        health.setdefault("detected_corruption", []).append(str(path.name))
        health.setdefault("quarantined", []).append(target.name)
        write_json(self.health_path, health, indent=2)

    def handle_request(
        self,
        raw_request: dict[str, Any],
        client: ControlAdapterClient,
        *,
        freshness_window_seconds: float = DEFAULT_FRESHNESS_WINDOW_SECONDS,
    ) -> dict[str, Any]:
        """Process a WebBridge request with replay detection and durable persistence."""
        canonical = canonical_web_bridge_request(raw_request)
        self._validate_freshness(canonical["issued_at"], freshness_window_seconds)

        req_id = canonical["adapter_request_id"]
        safe_id = safe_command_id(req_id)
        if safe_id is None:
            raise ValueError(f"invalid adapter_request_id: {req_id!r}")

        req_hash = web_bridge_request_hash(canonical)
        record_path = self.requests_dir / f"{safe_id}.json"

        with InterProcessFileLock(self.lock_path):
            if record_path.is_file():
                existing = read_json(record_path, None)
                if not isinstance(existing, dict):
                    self._quarantine(record_path, "unreadable WebBridge record")
                    raise WebBridgeCorruptionError(f"unreadable WebBridge record for {safe_id!r}")

                stored_hash = existing.get("request_hash")
                if stored_hash == req_hash:
                    # Exact replay: return stored response
                    return existing.get("response", {})
                raise WebBridgeConflictError(
                    f"adapter_request_id {safe_id!r} reused with different request payload"
                )

            # Process new request through ControlAdapterClient
            op = canonical["operation"]
            proj_id = canonical["project_id"]
            payload = canonical["payload"]

            if op == "status":
                result = client.status(proj_id)
            elif op == "logs":
                result = client.logs(
                    project_id=proj_id,
                    cursor=payload.get("cursor"),
                    limit=payload.get("limit", 50),
                )
            elif op == "submit_control":
                expected_rev = canonical.get("expected_revision") or payload.get("expected_revision")
                if not expected_rev:
                    raise ValueError("submit_control requires expected_revision")
                action = payload.get("action")
                if not action:
                    raise ValueError("submit_control requires payload.action")
                target = payload.get("target") or {}
                result = client.submit_control(
                    adapter_request_id=safe_id,
                    project_id=proj_id,
                    action=action,
                    expected_revision=expected_rev,
                    target=target,
                )
            elif op == "command_status":
                command_id = payload.get("command_id")
                if not command_id:
                    raise ValueError("command_status requires payload.command_id")
                result = client.command_status(command_id, project_id=proj_id)
            else:
                raise ValueError(f"unsupported operation: {op!r}")

            # Persist durable record
            record = {
                "schema_version": WEB_BRIDGE_SCHEMA_VERSION,
                "adapter_request_id": safe_id,
                "request_hash": req_hash,
                "operation": op,
                "project_id": proj_id,
                "issued_at": canonical["issued_at"],
                "processed_at": utc_now_iso(),
                "response": redact_secrets(result),
            }
            write_json(record_path, record, indent=2)

            return result
