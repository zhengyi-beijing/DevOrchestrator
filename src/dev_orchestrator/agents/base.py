"""Abstract backend contract and typed errors.

``AgentBackend.start()`` is the single explicit execution authority: probing
and routing never start a model. Unknown run ids fail closed with the typed
``UnknownRunError``; unexpected backend failures raise ``BackendError``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from dev_orchestrator.agents.models import (
    AgentRequest,
    AgentResult,
    AgentRun,
    BackendStatus,
    CapabilitySet,
)


class BackendError(Exception):
    """Base class for typed agent-backend failures."""


class UnknownRunError(BackendError):
    """Raised when a run id is unknown to a backend (fail closed)."""


class AgentBackend(ABC):
    """Provider-neutral execution contract implemented by every backend."""

    @property
    @abstractmethod
    def backend_id(self) -> str:
        """Stable, registry-unique backend identifier."""

    @abstractmethod
    def capabilities(self) -> CapabilitySet:
        """Roles, capability tags and provider identity this backend offers."""

    @abstractmethod
    def probe(self) -> BackendStatus:
        """Non-prompt availability/health check (never starts a model)."""

    @abstractmethod
    async def start(self, request: AgentRequest) -> AgentRun:
        """Explicit execution authority: start ``request`` as a new run."""

    @abstractmethod
    async def status(self, run_id: str) -> AgentRun:
        """Current snapshot of an existing run; unknown ids raise."""

    @abstractmethod
    async def cancel(self, run_id: str) -> AgentRun:
        """Cancel only the process belonging to ``run_id``."""

    @abstractmethod
    async def collect(self, run_id: str) -> AgentResult:
        """Wait for completion and return the run's captured output."""
