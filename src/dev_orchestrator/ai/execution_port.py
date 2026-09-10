"""Stable orchestration-side port for AI role execution."""
from __future__ import annotations

from typing import Protocol

from .contracts import AIRoleRequest, AIRoleResult


class AIExecutionPort(Protocol):
    """DevOrchestrator knows this port, never provider/model/account backends."""

    def execute(self, request: AIRoleRequest) -> AIRoleResult:
        """Execute exactly one semantic role request through the broker."""
        ...
