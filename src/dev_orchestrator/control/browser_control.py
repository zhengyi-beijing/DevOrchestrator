"""ChatGPT Plus Browser Control Bridge core protocol and service (P19.3).

Provides the versioned DEVORCH_ACTION_V1 protocol parser, execution engine,
idempotency store, confirmation gating, and audit logging for bridging
ChatGPT Plus web conversations to local DevOrchestrator control operations.

The bridge adapter is transport only: it executes no arbitrary shell or code,
enforces strict action allowlisting, gates effectful actions on explicit
human confirmation, and reuses the authoritative DevO service/operations layer.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from dev_orchestrator.accounting.events import InterProcessFileLock
from dev_orchestrator.control.logs import read_control_logs, redact_secrets
from dev_orchestrator.control.operations import (
    ControlOperationError,
    cancel_job,
    poll_job,
    read_file,
    spawn_job,
)
from dev_orchestrator.storage.json_store import parse_utc, read_json, utc_now_iso, write_json
from dev_orchestrator.web.server import external_status_payload

PROTOCOL_VERSION = "DEVORCH_ACTION_V1"
RESULT_PROTOCOL_VERSION = "DEVORCH_ACTION_RESULT_V1"
DEVORCH_ACTION_V1 = PROTOCOL_VERSION
DEVORCH_ACTION_RESULT_V1 = RESULT_PROTOCOL_VERSION

MAX_REQUEST_BYTES = 64 * 1024  # 64 KB
MAX_RESULT_BYTES = 1024 * 1024  # 1 MB

READ_ONLY_ACTIONS = frozenset({
    "status",
    "job_status",
    "read_log",
    "read_file",
    "commands_catalog",
})

EFFECTFUL_ACTIONS = frozenset({
    "start_task",
    "cancel_job",
})

ALL_ACTIONS = READ_ONLY_ACTIONS | EFFECTFUL_ACTIONS

_ACTION_ENVELOPE_RE = re.compile(
    r"(?:\[DEVORCH_ACTION_V1\]?|```(?:devorch_action|json:devorch_action|action))\s*(\{[\s\S]*?\})\s*(?:\[/DEVORCH_ACTION_V1\]|\]|```)",
    re.IGNORECASE,
)
_FALLBACK_DEVORCH_RE = re.compile(r"DEVORCH_ACTION_V1\s*(\{[\s\S]*?\})", re.IGNORECASE)


class BrowserControlError(Exception):
    """Base error for browser control operations."""

    def __init__(self, status_code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "status_code": self.status_code,
            "reason": self.reason,
            "error": self.message,
        }


class BrowserControlConfirmationRequiredError(BrowserControlError):
    """Raised when an effectful action is attempted without explicit human confirmation."""

    def __init__(self, action: str, project_id: str, request_id: str, details: dict[str, Any]) -> None:
        msg = f"Action '{action}' is effectful and requires explicit human confirmation in the browser"
        super().__init__(400, "Confirmation Required", msg)
        self.action = action
        self.project_id = project_id
        self.request_id = request_id
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d.update({
            "confirmation_required": True,
            "action": self.action,
            "project_id": self.project_id,
            "request_id": self.request_id,
            "details": self.details,
        })
        return d


class BrowserControlConflictError(BrowserControlError):
    """Raised when a request_id is reused with mismatched parameters or action."""

    def __init__(self, message: str) -> None:
        super().__init__(409, "Conflict", message)


def parse_action_envelope(raw_text: str) -> Optional[dict[str, Any]]:
    """Extract and validate a DEVORCH_ACTION_V1 envelope from raw message text.

    Supports:
    1. Bracket block: [DEVORCH_ACTION_V1] { ... } [/DEVORCH_ACTION_V1] or [DEVORCH_ACTION_V1 { ... }]
    2. Code fence: ```devorch_action { ... } ```
    3. Plain marker: DEVORCH_ACTION_V1 { ... }
    4. Raw JSON object if it contains protocol == "DEVORCH_ACTION_V1"
    """
    if not raw_text or not isinstance(raw_text, str):
        return None

    raw_trimmed = raw_text.strip()

    # Try raw JSON first
    if raw_trimmed.startswith("{") and raw_trimmed.endswith("}"):
        try:
            parsed = json.loads(raw_trimmed)
            if isinstance(parsed, dict) and parsed.get("protocol") == PROTOCOL_VERSION:
                return validate_action_envelope(parsed)
        except Exception:
            pass

    # Try regex patterns
    match = _ACTION_ENVELOPE_RE.search(raw_text)
    if not match:
        match = _FALLBACK_DEVORCH_RE.search(raw_text)

    if match:
        candidate_json = match.group(1).strip()
        try:
            parsed = json.loads(candidate_json)
            if isinstance(parsed, dict):
                return validate_action_envelope(parsed)
        except Exception:
            return None

    return None


def validate_action_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a parsed action envelope dictionary against the schema."""
    if not isinstance(data, dict):
        raise BrowserControlError(400, "Bad Request", "Action envelope must be a JSON object")

    protocol = data.get("protocol")
    if protocol != PROTOCOL_VERSION:
        raise BrowserControlError(400, "Bad Request", f"unsupported protocol: expected {PROTOCOL_VERSION!r}, got {protocol!r}")

    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise BrowserControlError(400, "Bad Request", "request_id must be a non-empty string")
    request_id = request_id.strip()

    action = data.get("action")
    if not isinstance(action, str) or action not in ALL_ACTIONS:
        raise BrowserControlError(400, "Bad Request", f"unsupported or unknown action: {action!r} (must be one of {sorted(ALL_ACTIONS)})")

    project_id = data.get("project_id", "devorchestrator")
    if not isinstance(project_id, str) or not project_id.strip():
        raise BrowserControlError(400, "Bad Request", "project_id must be a non-empty string")
    project_id = project_id.strip()

    parameters = data.get("parameters") if "parameters" in data else data.get("params", {})
    if not isinstance(parameters, dict):
        raise BrowserControlError(400, "Bad Request", "parameters must be a JSON object")

    confirmed = bool(data.get("confirmed", False))
    if not confirmed and isinstance(data.get("confirmation"), dict):
        confirmed = bool(data["confirmation"].get("confirmed", False))
    binding_id = data.get("binding_id")
    if binding_id is not None:
        binding_id = str(binding_id).strip()

    return {
        "protocol": PROTOCOL_VERSION,
        "request_id": request_id,
        "action": action,
        "project_id": project_id,
        "parameters": parameters,
        "confirmed": confirmed,
        "binding_id": binding_id,
    }


