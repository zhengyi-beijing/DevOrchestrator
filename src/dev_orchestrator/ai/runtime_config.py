"""Machine-local AIBroker execution-port configuration."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from dev_orchestrator.storage.json_store import read_json

from .aibroker_subprocess import AIBrokerClientConfig, AIBrokerExecutionPort
from .execution_transport import LocalTransport, SSHTransport, SSHTransportConfig

if TYPE_CHECKING:
    from dev_orchestrator.accounting.events import ExecutionRecorder

AIBROKER_EXECUTION_CONFIG = "aibroker-execution.json"


def load_aibroker_execution_port(
    runtime_root: Path | str,
    *,
    accounting: "ExecutionRecorder | None" = None,
) -> AIBrokerExecutionPort | None:
    """Load the optional machine-local subprocess bridge; missing means disabled."""
    runtime = Path(runtime_root)
    raw = read_json(runtime / AIBROKER_EXECUTION_CONFIG, None)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("aibroker-execution.json must be an object")
    required = ("python_executable", "broker_repo", "config_path")
    if any(not isinstance(raw.get(key), str) or not raw[key].strip() for key in required):
        raise ValueError("aibroker execution config requires python_executable, broker_repo, config_path")
    database = raw.get("database_path")
    if database is not None and (not isinstance(database, str) or not database.strip()):
        raise ValueError("database_path must be a nonblank string when present")
    timeout = raw.get("process_timeout_seconds", 600.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("process_timeout_seconds must be positive")
    probe = raw.get("probe_before_dispatch", True)
    if not isinstance(probe, bool):
        raise ValueError("probe_before_dispatch must be boolean")
    service_url = raw.get("service_url")
    service_token = raw.get("service_token")
    if service_url is not None and (not isinstance(service_url, str) or not service_url.strip()):
        raise ValueError("service_url must be a nonblank string when present")
    if service_token is not None and (not isinstance(service_token, str) or not service_token.strip()):
        raise ValueError("service_token must be a nonblank string when present")

    transport = None
    raw_transport = raw.get("transport")
    if raw_transport is not None:
        if isinstance(raw_transport, str):
            raw_transport = {"type": raw_transport}
        if not isinstance(raw_transport, dict):
            raise ValueError("transport must be an object or string when present")
        ttype = raw_transport.get("type", "local")
        if ttype == "local":
            transport = LocalTransport()
        elif ttype == "ssh":
            ssh_cfg_raw = raw_transport.get("ssh") if isinstance(raw_transport.get("ssh"), dict) else raw_transport
            peer = ssh_cfg_raw.get("peer") or raw.get("ssh_peer")
            if not isinstance(peer, str) or not peer.strip():
                raise ValueError("ssh transport requires nonblank 'peer'")
            user = ssh_cfg_raw.get("user") or raw.get("ssh_user")
            try:
                port = int(ssh_cfg_raw.get("port") or raw.get("ssh_port") or 22)
            except (TypeError, ValueError):
                raise ValueError("ssh port must be an integer")
            id_file_raw = ssh_cfg_raw.get("identity_file") or raw.get("ssh_identity_file")
            id_file = Path(id_file_raw) if id_file_raw else None
            known_hosts_raw = ssh_cfg_raw.get("known_hosts_file") or raw.get("ssh_known_hosts_file")
            known_hosts = Path(known_hosts_raw) if known_hosts_raw else None
            strict_host = str(ssh_cfg_raw.get("strict_host_key_checking") or raw.get("ssh_strict_host_key_checking") or "yes")
            remote_py = str(ssh_cfg_raw.get("remote_python") or raw.get("ssh_remote_python") or "python3")
            path_mapping = ssh_cfg_raw.get("path_mapping") or raw.get("path_mapping") or {}
            if not isinstance(path_mapping, dict):
                raise ValueError("path_mapping must be an object")
            expected_host_raw = ssh_cfg_raw.get("expected_host_identity") or raw.get("ssh_expected_host_identity")
            expected_host = expected_host_raw.strip() if isinstance(expected_host_raw, str) and expected_host_raw.strip() else None
            ssh_cfg = SSHTransportConfig(
                peer=peer.strip(),
                user=user.strip() if isinstance(user, str) and user.strip() else None,
                port=port,
                identity_file=id_file,
                known_hosts_file=known_hosts,
                strict_host_key_checking=strict_host,
                remote_python=remote_py,
                path_mapping=path_mapping,
                expected_host_identity=expected_host,
            )
            transport = SSHTransport(ssh_cfg)
        else:
            raise ValueError(f"unsupported transport type: {ttype!r}")

    return AIBrokerExecutionPort(
        AIBrokerClientConfig(
            python_executable=Path(raw["python_executable"]),
            broker_repo=Path(raw["broker_repo"]),
            config_path=Path(raw["config_path"]),
            database_path=Path(database) if database else None,
            process_timeout_seconds=float(timeout),
            probe_before_dispatch=probe,
            service_url=service_url,
            service_token=service_token,
        ),
        transport=transport,
        accounting=accounting,
    )
