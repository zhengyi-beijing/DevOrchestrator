"""Process-isolated AIResourceBroker execution port."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .contracts import AIRoleRequest, AIRoleResult, ResourceContext

if TYPE_CHECKING:
    from dev_orchestrator.accounting.events import ExecutionRecorder


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

    def __init__(
        self,
        config: AIBrokerClientConfig,
        *,
        accounting: "ExecutionRecorder | None" = None,
    ) -> None:
        self.config = config
        self.accounting = accounting

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        broker_src = str(self.config.broker_repo / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = broker_src + (os.pathsep + existing if existing else "")
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        env = self._build_env()
        transport_timeout = self.config.process_timeout_seconds
        if request.timeout_seconds is not None:
            transport_timeout = max(transport_timeout, float(request.timeout_seconds) + 60.0)
        try:
            with tempfile.TemporaryDirectory(prefix="devorch-aibroker-") as temp_dir:
                prompt_file = Path(temp_dir) / "prompt.txt"
                prompt_file.write_text(request.prompt, encoding="utf-8")
                argv = self._build_argv(request, prompt_file)
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
        result = self._result_from_payload(request, payload)
        self._record_provider_evidence(request, result)
        return result

    def _record_provider_evidence(self, request: AIRoleRequest, result: AIRoleResult) -> None:
        """Record only fields returned by Broker or carried by the exact request."""
        if self.accounting is None:
            return
        resource = result.resource_context
        previous = request.previous_resource_context
        previous_payload = None
        if previous is not None:
            previous_payload = {
                "resource_id": previous.resource_id,
                "provider": previous.provider,
                "account": previous.account,
                "model": previous.model,
            }
        request_metadata = request.metadata if isinstance(request.metadata, Mapping) else {}
        correlation_group = next(
            (
                str(request_metadata[key])
                for key in ("source_request_id", "control_command_id", "worker_source_request_id")
                if isinstance(request_metadata.get(key), str) and str(request_metadata[key]).strip()
            ),
            request.task_run_id or request.role_run_id or request.request_id,
        )
        if request.stage_run_id == "plan_review":
            accounting_role = "plan_reviewer"
        elif request.role == "worker" and request.stage_run_id == "remediation":
            accounting_role = "remediation_worker"
        else:
            accounting_role = request.role
        self.accounting.record_provider_result(
            request.request_id,
            result.status,
            occurred_at=result.finished_at,
            started_at=result.started_at,
            finished_at=result.finished_at,
            first_output_at=result.first_output_at,
            quota_observation=result.quota_observation,
            rate_limit_observation=result.rate_limit_observation,
            event_id="provider-result:" + request.request_id,
            project_id=request.project_id,
            task_id=request.task_run_id or None,
            role=accounting_role,
            stage_run_id=request.stage_run_id or None,
            role_run_id=request.role_run_id or None,
            dispatch_id=result.dispatch_id,
            decision_id=result.decision_id,
            execution_id=result.execution_id,
            session_id=result.session_id,
            resource_id=resource.resource_id if resource else None,
            provider=resource.provider if resource else None,
            account=resource.account if resource else None,
            model=resource.model if resource else None,
            metadata={
                "correlation_group": correlation_group,
                **({"previous_resource_context": previous_payload} if previous_payload else {}),
            },
        )

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
        env = self._build_env()
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

    def _build_argv(self, request: AIRoleRequest, prompt_file: Path) -> list[str]:
        argv = [
            str(self.config.python_executable), "-m", "ai_resource_broker.cli",
            "--config", str(self.config.config_path),
        ]
        if self.config.database_path is not None:
            argv += ["--database", str(self.config.database_path)]
        argv += [
            "dispatch", "--role", request.role, "--quality", request.quality,
            "--independence", request.independence, "--prompt-file", str(prompt_file),
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
                started_at=payload.get("started_at"), finished_at=payload.get("finished_at"),
                first_output_at=payload.get("first_output_at"),
                quota_observation=payload.get("quota_observation"),
                rate_limit_observation=(
                    payload.get("rate_limit_observation")
                    if payload.get("rate_limit_observation") is not None
                    else payload.get("rate_limit")
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AIBrokerInvocationError(f"invalid broker result: {exc}") from exc
