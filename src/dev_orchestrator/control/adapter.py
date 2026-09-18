"""Shared ControlAdapter service/client above the P12 HTTP API.

Provides bounded status, logs, submit_control, and command_status operations.
Enforces loopback-only authenticated API calls, stable caller request IDs, versioned
envelopes, and complete expected-identity derivation from daemon projections.
Never invokes lifecycle coordinators or mutates runtime stores directly.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from dev_orchestrator.control.command_store import (
    CONTROL_ACTIONS,
    EXPECTED_IDENTITY_FIELDS,
    safe_command_id,
)
from dev_orchestrator.control.security import is_loopback
from dev_orchestrator.storage.json_store import read_json


class ControlAdapterError(RuntimeError):
    """Base error for ControlAdapter operations."""


class ControlAdapterAuthError(ControlAdapterError):
    """Authentication or authorization failure."""


class ControlAdapterConflictError(ControlAdapterError):
    """Replay conflict or command conflict error."""


class ControlAdapterRevisionMismatchError(ControlAdapterError):
    """Expected revision does not match the authoritative projection revision."""


class ControlAdapterNotFoundError(ControlAdapterError):
    """Requested project, command, or resource was not found."""


class ControlAdapterClient:
    """Client for the authoritative loopback DevOrchestrator Control API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        runtime_root: Path | str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.runtime_root = Path(runtime_root) if runtime_root is not None else None
        self.timeout_seconds = max(1.0, float(timeout_seconds))

        if base_url is None and self.runtime_root is not None:
            web_info = read_json(self.runtime_root / "web.json", {})
            host = web_info.get("listen_address") if isinstance(web_info, dict) else "127.0.0.1"
            port = web_info.get("port") if isinstance(web_info, dict) else 8770
            base_url = f"http://{host}:{port}"
        elif base_url is None:
            base_url = "http://127.0.0.1:8770"

        self.base_url = self._validate_and_normalize_url(base_url)

        if token is None and self.runtime_root is not None:
            token_path = self.runtime_root / "control" / "api-token"
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except OSError:
                token = None

        self.token = token

    @staticmethod
    def _validate_and_normalize_url(url: str) -> str:
        parsed = urllib.parse.urlsplit(url.strip())
        if parsed.scheme.lower() != "http":
            raise ValueError(f"ControlAdapter requires loopback http:// url, got: {url!r}")
        hostname = parsed.hostname or ""
        if not is_loopback(hostname):
            raise ValueError(f"ControlAdapter target must be a loopback host, got: {hostname!r}")
        if parsed.username or parsed.password or "@" in parsed.netloc:
            raise ValueError("ControlAdapter URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("ControlAdapter URL must not contain query or fragment")
        port = parsed.port or 8770
        host_part = f"[{hostname}]" if ":" in hostname else hostname
        return f"http://{host_part}:{port}"

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8770

        req_headers = {"Accept": "application/json"}
        if self.token:
            req_headers["Authorization"] = f"Bearer {self.token}"
        if headers:
            req_headers.update(headers)

        encoded_body = None
        if body is not None:
            encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        conn = http.client.HTTPConnection(host, port, timeout=self.timeout_seconds)
        try:
            conn.request(method, path, body=encoded_body, headers=req_headers)
            response = conn.getresponse()
            raw_bytes = response.read()
        except OSError as exc:
            raise ControlAdapterError(f"Control API request to {path} failed: {exc}") from exc
        finally:
            conn.close()

        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ControlAdapterError(f"Control API returned invalid JSON from {path} (status {response.status})") from exc

        if not isinstance(payload, dict):
            raise ControlAdapterError(f"Control API response must be a JSON object from {path}")

        status = response.status
        if status in (200, 201, 202):
            return payload

        error_msg = str(payload.get("error") or payload.get("message") or f"HTTP {status}")
        if status in (401, 403):
            raise ControlAdapterAuthError(error_msg)
        if status == 404:
            raise ControlAdapterNotFoundError(error_msg)
        if status == 409:
            raise ControlAdapterConflictError(error_msg)
        raise ControlAdapterError(f"Control API returned error {status}: {error_msg}")

    def status(self, project_id: str | None = None) -> dict[str, Any]:
        """Inspect authoritative DevOrchestrator state (overview or specific project)."""
        if project_id:
            safe_id = urllib.parse.quote(project_id.strip(), safe="")
            return self._request("GET", f"/api/v1/control/projects/{safe_id}")
        return self._request("GET", "/api/v1/control/overview")

    def logs(
        self,
        project_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query bounded paginated authoritative logs."""
        params: list[tuple[str, str]] = [("limit", str(max(1, min(100, int(limit)))))]
        if project_id:
            params.append(("project_id", project_id.strip()))
        if cursor:
            params.append(("cursor", cursor.strip()))
        qs = urllib.parse.urlencode(params)
        return self._request("GET", f"/api/v1/control/logs?{qs}")

    def submit_control(
        self,
        adapter_request_id: str,
        project_id: str,
        action: str,
        expected_revision: str,
        target: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit a guarded lifecycle control with complete expected-identity derivation."""
        if not adapter_request_id or not isinstance(adapter_request_id, str):
            raise ValueError("adapter_request_id must be a nonblank string")
        if not project_id or not isinstance(project_id, str):
            raise ValueError("project_id must be a nonblank string")
        if action not in CONTROL_ACTIONS:
            raise ValueError(f"unsupported control action: {action!r}")
        if not expected_revision or not isinstance(expected_revision, str):
            raise ValueError("expected_revision must be a nonblank string")

        # 1. Fetch authoritative projection for the project
        proj_envelope = self.status(project_id)
        proj_data = proj_envelope.get("data")
        if not isinstance(proj_data, dict):
            raise ControlAdapterError(f"projection data missing for project {project_id!r}")

        identity = proj_data.get("control_identity") or proj_data.get("identity")
        if not isinstance(identity, dict):
            raise ControlAdapterError(f"control_identity missing in projection for project {project_id!r}")

        current_revision = str(identity.get("revision") or "")
        if current_revision != expected_revision.strip():
            raise ControlAdapterRevisionMismatchError(
                f"stale revision for project {project_id!r}: expected {expected_revision}, current is {current_revision}"
            )

        # 2. Derive complete expected identity
        expected = {k: identity.get(k) for k in EXPECTED_IDENTITY_FIELDS}

        # 3. Formulate deterministic command_id
        safe_req_id = "".join(c for c in adapter_request_id.strip() if c.isalnum() or c in "-_")[:64]
        if not safe_req_id:
            safe_req_id = "req"
        command_id = safe_req_id if safe_req_id.startswith("cmd-") else f"cmd-{safe_req_id}"

        body = {
            "schema_version": 1,
            "command_id": command_id,
            "project_id": project_id.strip(),
            "action": action,
            "expected": expected,
            "target": target or {},
        }

        # 4. Enqueue through POST /api/v1/control/commands
        return self._request("POST", "/api/v1/control/commands", body=body)

    def command_status(self, command_id: str) -> dict[str, Any]:
        """Check status and outcome of a submitted lifecycle control command."""
        safe_id = safe_command_id(command_id)
        if safe_id is None:
            raise ValueError(f"invalid command_id: {command_id!r}")
        return self._request("GET", f"/api/v1/control/commands/{safe_id}")