def format_action_result(
    request_id: str,
    action_or_status: str,
    status_or_data: Any = None,
    data: Any = None,
    error: Optional[str] = None,
    action: Optional[str] = None,
) -> str:
    """Format an action result envelope for transmission back to ChatGPT conversation.

    Supports both:
    1. format_action_result(request_id, status, data_or_error)
    2. format_action_result(request_id, action, status, data, error)
    """
    if status_or_data in ("success", "error", "rejected"):
        act = action_or_status
        stat = status_or_data
        d = data
        err = error
    else:
        act = action or "action"
        stat = action_or_status
        reason = None
        if stat == "success":
            d = status_or_data if data is None else data
            err = error
        else:
            d = data
            if isinstance(status_or_data, dict):
                err = status_or_data.get("error") or status_or_data.get("message") or "Execution failed"
                reason = status_or_data.get("reason")
            else:
                err = status_or_data if error is None else error

    payload: dict[str, Any] = {
        "protocol": RESULT_PROTOCOL_VERSION,
        "request_id": str(request_id),
        "action": str(act),
        "status": str(stat),
    }
    if d is not None:
        payload["data"] = d
    if err is not None:
        payload["error"] = str(err)
    if reason is not None:
        payload["reason"] = str(reason)

    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"[{RESULT_PROTOCOL_VERSION} {request_id}]\n{encoded}\n[/{RESULT_PROTOCOL_VERSION}]"


