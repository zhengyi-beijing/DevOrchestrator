"""Stable orchestration-side port for AI role execution."""
from __future__ import annotations

from typing import Protocol

from .contracts import AIRoleRequest, AIRoleResult

MANAGED_INTERRUPT_REASON = "DevOrchestrator managed daemon stop terminated Broker process tree"


class AIExecutionPort(Protocol):
    """DevOrchestrator knows this port, never provider/model/account backends."""

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        """Execute exactly one semantic role request through the broker."""
        ...

    def status(self, request_id: str) -> dict | None:
        """Return persisted Broker facts for one semantic request."""
        ...

    def interrupt(self, request_id: str, reason: str) -> dict | None:
        """Mark a Broker request interrupted only after its process is known stopped."""
        ...
