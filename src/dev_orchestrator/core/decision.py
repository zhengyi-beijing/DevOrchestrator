"""Pure Web Sol decision guard (fail closed).

Web Sol never controls a Worker directly. After a response arrives,
DevOrchestrator obtains fresh repository truth and validates the response here
before any future workflow may act. Validation is a pure function of the
request, the response and the truth snapshot — no I/O, no execution.

Fail-closed rules (design ``docs/MULTIPROJECT_WEBSOL_CORE_DESIGN.md``):

- project/request/nonce mismatch -> IGNORE;
- task/stage/role/event identity mismatch -> IGNORE;
- repository truth unavailable -> STOP;
- response/request/current branch or HEAD mismatch -> STALE, no execution;
- missing/invalid ``next_action`` -> STOP;
- invalid decision/``next_action`` combination -> STOP;
- dirty workspace without an established known-dirty contract -> REVIEW_REQUIRED;
- OWNER_GATE -> OWNER_GATE (wait for human authorization);
- otherwise APPLY with the explicitly bounded ``next_action`` preserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from dev_orchestrator.core.repository import RepositoryTruth
from dev_orchestrator.core.websol import (
    NextAction,
    WebSolDecision,
    WebSolRequest,
    WebSolResponse,
)


class DecisionDisposition(Enum):
    """Outcome vocabulary of the Web Sol decision guard."""

    APPLY = "apply"
    IGNORE = "ignore"
    STALE = "stale"
    STOP = "stop"
    REVIEW_REQUIRED = "review_required"
    OWNER_GATE = "owner_gate"


_ALLOWED_NEXT_ACTIONS: dict[WebSolDecision, frozenset] = {
    WebSolDecision.NEXT: frozenset(
        {
            NextAction.CONTINUE_CURRENT_STAGE,
            NextAction.NEXT_TASK,
            NextAction.NEXT_STAGE,
        }
    ),
    WebSolDecision.REMEDIATE: frozenset({NextAction.CONTINUE_CURRENT_STAGE}),
    WebSolDecision.RETRY: frozenset({NextAction.CONTINUE_CURRENT_STAGE}),
    WebSolDecision.OWNER_GATE: frozenset({NextAction.STOP}),
    WebSolDecision.STOP: frozenset({NextAction.STOP}),
}


@dataclass(frozen=True)
class DecisionVerdict:
    """One guard outcome.

    ``next_action`` is preserved only when the response may proceed (APPLY) or
    when an OWNER_GATE explicitly requests a STOP wait; every other
    fail-closed outcome carries ``None`` so nothing can be accidentally
    executed from a rejected response.
    """

    disposition: DecisionDisposition
    next_action: Optional[NextAction] = None
    reason: str = ""


def validate_websol_response(
    request: WebSolRequest,
    response: WebSolResponse,
    truth: RepositoryTruth,
    *,
    known_dirty_status_hash: Optional[str] = None,
) -> DecisionVerdict:
    """Validate a response against its request and fresh repository truth."""
    # project / request / nonce identity mismatch -> IGNORE
    if (
        response.project_id != request.project_id
        or response.request_id != request.request_id
        or response.nonce != request.nonce
    ):
        return DecisionVerdict(
            DecisionDisposition.IGNORE,
            reason="project/request/nonce identity mismatch",
        )

    # task / stage / role / event identity mismatch -> IGNORE
    if (
        response.task_id != request.task_id
        or response.stage_id != request.stage_id
        or response.role != request.role
        or response.event != request.event
    ):
        return DecisionVerdict(
            DecisionDisposition.IGNORE,
            reason="task/stage/role/event identity mismatch",
        )

    # repository truth unavailable -> STOP
    if not truth.valid:
        return DecisionVerdict(
            DecisionDisposition.STOP,
            reason="repository truth unavailable",
        )

    # response/request/current branch or HEAD mismatch -> STALE, no execution
    if (
        request.branch != truth.branch
        or response.branch != truth.branch
        or request.head != truth.head
        or response.head != truth.head
    ):
        return DecisionVerdict(
            DecisionDisposition.STALE,
            reason="response/request/current branch or HEAD mismatch",
        )

    # missing/invalid next_action -> STOP
    if not isinstance(response.next_action, NextAction):
        return DecisionVerdict(
            DecisionDisposition.STOP,
            reason="missing or invalid next_action",
        )

    # decision must pair with an allowed next_action -> STOP
    allowed = _ALLOWED_NEXT_ACTIONS.get(response.decision)
    if allowed is None or response.next_action not in allowed:
        return DecisionVerdict(
            DecisionDisposition.STOP,
            reason="invalid decision/next_action combination",
        )

    # Dirty work may proceed only for an explicitly reviewed remediation of
    # the same current stage, and only while the exact reviewed Git porcelain
    # fingerprint is unchanged. Any late/unreviewed worktree change fails closed.
    if truth.dirty:
        reviewed_remediation = (
            response.decision is WebSolDecision.REMEDIATE
            and response.next_action is NextAction.CONTINUE_CURRENT_STAGE
            and isinstance(known_dirty_status_hash, str)
            and bool(known_dirty_status_hash.strip())
            and truth.status_hash == known_dirty_status_hash
        )
        if not reviewed_remediation:
            return DecisionVerdict(
                DecisionDisposition.REVIEW_REQUIRED,
                reason="dirty workspace requires review or reviewed fingerprint changed",
            )

    # OWNER_GATE -> STOP and wait for human authorization
    if response.decision is WebSolDecision.OWNER_GATE:
        return DecisionVerdict(
            DecisionDisposition.OWNER_GATE,
            next_action=response.next_action,
            reason="OWNER_GATE waits for human authorization",
        )

    if response.decision is WebSolDecision.STOP:
        return DecisionVerdict(
            DecisionDisposition.STOP,
            next_action=response.next_action,
            reason="explicit STOP decision",
        )

    return DecisionVerdict(
        DecisionDisposition.APPLY,
        next_action=response.next_action,
        reason="valid decision with explicit transition scope",
    )