def compute_action_request_hash(action_req: dict[str, Any]) -> str:
    """Deterministic hash of canonical action request fields for idempotency."""
    canonical = {
        "protocol": action_req.get("protocol"),
        "request_id": action_req.get("request_id"),
        "action": action_req.get("action"),
        "project_id": action_req.get("project_id"),
        "parameters": action_req.get("parameters", {}),
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class BrowserControlRequestStore:
    """Persistent idempotency store for browser control requests."""

    def __init__(self, runtime_root: Path | str) -> None:
        self.runtime_root = Path(runtime_root)
        self.root = self.runtime_root / "control"
        self.store_dir = self.root / "browser_control_requests"
        self.lock_path = self.root / "browser_control.lock"
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, request_id: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", request_id)
        return self.store_dir / f"{safe_name}.json"

    def get_or_claim(
        self, request_id: str, request_hash: str
    ) -> tuple[Optional[dict[str, Any]], bool]:
        """Check for existing request.

        Returns (cached_response, is_replay).
        Raises BrowserControlConflictError if request_id matches but hash differs.
        """
        path = self._file_path(request_id)
        with InterProcessFileLock(self.lock_path):
            if path.is_file():
                existing = read_json(path, None)
                if isinstance(existing, dict):
                    stored_hash = existing.get("request_hash")
                    if stored_hash == request_hash:
                        return existing.get("response"), True
                    raise BrowserControlConflictError(
                        f"request_id {request_id!r} was reused with conflicting parameters or action"
                    )
        return None, False

    def save_response(
        self,
        request_id: str,
        request_hash: str,
        action: str,
        project_id: str,
        response: dict[str, Any],
        capability_id: str = "master",
    ) -> None:
        """Persist successful or terminal response for idempotency."""
        path = self._file_path(request_id)
        record = {
            "schema_version": 1,
            "request_id": request_id,
            "request_hash": request_hash,
            "action": action,
            "project_id": project_id,
            "capability_id": capability_id,
            "recorded_at": utc_now_iso(),
            "response": response,
        }
        with InterProcessFileLock(self.lock_path):
            write_json(path, record, indent=2)


class BrowserControlService:
    """Authoritative execution service for ChatGPT browser control actions.

    All operations delegate to existing shared DevO control/operations methods.
    Does NOT invent new lifecycle mutations or shell execution paths.
    """

    def __init__(
        self,
        runtime_root: Path | str,
        *,
        config_path: Optional[Path | str] = None,
        bridge_store: Optional[Any] = None,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.config_path = Path(config_path) if config_path else None
        self.bridge_store = bridge_store
        self.store = BrowserControlRequestStore(self.runtime_root)
        self.audit_file = self.runtime_root / "control" / "audit.jsonl"
        self.transport_audit_file = self.runtime_root / "logs" / "transport-operations.ndjson"

    def _log_audit(
        self,
        *,
        action: str,
        request_id: str,
        project_id: str,
        capability_id: str,
        status: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        entry = {
            "timestamp": utc_now_iso(),
            "event": "browser_control_action",
            "action": action,
            "request_id": request_id,
            "project_id": project_id,
            "capability_id": capability_id,
            "status": status,
            "details": details or {},
        }
        try:
            self.audit_file.parent.mkdir(parents=True, exist_ok=True)
            with InterProcessFileLock(self.runtime_root / "control" / "audit.lock"):
                with self.audit_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def execute_action(
        self,
        raw_request: dict[str, Any],
        *,
        capability_id: str = "master",
        host_id: str = "local",
    ) -> dict[str, Any]:
        """Execute a validated browser-control action request.

        1. Validates envelope and schema.
        2. Checks idempotency cache.
        3. Enforces confirmation policy for effectful actions.
        4. Dispatches to shared operations layer.
        5. Saves result in request store and writes audit log.
        """
        validated = validate_action_envelope(raw_request)
        action = validated["action"]
        request_id = validated["request_id"]
        project_id = validated["project_id"]
        parameters = validated["parameters"]
        confirmed = validated["confirmed"]
        req_hash = compute_action_request_hash(validated)

        # 1. Check idempotency
        cached_resp, is_replay = self.store.get_or_claim(request_id, req_hash)
        if is_replay and cached_resp is not None:
            return cached_resp

        # 2. Confirmation gate for effectful actions
        if action in EFFECTFUL_ACTIONS and not confirmed:
            details = {
                "command_ref": parameters.get("command_ref"),
                "job_id": parameters.get("job_id"),
                "parameters": parameters,
            }
            raise BrowserControlConfirmationRequiredError(action, project_id, request_id, details)

        # 3. Action dispatch
        try:
            result_data = self._dispatch(action, project_id, request_id, parameters, host_id, capability_id)
            response_payload = {
                "protocol": RESULT_PROTOCOL_VERSION,
                "request_id": request_id,
                "action": action,
                "project_id": project_id,
                "status": "success",
                "data": result_data,
            }
            self._log_audit(
                action=action,
                request_id=request_id,
                project_id=project_id,
                capability_id=capability_id,
                status="success",
            )
            self.store.save_response(request_id, req_hash, action, project_id, response_payload, capability_id)
            return response_payload
        except ControlOperationError as exc:
            self._log_audit(
                action=action,
                request_id=request_id,
                project_id=project_id,
                capability_id=capability_id,
                status="error",
                details={"status_code": exc.status_code, "reason": exc.reason, "error": exc.message},
            )
            raise BrowserControlError(exc.status_code, exc.reason, exc.message) from exc
        except BrowserControlError as exc:
            self._log_audit(
                action=action,
                request_id=request_id,
                project_id=project_id,
                capability_id=capability_id,
                status="error",
                details={"status_code": exc.status_code, "reason": exc.reason, "error": exc.message},
            )
            raise
        except Exception as exc:
            self._log_audit(
                action=action,
                request_id=request_id,
                project_id=project_id,
                capability_id=capability_id,
                status="error",
                details={"status_code": 500, "reason": "Internal Error", "error": str(exc)},
            )
            raise BrowserControlError(500, "Internal Server Error", str(exc)) from exc

    def _dispatch(
        self,
        action: str,
        project_id: str,
        request_id: str,
        params: dict[str, Any],
        host_id: str,
        capability_id: str,
    ) -> Any:
        target_host = params.get("host_id") or ("local" if host_id in ("bridge-local", "web-local", "test", "") else host_id)

        if action == "status":
            try:
                raw_status = external_status_payload(
                    self.runtime_root,
                    config_path=self.config_path,
                    bridge_store=self.bridge_store,
                    project_id=project_id,
                )
            except KeyError:
                raw_status = external_status_payload(
                    self.runtime_root,
                    config_path=self.config_path,
                    bridge_store=self.bridge_store,
                    project_id=None,
                )
            return redact_secrets(raw_status)

        elif action == "job_status":
            job_id = params.get("job_id") or params.get("operation_id")
            if not job_id:
                raise BrowserControlError(400, "Bad Request", "job_id is required for job_status")
            res = poll_job(
                self.runtime_root,
                project_id=project_id,
                job_id=str(job_id),
                host_id=target_host,
                request_id=request_id,
                capability_id=capability_id,
            )
            return redact_secrets(res)

        elif action == "read_log":
            source = str(params.get("source", "runs"))
            limit = int(params.get("limit", 20))
            cursor = params.get("cursor")
            job_id = params.get("job_id")
            res = read_control_logs(
                self.runtime_root,
                source=source,
                project_id=project_id,
                job_id=job_id,
                limit=limit,
                cursor=cursor,
            )
            return redact_secrets(res)

        elif action == "read_file":
            path = params.get("path")
            if not path:
                raise BrowserControlError(400, "Bad Request", "path is required for read_file")
            max_bytes = min(MAX_RESULT_BYTES, int(params.get("max_bytes", 65536)))
            offset_bytes = max(0, int(params.get("offset_bytes", 0)))
            res = read_file(
                self.runtime_root,
                project_id=project_id,
                path=str(path),
                host_id=target_host,
                max_bytes=max_bytes,
                offset_bytes=offset_bytes,
                request_id=request_id,
                capability_id=capability_id,
            )
            text = res.get("content_text")
            if text is None and res.get("content_base64"):
                try:
                    text = base64.b64decode(res["content_base64"]).decode("utf-8")
                except Exception:
                    text = None
            res["is_text"] = (text is not None)
            res["content"] = text if text is not None else res.get("content_base64")
            return redact_secrets(res)

        elif action == "commands_catalog":
            from dev_orchestrator.jobs.config import load_jobs_config
            jobs_cfg = load_jobs_config(self.runtime_root / "execution-jobs.json")
            commands = []
            if jobs_cfg is not None:
                project_cfg = jobs_cfg.projects.get(project_id)
                if project_cfg is not None:
                    for cref, cmd in project_cfg.commands.items():
                        param_types = {}
                        if isinstance(cmd.parameters, dict):
                            for pname, pdef in cmd.parameters.items():
                                if isinstance(pdef, dict):
                                    param_types[pname] = pdef.get("type", "unknown")
                                else:
                                    param_types[pname] = str(pdef)
                        is_hw = (cmd.effect_class == "hardware")
                        commands.append({
                            "command_ref": cref,
                            "effect_class": cmd.effect_class,
                            "description": getattr(cmd, "description", "") or getattr(cmd, "doc", "") or cref,
                            "timeout_seconds": getattr(cmd, "max_runtime_seconds", 300),
                            "parameters": param_types,
                            "is_hardware": is_hw,
                            "selectable": not is_hw,
                        })
            return {"project_id": project_id, "host_id": target_host, "commands": sorted(commands, key=lambda c: c["command_ref"])}

        elif action == "start_task":
            command_ref = params.get("command_ref")
            if not command_ref:
                raise BrowserControlError(400, "Bad Request", "command_ref is required for start_task")
            idempotency_key = params.get("idempotency_key") or f"bc-{request_id}"
            task_params = params.get("parameters")
            expected_cwd = params.get("expected_working_directory")
            res = spawn_job(
                self.runtime_root,
                project_id=project_id,
                command_ref=str(command_ref),
                idempotency_key=str(idempotency_key),
                host_id=target_host,
                parameters=task_params,
                expected_working_directory=expected_cwd,
                request_id=request_id,
                capability_id=capability_id,
            )
            return redact_secrets(res)

        elif action == "cancel_job":
            job_id = params.get("job_id")
            if not job_id:
                raise BrowserControlError(400, "Bad Request", "job_id is required for cancel_job")
            reason = str(params.get("reason", "cancelled via ChatGPT browser control"))
            res = cancel_job(
                self.runtime_root,
                project_id=project_id,
                job_id=str(job_id),
                host_id=target_host,
                reason=reason,
                request_id=request_id,
                capability_id=capability_id,
            )
            return redact_secrets(res)

        raise BrowserControlError(400, "Bad Request", f"unhandled action: {action}")
