"""Process-isolated AIResourceBroker execution port."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import AIRoleRequest, AIRoleResult, ResourceContext


class AIBrokerInvocationError(RuntimeError):
    """The broker transport/contract failed before a valid dispatch result."""


@dataclass(frozen=True, slots=True)
class AIBrokerClientConfig:
    python_executable: Path
    broker_repo: Path
    config_path: Path
    database_path: Path | None = None
    process_timeout_seconds: float = 600.0
    probe_before_dispatch: bool = True

    def __post_init__(self) -> None:
        for name in ("python_executable", "broker_repo", "config_path"):
            object.__setattr__(self, name, Path(getattr(self, name)))
        if self.database_path is not None:
            object.__setattr__(self, "database_path", Path(self.database_path))


class AIBrokerExecutionPort:
    """Invoke AIBroker P2.5 dispatch in its own Python environment."""

    def __init__(self, config: AIBrokerClientConfig) -> None:
        self.config = config

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        argv = self._build_argv(request)
        env = dict(os.environ)
        broker_src = str(self.config.broker_repo / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = broker_src + (os.pathsep + existing if existing else "")
        transport_timeout = self.config.process_timeout_seconds
        if request.timeout_seconds is not None:
            transport_timeout = max(transport_timeout, float(request.timeout_seconds) + 60.0)
        try:
            completed = subprocess.run(
                argv,
                cwd=str(self.config.broker_repo),
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=transport_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AIBrokerInvocationError(f"broker invocation failed: {exc}") from exc

        if completed.returncode not in (0, 1):
            detail = (completed.stderr or completed.stdout).strip()
            raise AIBrokerInvocationError(
                f"broker rejected request (exit {completed.returncode}): {detail}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AIBrokerInvocationError("broker returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AIBrokerInvocationError("broker result must be a JSON object")
        return self._result_from_payload(request, payload)

    def status(self, request_id: str) -> dict[str, Any] | None:
        payload = self._reconcile_call(["dispatch-status", request_id])
        return None if payload.get("status") == "not_found" else payload

    def interrupt(self, request_id: str, reason: str) -> dict[str, Any] | None:
        payload = self._reconcile_call(["interrupt-dispatch", request_id, "--reason", reason])
        return None if payload.get("status") == "not_found" else payload

    def _reconcile_call(self, args: list[str]) -> dict[str, Any]:
        argv = [str(self.config.python_executable), "-m", "ai_resource_broker.cli",
                "--config", str(self.config.config_path)]
        if self.config.database_path is not None:
            argv += ["--database", str(self.config.database_path)]
        argv += args
        env = dict(os.environ)
        broker_src = str(self.config.broker_repo / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = broker_src + (os.pathsep + existing if existing else "")
        try:
            completed = subprocess.run(
                argv, cwd=str(self.config.broker_repo), env=env, text=True,
                encoding="utf-8", errors="replace", capture_output=True,
                timeout=min(self.config.process_timeout_seconds, 60.0), check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AIBrokerInvocationError(f"broker reconciliation failed: {exc}") from exc
        if completed.returncode not in (0, 1):
            raise AIBrokerInvocationError(
                f"broker reconciliation rejected request (exit {completed.returncode}): {(completed.stderr or completed.stdout).strip()}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AIBrokerInvocationError("broker reconciliation returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AIBrokerInvocationError("broker reconciliation result must be an object")
        return payload

    def _build_argv(self, request: AIRoleRequest) -> list[str]:
        argv = [
            str(self.config.python_executable), "-m", "ai_resource_broker.cli",
            "--config", str(self.config.config_path),
        ]
        if self.config.database_path is not None:
            argv += ["--database", str(self.config.database_path)]
        argv += [
            "dispatch", "--role", request.role, "--quality", request.quality,
            "--independence", request.independence, "--prompt", request.prompt,
            "--request-id", request.request_id,
            "--cwd", str(request.working_directory),
        ]
        if request.timeout_seconds is not None:
            argv += ["--timeout", str(request.timeout_seconds)]
        if self.config.probe_before_dispatch:
            argv.append("--probe")

        previous = request.previous_resource_context
        if previous is not None:
            for flag, value in (
                ("--previous-resource-id", previous.resource_id),
                ("--previous-provider", previous.provider),
                ("--previous-account", previous.account),
                ("--previous-model", previous.model),
            ):
                if value is not None:
                    argv += [flag, value]
        return argv

    @staticmethod
    def _result_from_payload(request: AIRoleRequest, payload: dict[str, Any]) -> AIRoleResult:
        context_payload = payload.get("resource_context")
        context = None
        if context_payload is not None:
            if not isinstance(context_payload, dict):
                raise AIBrokerInvocationError("resource_context must be an object")
            context = ResourceContext(
                context_payload.get("resource_id"), context_payload.get("provider"),
                context_payload.get("account"), context_payload.get("model"),
            )
        if payload.get("request_id") != request.request_id:
            raise AIBrokerInvocationError("broker request_id correlation mismatch")
        try:
            return AIRoleResult(
                request_id=request.request_id,
                role_run_id=request.role_run_id,
                status=payload["status"], output=payload.get("output"),
                error=payload.get("error"), dispatch_id=payload.get("dispatch_id"),
                decision_id=payload.get("decision_id"), execution_id=payload.get("execution_id"),
                session_id=payload.get("session_id"), resource_context=context,
                usage=payload.get("usage"), usage_source=payload.get("usage_source", "unknown"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AIBrokerInvocationError(f"invalid broker result: {exc}") from exc
