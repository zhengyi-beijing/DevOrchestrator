"""Host-local trusted configuration and path containment for execution jobs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from dev_orchestrator.storage.json_store import read_json

DEFAULT_MAX_LINE_BYTES = 4096
DEFAULT_MAX_JOB_BYTES = 2 * 1024 * 1024  # 2MB
DEFAULT_HEAD_LINES = 1000
DEFAULT_TAIL_LINES = 1000
DEFAULT_MAX_JOBS_RETENTION = 100
DEFAULT_MAX_AGE_DAYS = 7


def canonical_path(path: Path | str) -> str:
    """Return normalized case and realpath of absolute path."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def is_path_contained(parent: Path | str, candidate: Path | str) -> bool:
    """Return True if candidate is strictly within or equal to parent, resolving symlinks."""
    c_parent = canonical_path(parent)
    c_cand = canonical_path(candidate)
    try:
        return os.path.commonpath([c_parent, c_cand]) == c_parent
    except (ValueError, OSError):
        return False


@dataclass(frozen=True)
class JobCommandConfig:
    """Configuration for a single allowlisted command reference."""

    argv: list[str]
    cwd: str = "."
    duration_class: str = "short"
    heartbeat_interval_seconds: float = 5.0
    max_runtime_seconds: float = 300.0


@dataclass(frozen=True)
class JobProjectConfig:
    """Project-scoped repository root and command allowlist."""

    repo_path: Path
    commands: dict[str, JobCommandConfig]


@dataclass(frozen=True)
class JobsConfig:
    """Host-local trusted jobs configuration."""

    runtime_root: Path
    enabled: bool = True
    log_caps: dict[str, int] = field(default_factory=lambda: {
        "max_line_bytes": DEFAULT_MAX_LINE_BYTES,
        "max_job_bytes": DEFAULT_MAX_JOB_BYTES,
        "head_lines": DEFAULT_HEAD_LINES,
        "tail_lines": DEFAULT_TAIL_LINES,
    })
    retention: dict[str, Any] = field(default_factory=lambda: {
        "max_jobs": DEFAULT_MAX_JOBS_RETENTION,
        "max_age_days": DEFAULT_MAX_AGE_DAYS,
    })
    projects: dict[str, JobProjectConfig] = field(default_factory=dict)


def resolve_local_jobs_config_path(runtime_root: Path | str) -> Path:
    """Local config path is anchored strictly to the daemon's runtime root."""
    return Path(runtime_root) / "execution-jobs.json"


def resolve_remote_jobs_config_path() -> Path:
    """Remote helper resolves strictly in fixed order from host-local sources."""
    env_path = os.environ.get("DEVORCH_JOBS_CONFIG")
    if env_path and env_path.strip():
        return Path(env_path.strip())
    return Path.home() / ".devorch" / "execution-jobs.json"


def load_jobs_config(config_path: Path | str | None) -> JobsConfig | None:
    """Load and validate host-local execution-jobs.json.
    
    Returns None if file does not exist or if enabled is False.
    """
    if config_path is None:
        return None
    path = Path(config_path)
    if not path.is_file():
        return None

    raw = read_json(path, None)
    if not isinstance(raw, dict):
        return None

    enabled = bool(raw.get("enabled", True))
    if not enabled:
        return None

    rt_root_raw = raw.get("runtime_root")
    if not rt_root_raw:
        # Fallback to config file parent directory
        rt_root = path.parent
    else:
        rt_root = Path(rt_root_raw)

    log_caps = {
        "max_line_bytes": int(raw.get("log_caps", {}).get("max_line_bytes", DEFAULT_MAX_LINE_BYTES)),
        "max_job_bytes": int(raw.get("log_caps", {}).get("max_job_bytes", DEFAULT_MAX_JOB_BYTES)),
        "head_lines": int(raw.get("log_caps", {}).get("head_lines", DEFAULT_HEAD_LINES)),
        "tail_lines": int(raw.get("log_caps", {}).get("tail_lines", DEFAULT_TAIL_LINES)),
    }

    retention = {
        "max_jobs": int(raw.get("retention", {}).get("max_jobs", DEFAULT_MAX_JOBS_RETENTION)),
        "max_age_days": int(raw.get("retention", {}).get("max_age_days", DEFAULT_MAX_AGE_DAYS)),
    }

    projects: dict[str, JobProjectConfig] = {}
    raw_projects = raw.get("projects", {})
    if isinstance(raw_projects, dict):
        for pid, pdata in raw_projects.items():
            if not isinstance(pdata, dict):
                continue
            repo_raw = pdata.get("repo_path")
            if not repo_raw:
                continue
            repo_path = Path(repo_raw)

            commands: dict[str, JobCommandConfig] = {}
            raw_commands = pdata.get("commands", {})
            if isinstance(raw_commands, dict):
                for cref, cdata in raw_commands.items():
                    if not isinstance(cdata, dict):
                        continue
                    argv = cdata.get("argv")
                    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                        continue
                    commands[cref] = JobCommandConfig(
                        argv=[str(x) for x in argv],
                        cwd=str(cdata.get("cwd", ".")),
                        duration_class=str(cdata.get("duration_class", "short")),
                        heartbeat_interval_seconds=float(cdata.get("heartbeat_interval_seconds", 5.0)),
                        max_runtime_seconds=float(cdata.get("max_runtime_seconds", 300.0)),
                    )

            projects[pid] = JobProjectConfig(repo_path=repo_path, commands=commands)

    return JobsConfig(
        runtime_root=rt_root,
        enabled=True,
        log_caps=log_caps,
        retention=retention,
        projects=projects,
    )


def validate_and_resolve_execution(
    config: JobsConfig,
    project_id: str,
    command_ref: str,
    expected_working_directory: Optional[str] = None,
) -> tuple[bool, Optional[str], Optional[Path], Optional[JobCommandConfig]]:
    """Resolve repo_path, cwd and argv strictly from config and enforce path containment.
    
    Returns (ok, failure_kind, resolved_cwd, command_config).
    """
    if project_id not in config.projects:
        return False, "unknown_project_id", None, None

    proj_cfg = config.projects[project_id]
    if command_ref not in proj_cfg.commands:
        return False, "unknown_command_ref", None, None

    cmd_cfg = proj_cfg.commands[command_ref]
    repo_path = proj_cfg.repo_path

    # Check cwd is relative (disallow absolute path override)
    cwd_raw = cmd_cfg.cwd
    if os.path.isabs(cwd_raw) or (os.name == "nt" and len(cwd_raw) > 1 and cwd_raw[1] == ":"):
        return False, "absolute_cwd_override_rejected", None, None

    candidate_cwd = repo_path / cwd_raw
    if not is_path_contained(repo_path, candidate_cwd):
        return False, "cwd_escapes_repo_containment", None, None

    resolved_cwd = Path(canonical_path(candidate_cwd))

    # Assert expected_working_directory equality if supplied by wire
    if expected_working_directory is not None:
        expected_canonical = canonical_path(expected_working_directory)
        if canonical_path(resolved_cwd) != expected_canonical:
            return False, "working_directory_assertion_mismatch", None, None

    return True, None, resolved_cwd, cmd_cfg
