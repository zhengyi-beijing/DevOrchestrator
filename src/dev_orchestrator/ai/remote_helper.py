"""Fixed remote DevOrchestrator helper process.

Executed remotely via `python -m dev_orchestrator.ai.remote_helper` over OpenSSH.
Accepts structured JSON requests on stdin, dispatches local broker operations,
and returns structured JSON on stdout with correlated request_id and host_identity.
Prohibits arbitrary shell commands or path operations.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


from dev_orchestrator.ai.aibroker_subprocess import (
    _SERVICE_OPENER,
    sanitize_url,
    validate_loopback_url,
)


def _build_env(broker_repo: str | None) -> dict[str, str]:
    env = dict(os.environ)
    if broker_repo:
        broker_src = str(Path(broker_repo) / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = broker_src + (os.pathsep + existing if existing else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _service_call(
    service_url: str,
    endpoint: str,
    body: dict[str, Any] | None,
    token: str | None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    base = validate_loopback_url(service_url)
    url = base + endpoint
    safe_target = sanitize_url(url)
    req_headers = {"Accept": "application/json"}
    if token:
        req_headers["X-AIResourceBroker-Token"] = token
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    if data is not None:
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST" if body is not None else "GET")
    try:
        with _SERVICE_OPENER.open(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            try:
                raw = exc.read().decode("utf-8")
                parsed_404 = json.loads(raw)
                if isinstance(parsed_404, dict) and parsed_404.get("status") == "not_found":
                    return parsed_404
            except Exception:
                pass
            return {"status": "not_found"}
        if 300 <= exc.code < 400:
            raise RuntimeError(f"broker service redirect not permitted ({exc.code})") from exc
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(err_body)
        except Exception:
            err_msg = f"broker service error {exc.code} for {safe_target}: {err_body}"
            if token and token in err_msg:
                err_msg = err_msg.replace(token, "[REDACTED]")
            raise RuntimeError(err_msg) from exc
    except (OSError, urllib.error.URLError) as exc:
        err_msg = str(exc)
        if token and token in err_msg:
            err_msg = err_msg.replace(token, "[REDACTED]")
        raise RuntimeError(f"broker service invocation failed for {safe_target}: {err_msg}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"broker service returned invalid JSON for {safe_target}") from exc


def execute_request(req: dict[str, Any]) -> dict[str, Any]:
    operation = req.get("operation")
    req_id = req.get("request_id")
    if not operation or not req_id:
        raise ValueError("operation and request_id are required")

    broker_repo = req.get("broker_repo")
    config_path = req.get("config_path")
    database_path = req.get("database_path")
    service_url = req.get("service_url")
    service_token = req.get("service_token")

    if operation == "dispatch":
        role_req = req.get("request") or {}
        if not isinstance(role_req, dict):
            raise ValueError("dispatch request must be a dictionary")
        if not role_req.get("request_id") and req_id:
            role_req["request_id"] = req_id

        if service_url:
            timeout_sec = 60.0
            if role_req.get("timeout_seconds") is not None:
                try:
                    timeout_sec = max(60.0, float(role_req["timeout_seconds"]) + 60.0)
                except (TypeError, ValueError):
                    pass
            return _service_call(service_url, "/api/dispatch", role_req, service_token, timeout_seconds=timeout_sec)

        # CLI subprocess execution
        env = _build_env(broker_repo)

        with tempfile.TemporaryDirectory(prefix="devorch-remote-") as temp_dir:
            prompt_file = Path(temp_dir) / "prompt.txt"
            prompt_file.write_text(role_req.get("prompt", ""), encoding="utf-8")
            argv = [
                sys.executable, "-m", "ai_resource_broker.cli",
                "--config", str(config_path),
            ]
            if database_path:
                argv += ["--database", str(database_path)]
            argv += [
                "dispatch",
                "--role", str(role_req.get("role", "")),
                "--quality", str(role_req.get("quality", "standard")),
                "--independence", str(role_req.get("independence", "isolated")),
                "--prompt-file", str(prompt_file),
                "--request-id", str(role_req.get("request_id", req_id)),
                "--cwd", str(role_req.get("working_directory") or role_req.get("cwd") or "."),
            ]
            timeout = role_req.get("timeout_seconds") or role_req.get("timeout")
            if timeout is not None:
                argv += ["--timeout", str(timeout)]
            if req.get("probe") or role_req.get("probe"):
                argv.append("--probe")
            for res_id in role_req.get("excluded_resource_ids", []):
                argv += ["--excluded-resource-id", str(res_id)]

            prev_context = role_req.get("previous_resource_context")
            if isinstance(prev_context, dict):
                for flag, key in (
                    ("--previous-resource-id", "resource_id"),
                    ("--previous-provider", "provider"),
                    ("--previous-account", "account"),
                    ("--previous-model", "model"),
                ):
                    val = prev_context.get(key)
                    if val is not None:
                        argv += [flag, str(val)]

            completed = subprocess.run(
                argv,
                cwd=str(broker_repo) if broker_repo else None,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )

        if completed.returncode not in (0, 1):
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker dispatch rejected (exit {completed.returncode}): {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker dispatch returned invalid JSON (exit {completed.returncode}): {detail}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("remote broker dispatch payload must be a JSON object")
        return payload

    if operation == "status":
        if service_url:
            return _service_call(
                service_url,
                "/api/dispatches/" + urllib.parse.quote(str(req_id), safe=""),
                None,
                service_token,
            )
        env = _build_env(broker_repo)
        argv = [sys.executable, "-m", "ai_resource_broker.cli", "--config", str(config_path)]
        if database_path:
            argv += ["--database", str(database_path)]
        argv += ["dispatch-status", str(req_id)]
        completed = subprocess.run(
            argv,
            cwd=str(broker_repo) if broker_repo else None,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode not in (0, 1):
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker status rejected (exit {completed.returncode}): {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker status returned invalid JSON (exit {completed.returncode}): {detail}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("remote broker status payload must be a JSON object")
        return payload

    if operation == "interrupt":
        reason = req.get("reason", "DevOrchestrator managed stop")
        if service_url:
            return _service_call(
                service_url,
                "/api/dispatches/" + urllib.parse.quote(str(req_id), safe="") + "/interrupt",
                {"reason": reason},
                service_token,
            )
        env = _build_env(broker_repo)
        argv = [sys.executable, "-m", "ai_resource_broker.cli", "--config", str(config_path)]
        if database_path:
            argv += ["--database", str(database_path)]
        argv += ["interrupt-dispatch", str(req_id), "--reason", reason]
        completed = subprocess.run(
            argv,
            cwd=str(broker_repo) if broker_repo else None,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode not in (0, 1):
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker interrupt rejected (exit {completed.returncode}): {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"remote broker interrupt returned invalid JSON (exit {completed.returncode}): {detail}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("remote broker interrupt payload must be a JSON object")
        return payload

    raise ValueError(f"unsupported remote helper operation: {operation!r}")


def handle_request(req: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = execute_request(req)
        return {
            "request_id": req.get("request_id") if isinstance(req, dict) else None,
            "host_identity": socket.gethostname(),
            "status": "success",
            "payload": payload,
        }
    except Exception as exc:
        return {
            "request_id": req.get("request_id") if isinstance(req, dict) else None,
            "host_identity": socket.gethostname(),
            "status": "error",
            "error": str(exc),
            "payload": {},
        }


def main() -> int:
    try:
        raw_in = sys.stdin.read()
        if not raw_in.strip():
            sys.stderr.write("remote helper received empty input on stdin\n")
            return 1
        req = json.loads(raw_in)
        response = handle_request(req)
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0 if response.get("status") == "success" else 1
    except Exception as exc:
        err_response = {
            "request_id": None,
            "host_identity": socket.gethostname(),
            "status": "error",
            "error": str(exc),
            "payload": {},
        }
        sys.stdout.write(json.dumps(err_response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
