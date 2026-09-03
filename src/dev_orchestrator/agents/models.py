"""Frozen domain model for the agent backend + router layer.

Defines the role/quota/run enums and the immutable value objects exchanged
between ``AgentBackend`` implementations, the ``BackendRegistry`` and the
``AgentRouter``. Provider names stay below the workflow boundary: a workflow
requests a role/capability set, never a concrete vendor/model.

All dataclasses are frozen so routing evidence and run records can never be
mutated by callers; the enum members mirror the frozen design vocabulary
(roles, quota states and run states).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class AgentRole(Enum):
    """Agent roles a workflow may request (design-frozen vocabulary)."""

    DESIGNER = "designer"
    WORKER = "worker"
    REVIEWER = "reviewer"
    SUPERVISOR = "supervisor"
    MECHANICAL = "mechanical"


class QuotaState(Enum):
    """Provider quota states reported by backends.

    ``UNKNOWN`` quota remains eligible; ``EXHAUSTED`` never is.
    """

    HEALTHY = "HEALTHY"
    CONSERVE = "CONSERVE"
    LOW = "LOW"
    EXHAUSTED = "EXHAUSTED"
    UNKNOWN = "UNKNOWN"


class AgentRunState(Enum):
    """Lifecycle states of an ``AgentRun`` (design-frozen vocabulary)."""

    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class CapabilitySet:
    """What a backend offers: roles, capability tags and provider identity.

    ``tags`` holds plain string capability tags (e.g. ``code``,
    ``repository``); ``provider`` and ``independence_domain`` name the
    provider-neutral identity so independent providers never share quota.
    """

    roles: frozenset[AgentRole]
    tags: frozenset[str]
    provider: str
    independence_domain: str
    cancellation_supported: bool = False


@dataclass(frozen=True)
class BackendStatus:
    """Result of a non-prompt ``probe()`` availability/health check.

    Field order is part of the frozen contract: ``backend_id``, ``available``,
    ``reason``, ``quota``, then the optional ``model_label`` and ``latency_ms``.
    """

    backend_id: str
    available: bool
    reason: str
    quota: QuotaState = QuotaState.UNKNOWN
    model_label: Optional[str] = None
    latency_ms: Optional[float] = None


@dataclass(frozen=True)
class AgentRequest:
    """One execution request from a future workflow boundary.

    The working directory is always owned by the request; required capability
    tags, preferred backends and excluded backends constrain routing. Defaults
    keep a minimal request (project, role, prompt, cwd, required capabilities)
    sufficient for direct backend execution.
    """

    project_id: str
    role: AgentRole
    prompt: str
    working_directory: Path
    required_capabilities: frozenset[str]
    preferred_backends: tuple[str, ...] = ()
    excluded_backends: frozenset[str] = frozenset()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRun:
    """Immutable snapshot of one backend run (same-process tracking only)."""

    run_id: str
    backend_id: str
    state: AgentRunState
    exit_code: Optional[int] = None
    pid: Optional[int] = None
    project_id: Optional[str] = None


@dataclass(frozen=True)
class AgentResult:
    """Collected terminal output of one backend run."""

    run_id: str
    backend_id: str
    state: AgentRunState
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class RoutingCandidate:
    """Per-backend routing evidence: eligibility, score and rejection reasons."""

    backend_id: str
    eligible: bool
    score: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutingDecision:
    """Deterministic router outcome with explicit evidence.

    ``selected_backend_id`` is ``None`` when no registered backend is
    eligible; the router never silently falls back around an explicit
    exclusion or a missing required capability.
    """

    selected_backend_id: Optional[str]
    candidates: tuple[RoutingCandidate, ...]
    reason: str
