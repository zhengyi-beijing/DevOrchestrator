"""Shared fixed-command OpenSSH channel for remote execution helper communication.

Provides strict host verification, argv generation, structured JSON envelopes,
request correlation, and response limits for AI, Jobs, and Machine transports.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from typing import TYPE_CHECKING, Any, Optional

from dev_orchestrator.ai.execution_transport import ExecutionTransportError
from dev_orchestrator.platform.process import hidden_subprocess_kwargs
from dev_orchestrator.transport.contracts import (
    TransportAmbiguousError,
    TransportUnavailableError,
)

if TYPE_CHECKING:
    from dev_orchestrator.ai.execution_transport import SSHTransportConfig


DEFAULT_MAX_SSH_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB


def host_identities_match(expected: str, actual: str) -> bool:
    """Return True if host identities match exactly or by short/FQDN convention."""
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


def build_ssh_argv(cfg: "SSHTransportConfig") -> list[str]:
    """Construct deterministic OpenSSH argv for invoking fixed remote helper module."""
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


def run_remote_helper_envelope(
    cfg: "SSHTransportConfig",
    request_envelope: dict[str, Any],
    timeout_seconds: float,
    *,
    subprocess_module: Any = None,
    max_response_bytes: int = DEFAULT_MAX_SSH_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Dispatch structured request to remote helper over SSH and parse correlated response.
    
    Raises TransportUnavailableError on pre-dispatch connection failure (exit 255).
    Raises TransportAmbiguousError on timeout during execution.
    Raises ExecutionTransportError on validation/correlation failures.
    """
    subproc = subprocess_module or subprocess
    argv = build_ssh_argv(cfg)
    req_bytes = json.dumps(request_envelope, ensure_ascii=False).encode("utf-8")

    try:
        completed = subproc.run(
            argv,
            input=req_bytes,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TransportAmbiguousError(
            f"SSH transport timed out after {timeout_seconds}s (post-dispatch ambiguous)"
        ) from exc
    except OSError as exc:
        raise TransportUnavailableError(f"SSH invocation failed: {exc}") from exc

    raw_stdout = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout or "").encode("utf-8")
    if len(raw_stdout) > max_response_bytes:
        raise ExecutionTransportError(
            f"SSH response size {len(raw_stdout)} bytes exceeded limit of {max_response_bytes} bytes"
        )

    if completed.returncode != 0:
        stderr_text = (
            completed.stderr.decode("utf-8", errors="replace")
            if isinstance(completed.stderr, bytes)
            else str(completed.stderr or "")
        ).strip()
        stdout_text = (
            raw_stdout.decode("utf-8", errors="replace")
        ).strip()
        detail = stderr_text or stdout_text or f"exit code {completed.returncode}"
        # Exit code 255 in OpenSSH indicates connection failure, host key error, or timeout
        if completed.returncode == 255:
            raise TransportUnavailableError(f"SSH transport failed (exit 255): {detail}")
        raise ExecutionTransportError(f"SSH transport failed (exit {completed.returncode}): {detail}")

    try:
        decoded_stdout = raw_stdout.decode("utf-8")
        resp = json.loads(decoded_stdout)
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

    expected_host = (cfg.expected_host_identity or cfg.peer).strip().lower()
    actual_host = returned_host_id.strip().lower()
    if not host_identities_match(expected_host, actual_host):
        raise ExecutionTransportError(
            f"SSH host identity mismatch: expected {expected_host!r}, received {actual_host!r}"
        )

    return resp
