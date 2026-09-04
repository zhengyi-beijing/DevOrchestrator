"""Response Consumer: Bridge response -> parse -> Decision Guard -> disposition.

Bounded chain (authorized slice ``agent/next.md``):

    Bridge response -> parse exact DEVORCH_WEB_SOL_RESPONSE marker/JSON
        -> construct WebSolResponse -> validate echoed identity
        -> fresh repository truth -> Decision Guard -> disposition

The consumer is Core, not transport and not execution:

- It matches the exact ``[DEVORCH_WEB_SOL_RESPONSE <request_id>]`` marker and
  parses exactly one JSON object after it; a missing/wrong marker, an
  unparseable payload, extra top-level fields or trailing non-whitespace
  content fail closed (STOP).
- Every accepted payload must construct a :class:`WebSolResponse`; the
  existing decision guard (``dev_orchestrator.core.decision``) then requires
  the exact echo of project/request/task/stage/branch/head/role/event/nonce
  and answers IGNORE for identity mismatches.
- Fresh repository truth is read immediately before the guard runs, so a
  decision is never accepted against stale branch/HEAD/dirty state.
- Only a disposition/intention is produced and persisted under
  ``runtime/websol-decisions.json`` (APPLY/IGNORE/STALE/STOP/
  REVIEW_REQUIRED/OWNER_GATE), preserving project/binding isolation and
  deterministic request identity. Each responded request is consumed exactly
  once; repeated calls return nothing new.

No Worker is started, no Agent Router invoked, no NEXT/REMEDIATE/RETRY
auto-executed, no PHASE_AUTO implemented, and no LabDemo/hardware action is
touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.decision import DecisionDisposition, validate_websol_response
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.websol import (
    NextAction,
    WebSolDecision,
    WebSolEvent,
    WebSolRequest,
    WebSolResponse,
    WebSolRole,
)
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

DECISIONS_FILE = "websol-decisions.json"
_DECISIONS_VERSION = 1
_BROWSER_BRIDGE_TRANSPORT = "browser_bridge"
_MARKER_TEMPLATE = "[DEVORCH_WEB_SOL_RESPONSE {0}]"
_RESPONSE_FIELDS = frozenset(
    {
        "project_id",
        "request_id",
        "task_id",
        "stage_id",
        "branch",
        "head",
        "role",
        "event",
        "nonce",
        "decision",
        "next_action",
    }
)


@dataclass(frozen=True)
class WebSolConsumption:
    """One newly consumed Bridge response and its guard disposition.

    ``next_action`` is preserved only when the response may proceed (APPLY) or
    when an OWNER_GATE/STOP explicitly requests a wait; every other
    fail-closed outcome carries ``None`` so nothing can be accidentally
    executed from a rejected response.
    """

    project_id: str
    request_id: str
    disposition: DecisionDisposition
    next_action: Optional[NextAction] = None
    reason: str = ""


# --------------------------------------------------------------------------
# small fail-closed helpers
# --------------------------------------------------------------------------


def _non_blank(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _binding_route(binding: Any) -> Optional[tuple]:
    """Return ``(adapter, binding_id)`` for a browser-bridge route.

    Mirrors the dispatcher's route rule: transport/adapter/binding_id must be
    non-blank strings and transport must be the browser-bridge transport this
    slice can reach. Anything else returns ``None`` (project stays monitor
    only; nothing is consumed).
    """
    if not isinstance(binding, dict):
        return None
    transport = binding.get("transport")
    adapter = binding.get("adapter")
    binding_id = binding.get("binding_id")
    fields = (transport, adapter, binding_id)
    if not all(isinstance(value, str) and value.strip() for value in fields):
        return None
    if transport != _BROWSER_BRIDGE_TRANSPORT:
        return None
    return (adapter, binding_id)


def _brief(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _stop_outcome(project_id: str, request_id: str, reason: str) -> WebSolConsumption:
    return WebSolConsumption(
        project_id=project_id,
        request_id=request_id,
        disposition=DecisionDisposition.STOP,
        next_action=None,
        reason=reason,
    )


# --------------------------------------------------------------------------
# parse / construct (fail closed)
# --------------------------------------------------------------------------


def _extract_payload(request_id: str, response_text: str) -> dict:
    """Return the single JSON object after the exact response marker.

    The response must contain ``[DEVORCH_WEB_SOL_RESPONSE <request_id>]`` and
    exactly one JSON object after it (leading whitespace tolerated, no
    trailing non-whitespace content). Anything else raises ``ValueError`` so
    the caller fails closed.
    """
    if not isinstance(response_text, str):
        raise ValueError("response_text must be a string")
    marker = _MARKER_TEMPLATE.format(request_id)
    index = response_text.find(marker)
    if index < 0:
        raise ValueError("response text lacks the exact DEVORCH_WEB_SOL_RESPONSE marker")
    remainder = response_text[index + len(marker):].lstrip()
    if not remainder:
        raise ValueError("response text has no JSON payload after the marker")
    try:
        payload, end = json.JSONDecoder().raw_decode(remainder)
    except json.JSONDecodeError as exc:
        raise ValueError("response JSON after the marker is not parseable") from exc
    if remainder[end:].strip():
        raise ValueError("more than one JSON object after the marker")
    if not isinstance(payload, dict):
        raise ValueError("response payload must be a single JSON object")
    return payload


def _response_from_payload(payload: dict) -> WebSolResponse:
    """Construct a :class:`WebSolResponse` from one parsed payload.

    Rejects extra top-level fields, missing identity keys and unknown enum
    values (all fail closed via ``ValueError``). Enum values use the Core wire
    values (``reviewer``, ``worker_done``, ``next``, ``next_task``, ...).
    """
    extra = set(payload).difference(_RESPONSE_FIELDS)
    if extra:
        raise ValueError("response JSON contains unexpected field(s): {0}".format(", ".join(sorted(extra))))
    try:
        role = WebSolRole(payload["role"])
        event = WebSolEvent(payload["event"])
        decision = WebSolDecision(payload["decision"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("response JSON role/event/decision is missing or invalid") from exc
    raw_next = payload.get("next_action")
    try:
        next_action = NextAction(raw_next) if raw_next is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("response JSON next_action is invalid") from exc
    try:
        return WebSolResponse(
            project_id=payload["project_id"],
            request_id=payload["request_id"],
            task_id=payload.get("task_id"),
            stage_id=payload.get("stage_id"),
            branch=payload["branch"],
            head=payload["head"],
            role=role,
            event=event,
            nonce=payload["nonce"],
            decision=decision,
            next_action=next_action,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("response JSON identity is missing or invalid") from exc


def _request_from_record(record: dict) -> WebSolRequest:
    """Rebuild the exact WebSolRequest frozen before the Bridge submit."""
    try:
        return WebSolRequest(
            project_id=str(record["project_id"]),
            request_id=str(record["request_id"]),
            task_id=record.get("task_id"),
            stage_id=record.get("stage_id"),
            branch=str(record["branch"]),
            head=str(record["head"]),
            role=WebSolRole(str(record["role"])),
            event=WebSolEvent(str(record["event"])),
            nonce=str(record["nonce"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("stored request record is not reconstructable") from exc


# --------------------------------------------------------------------------
# persisted decision ledger (runtime/websol-decisions.json)
# --------------------------------------------------------------------------


def _decisions_path(runtime: Path | str) -> Path:
    return Path(runtime) / DECISIONS_FILE


def _load_decisions(path: Path) -> dict:
    """Load persisted consumption decisions; malformed pieces fail closed."""
    data = read_json(path, None)
    if not isinstance(data, dict):
        data = {}
    entries = data.get("decisions")
    if not isinstance(entries, dict):
        entries = {}
    decisions: dict[str, dict[str, Any]] = {}
    for request_id, record in entries.items():
        if isinstance(request_id, str) and isinstance(record, dict):
            decisions[request_id] = record
    return {"version": _DECISIONS_VERSION, "decisions": decisions}


def _save_decisions(path: Path, ledger: dict) -> None:
    write_json(path, ledger, indent=2)


# --------------------------------------------------------------------------
# one project
# --------------------------------------------------------------------------


def _consume_one(snapshot: dict, record: dict, repo_path: str) -> WebSolConsumption:
    """Turn one RESPONDED Bridge record into a persisted disposition.

    Every failure path returns a STOP outcome (fail closed): the raw record
    may be un-reconstructable, the payload malformed, identity invalid, truth
    unavailable, or the guard itself rejects the response. Nothing here
    executes a next_action.
    """
    request_id = _non_blank(record.get("request_id"))
    project_id = _non_blank(record.get("project_id"))
    fallback_project = _non_blank(snapshot.get("project_id"))
    if request_id is None:
        return _stop_outcome(
            project_id or fallback_project or "unknown",
            request_id or "unknown",
            "responded record has no request_id",
        )
    try:
        request = _request_from_record(record)
    except ValueError as exc:
        return _stop_outcome(
            project_id or fallback_project or "unknown", request_id, _brief(exc)
        )
    try:
        payload = _extract_payload(request.request_id, str(record.get("response_text") or ""))
        response = _response_from_payload(payload)
    except ValueError as exc:
        return _stop_outcome(request.project_id, request.request_id, _brief(exc))

    truth = read_repository_truth(repo_path)
    verdict = validate_websol_response(request, response, truth)
    return WebSolConsumption(
        project_id=request.project_id,
        request_id=request.request_id,
        disposition=verdict.disposition,
        next_action=verdict.next_action,
        reason=verdict.reason,
    )


def _consume_project(
    snapshot: Any, store: BrowserBridgeStore, path: Path, ledger: dict
) -> list[WebSolConsumption]:
    """Consume every newly responded request under one project's binding."""
    if not isinstance(snapshot, dict):
        return []
    if snapshot.get("orchestration_ready") is not True:
        return []
    repo_path = _non_blank(snapshot.get("repo_path"))
    if repo_path is None:
        return []
    route = _binding_route(snapshot.get("conversation_binding"))
    if route is None:
        return []
    adapter, binding_id = route

    outcomes: list[WebSolConsumption] = []
    for record in store.list_responded(adapter, binding_id):
        request_id = _non_blank(record.get("request_id"))
        if request_id is None or request_id in ledger["decisions"]:
            continue
        outcome = _consume_one(snapshot, record, repo_path)
        ledger["decisions"][outcome.request_id] = {
            "project_id": outcome.project_id,
            "request_id": outcome.request_id,
            "disposition": outcome.disposition.value,
            "next_action": outcome.next_action.value if outcome.next_action is not None else None,
            "reason": outcome.reason,
            "consumed_at": utc_now_iso(),
        }
        _save_decisions(path, ledger)
        outcomes.append(outcome)
    return outcomes


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------


def consume_websol_responses(
    summary: Any, store: BrowserBridgeStore, runtime: Any
) -> list[WebSolConsumption]:
    """Consume newly RESPONDED Bridge responses into guard dispositions.

    ``summary`` is one monitor summary (``{"projects": [snapshot, ...]}``).
    ``store`` is the process-local :class:`BrowserBridgeStore`; ``runtime`` is
    the DevOrchestrator runtime root where ``websol-decisions.json`` is
    persisted.

    Only orchestration-ready projects with a browser-bridge binding route are
    scanned, and only their exact binding queues are read (project/binding
    isolation by construction). Every request id is consumed at most once
    across calls/restarts. Returns the :class:`WebSolConsumption` outcomes for
    responses newly consumed in this call — ``[]`` when there is nothing new.
    """
    if not isinstance(summary, dict):
        return []
    projects = summary.get("projects")
    if not isinstance(projects, list):
        return []
    path = _decisions_path(runtime)
    ledger = _load_decisions(path)
    outcomes: list[WebSolConsumption] = []
    for snapshot in projects:
        outcomes.extend(_consume_project(snapshot, store, path, ledger))
    return outcomes
