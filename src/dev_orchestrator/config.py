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
  ``conversation_binding`` exists or a direct ``ai_roles.reviewer`` is explicitly enabled.
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
    normalized["orchestration_ready"] = (
        _binding_ready(normalized.get("conversation_binding"))
        or _direct_reviewer_ready(normalized)
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
