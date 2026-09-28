"""Host-local trusted configuration and path containment for execution jobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from dev_orchestrator.storage.json_store import read_json

MAX_FILE_WRITE_BYTES = 8 * 1024 * 1024  # 8 MiB ceiling
DEFAULT_MAX_LINE_BYTES = 4096
DEFAULT_MAX_JOB_BYTES = 2 * 1024 * 1024  # 2MB
DEFAULT_HEAD_LINES = 1000
DEFAULT_TAIL_LINES = 1000
DEFAULT_MAX_JOBS_RETENTION = 100
DEFAULT_MAX_AGE_DAYS = 7

EFFECT_CLASSES = frozenset({"read_only", "idempotent", "effectful", "hardware"})
PARAMETER_TYPES = frozenset({"enum", "integer", "path_within_repo"})


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
    effect_class: str = "read_only"
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobProjectConfig:
    """Project-scoped repository root, file roots and command allowlist."""

    repo_path: Path
    commands: dict[str, JobCommandConfig]
    file_roots: list[str] = field(default_factory=list)
    max_file_write_bytes: int = MAX_FILE_WRITE_BYTES


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
    ssh: Optional[dict[str, Any]] = None
    max_file_write_bytes: int = MAX_FILE_WRITE_BYTES


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

    global_max_write = int(raw.get("max_file_write_bytes", MAX_FILE_WRITE_BYTES))
    global_max_write = min(MAX_FILE_WRITE_BYTES, max(0, global_max_write))

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

            file_roots_raw = pdata.get("file_roots", [])
            file_roots = [str(r) for r in file_roots_raw if isinstance(r, str)] if isinstance(file_roots_raw, list) else []
            proj_max_write = int(pdata.get("max_file_write_bytes", global_max_write))
            proj_max_write = min(global_max_write, max(0, proj_max_write))

            commands: dict[str, JobCommandConfig] = {}
            raw_commands = pdata.get("commands", {})
            if isinstance(raw_commands, dict):
                for cref, cdata in raw_commands.items():
                    if not isinstance(cdata, dict):
                        continue
                    argv = cdata.get("argv")
                    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                        continue
                    eff_cls = str(cdata.get("effect_class", "read_only")).strip()
                    if eff_cls not in EFFECT_CLASSES:
                        eff_cls = "read_only"
                    params_raw = cdata.get("parameters", {})
                    params_dict = dict(params_raw) if isinstance(params_raw, dict) else {}
                    commands[cref] = JobCommandConfig(
                        argv=[str(x) for x in argv],
                        cwd=str(cdata.get("cwd", ".")),
                        duration_class=str(cdata.get("duration_class", "short")),
                        heartbeat_interval_seconds=float(cdata.get("heartbeat_interval_seconds", 5.0)),
                        max_runtime_seconds=float(cdata.get("max_runtime_seconds", 300.0)),
                        effect_class=eff_cls,
                        parameters=params_dict,
                    )

            projects[pid] = JobProjectConfig(
                repo_path=repo_path,
                commands=commands,
                file_roots=file_roots,
                max_file_write_bytes=proj_max_write,
            )

    ssh_raw = raw.get("ssh") or raw.get("transports", {}).get("ssh")
    ssh_dict = dict(ssh_raw) if isinstance(ssh_raw, dict) else None

    return JobsConfig(
        runtime_root=rt_root,
        enabled=True,
        log_caps=log_caps,
        retention=retention,
        projects=projects,
        ssh=ssh_dict,
        max_file_write_bytes=global_max_write,
    )


_SHELL_INJECTION_RE = re.compile(r"[\&\|\;\$\`\<\>\n\r\x00]")


def canonicalize_parameters(
    schema: Mapping[str, Any],
    params: Optional[Mapping[str, Any]],
    *,
    repo_path: Optional[Path | str] = None,
) -> tuple[bool, Optional[str], Optional[dict[str, Any]]]:
    """Validate and canonicalize named parameters strictly against schema.

    Returns (ok, error_message, canonical_params_dict).
    """
    params_dict = dict(params) if params is not None else {}

    if not schema:
        if params_dict:
            return False, f"command ref declares no parameters, but received {sorted(params_dict.keys())}", None
        return True, None, {}

    # Check for unknown parameters
    unknown = sorted(set(params_dict.keys()) - set(schema.keys()))
    if unknown:
        return False, f"unknown parameters: {unknown}", None

    # Check for missing parameters
    missing = sorted(set(schema.keys()) - set(params_dict.keys()))
    if missing:
        return False, f"missing required parameters: {missing}", None

    canonical: dict[str, Any] = {}
    for name in sorted(schema.keys()):
        p_def = schema[name]
        if not isinstance(p_def, dict):
            return False, f"invalid schema definition for parameter {name!r}", None
        p_type = p_def.get("type")
        if p_type not in PARAMETER_TYPES:
            return False, f"unsupported parameter type {p_type!r} for parameter {name!r}", None

        raw_val = params_dict[name]

        if p_type == "enum":
            if not isinstance(raw_val, str):
                return False, f"parameter {name!r} must be a string", None
            if _SHELL_INJECTION_RE.search(raw_val):
                return False, f"parameter {name!r} contains prohibited characters", None
            allowed = p_def.get("allowed_values")
            if not isinstance(allowed, list) or raw_val not in allowed:
                return False, f"parameter {name!r} value {raw_val!r} not in allowed enum values {allowed!r}", None
            canonical[name] = raw_val

        elif p_type == "integer":
            if isinstance(raw_val, bool):
                return False, f"parameter {name!r} must be an integer, got bool", None
            try:
                int_val = int(raw_val)
            except (ValueError, TypeError):
                return False, f"parameter {name!r} must be an integer, got {raw_val!r}", None
            min_val = p_def.get("min")
            if min_val is not None and int_val < int(min_val):
                return False, f"parameter {name!r} value {int_val} below minimum {min_val}", None
            max_val = p_def.get("max")
            if max_val is not None and int_val > int(max_val):
                return False, f"parameter {name!r} value {int_val} exceeds maximum {max_val}", None
            canonical[name] = int_val

        elif p_type == "path_within_repo":
            if not isinstance(raw_val, str) or not raw_val.strip():
                return False, f"parameter {name!r} must be a non-empty relative path string", None
            clean_path = raw_val.strip()
            if _SHELL_INJECTION_RE.search(clean_path):
                return False, f"parameter {name!r} contains prohibited characters", None
            if os.path.isabs(clean_path) or (os.name == "nt" and len(clean_path) > 1 and clean_path[1] == ":"):
                return False, f"parameter {name!r} must be a relative path, got absolute: {clean_path!r}", None
            norm_parts = Path(clean_path).parts
            if ".." in norm_parts:
                return False, f"parameter {name!r} contains path traversal '..'", None
            if repo_path is not None:
                resolved_p = Path(repo_path) / clean_path
                if not is_path_contained(repo_path, resolved_p):
                    return False, f"parameter {name!r} path escapes repository containment", None
            canonical[name] = clean_path.replace("\\", "/")

    return True, None, canonical


def compute_parameters_digest(params: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Compute deterministic SHA-256 digest of canonical validated parameters."""
    if not params:
        return None
    canonical = {k: params[k] for k in sorted(params.keys())}
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_execution_policy_digest(
    repo_path: Path | str,
    cwd: str,
    argv: list[str],
    effect_class: str,
    parameters_schema: Mapping[str, Any],
    runtime_limits: Mapping[str, Any],
    file_roots: list[str],
) -> str:
    """Compute deterministic SHA-256 digest of host-local command execution policy."""
    canonical_roots = sorted(canonical_path(r) for r in file_roots)
    sorted_params = {k: parameters_schema[k] for k in sorted(parameters_schema.keys())}
    payload = {
        "argv": list(argv),
        "cwd": cwd,
        "effect_class": effect_class,
        "file_roots": canonical_roots,
        "parameters": sorted_params,
        "repo_path": canonical_path(repo_path),
        "runtime_limits": {
            "duration_class": str(runtime_limits.get("duration_class", "short")),
            "heartbeat_interval_seconds": float(runtime_limits.get("heartbeat_interval_seconds", 5.0)),
            "max_runtime_seconds": float(runtime_limits.get("max_runtime_seconds", 300.0)),
        },
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_resolution_digest(
    command_ref: str,
    parameters_digest: Optional[str],
    resolved_argv: list[str],
    resolved_cwd: Path | str,
    effect_class: str,
    target_job_id: str,
) -> str:
    """Compute deterministic SHA-256 digest of resolved command execution."""
    payload = {
        "command_ref": command_ref,
        "effect_class": effect_class,
        "parameters_digest": parameters_digest,
        "resolved_argv": list(resolved_argv),
        "resolved_cwd": canonical_path(resolved_cwd),
        "target_job_id": target_job_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolvedExecutionPolicy:
    """Resolved execution policy, parameter substitution and digests strictly from config."""

    project_id: str
    command_ref: str
    effect_class: str
    repo_path: Path
    resolved_cwd: Path
    resolved_argv: list[str]
    parameters: dict[str, Any]
    parameters_digest: Optional[str]
    execution_policy_digest: str
    resolution_digest: str
    file_roots: list[str]
    max_file_write_bytes: int
    command_config: JobCommandConfig
    duration_class: str
    max_runtime_seconds: float
    heartbeat_interval_seconds: float

    @property
    def canonical_parameters(self) -> dict[str, Any]:
        return dict(self.parameters)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "command_ref": self.command_ref,
            "effect_class": self.effect_class,
            "repo_path": self.repo_path,
            "resolved_cwd": self.resolved_cwd,
            "resolved_argv": list(self.resolved_argv),
            "parameters": dict(self.parameters),
            "parameters_digest": self.parameters_digest,
            "execution_policy_digest": self.execution_policy_digest,
            "resolution_digest": self.resolution_digest,
            "file_roots": list(self.file_roots),
            "max_file_write_bytes": self.max_file_write_bytes,
            "command_config": self.command_config,
            "duration_class": self.duration_class,
            "max_runtime_seconds": self.max_runtime_seconds,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
        }


def resolve_execution_policy(
    config: JobsConfig,
    project_id: str,
    command_ref: str,
    *,
    parameters: Optional[Mapping[str, Any]] = None,
    expected_working_directory: Optional[str] = None,
    target_job_id: Optional[str] = None,
) -> tuple[bool, Optional[str], Optional[ResolvedExecutionPolicy]]:
    """Resolve full execution policy, parameter substitution and digests strictly from config.
    
    Returns (ok, failure_kind, resolved_policy).
    """
    if project_id not in config.projects:
        return False, "unknown_project_id", None

    proj_cfg = config.projects[project_id]
    if command_ref not in proj_cfg.commands:
        return False, "unknown_command_ref", None

    cmd_cfg = proj_cfg.commands[command_ref]
    repo_path = proj_cfg.repo_path

    # Check hardware rejection
    if cmd_cfg.effect_class == "hardware":
        return False, "hardware_execution_not_supported_in_p18", None

    # Check cwd containment
    cwd_raw = cmd_cfg.cwd
    if os.path.isabs(cwd_raw) or (os.name == "nt" and len(cwd_raw) > 1 and cwd_raw[1] == ":"):
        return False, "absolute_cwd_override_rejected", None

    candidate_cwd = repo_path / cwd_raw
    if not is_path_contained(repo_path, candidate_cwd):
        return False, "cwd_escapes_repo_containment", None

    resolved_cwd = Path(canonical_path(candidate_cwd))

    # Assert expected_working_directory equality if supplied by wire
    if expected_working_directory is not None:
        expected_canonical = canonical_path(expected_working_directory)
        if canonical_path(resolved_cwd) != expected_canonical:
            return False, "working_directory_assertion_mismatch", None

    # Canonicalize parameters
    ok_p, p_err, val_params = canonicalize_parameters(
        cmd_cfg.parameters, parameters, repo_path=repo_path
    )
    if not ok_p:
        return False, p_err or "parameter_validation_failed", None
    val_params = val_params or {}

    # Substitute parameters in argv: whole element substitution only!
    resolved_argv: list[str] = []
    param_element_re = re.compile(r"^\{([A-Za-z0-9_-]+)\}$")
    for elem in cmd_cfg.argv:
        m = param_element_re.match(elem)
        if m:
            p_name = m.group(1)
            if p_name in val_params:
                resolved_argv.append(str(val_params[p_name]))
            elif p_name == "job_dir":
                # placeholder for supervisor
                resolved_argv.append(elem)
            else:
                return False, f"unresolved_parameter_in_argv: {p_name}", None
        else:
            # Check for partial embedding or concatenation of parameters
            placeholders = re.findall(r"\{([A-Za-z0-9_-]+)\}", elem)
            for ph in placeholders:
                if ph in cmd_cfg.parameters:
                    return False, f"partial_parameter_embedding_rejected: {elem}", None
            resolved_argv.append(elem)

    # File roots
    roots = list(proj_cfg.file_roots)
    if not roots:
        roots = [canonical_path(repo_path)]
    else:
        roots = [canonical_path(r) for r in roots]

    # Compute digests
    p_digest = compute_parameters_digest(val_params)
    runtime_limits = {
        "duration_class": cmd_cfg.duration_class,
        "heartbeat_interval_seconds": cmd_cfg.heartbeat_interval_seconds,
        "max_runtime_seconds": cmd_cfg.max_runtime_seconds,
    }
    ep_digest = compute_execution_policy_digest(
        repo_path=repo_path,
        cwd=cmd_cfg.cwd,
        argv=cmd_cfg.argv,
        effect_class=cmd_cfg.effect_class,
        parameters_schema=cmd_cfg.parameters,
        runtime_limits=runtime_limits,
        file_roots=roots,
    )
    effective_job_id = target_job_id or "job-unresolved"
    res_digest = compute_resolution_digest(
        command_ref=command_ref,
        parameters_digest=p_digest,
        resolved_argv=resolved_argv,
        resolved_cwd=resolved_cwd,
        effect_class=cmd_cfg.effect_class,
        target_job_id=effective_job_id,
    )

    resolved_policy = ResolvedExecutionPolicy(
        project_id=project_id,
        command_ref=command_ref,
        effect_class=cmd_cfg.effect_class,
        repo_path=repo_path,
        resolved_cwd=resolved_cwd,
        resolved_argv=resolved_argv,
        parameters=val_params,
        parameters_digest=p_digest,
        execution_policy_digest=ep_digest,
        resolution_digest=res_digest,
        file_roots=roots,
        max_file_write_bytes=proj_cfg.max_file_write_bytes,
        command_config=cmd_cfg,
        duration_class=cmd_cfg.duration_class,
        max_runtime_seconds=cmd_cfg.max_runtime_seconds,
        heartbeat_interval_seconds=cmd_cfg.heartbeat_interval_seconds,
    )
    return True, None, resolved_policy



def validate_and_resolve_execution(
    config: JobsConfig,
    project_id: str,
    command_ref: str,
    expected_working_directory: Optional[str] = None,
    *,
    parameters: Optional[Mapping[str, Any]] = None,
    target_job_id: Optional[str] = None,
) -> tuple[bool, Optional[str], Optional[Path], Optional[JobCommandConfig]]:
    """Resolve repo_path, cwd and argv strictly from config and enforce path containment.

    Returns (ok, failure_kind, resolved_cwd, command_config).
    """
    ok, failure_kind, resolved_info = resolve_execution_policy(
        config,
        project_id,
        command_ref,
        parameters=parameters,
        expected_working_directory=expected_working_directory,
        target_job_id=target_job_id,
    )
    if not ok or resolved_info is None:
        return False, failure_kind, None, None
    return True, None, resolved_info["resolved_cwd"], resolved_info["command_config"]
