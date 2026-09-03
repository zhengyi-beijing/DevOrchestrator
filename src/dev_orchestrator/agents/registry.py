"""Explicit backend registry with duplicate-id rejection.

Registration is the only way a backend becomes routable; nothing auto-
discovers or lazily imports provider adapters. Backend ids must be unique,
and iteration preserves registration order for deterministic routing.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from dev_orchestrator.agents.base import AgentBackend


class BackendRegistry:
    """Ordered collection of explicitly registered ``AgentBackend`` objects."""

    def __init__(self) -> None:
        self._backends: Dict[str, AgentBackend] = {}

    def register(self, backend: AgentBackend) -> AgentBackend:
        """Register ``backend``; reject duplicate backend ids explicitly."""
        if not isinstance(backend, AgentBackend):
            raise TypeError("register() requires an AgentBackend instance")
        backend_id = backend.backend_id
        if not backend_id:
            raise ValueError("backend id must be a non-empty string")
        if backend_id in self._backends:
            raise ValueError(
                "duplicate backend id {0!r}: already registered".format(backend_id)
            )
        self._backends[backend_id] = backend
        return backend

    def get(self, backend_id: str) -> Optional[AgentBackend]:
        """Return the registered backend or ``None`` when unknown."""
        return self._backends.get(backend_id)

    def __contains__(self, backend_id: object) -> bool:
        return backend_id in self._backends

    def backend_ids(self) -> Tuple[str, ...]:
        """Registered backend ids in registration order."""
        return tuple(self._backends)

    def all(self) -> Tuple[AgentBackend, ...]:
        """Registered backends in registration order."""
        return tuple(self._backends.values())

    def __len__(self) -> int:
        return len(self._backends)
