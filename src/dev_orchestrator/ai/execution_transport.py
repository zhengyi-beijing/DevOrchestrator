"""ExecutionTransport protocol and implementations (LocalTransport, SSHTransport).

Separates execution transport beneath AIExecutionPort/AIBrokerExecutionPort.
Supports Local execution (default subprocess/service) and SSH over Tailscale
with noninteractive strict host verification, structured stdin/stdout, fixed argv,
and closed helper operations.
Prohibits automatic RDC fallback and duplicate dispatch on ambiguous transport failure.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Protocol, runtime_checkable

from dev_orchestrator.storage.json_store import utc_now_iso


if TYPE_CHECKING:
    from .contracts import AIRoleRequest
    from .aibroker_subprocess import AIBrokerClientConfig


class ExecutionTransportError(RuntimeError):
    """Transport-level execution failure."""


@dataclass(frozen=True)
class ExecutionTransportResult:
    """Correlated evidence returned by an ExecutionTransport dispatch."""
    payload: dict[str, Any]
    transport_name: str  # "local" or "ssh"
    host_identity: str
    started_at: str | None = None
    finished_at: str | None = None
    status: str = "available"  # "available", "unavailable", "unknown"
    error: str | None = None
    raw_evidence: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ExecutionTransport(Protocol):
    """Protocol for AI execution transport."""

    def dispatch(
        self,
        request: "AIRoleRequest",
        config: "AIBrokerClientConfig",
        env: dict[str, str],
        timeout_seconds: float,
        *,
        service_caller: Any = None,
    ) -> ExecutionTransportResult:
        """Dispatch exactly one role request through the transport."""
        ...

    def status(
        self,
        request_id: str,
        config: "AIBrokerClientConfig",
        env: dict[str, str],
        *,
        service_caller: Any = None,
    ) -> dict[str, Any] | None:
        """Poll status for one request ID."""
        ...

    def interrupt(
        self,
        request_id: str,
        reason: str,
        config: "AIBrokerClientConfig",
        env: dict[str, str],
        *,
        service_caller: Any = None,
    ) -> dict[str, Any] | None:
        """Issue managed interrupt for one request ID."""
        ...


class LocalTransport:
    """Default local subprocess/service execution transport."""

    def __init__(self, *, subprocess_module: Any = None) -> None:
        self._subprocess = subprocess_module or subprocess

    def dispatch(
        self,
        request: "AIRoleRequest",
        config: "AIBrokerClientConfig",
        env: dict[str, str],
        timeout_seconds: float,
        *,
        service_caller: Any = None,
    ) -> ExecutionTransportResult:
        started_at = utc_now_iso()

        if config.service_url and service_caller is not None:
            # Delegate to service caller
            payload = self._request_payload(request, config)
            result = service_caller("/api/dispatch", payload)
            if not isinstance(result, dict):
                raise ExecutionTransportError("broker service result must be an object")
            return ExecutionTransportResult(
                payload=result,
                transport_name="local",
                host_identity="localhost",
                started_at=started_at,
                finished_at=utc_now_iso(),
                status="available",
            )

        # Subprocess invocation
        try:
            with tempfile.TemporaryDirectory(prefix="devorch-aibroker-") as temp_dir:
                prompt_file = Path(temp_dir) / "prompt.txt"
                prompt_file.write_text(request.prompt, encoding="utf-8")
                argv = self._build_argv(request, prompt_file, config)
                completed = self._subprocess.run(
                    argv,
                    cwd=str(config.broker_repo),
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=timeout_seconds,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionTransportError(f"local broker invocation failed: {exc}") from exc


        if completed.returncode != 0 and not (completed.returncode == 1 and completed.stdout.strip()):
            detail = (completed.stderr or completed.stdout).strip()
            raise ExecutionTransportError(
                f"broker rejected request (exit {completed.returncode}): {detail}"
            )

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionTransportError("broker returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise ExecutionTransportError("broker result must be a JSON object")

        return ExecutionTransportResult(
            payload=payload,
            transport_name="local",
            host_identity="localhost",
            started_at=started_at,
            finished_at=utc_now_iso(),
            status="available",
            raw_evidence={"exit_code": completed.returncode},
        )

    def status(
        self,
        request_id: str,
        config: "AIBrokerClientConfig",
        env: dict[str, str],
        *,
        service_caller: Any = None,
    ) -> dict[str, Any] | None:
        if config.service_url and service_caller is not None:
            import urllib.parse
            payload = service_caller("/api/dispatches/" + urllib.parse.quote(str(request_id), safe=""), None)
            return None if payload.get("status") == "not_found" else payload

        payload = self._reconcile_call(["dispatch-status", str(request_id)], config, env)
        return None if payload.get("status") == "not_found" else payload

    def interrupt(
        self,
        request_id: str,
        reason: str,
        config: "AIBrokerClientConfig",
        env: dict[str, str],
        *,
        service_caller: Any = None,
    ) -> dict[str, Any] | None:
        if config.service_url and service_caller is not None:
            import urllib.parse
            payload = service_caller(
                "/api/dispatches/" + urllib.parse.quote(str(request_id), safe="") + "/interrupt",
                {"reason": reason},
            )
            return None if payload.get("status") == "not_found" else payload

        payload = self._reconcile_call(["interrupt-dispatch", str(request_id), "--reason", reason], config, env)
        return None if payload.get("status") == "not_found" else payload

    def _reconcile_call(
        self,
        args: list[str],
        config: "AIBrokerClientConfig",
        env: dict[str, str],
    ) -> dict[str, Any]:
        argv = [
            str(config.python_executable),
            "-m",
            "ai_resource_broker.cli",
            "--config",
            str(config.config_path),
        ]
        if config.database_path is not None:
            argv += ["--database", str(config.database_path)]
        argv += args
        try:
            completed = self._subprocess.run(
                argv,
                cwd=str(config.broker_repo),
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=min(config.process_timeout_seconds, 60.0),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionTransportError(f"broker reconciliation failed: {exc}") from exc

        if completed.returncode not in (0, 1):
            detail = (completed.stderr or completed.stdout).strip()
            raise ExecutionTransportError(
                f"broker reconciliation rejected request (exit {completed.returncode}): {detail}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionTransportError("broker reconciliation returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ExecutionTransportError("broker reconciliation result must be an object")
        return payload

    @staticmethod
    def _build_argv(request: "AIRoleRequest", prompt_file: Path, config: "AIBrokerClientConfig") -> list[str]:
        argv = [
            str(config.python_executable), "-m", "ai_resource_broker.cli",
            "--config", str(config.config_path),
        ]
        if config.database_path is not None:
            argv += ["--database", str(config.database_path)]
        argv += [
            "dispatch", "--role", request.role, "--quality", request.quality,
            "--independence", request.independence, "--prompt-file", str(prompt_file),
            "--request-id", request.request_id,
            "--cwd", str(request.working_directory),
        ]
        if request.timeout_seconds is not None:
            argv += ["--timeout", str(request.timeout_seconds)]
        if config.probe_before_dispatch:
            argv.append("--probe")
        for resource_id in request.excluded_resource_ids:
            argv += ["--excluded-resource-id", resource_id]

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
    def _request_payload(request: "AIRoleRequest", config: "AIBrokerClientConfig") -> dict[str, Any]:
        previous = request.previous_resource_context
        return {
            "project_id": request.project_id,
            "role": request.role,
            "prompt": request.prompt,
            "request_id": request.request_id,
            "quality": request.quality,
            "independence": request.independence,
            "excluded_resource_ids": list(request.excluded_resource_ids),
            "working_directory": str(request.working_directory),
            "timeout_seconds": request.timeout_seconds,
            "task_id": request.task_run_id,
            "managed_worktree": bool(request.metadata.get("managed_worktree", False)),
            "probe": config.probe_before_dispatch,
            **({"previous_resource_context": {
                "resource_id": previous.resource_id,
                "provider": previous.provider,
                "account": previous.account,
                "model": previous.model,
            }} if previous else {}),
        }



@dataclass(frozen=True)
class SSHTransportConfig:
    """Validated machine-local configuration for remote SSH execution."""
    peer: str
    user: str | None = None
    port: int = 22
    identity_file: Path | None = None
    known_hosts_file: Path | None = None
    strict_host_key_checking: str = "yes"
    remote_python: str = "python3"
    remote_helper_module: str = "dev_orchestrator.ai.remote_helper"
    path_mapping: Mapping[str, str] = field(default_factory=dict)
    ssh_executable: str = "ssh"
    connect_timeout_seconds: float = 30.0
    expected_host_identity: str | None = None


class SSHTransport:
    """Remote execution transport over OpenSSH and Tailscale peer.

    Communicates with a fixed remote DevOrchestrator helper over structured stdin/stdout.
    Uses noninteractive authentication, strict host verification, path mapping, and
    result-correlation checks. Prohibits arbitrary shell fragments or environment injection.
    """

    def __init__(self, ssh_config: SSHTransportConfig, *, subprocess_module: Any = None) -> None:
        self.ssh_config = ssh_config
        self._subprocess = subprocess_module or subprocess

    def map_path(self, local_path: str | Path) -> str:
        """Translate a local filesystem path to the corresponding remote path."""
        p_str = str(local_path)
        # Sort mappings by key length descending for most specific prefix match
        sorted_mappings = sorted(
            self.ssh_config.path_mapping.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for local_prefix, remote_prefix in sorted_mappings:
            abs_local = os.path.abspath(local_prefix)
            abs_target = os.path.abspath(p_str)
            norm_local = os.path.normcase(abs_local)
            norm_target = os.path.normcase(abs_target)
            if norm_target == norm_local:
                return remote_prefix
            if norm_target.startswith(norm_local + os.sep):
                relative = os.path.relpath(abs_target, abs_local)
                if relative.startswith(".."):
                    continue
                # Join with POSIX forward slashes for remote
                return remote_prefix.rstrip("/") + "/" + relative.replace("\\", "/")
        return p_str

    @staticmethod
    def _host_identities_match(expected: str, actual: str) -> bool:
        if expected == actual:
            return True
        # If both contain dots (e.g. both are FQDNs or IP addresses), they must match exactly
        if "." in expected and "." in actual:
            return False
        # If one is an unqualified short name (no dots), it can match the first DNS label of a FQDN
        # provided neither is an IP address
        try:
            ipaddress.ip_address(expected)
            return False
        except ValueError:
            pass
        try:
            ipaddress.ip_address(actual)
            return False
        except ValueError:
            pass

        if "." not in expected and actual.split(".")[0] == expected:
            return True
        if "." not in actual and expected.split(".")[0] == actual:
            return True
        return False

    def _build_ssh_argv(self) -> list[str]:
        cfg = self.ssh_config
        argv = [
            cfg.ssh_executable,
            "-o", "BatchMode=yes",
            "-o", f"StrictHostKeyChecking={cfg.strict_host_key_checking}",
            "-o", f"ConnectTimeout={int(cfg.connect_timeout_seconds)}",
        ]
        if cfg.port != 22:
            argv.extend(["-p", str(cfg.port)])
        if cfg.identity_file is not None:
            argv.extend(["-i", str(cfg.identity_file)])
        if cfg.known_hosts_file is not None:
            argv.extend(["-o", f"UserKnownHostsFile={cfg.known_hosts_file}"])

        target = f"{cfg.user}@{cfg.peer}" if cfg.user else cfg.peer
        argv.append(target)
        argv.extend([cfg.remote_python, "-m", cfg.remote_helper_module])
        return argv

    def _run_remote_helper(
        self,
        request_envelope: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        argv = self._build_ssh_argv()
        req_bytes = json.dumps(request_envelope, ensure_ascii=False).encode("utf-8")

        try:
            completed = self._subprocess.run(
                argv,
                input=req_bytes,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutionTransportError(f"SSH transport timed out after {timeout_seconds}s") from exc
        except OSError as exc:
            raise ExecutionTransportError(f"SSH invocation failed: {exc}") from exc

        if completed.returncode != 0:
            stderr_text = (completed.stderr.decode("utf-8", errors="replace") if isinstance(completed.stderr, bytes) else str(completed.stderr or "")).strip()
            stdout_text = (completed.stdout.decode("utf-8", errors="replace") if isinstance(completed.stdout, bytes) else str(completed.stdout or "")).strip()
            detail = stderr_text or stdout_text or f"exit code {completed.returncode}"
            raise ExecutionTransportError(f"SSH transport failed (exit {completed.returncode}): {detail}")

        try:
            raw_stdout = completed.stdout.decode("utf-8") if isinstance(completed.stdout, bytes) else str(completed.stdout)
            resp = json.loads(raw_stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExecutionTransportError("SSH remote helper returned invalid JSON response") from exc

        if not isinstance(resp, dict):
            raise ExecutionTransportError("SSH remote helper response must be a JSON object")

        # Result correlation check
        sent_req_id = request_envelope.get("request_id")
        returned_req_id = resp.get("request_id")
        if sent_req_id != returned_req_id:
            raise ExecutionTransportError(
                f"SSH result correlation mismatch: sent {sent_req_id!r}, received {returned_req_id!r}"
            )

        # Host identity validation
        returned_host_id = resp.get("host_identity")
        if not returned_host_id or not isinstance(returned_host_id, str):
            raise ExecutionTransportError("SSH remote helper response missing valid host_identity")

        expected_host = (self.ssh_config.expected_host_identity or self.ssh_config.peer).strip().lower()
        actual_host = returned_host_id.strip().lower()
        if not self._host_identities_match(expected_host, actual_host):
            raise ExecutionTransportError(
                f"SSH host identity mismatch: expected {expected_host!r}, received {actual_host!r}"
            )

        if resp.get("status") != "success":
            err_msg = str(resp.get("error") or "remote helper execution failed")
            raise ExecutionTransportError(f"SSH remote helper reported error: {err_msg}")

        payload = resp.get("payload")
        if not isinstance(payload, dict):
            raise ExecutionTransportError("SSH remote helper payload must be a JSON object")

        return resp

    def dispatch(
        self,
        request: "AIRoleRequest",
        config: "AIBrokerClientConfig",
        env: dict[str, str],
        timeout_seconds: float,
        *,
        service_caller: Any = None,
    ) -> ExecutionTransportResult:
        started_at = utc_now_iso()

        mapped_cwd = self.map_path(request.working_directory)
        mapped_broker_repo = self.map_path(config.broker_repo)
        mapped_config_path = self.map_path(config.config_path)
        mapped_database_path = self.map_path(config.database_path) if config.database_path else None

        role_payload = LocalTransport._request_payload(request, config)
        role_payload["working_directory"] = mapped_cwd
        role_payload["stage_run_id"] = request.stage_run_id
        role_payload["task_run_id"] = request.task_run_id
        role_payload["role_run_id"] = request.role_run_id
        role_payload["attempt_number"] = getattr(request, "attempt_number", 1)

        envelope = {
            "operation": "dispatch",
            "request_id": request.request_id,
            "request": role_payload,
            "broker_repo": mapped_broker_repo,
            "config_path": mapped_config_path,
            "database_path": mapped_database_path,
            "service_url": config.service_url,
            "service_token": config.service_token,
            "probe": config.probe_before_dispatch,
        }

        transport_timeout = timeout_seconds + self.ssh_config.connect_timeout_seconds
        resp = self._run_remote_helper(envelope, transport_timeout)

        host_id = str(resp.get("host_identity") or self.ssh_config.peer)
        payload = resp["payload"]

        return ExecutionTransportResult(
            payload=payload,
            transport_name="ssh",
            host_identity=host_id,
            started_at=started_at,
            finished_at=utc_now_iso(),
            status="available",
            raw_evidence={"peer": self.ssh_config.peer},
        )

    def status(
        self,
        request_id: str,
        config: "AIBrokerClientConfig",
        env: dict[str, str],
        *,
        service_caller: Any = None,
    ) -> dict[str, Any] | None:
        envelope = {
            "operation": "status",
            "request_id": request_id,
            "broker_repo": self.map_path(config.broker_repo),
            "config_path": self.map_path(config.config_path),
            "database_path": self.map_path(config.database_path) if config.database_path else None,
            "service_url": config.service_url,
            "service_token": config.service_token,
        }
        resp = self._run_remote_helper(envelope, 60.0)
        payload = resp["payload"]
        return None if payload.get("status") == "not_found" else payload

    def interrupt(
        self,
        request_id: str,
        reason: str,
        config: "AIBrokerClientConfig",
        env: dict[str, str],
        *,
        service_caller: Any = None,
    ) -> dict[str, Any] | None:
        envelope = {
            "operation": "interrupt",
            "request_id": request_id,
            "reason": reason,
            "broker_repo": self.map_path(config.broker_repo),
            "config_path": self.map_path(config.config_path),
            "database_path": self.map_path(config.database_path) if config.database_path else None,
            "service_url": config.service_url,
            "service_token": config.service_token,
        }
        resp = self._run_remote_helper(envelope, 60.0)
        payload = resp["payload"]
        return None if payload.get("status") == "not_found" else payload
