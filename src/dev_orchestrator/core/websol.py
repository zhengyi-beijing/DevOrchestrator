"""Web Sol event/protocol models (frozen design vocabulary).

ChatGPT Web is not a log terminal: only reasoning events are eligible for Web
Sol transport in this slice. ``should_send_to_web_sol`` is the pure event
eligibility gate; PROGRESS/HEARTBEAT/ordinary sampling never leave the daemon
runtime/UI here.

Every request and response identity carries project/request/task/stage/
branch/head/role/event/nonce, and at least one of ``task_id``/``stage_id``
must be present. The ``project_id``/``request_id``/``branch``/``head``/
``nonce`` identity fields reject blank/whitespace values; ``task_id``/
``stage_id`` count only when nonblank. These models are pure data — nothing
here sends anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class WebSolEvent(Enum):
    """Reasoning events eligible for Web Sol transport (frozen slice)."""

    REVIEW_REQUIRED = "review_required"
    WORKER_DONE = "worker_done"
    TEST_FAILED = "test_failed"
    OWNER_GATE = "owner_gate"
    RECOVERY_REQUIRED = "recovery_required"


class WebSolRole(Enum):
    """Web Sol roles that may answer a reasoning event."""

    PLANNER = "planner"
    REVIEWER = "reviewer"
    SUPERVISOR = "supervisor"


class WebSolDecision(Enum):
    """Decision vocabulary (separate from the structured ``next_action``)."""

    NEXT = "next"
    REMEDIATE = "remediate"
    RETRY = "retry"
    OWNER_GATE = "owner_gate"
    STOP = "stop"


class NextAction(Enum):
    """Structured transition scope; mandatory and separate from rationale."""

    CONTINUE_CURRENT_STAGE = "continue_current_stage"
    NEXT_TASK = "next_task"
    NEXT_STAGE = "next_stage"
    STOP = "stop"


_ELIGIBLE_FOR_WEB_SOL = frozenset(
    {
        WebSolEvent.REVIEW_REQUIRED,
        WebSolEvent.WORKER_DONE,
        WebSolEvent.TEST_FAILED,
        WebSolEvent.OWNER_GATE,
        WebSolEvent.RECOVERY_REQUIRED,
    }
)


def should_send_to_web_sol(event: WebSolEvent) -> bool:
    """Whether ``event`` is a reasoning event eligible for Web Sol transport.

    Anything outside the frozen eligible set (including non-members and future
    daemon-resident events such as progress/heartbeat samples) is ``False``.
    """
    return isinstance(event, WebSolEvent) and event in _ELIGIBLE_FOR_WEB_SOL


_REQUIRED_IDENTITY_FIELDS = ("project_id", "request_id", "branch", "head", "nonce")


def _validate_identity(
    *,
    kind: str,
    project_id: Optional[str],
    request_id: Optional[str],
    branch: Optional[str],
    head: Optional[str],
    nonce: Optional[str],
    task_id: Optional[str],
    stage_id: Optional[str],
) -> None:
    """Reject blank identities and require at least one nonblank task/stage.

    Identity fields that are ``None``, not strings, or blank/whitespace-only
    make the Web Sol message invalid (fail closed). ``task_id``/``stage_id``
    count only when nonblank, and at least one of them must be nonblank.
    """
    identity_values = {
        "project_id": project_id,
        "request_id": request_id,
        "branch": branch,
        "head": head,
        "nonce": nonce,
    }
    for field in _REQUIRED_IDENTITY_FIELDS:
        value = identity_values[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "Web Sol {0} {1} must be a non-blank string".format(kind, field)
            )
    task_present = isinstance(task_id, str) and bool(task_id.strip())
    stage_present = isinstance(stage_id, str) and bool(stage_id.strip())
    if not task_present and not stage_present:
        raise ValueError(
            "Web Sol {0} requires at least one of task_id or stage_id".format(kind)
        )


@dataclass(frozen=True)
class WebSolRequest:
    """One reasoning-event request sent to a Web Sol role.

    Immutable value object; at least one of ``task_id``/``stage_id`` is
    required. Pure data — no send/transport behavior.
    """

    project_id: str
    request_id: str
    task_id: Optional[str]
    stage_id: Optional[str]
    branch: str
    head: str
    role: WebSolRole
    event: WebSolEvent
    nonce: str

    def __post_init__(self) -> None:
        _validate_identity(
            kind="request",
            project_id=self.project_id,
            request_id=self.request_id,
            branch=self.branch,
            head=self.head,
            nonce=self.nonce,
            task_id=self.task_id,
            stage_id=self.stage_id,
        )


@dataclass(frozen=True)
class WebSolResponse:
    """A Web Sol answer to a ``WebSolRequest``.

    ``next_action`` is mandatory protocol data but may be absent on an invalid
    wire payload; the decision guard fails closed (STOP) rather than
    constructing here. Immutable value object with no execution behavior.
    """

    project_id: str
    request_id: str
    task_id: Optional[str]
    stage_id: Optional[str]
    branch: str
    head: str
    role: WebSolRole
    event: WebSolEvent
    nonce: str
    decision: WebSolDecision
    next_action: Optional[NextAction] = None

    def __post_init__(self) -> None:
        _validate_identity(
            kind="response",
            project_id=self.project_id,
            request_id=self.request_id,
            branch=self.branch,
            head=self.head,
            nonce=self.nonce,
            task_id=self.task_id,
            stage_id=self.stage_id,
        )
