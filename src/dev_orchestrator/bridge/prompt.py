"""Render Web Sol bridge request prompts with exact response markers.

Rendered requests begin with ``[DEVORCH_WEB_SOL_REQUEST <request_id>]`` and
carry the canonical identity JSON plus role/event context. The prompt requires
the final answer to contain ``[DEVORCH_WEB_SOL_RESPONSE <same request_id>]``
followed by structured response JSON that echoes the full request identity and
adds the mandatory Core ``decision`` and ``next_action`` fields.

Rendering is transport only: nothing here validates a structured response,
starts a Worker, or applies ``next_action``.
"""

from __future__ import annotations

import json

from dev_orchestrator.core.websol import NextAction, WebSolDecision, WebSolRequest

_REQUEST_HEAD = "[DEVORCH_WEB_SOL_REQUEST "
_RESPONSE_HEAD = "[DEVORCH_WEB_SOL_RESPONSE "

# Accepted Core wire enum values rendered verbatim into every prompt.
_DECISION_VALUES = [decision.value for decision in WebSolDecision]
_NEXT_ACTION_VALUES = [action.value for action in NextAction]


def render_websol_prompt(request: WebSolRequest, context: str) -> str:
    """Render one Web Sol request into the exact bridge prompt contract.

    ``context`` is the free-form role/event evidence the role should review;
    it is embedded verbatim. The returned text starts with the request marker,
    includes the canonical identity JSON, and demands a final answer carrying
    the matching response marker plus a complete structured response JSON that
    echoes the identity and supplies ``decision``/``next_action``.
    """
    if not isinstance(context, str) or not context.strip():
        raise ValueError("context must be a non-blank string")
    identity = {
        "project_id": request.project_id,
        "request_id": request.request_id,
        "task_id": request.task_id,
        "stage_id": request.stage_id,
        "branch": request.branch,
        "head": request.head,
        "role": request.role.value,
        "event": request.event.value,
        "nonce": request.nonce,
    }
    identity_json = json.dumps(identity, ensure_ascii=False, indent=2)
    decision_choices = ", ".join('"{0}"'.format(v) for v in _DECISION_VALUES)
    action_choices = ", ".join('"{0}"'.format(v) for v in _NEXT_ACTION_VALUES)
    lines = [
        "{0}{1}]".format(_REQUEST_HEAD, request.request_id),
        "",
        identity_json,
        "",
        "Context:",
        context,
        "",
        "Reply with a final answer that begins with the exact marker line:",
        "{0}{1}]".format(_RESPONSE_HEAD, request.request_id),
        "followed by your structured response JSON on the following lines.",
        "",
        "The marker request id and your response marker id must be identical: {0}.".format(
            request.request_id
        ),
        "",
        'Your structured response JSON MUST echo the request identity exactly - the keys "project_id", "request_id", "task_id", "stage_id", "branch", "head", "role", "event", "nonce" with the same values as the request above - and MUST then add the two mandatory decision fields:',
        "",
        '"decision": one of {0}'.format(decision_choices),
        '"next_action": one of {0}'.format(action_choices),
        "",
        "Emit one JSON object only, containing the echoed identity keys and those two fields.",
    ]
    return "\n".join(lines) + "\n"
