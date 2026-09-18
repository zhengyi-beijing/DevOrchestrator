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


def _service_call(service_url: str, endpoint: str, body: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
    url = service_url.rstrip("/") + endpoint
    req_headers = {"Accept": "application/json"}
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    if data is not None:
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "not_found"}
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(err_body)
        except Exception:
            raise RuntimeError(f"Service error {exc.code}: {err_body}") from exc


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
        if service_url:
            return _service_call(service_url, "/api/dispatch", role_req, service_token)

        # CLI subprocess execution
        env = dict(os.environ)
        if broker_repo:
            broker_src = str(Path(broker_repo) / "src")
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = broker_src + (os.pathsep + existing if existing else "")
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

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
                "--project-id", str(role_req.get("project_id", "")),
                "--role", str(role_req.get("role", "")),
                "--prompt-file", str(prompt_file),
                "--request-id", str(role_req.get("request_id", "")),
                "--quality", str(role_req.get("quality", "standard")),
                "--independence", str(role_req.get("independence", "isolated")),
                "--working-directory", str(role_req.get("working_directory", ".")),
            ]
            if role_req.get("timeout_seconds") is not None:
                argv += ["--timeout-seconds", str(role_req["timeout_seconds"])]
            for res_id in role_req.get("excluded_resource_ids", []):
                argv += ["--exclude-resource-id", str(res_id)]

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
        return json.loads(completed.stdout)

    if operation == "status":
        if service_url:
            return _service_call(
                service_url,
                "/api/dispatches/" + urllib.parse.quote(str(req_id), safe=""),
                None,
                service_token,
            )
        argv = [sys.executable, "-m", "ai_resource_broker.cli", "--config", str(config_path)]
        if database_path:
            argv += ["--database", str(database_path)]
        argv += ["dispatch-status", str(req_id)]
        completed = subprocess.run(argv, cwd=str(broker_repo) if broker_repo else None, capture_output=True, text=True, check=False)
        return json.loads(completed.stdout)

    if operation == "interrupt":
        reason = req.get("reason", "DevOrchestrator managed stop")
        if service_url:
            return _service_call(
                service_url,
                "/api/dispatches/" + urllib.parse.quote(str(req_id), safe="") + "/interrupt",
                {"reason": reason},
                service_token,
            )
        argv = [sys.executable, "-m", "ai_resource_broker.cli", "--config", str(config_path)]
        if database_path:
            argv += ["--database", str(database_path)]
        argv += ["interrupt-dispatch", str(req_id), "--reason", reason]
        completed = subprocess.run(argv, cwd=str(broker_repo) if broker_repo else None, capture_output=True, text=True, check=False)
        return json.loads(completed.stdout)

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
