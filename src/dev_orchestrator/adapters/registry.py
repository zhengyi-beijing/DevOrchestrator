"""Explicit ProjectAdapter registry with unknown-adapter fail-closed lookup.

Registration is the only way an adapter becomes selectable; nothing auto-
discovers adapters. Adapter ids must be unique and iteration preserves
registration order. ``require`` raises ``UnknownProjectAdapterError`` so Core
fails closed (MONITOR_ERROR) when project configuration names an adapter that
is not registered — it never silently falls back to a default.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from dev_orchestrator.adapters.base import ProjectAdapter


class UnknownProjectAdapterError(ValueError):
    """Raised when a configured adapter id is not registered (fail closed)."""


class ProjectAdapterRegistry:
    """Ordered collection of explicitly registered ``ProjectAdapter`` objects."""

    def __init__(self) -> None:
        self._adapters: Dict[str, ProjectAdapter] = {}

    def register(self, adapter: ProjectAdapter) -> ProjectAdapter:
        """Register ``adapter``; reject duplicate adapter ids explicitly."""
        if not isinstance(adapter, ProjectAdapter):
            raise TypeError("register() requires a ProjectAdapter instance")
        adapter_id = adapter.adapter_id
        if not adapter_id:
            raise ValueError("adapter id must be a non-empty string")
        if adapter_id in self._adapters:
            raise ValueError(
                "duplicate adapter id {0!r}: already registered".format(adapter_id)
            )
        self._adapters[adapter_id] = adapter
        return adapter

    def get(self, adapter_id: str) -> Optional[ProjectAdapter]:
        """Return the registered adapter or ``None`` when unknown."""
        return self._adapters.get(adapter_id)

    def require(self, adapter_id: str) -> ProjectAdapter:
        """Return the registered adapter or fail closed when unknown."""
        adapter = self._adapters.get(adapter_id)
        if adapter is None:
            raise UnknownProjectAdapterError(
                "unknown project adapter {0!r}: registry has {1}".format(
                    adapter_id, ", ".join(repr(i) for i in self._adapters) or "(none)"
                )
            )
        return adapter

    def adapter_ids(self) -> Tuple[str, ...]:
        """Registered adapter ids in registration order."""
        return tuple(self._adapters)

    def __contains__(self, adapter_id: object) -> bool:
        return adapter_id in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)
