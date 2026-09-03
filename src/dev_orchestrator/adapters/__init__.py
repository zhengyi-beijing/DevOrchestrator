"""Configuration-driven ProjectAdapter selection.

Core selects a ProjectAdapter purely from each project's ``adapter`` setting;
``agent_files`` (the existing agent-file contract) is the registered default.
Unknown adapter ids fail closed via ``UnknownProjectAdapterError`` instead of
silently falling back.
"""

from __future__ import annotations

from dev_orchestrator.adapters.agent_files import AgentFilesAdapter
from dev_orchestrator.adapters.base import DEFAULT_ADAPTER_ID, ProjectAdapter
from dev_orchestrator.adapters.registry import (
    ProjectAdapterRegistry,
    UnknownProjectAdapterError,
)

__all__ = [
    "AgentFilesAdapter",
    "DEFAULT_ADAPTER_ID",
    "ProjectAdapter",
    "ProjectAdapterRegistry",
    "UnknownProjectAdapterError",
    "get_project_adapter",
    "default_registry",
]

_registry: ProjectAdapterRegistry = ProjectAdapterRegistry()
_registry.register(AgentFilesAdapter())


def default_registry() -> ProjectAdapterRegistry:
    """The process-wide registry with the default ``agent_files`` adapter."""
    return _registry


def get_project_adapter(adapter_id: str) -> ProjectAdapter:
    """Return the registered adapter for ``adapter_id`` (fail closed)."""
    return _registry.require(adapter_id)
