"""Machine-local AIBroker execution-port configuration."""
from __future__ import annotations

from pathlib import Path

from dev_orchestrator.storage.json_store import read_json

from .aibroker_subprocess import AIBrokerClientConfig, AIBrokerExecutionPort

AIBROKER_EXECUTION_CONFIG = "aibroker-execution.json"


def load_aibroker_execution_port(runtime_root: Path | str) -> AIBrokerExecutionPort | None:
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
    return AIBrokerExecutionPort(
        AIBrokerClientConfig(
            python_executable=Path(raw["python_executable"]),
            broker_repo=Path(raw["broker_repo"]),
            config_path=Path(raw["config_path"]),
            database_path=Path(database) if database else None,
            process_timeout_seconds=float(timeout),
            probe_before_dispatch=probe,
        )
    )
