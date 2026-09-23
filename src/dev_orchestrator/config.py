"""Configuration loading, canonical normalization, and default path resolution.

``config/projects.json`` remains the public configuration source; entries are
normalized onto the canonical ``project_id`` / ``repo_path`` vocabulary while
the legacy ``id`` / ``root`` keys stay accepted and are mirrored onto the
returned copy, so consumers can read either vocabulary and the monitor never
rewrites the configuration file.

Canonical project fields (design: ``docs/MULTIPROJECT_WEBSOL_CORE_DESIGN.md``):

    project_id, repo_path, conversation_binding, worker_runtime,
    adapter (default ``agent_files``), eta

Rules enforced here:

- ``project_id``/``repo_path`` normalize from ``id``/``root`` when present.
- Relative ``repo_path`` values resolve from the configuration file directory,
  not the DevOrchestrator process working directory.
- Every project requires a non-blank canonical ``repo_path`` (legacy ``root``
  is accepted); missing/blank paths are rejected with explicit repo-path
  evidence.
- Duplicate canonical project ids are rejected explicitly.
- Among orchestration-ready projects, ``(transport, adapter, binding_id)``
  conversation bindings must be unique; a duplicate is a configuration error
  because it could cross-route two project workflows into one conversation.
- ``adapter`` defaults to ``agent_files`` when omitted.
- ``orchestration_ready`` is true when either a structurally valid browser
  ``conversation_binding`` exists or a direct ``ai_roles.reviewer`` or
  ``ai_roles.planner`` is explicitly enabled.
  Projects with neither route remain monitorable but not orchestration-ready.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.adapters.base import DEFAULT_ADAPTER_ID

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "projects.json"
DEFAULT_RUNTIME_ROOT = REPO_ROOT / "runtime"
DEFAULT_WEB_ROOT = REPO_ROOT / "web"

_BINDING_KEYS = ("transport", "adapter", "binding_id")


def resolve_config_path(value: Optional[str]) -> Path:
    return Path(value).expanduser() if value else DEFAULT_CONFIG_PATH


def resolve_runtime_root(value: Optional[str]) -> Path:
    return Path(value).expanduser() if value else DEFAULT_RUNTIME_ROOT


def resolve_web_root(value: Optional[str]) -> Path:
    return Path(value).expanduser() if value else DEFAULT_WEB_ROOT


def _direct_reviewer_ready(project: dict[str, Any]) -> bool:
    execution = project.get("execution")
    if not isinstance(execution, dict) or execution.get("engine") != "aibroker":
        return False
    roles = project.get("ai_roles")
    if not isinstance(roles, dict):
        return False
    reviewer = roles.get("reviewer")
    return isinstance(reviewer, dict) and reviewer.get("enabled") is True


def _direct_planner_ready(project: dict[str, Any]) -> bool:
    execution = project.get("execution")
    if not isinstance(execution, dict) or execution.get("engine") != "aibroker":
        return False
    roles = project.get("ai_roles")
    if not isinstance(roles, dict):
        return False
    planner = roles.get("planner")
    return isinstance(planner, dict) and planner.get("enabled") is True


def _binding_ready(binding: Any) -> bool:
    """Whether ``conversation_binding`` is structurally orchestration-ready.

    The binding is routing metadata only (never credentials). Every field of
    ``{ transport, adapter, binding_id }`` must be a non-empty string or the
    project is treated as monitor-only / not orchestration-ready.
    """
    if not isinstance(binding, dict):
        return False
    for key in _BINDING_KEYS:
        value = binding.get(key)
        if not isinstance(value, str) or not value.strip():
            return False
    return True


_ALLOWED_CONTEXT_KEYS = frozenset({
    "enabled",
    "document_path",
    "supplement_path",
    "require_valid",
    "max_chars",
    "inject_roles",
})
_ALLOWED_INJECT_ROLES = frozenset({"planner", "worker", "reviewer"})


def _normalize_project_context(raw: Any, index: int, config_path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(
            "config {0} project #{1} project_context must be an object".format(config_path, index)
        )
    unknown = set(raw.keys()) - _ALLOWED_CONTEXT_KEYS
    if unknown:
        raise ValueError(
            "config {0} project #{1} project_context has unknown keys: {2}".format(
                config_path, index, sorted(unknown)
            )
        )
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            "config {0} project #{1} project_context.enabled must be a boolean".format(
                config_path, index
            )
        )
    doc_path = raw.get("document_path", "agent/project-context.json")
    if not isinstance(doc_path, str) or not doc_path.strip():
        raise ValueError(
            "config {0} project #{1} project_context.document_path must be a non-blank string".format(
                config_path, index
            )
        )
    doc_path = doc_path.strip()
    p_doc = Path(doc_path)
    if p_doc.is_absolute() or p_doc.drive:
        raise ValueError(
            "config {0} project #{1} project_context.document_path must be a repo-relative path".format(
                config_path, index
            )
        )

    supp_path = raw.get("supplement_path")
    if supp_path is not None:
        if not isinstance(supp_path, str) or not supp_path.strip():
            raise ValueError(
                "config {0} project #{1} project_context.supplement_path must be a non-blank string".format(
                    config_path, index
                )
            )
        supp_path = supp_path.strip()
        p_supp = Path(supp_path)
        if p_supp.is_absolute() or p_supp.drive:
            raise ValueError(
                "config {0} project #{1} project_context.supplement_path must be a repo-relative path".format(
                    config_path, index
                )
            )

    require_valid = raw.get("require_valid", True)
    if not isinstance(require_valid, bool):
        raise ValueError(
            "config {0} project #{1} project_context.require_valid must be a boolean".format(
                config_path, index
            )
        )

    max_chars = raw.get("max_chars", 6000)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise ValueError(
            "config {0} project #{1} project_context.max_chars must be an integer".format(
                config_path, index
            )
        )
    if max_chars < 500 or max_chars > 20000:
        raise ValueError(
            "config {0} project #{1} project_context.max_chars must be between 500 and 20000".format(
                config_path, index
            )
        )

    roles = raw.get("inject_roles")
    if roles is None:
        normalized_roles = ["planner", "worker", "reviewer"]
    else:
        if not isinstance(roles, list) or not roles:
            raise ValueError(
                "config {0} project #{1} project_context.inject_roles must be a non-empty list".format(
                    config_path, index
                )
            )
        for r in roles:
            if not isinstance(r, str) or r not in _ALLOWED_INJECT_ROLES:
                raise ValueError(
                    "config {0} project #{1} project_context.inject_roles contains invalid role: {2!r}".format(
                        config_path, index, r
                    )
                )
        if len(set(roles)) != len(roles):
            raise ValueError(
                "config {0} project #{1} project_context.inject_roles contains duplicate roles".format(
                    config_path, index
                )
            )
        normalized_roles = list(roles)

    return {
        "enabled": enabled,
        "document_path": doc_path,
        "supplement_path": supp_path,
        "require_valid": require_valid,
        "max_chars": max_chars,
        "inject_roles": normalized_roles,
    }


_ALLOWED_WATCHDOG_KEYS = frozenset({
    "enabled",
    "no_progress_threshold_minutes",
    "lifecycle_overrides",
    "cooldown_minutes",
    "max_attempts_per_run",
    "diagnostic_timeout_seconds",
    "auto_recovery",
})

_ALLOWED_WATCHDOG_LIFECYCLES = frozenset({
    "PLANNING",
    "REVIEWING_PLAN",
    "REMEDIATING_PLAN",
    "APPLYING_PLAN",
    "EXECUTING",
    "REVIEWING",
    "REVIEW_FAILED",
    "PLAN_FAILED",
    "REMEDIATING",
})


def _normalize_project_watchdog(raw: Any, index: int, config_path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(
            "config {0} project #{1} watchdog must be an object".format(config_path, index)
        )
    unknown = set(raw.keys()) - _ALLOWED_WATCHDOG_KEYS
    if unknown:
        raise ValueError(
            "config {0} project #{1} watchdog has unknown keys: {2}".format(
                config_path, index, sorted(unknown)
            )
        )

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(
            "config {0} project #{1} watchdog.enabled must be a boolean".format(
                config_path, index
            )
        )

    threshold = raw.get("no_progress_threshold_minutes", 15)
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        raise ValueError(
            "config {0} project #{1} watchdog.no_progress_threshold_minutes must be an integer".format(
                config_path, index
            )
        )
    if threshold < 1 or threshold > 1440:
        raise ValueError(
            "config {0} project #{1} watchdog.no_progress_threshold_minutes must be between 1 and 1440".format(
                config_path, index
            )
        )

    cooldown = raw.get("cooldown_minutes", 30)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int):
        raise ValueError(
            "config {0} project #{1} watchdog.cooldown_minutes must be an integer".format(
                config_path, index
            )
        )
    if cooldown < 1 or cooldown > 1440:
        raise ValueError(
            "config {0} project #{1} watchdog.cooldown_minutes must be between 1 and 1440".format(
                config_path, index
            )
        )

    max_attempts = raw.get("max_attempts_per_run", 3)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError(
            "config {0} project #{1} watchdog.max_attempts_per_run must be an integer".format(
                config_path, index
            )
        )
    if max_attempts < 1 or max_attempts > 10:
        raise ValueError(
            "config {0} project #{1} watchdog.max_attempts_per_run must be between 1 and 10".format(
                config_path, index
            )
        )

    timeout = raw.get("diagnostic_timeout_seconds", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValueError(
            "config {0} project #{1} watchdog.diagnostic_timeout_seconds must be an integer".format(
                config_path, index
            )
        )
    if timeout < 10 or timeout > 600:
        raise ValueError(
            "config {0} project #{1} watchdog.diagnostic_timeout_seconds must be between 10 and 600".format(
                config_path, index
            )
        )

    auto_recovery = raw.get("auto_recovery", False)
    if not isinstance(auto_recovery, bool):
        raise ValueError(
            "config {0} project #{1} watchdog.auto_recovery must be a boolean".format(
                config_path, index
            )
        )

    raw_overrides = raw.get("lifecycle_overrides", {})
    if not isinstance(raw_overrides, dict):
        raise ValueError(
            "config {0} project #{1} watchdog.lifecycle_overrides must be an object".format(
                config_path, index
            )
        )
    normalized_overrides: dict[str, int] = {}
    for raw_k, val in raw_overrides.items():
        if not isinstance(raw_k, str) or not raw_k.strip():
            raise ValueError(
                "config {0} project #{1} watchdog.lifecycle_overrides has invalid state key: {2!r}".format(
                    config_path, index, raw_k
                )
            )
        norm_k = raw_k.strip().upper()
        if norm_k in normalized_overrides:
            raise ValueError(
                "config {0} project #{1} watchdog.lifecycle_overrides has duplicate normalized key {2!r}".format(
                    config_path, index, norm_k
                )
            )
        if norm_k not in _ALLOWED_WATCHDOG_LIFECYCLES:
            raise ValueError(
                "config {0} project #{1} watchdog.lifecycle_overrides key {2!r} is not an allowed lifecycle state {3}".format(
                    config_path, index, raw_k, sorted(_ALLOWED_WATCHDOG_LIFECYCLES)
                )
            )
        if isinstance(val, bool) or not isinstance(val, int):
            raise ValueError(
                "config {0} project #{1} watchdog.lifecycle_overrides[{2!r}] must be an integer".format(
                    config_path, index, raw_k
                )
            )
        if val < 1 or val > 1440:
            raise ValueError(
                "config {0} project #{1} watchdog.lifecycle_overrides[{2!r}] must be between 1 and 1440".format(
                    config_path, index, raw_k
                )
            )
        normalized_overrides[norm_k] = val

    return {
        "enabled": enabled,
        "no_progress_threshold_minutes": threshold,
        "lifecycle_overrides": normalized_overrides,
        "cooldown_minutes": cooldown,
        "max_attempts_per_run": max_attempts,
        "diagnostic_timeout_seconds": timeout,
        "auto_recovery": auto_recovery,
    }


_ALLOWED_SELF_HEALING_KEYS = frozenset({
    "enabled",
    "max_recovery_actions",
    "max_identical_failures",
    "max_launch_minutes",
    "backoff_seconds",
})


def _normalize_project_self_healing(raw: Any, index: int, config_path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(
            "config {0} project #{1} self_healing must be an object".format(config_path, index)
        )
    unknown = set(raw.keys()) - _ALLOWED_SELF_HEALING_KEYS
    if unknown:
        raise ValueError(
            "config {0} project #{1} self_healing has unknown keys: {2}".format(
                config_path, index, sorted(unknown)
            )
        )

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(
            "config {0} project #{1} self_healing.enabled must be a boolean".format(
                config_path, index
            )
        )

    max_actions = raw.get("max_recovery_actions", 20)
    if isinstance(max_actions, bool) or not isinstance(max_actions, int):
        raise ValueError(
            "config {0} project #{1} self_healing.max_recovery_actions must be an integer".format(
                config_path, index
            )
        )
    if max_actions < 1 or max_actions > 100:
        raise ValueError(
            "config {0} project #{1} self_healing.max_recovery_actions must be between 1 and 100".format(
                config_path, index
            )
        )

    max_failures = raw.get("max_identical_failures", 3)
    if isinstance(max_failures, bool) or not isinstance(max_failures, int):
        raise ValueError(
            "config {0} project #{1} self_healing.max_identical_failures must be an integer".format(
                config_path, index
            )
        )
    if max_failures < 1 or max_failures > 20:
        raise ValueError(
            "config {0} project #{1} self_healing.max_identical_failures must be between 1 and 20".format(
                config_path, index
            )
        )

    max_minutes = raw.get("max_launch_minutes", 30)
    if isinstance(max_minutes, bool) or not isinstance(max_minutes, int):
        raise ValueError(
            "config {0} project #{1} self_healing.max_launch_minutes must be an integer".format(
                config_path, index
            )
        )
    if max_minutes < 1 or max_minutes > 1440:
        raise ValueError(
            "config {0} project #{1} self_healing.max_launch_minutes must be between 1 and 1440".format(
                config_path, index
            )
        )

    backoff = raw.get("backoff_seconds", 5)
    if isinstance(backoff, bool) or not isinstance(backoff, int):
        raise ValueError(
            "config {0} project #{1} self_healing.backoff_seconds must be an integer".format(
                config_path, index
            )
        )
    if backoff < 1 or backoff > 300:
        raise ValueError(
            "config {0} project #{1} self_healing.backoff_seconds must be between 1 and 300".format(
                config_path, index
            )
        )

    return {
        "enabled": enabled,
        "max_recovery_actions": max_actions,
        "max_identical_failures": max_failures,
        "max_launch_minutes": max_minutes,
        "backoff_seconds": backoff,
    }


_ALLOWED_HARNESS_KEYS = frozenset({
    "enabled",
    "backend",
    "adapter",
    "ocr_executable",
    "command_ref",
    "mode",
    "diff_mode",
    "rule_pack_path",
    "rule_pack",
    "scan_roots",
    "file_limits",
    "max_files",
    "max_bytes",
    "packet_size",
    "max_packet_bytes",
    "transport",
    "blocking_severities",
    "independent_gates",
    "timeout_seconds",
    "poll_interval_seconds",
})


def _normalize_reviewer_harness(raw: Any, index: int, config_path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(
            "config {0} project #{1} reviewer_harness must be an object".format(config_path, index)
        )
    unknown = set(raw.keys()) - _ALLOWED_HARNESS_KEYS
    if unknown:
        raise ValueError(
            "config {0} project #{1} reviewer_harness has unknown keys: {2}".format(
                config_path, index, sorted(unknown)
            )
        )
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.enabled must be a boolean".format(config_path, index)
        )
    backend = str(raw.get("backend") or raw.get("adapter") or "opencode_review").strip()
    if backend != "opencode_review":
        raise ValueError(
            "config {0} project #{1} reviewer_harness.backend must be 'opencode_review'".format(config_path, index)
        )
    ocr_exec = raw.get("ocr_executable")
    if ocr_exec is not None and not isinstance(ocr_exec, str):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.ocr_executable must be a string or null".format(config_path, index)
        )
    cmd_ref = str(raw.get("command_ref", "review-runner")).strip()
    if not cmd_ref:
        raise ValueError(
            "config {0} project #{1} reviewer_harness.command_ref must be nonblank".format(config_path, index)
        )
    mode = str(raw.get("mode", "diff")).strip().lower()
    if mode not in ("diff", "scan"):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.mode must be 'diff' or 'scan'".format(config_path, index)
        )
    diff_mode = str(raw.get("diff_mode", "workspace")).strip().lower()
    if diff_mode not in ("workspace", "range", "commit"):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.diff_mode must be 'workspace', 'range', or 'commit'".format(config_path, index)
        )
    rule_pack = raw.get("rule_pack_path") or raw.get("rule_pack")
    if rule_pack is not None and (not isinstance(rule_pack, str) or not rule_pack.strip()):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.rule_pack_path must be a nonblank string".format(config_path, index)
        )
    scan_roots = raw.get("scan_roots", ["."])
    if not isinstance(scan_roots, list) or not all(isinstance(r, str) for r in scan_roots):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.scan_roots must be a list of strings".format(config_path, index)
        )
    transport = str(raw.get("transport", "local")).strip().lower()
    if transport not in ("local", "ssh"):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.transport must be 'local' or 'ssh'".format(config_path, index)
        )
    blocking = raw.get("blocking_severities", ["blocking"])
    if not isinstance(blocking, list) or not all(isinstance(b, str) for b in blocking):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.blocking_severities must be a list of strings".format(config_path, index)
        )
    gates = raw.get("independent_gates", [])
    if not isinstance(gates, list) or not all(isinstance(g, str) for g in gates):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.independent_gates must be a list of strings".format(config_path, index)
        )
    raw_limits = raw.get("file_limits")
    if raw_limits is None:
        raw_limits = {}
    elif not isinstance(raw_limits, dict):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.file_limits must be an object".format(config_path, index)
        )
    file_limits = {
        "max_files": int(raw.get("max_files", raw_limits.get("max_files", 100))),
        "max_total_bytes": int(raw.get("max_bytes", raw_limits.get("max_total_bytes", raw_limits.get("max_bytes", 1024 * 1024)))),
        "max_packet_files": int(raw.get("packet_size", raw_limits.get("max_packet_files", raw_limits.get("packet_size", 10)))),
        "max_packet_bytes": int(raw.get("max_packet_bytes", raw_limits.get("max_packet_bytes", 200 * 1024))),
    }
    timeout_raw = raw.get("timeout_seconds")
    if timeout_raw is not None and (not isinstance(timeout_raw, (int, float)) or timeout_raw <= 0):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.timeout_seconds must be a positive number".format(config_path, index)
        )
    poll_raw = raw.get("poll_interval_seconds")
    if poll_raw is not None and (not isinstance(poll_raw, (int, float)) or poll_raw <= 0):
        raise ValueError(
            "config {0} project #{1} reviewer_harness.poll_interval_seconds must be a positive number".format(config_path, index)
        )
    normalized_rule_pack = rule_pack.strip() if isinstance(rule_pack, str) and rule_pack.strip() else None
    return {
        "enabled": enabled,
        "backend": backend,
        "adapter": backend,
        "ocr_executable": ocr_exec.strip() if isinstance(ocr_exec, str) and ocr_exec.strip() else None,
        "command_ref": cmd_ref,
        "mode": mode,
        "diff_mode": diff_mode,
        "rule_pack_path": normalized_rule_pack,
        "rule_pack": normalized_rule_pack,
        "scan_roots": [r.strip() for r in scan_roots if r.strip()],
        "transport": transport,
        "blocking_severities": blocking,
        "independent_gates": [g.strip() for g in gates if g.strip()],
        "file_limits": file_limits,
        "timeout_seconds": float(timeout_raw) if timeout_raw is not None else 600.0,
        "poll_interval_seconds": float(poll_raw) if poll_raw is not None else 0.2,
    }


def _normalize_project(project: Any, index: int, config_path: Path) -> dict[str, Any]:
    """One project entry -> canonical copy with legacy aliases preserved."""
    if not isinstance(project, dict):
        raise ValueError(
            "config {0} project #{1} must be a JSON object".format(config_path, index)
        )
    raw_id = project.get("project_id")
    if raw_id is None:
        raw_id = project.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError(
            "config {0} project #{1} is missing a project id".format(config_path, index)
        )
    project_id = raw_id.strip()

    raw_path = project.get("repo_path")
    if raw_path is None:
        raw_path = project.get("root")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(
            "config {0} project #{1} is missing a non-blank repo path "
            "(set canonical repo_path or legacy root)".format(config_path, index)
        )
    repo_path = raw_path.strip()
    repo_candidate = Path(repo_path).expanduser()
    if not repo_candidate.is_absolute():
        repo_candidate = (config_path.parent / repo_candidate).resolve(strict=False)
        repo_path = str(repo_candidate)

    normalized = dict(project)
    normalized["project_id"] = project_id
    normalized["repo_path"] = repo_path
    # P2 compatibility: legacy keys mirror the canonical values on the returned
    # copy so accepted legacy consumers keep working unchanged.
    normalized["id"] = project_id
    normalized["root"] = repo_path

    adapter = normalized.get("adapter")
    normalized["adapter"] = (
        str(adapter).strip() if isinstance(adapter, str) and adapter.strip() else DEFAULT_ADAPTER_ID
    )
    raw_context = project.get("project_context")
    if raw_context is not None:
        normalized["project_context"] = _normalize_project_context(raw_context, index, config_path)
    raw_watchdog = project.get("watchdog")
    if raw_watchdog is not None:
        normalized["watchdog"] = _normalize_project_watchdog(raw_watchdog, index, config_path)
    raw_self_healing = project.get("self_healing")
    if raw_self_healing is not None:
        normalized["self_healing"] = _normalize_project_self_healing(raw_self_healing, index, config_path)
    raw_harness = project.get("reviewer_harness")
    if raw_harness is not None:
        normalized["reviewer_harness"] = _normalize_reviewer_harness(raw_harness, index, config_path)
    normalized["orchestration_ready"] = (
        _binding_ready(normalized.get("conversation_binding"))
        or _direct_reviewer_ready(normalized)
        or _direct_planner_ready(normalized)
        or (isinstance(normalized.get("reviewer_harness"), dict) and normalized["reviewer_harness"].get("enabled") is True)
    )
    return normalized


_ALLOWED_MOBILE_GATEWAY_KEYS = frozenset({
    "enabled",
    "listen_address",
    "port",
})


def _normalize_mobile_gateway(raw: Any, config_path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(
            "config {0} mobile_gateway must be an object".format(config_path)
        )
    unknown = set(raw.keys()) - _ALLOWED_MOBILE_GATEWAY_KEYS
    if unknown:
        raise ValueError(
            "config {0} mobile_gateway has unknown keys: {1}".format(
                config_path, sorted(unknown)
            )
        )
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            "config {0} mobile_gateway.enabled must be a boolean".format(config_path)
        )
    listen_addr = raw.get("listen_address", "")
    if not isinstance(listen_addr, str):
        raise ValueError(
            "config {0} mobile_gateway.listen_address must be a string".format(config_path)
        )
    listen_addr = listen_addr.strip()
    port = raw.get("port", 8780)
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError(
            "config {0} mobile_gateway.port must be an integer".format(config_path)
        )
    if port < 1 or port > 65535:
        raise ValueError(
            "config {0} mobile_gateway.port must be between 1 and 65535".format(config_path)
        )
    return {
        "enabled": enabled,
        "listen_address": listen_addr,
        "port": port,
    }


def load_projects_config(path: Path | str) -> dict[str, Any]:
    """Load, validate and canonically normalize the projects configuration.

    Every project entry is returned with canonical ``project_id``/``repo_path``
    plus derived ``adapter`` (default ``agent_files``) and
    ``orchestration_ready``. Legacy ``id``/``root`` keys are preserved as
    aliases for P2 compatibility. Duplicate project ids raise ``ValueError``.
    """
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8-sig") as handle:
        try:
            data = json.load(handle)
        except ValueError as exc:
            raise ValueError("invalid config JSON in {0}: {1}".format(config_path, exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise ValueError("config {0} must contain a \"projects\" list".format(config_path))

    normalized_projects: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    for index, project in enumerate(data["projects"]):
        normalized = _normalize_project(project, index, config_path)
        project_id = normalized["project_id"]
        if project_id in seen_ids:
            raise ValueError(
                "duplicate project id {0!r} in config {1} (projects #{2} and #{3})".format(
                    project_id, config_path, seen_ids[project_id], index
                )
            )
        seen_ids[project_id] = index
        normalized_projects.append(normalized)

    _reject_duplicate_conversation_bindings(normalized_projects, config_path)

    data = dict(data)
    data["projects"] = normalized_projects
    raw_activation_profiles = data.get("activation_profiles")
    if raw_activation_profiles is not None:
        if not isinstance(raw_activation_profiles, dict):
            raise ValueError(
                "config {0} activation_profiles must be an object".format(config_path)
            )
        for profile_name, profile in raw_activation_profiles.items():
            if not isinstance(profile, dict):
                raise ValueError(
                    "config {0} activation_profiles[{1!r}] must be an object".format(
                        config_path, profile_name
                    )
                )
        data["activation_profiles"] = dict(raw_activation_profiles)
    raw_mobile = data.get("mobile_gateway")
    if raw_mobile is not None:
        data["mobile_gateway"] = _normalize_mobile_gateway(raw_mobile, config_path)
    return data


def _reject_duplicate_conversation_bindings(
    projects: list[dict[str, Any]], config_path: Path
) -> None:
    """Reject orchestration-ready projects sharing one binding triple.

    Only structurally ready bindings participate; monitor-only legacy projects
    (missing/malformed binding) are untouched. A duplicate
    ``(transport, adapter, binding_id)`` is a configuration error because it
    could cross-route two project workflows into one conversation.
    """
    seen_bindings: dict[tuple, tuple] = {}
    for index, project in enumerate(projects):
        if not bool(project.get("orchestration_ready")):
            continue
        binding = project.get("conversation_binding")
        if not isinstance(binding, dict):
            continue
        key = (
            binding.get("transport"),
            binding.get("adapter"),
            binding.get("binding_id"),
        )
        if key in seen_bindings:
            first_index, first_id = seen_bindings[key]
            raise ValueError(
                "duplicate conversation binding ({0}, {1}, {2}) in config {3} "
                "(projects {4!r} and {5!r})".format(
                    key[0], key[1], key[2], config_path, first_id, project["project_id"]
                )
            )
        seen_bindings[key] = (index, project["project_id"])
