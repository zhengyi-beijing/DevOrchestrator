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
from base64 import b64encode
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
            response_request_id = response.getheader("X-DevOrch-Request-ID")
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
        if response_request_id and "request_id" not in payload:
            payload["request_id"] = response_request_id

        status = response.status
        if status in (200, 201, 202):
            return payload

        error_msg = str(payload.get("message") or payload.get("error") or f"HTTP {status}")
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

    def project_status(self, project_id: str) -> dict[str, Any]:
        """Return the bounded read-only status bundle used by browser control.

        The data is composed exclusively from the authoritative 8770 project and
        overview projections.  This client-side composition deliberately adds no
        lifecycle authority and exposes no generic URL, shell, or filesystem
        primitive.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a nonblank string")
        project_id = project_id.strip()

        project_envelope = self.status(project_id)
        overview_envelope = self.status()
        project = project_envelope.get("data")
        overview = overview_envelope.get("data")
        if not isinstance(project, dict):
            raise ControlAdapterNotFoundError(f"project not found: {project_id!r}")
        if not isinstance(overview, dict):
            raise ControlAdapterError("overview projection data is unavailable")

        def bounded(value: Any, fields: tuple[str, ...]) -> dict[str, Any] | None:
            if not isinstance(value, dict):
                return None
            return {name: value.get(name) for name in fields if name in value}

        control_identity = project.get("control_identity")
        if not isinstance(control_identity, dict):
            control_identity = {}
        authoritative = project.get("authoritative_lifecycle")
        if not isinstance(authoritative, dict):
            authoritative = {}
        conversation = project.get("conversation")
        if not isinstance(conversation, dict):
            conversation = {}
        binding = conversation.get("binding")
        if not isinstance(binding, dict):
            binding = project.get("conversation_binding")
        if not isinstance(binding, dict):
            binding = None

        binding_id = (
            str(binding.get("binding_id") or "") if isinstance(binding, dict)
            else str(control_identity.get("binding_id") or "")
        )
        binding_adapter = (
            str(binding.get("adapter") or "") if isinstance(binding, dict)
            else str(control_identity.get("binding_adapter") or "")
        )
        sessions = overview.get("sessions") if isinstance(overview.get("sessions"), list) else []
        session = next(
            (
                row for row in sessions
                if isinstance(row, dict)
                and row.get("binding_id") == binding_id
                and row.get("adapter") == binding_adapter
            ),
            None,
        )

        watchdog_root = overview.get("watchdog")
        watchdog_projects = (
            watchdog_root.get("projects")
            if isinstance(watchdog_root, dict) and isinstance(watchdog_root.get("projects"), dict)
            else {}
        )
        watchdog = watchdog_projects.get(project_id)

        executions_root = overview.get("executions")
        executions = (
            executions_root.get("executions")
            if isinstance(executions_root, dict) and isinstance(executions_root.get("executions"), list)
            else []
        )
        terminal_execution_states = {"completed", "failed", "cancelled", "succeeded"}
        active_broker_executions = [
            row for row in executions
            if isinstance(row, dict)
            and str(row.get("status") or "").lower() not in terminal_execution_states
        ]
        active_resource_ids = {
            str(row.get("resource_id")) for row in active_broker_executions if row.get("resource_id")
        }
        resources_root = overview.get("resources")
        resources = (
            resources_root.get("resources")
            if isinstance(resources_root, dict) and isinstance(resources_root.get("resources"), list)
            else []
        )
        active_resources = [
            bounded(row, (
                "resource_id", "provider", "model", "status", "state", "busy",
                "health", "execution_id",
            ))
            for row in resources
            if isinstance(row, dict) and str(row.get("resource_id") or "") in active_resource_ids
        ]

        git = project.get("git") if isinstance(project.get("git"), dict) else {}
        telemetry = project.get("telemetry") if isinstance(project.get("telemetry"), dict) else {}
        current_task = (
            authoritative.get("current_task_id")
            or control_identity.get("task_id")
            or telemetry.get("task_id")
        )
        active_executions = [
            bounded(row, (
                "execution_id", "request_id", "project_id", "task_id", "role",
                "resource_id", "provider", "model", "status", "state",
                "started_at", "updated_at",
            ))
            for row in active_broker_executions
            if row.get("project_id") == project_id
        ]
        raw_readiness = project.get("readiness")
        readiness = (
            bounded(raw_readiness, ("state", "status", "valid", "ready", "reason"))
            if isinstance(raw_readiness, dict)
            else raw_readiness
        )
        active_roles = project.get("active_roles") if isinstance(project.get("active_roles"), list) else []
        safe_roles = [
            bounded(row, ("role", "status", "state", "provider", "model", "resource_id", "execution_id"))
            if isinstance(row, dict) else row
            for row in active_roles
            if isinstance(row, (dict, str))
        ]
        return {
            "schema_version": 1,
            "generated_at": overview_envelope.get("generated_at") or project_envelope.get("generated_at"),
            "data": {
                "project_id": project_id,
                "git": {
                    "branch": git.get("branch"),
                    "head": git.get("head"),
                    "dirty": git.get("dirty"),
                    "changed_entries": git.get("changed_entries"),
                },
                "current_task": current_task,
                "lifecycle_state": (
                    authoritative.get("lifecycle_state")
                    or control_identity.get("lifecycle_state")
                    or project.get("lifecycle_state")
                    or project.get("state")
                ),
                "paused": control_identity.get("paused"),
                "readiness": readiness,
                "active_execution": bounded(project.get("active_execution"), (
                    "execution_id", "request_id", "project_id", "task_id", "role",
                    "resource_id", "provider", "model", "status", "state",
                    "started_at", "updated_at",
                )),
                "active_ai_roles": safe_roles,
                "active_executions": active_executions,
                "active_resources": active_resources,
                "watchdog": bounded(watchdog, (
                    "schema_version", "state", "status", "stage", "last_progress_at",
                    "last_observed_at", "stalled_since", "recovery_state", "reason",
                    "degraded_reason",
                )),
                "conversation": {
                    "binding": bounded(binding, (
                        "project_id", "state", "adapter", "binding_id", "title",
                        "url", "bound_at", "updated_at", "provenance",
                    )),
                    "session": bounded(session, (
                        "adapter", "binding_id", "title", "url", "state", "last_seen_at",
                        "active_tab_count", "stale_tab_count", "verified",
                    )),
                    "state": session.get("state") if isinstance(session, dict) else "undiscovered",
                },
                "control_identity": bounded(control_identity, (
                    "project_id", "task_id", "lifecycle_state", "paused",
                    "binding_id", "binding_adapter",
                )),
            },
            "warnings": list(project_envelope.get("warnings") or []) + list(overview_envelope.get("warnings") or []),
            "sources": list(project_envelope.get("sources") or []) + list(overview_envelope.get("sources") or []),
        }

    def external_status(self, project_id: str | None = None) -> dict[str, Any]:
        """Inspect the authenticated, bounded external-control status view."""
        path = "/api/v1/control/external/status"
        if project_id:
            path += "?" + urllib.parse.urlencode({"project_id": project_id.strip()})
        return self._request("GET", path)

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
        *,
        source: str | None = None,
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
        headers = {}
        if source:
            headers["X-DevO-Control-Source"] = source.strip()
        return self._request("POST", "/api/v1/control/commands", body=body, headers=headers or None)

    def command_status(self, command_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Check status and outcome of a submitted lifecycle control command."""
        safe_id = safe_command_id(command_id)
        if safe_id is None:
            raise ValueError(f"invalid command_id: {command_id!r}")
        result = self._request("GET", f"/api/v1/control/commands/{safe_id}")
        if project_id is not None:
            cmd_data = result.get("data") if isinstance(result.get("data"), dict) else {}
            cmd_project = cmd_data.get("project_id")
            if cmd_project and cmd_project != project_id.strip():
                raise ControlAdapterError(
                    f"cross-project access denied: command {command_id!r} belongs to project {cmd_project!r}, not {project_id!r}"
                )
        return result

    def transport_hosts(self) -> dict[str, Any]:
        """Fetch configured transport hosts."""
        return self._request("GET", "/api/v1/control/transport/hosts")

    def transport_capabilities(self, host_id: str | None = None) -> dict[str, Any]:
        """Fetch host capabilities."""
        path = "/api/v1/control/transport/capabilities"
        if host_id:
            path += f"?host_id={urllib.parse.quote(host_id)}"
        return self._request("GET", path)

    def transport_operations(self, limit: int = 50) -> dict[str, Any]:
        """Fetch recent transport operations."""
        return self._request("GET", f"/api/v1/control/transport/operations?limit={limit}")

    @staticmethod
    def _request_headers(request_id: str | None = None) -> dict[str, str] | None:
        if request_id is None:
            return None
        value = request_id.strip()
        if not value:
            raise ValueError("request_id must be nonblank when supplied")
        return {"X-DevOrch-Request-ID": value}

    def transport_exec(
        self,
        *,
        project_id: str,
        command_ref: str,
        idempotency_key: str,
        host_id: str = "local",
        parameters: dict[str, Any] | None = None,
        expected_working_directory: str | None = None,
        timeout_seconds: float | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "project_id": project_id,
            "command_ref": command_ref,
            "idempotency_key": idempotency_key,
            "host_id": host_id,
        }
        if parameters is not None:
            body["parameters"] = parameters
        if expected_working_directory is not None:
            body["expected_working_directory"] = expected_working_directory
        if timeout_seconds is not None:
            body["timeout_seconds"] = timeout_seconds
        return self._request("POST", "/api/v1/control/transport/exec", body, self._request_headers(request_id))

    def transport_spawn(
        self,
        *,
        project_id: str,
        command_ref: str,
        idempotency_key: str,
        host_id: str = "local",
        parameters: dict[str, Any] | None = None,
        expected_working_directory: str | None = None,
        input_digest: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "project_id": project_id,
            "command_ref": command_ref,
            "idempotency_key": idempotency_key,
            "host_id": host_id,
        }
        if parameters is not None:
            body["parameters"] = parameters
        if expected_working_directory is not None:
            body["expected_working_directory"] = expected_working_directory
        if input_digest is not None:
            body["input_digest"] = input_digest
        return self._request("POST", "/api/v1/control/transport/spawn", body, self._request_headers(request_id))

    def transport_poll(
        self, *, operation_id: str, project_id: str, host_id: str = "local",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST", "/api/v1/control/transport/poll",
            {"operation_id": operation_id, "project_id": project_id, "host_id": host_id},
            self._request_headers(request_id),
        )

    def transport_cancel(
        self, *, operation_id: str, project_id: str, host_id: str = "local",
        reason: str = "cancelled", request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST", "/api/v1/control/transport/cancel",
            {"operation_id": operation_id, "project_id": project_id, "host_id": host_id, "reason": reason},
            self._request_headers(request_id),
        )

    def transport_read_file(
        self, *, project_id: str, path: str, host_id: str = "local",
        max_bytes: int = 10 * 1024 * 1024, offset_bytes: int = 0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({
            "project_id": project_id, "path": path, "host_id": host_id,
            "max_bytes": int(max_bytes), "offset_bytes": int(offset_bytes),
        })
        return self._request(
            "GET", "/api/v1/control/transport/read?" + query,
            headers=self._request_headers(request_id),
        )

    def transport_stat(
        self, *, project_id: str, path: str, host_id: str = "local",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"project_id": project_id, "path": path, "host_id": host_id})
        return self._request(
            "GET", "/api/v1/control/transport/stat?" + query,
            headers=self._request_headers(request_id),
        )

    def transport_stage_write(
        self, *, project_id: str, content: bytes, host_id: str = "local",
        content_sha256: str, request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST", "/api/v1/control/transport/stage-write",
            {
                "project_id": project_id,
                "host_id": host_id,
                "content_base64": b64encode(content).decode("ascii"),
                "decoded_size_bytes": len(content),
                "content_sha256": content_sha256,
            },
            self._request_headers(request_id),
        )

    def transport_write_file(
        self, *, project_id: str, target_path: str, idempotency_key: str,
        content_ref: str, content_sha256: str, decoded_size_bytes: int,
        host_id: str = "local", if_absent: bool | None = None,
        expected_sha256: str | None = None, expected_file_policy_digest: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "project_id": project_id,
            "host_id": host_id,
            "target_path": target_path,
            "idempotency_key": idempotency_key,
            "content_ref": content_ref,
            "content_sha256": content_sha256,
            "decoded_size_bytes": int(decoded_size_bytes),
        }
        if if_absent is not None:
            body["if_absent"] = if_absent
        if expected_sha256 is not None:
            body["expected_sha256"] = expected_sha256
        if expected_file_policy_digest is not None:
            body["expected_file_policy_digest"] = expected_file_policy_digest
        return self._request(
            "POST", "/api/v1/control/transport/write", body,
            self._request_headers(request_id),
        )
