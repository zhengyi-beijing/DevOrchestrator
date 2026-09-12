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
    "APPLYING_PLAN",
    "EXECUTING",
    "REVIEWING",
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
    normalized["orchestration_ready"] = (
        _binding_ready(normalized.get("conversation_binding"))
        or _direct_reviewer_ready(normalized)
        or _direct_planner_ready(normalized)
    )
    return normalized


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
