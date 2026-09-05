"""Event Dispatcher: WORKER_DONE -> WebSolRequest -> the project's Bridge queue.

This is the first bounded event slice of the accepted Web Sol transport:

    monitor summary -> WORKER_DONE -> WebSolRequest(REVIEWER)
                       -> rendered prompt -> BrowserBridgeStore.submit()

A project snapshot dispatches exactly one ``WORKER_DONE`` request when an
eligible project shows a completed task Worker run. Delivery is withheld until
the project is orchestration-ready and routed to a ``browser_bridge`` binding
(and, for live-binding stores, until that conversation is actually being
watched); an occurrence that cannot be delivered yet stays frozen as
``prepared``/``unbound`` and resumes with the same identity on a later tick.
The dispatcher never claims, responds, or interprets a Web Sol response — the
Bridge remains transport only (see ``docs/BROWSER_BRIDGE_CHATGPT_BINDING_DESIGN.md``).

Guarantees (fail closed):

- Fresh repository truth is read immediately before request construction;
  ``branch``/``head`` in the request come from that truth, never from stale
  monitor ``git`` text.
- Event identity is deterministic and idempotent for one completed Worker
  occurrence: ``request_id`` and ``nonce`` are pure functions of
  ``(project_id, run_id, event, role)``, so repeated monitor ticks or a daemon
  restart can never enqueue a second request for the same occurrence. A
  persisted dispatcher ledger under the runtime root additionally prevents
  re-submission once an occurrence has been dispatched, while a persisted
  Response Consumer decision acts as a durable tombstone even if dispatcher or
  Bridge transport state is later pruned/recovered.
- Every eligible completed occurrence is frozen exactly once under the ledger
  as ``prepared`` with its request identity, fresh branch/head and rendered
  prompt. Delivery into a Bridge queue happens only when the project is
  orchestration-ready and routed to a ``browser_bridge`` binding; anything not
  yet deliverable stays ``prepared`` with ``delivery_state=unbound`` and is
  re-attempted on a later tick (no new request identity is ever created for the
  same occurrence, so a later binding/rebinding resumes the frozen request).
- Stores created with ``require_live_binding=True`` additionally withhold
  delivery until the exact binding shows live adapter presence
  (``BrowserBridgeStore.binding_status() == bound``): a request is never handed
  to a ChatGPT conversation that no adapter is currently watching. Ordinary
  stores keep the accepted direct-submit behavior.
- Routing uses only the project's validated ``conversation_binding``
  ``(transport, adapter, binding_id)``; the only transport implemented in this
  slice is ``browser_bridge``. Missing/malformed bindings, non-browser
  transports, non-``task`` Workers, unattributed completions (no ``run_id`` /
  ``task_id``) and unavailable repository truth stay monitorable and emit
  nothing.
- Project isolation is preserved by construction: the route comes from the
  project's own snapshot binding, the deterministic request id embeds the
  project id, and the Bridge store enforces one global binding per request id.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dev_orchestrator.bridge.prompt import render_websol_prompt
from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.repository import read_repository_truth
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole
from dev_orchestrator.storage.json_store import read_json, utc_now_iso, write_json

DISPATCHER_STATE_FILE = "dispatcher-state.json"
_LEDGER_VERSION = 2
_LEDGER_EVENT_KEY = WebSolEvent.WORKER_DONE.value  # "worker_done"
_BROWSER_BRIDGE_TRANSPORT = "browser_bridge"
_RESPONSE_DECISIONS_FILE = "websol-decisions.json"

_STATE_PREPARED = "prepared"
_STATE_SUBMITTED = "submitted"
_DELIVERY_UNBOUND = "unbound"


@dataclass(frozen=True)
class WorkerDoneDispatch:
    """One WORKER_DONE request newly enqueued into a project's Bridge binding.

    Only *new* enqueues appear in the returned list: repeated ticks and
    restarts that deduplicate an already-dispatched occurrence return nothing.
    """

    project_id: str
    adapter: str
    binding_id: str
    request_id: str
    run_id: str
    branch: str
    head: str
    event: str = WebSolEvent.WORKER_DONE.value
    role: str = WebSolRole.REVIEWER.value


# --------------------------------------------------------------------------
# pure candidate / route / identity helpers (fail closed)
# --------------------------------------------------------------------------


def _binding_route(binding: Any) -> Optional[tuple]:
    """Return ``(transport, adapter, binding_id)`` for a validated route.

    The binding must be a dict whose ``transport``/``adapter``/``binding_id``
    are all non-blank strings, and ``transport`` must be the browser-bridge
    transport this slice can reach. Anything else returns ``None`` so the
    project stays monitor-only.
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
    return (transport, adapter, binding_id)


def _non_blank(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _request_id(project_id: str, run_id: str) -> str:
    """Deterministic per-occurrence request id (stable across ticks/restarts)."""
    return "worker_done:{0}:{1}".format(project_id, run_id)


def _nonce(project_id: str, run_id: str) -> str:
    """Deterministic per-occurrence nonce (stable across ticks/restarts)."""
    digest = hashlib.sha256()
    digest.update("worker_done".encode("utf-8"))
    digest.update(b"\x00")
    digest.update(project_id.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(run_id.encode("utf-8"))
    return digest.hexdigest()


def _worker_done_evidence(
    snapshot: dict, truth: Any, worker: dict, task_id: Optional[str], stage_id: Optional[str], run_id: str
) -> str:
    """Free-form WORKER_DONE/REVIEWER evidence rendered into the prompt."""
    exit_code = worker.get("exit_code")
    lines = [
        "DevOrchestrator project {0} has a completed Worker task run that is ready for review.".format(
            snapshot.get("project_id") or ""
        ),
        "",
        "Event: WORKER_DONE",
        "Role: REVIEWER",
        "",
        "Project: {0}".format(snapshot.get("project_id") or ""),
        "Task: {0}".format(task_id or "unknown"),
        "Stage: {0}".format(stage_id or "unknown"),
        "Run: {0}".format(run_id),
        "Repository branch: {0}".format(truth.branch),
        "Repository HEAD: {0}".format(truth.head),
        "Worker command: {0}".format(worker.get("command") or "unknown"),
        "Worker exit code: {0}".format(exit_code if exit_code is not None else "unknown"),
        "Worker completed at: {0}".format(worker.get("updated_at") or "unknown"),
        "",
        "Review the completed Worker result and reply with a structured Web Sol response.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# dispatcher ledger (persisted under the runtime root)
# --------------------------------------------------------------------------


def _ledger_path(runtime: Path | str) -> Path:
    return Path(runtime) / DISPATCHER_STATE_FILE


def _load_ledger(path: Path) -> dict:
    """Load persisted dispatcher state; malformed pieces fail closed."""
    data = read_json(path, None)
    if not isinstance(data, dict):
        return {"version": _LEDGER_VERSION, "worker_done": {}}
    entries = data.get(_LEDGER_EVENT_KEY)
    if not isinstance(entries, dict):
        entries = {}
    clean: dict[str, dict] = {}
    for project_id, record in entries.items():
        if not isinstance(project_id, str) or not isinstance(record, dict):
            continue
        occurrences = record.get("occurrences")
        if isinstance(occurrences, dict):
            clean[project_id] = {
                "occurrences": {
                    str(run_id): occurrence
                    for run_id, occurrence in occurrences.items()
                    if isinstance(run_id, str) and isinstance(occurrence, dict)
                }
            }
    return {"version": _LEDGER_VERSION, "worker_done": clean}


def _save_ledger(path: Path, ledger: dict) -> None:
    write_json(path, ledger, indent=2)


def _response_already_consumed(
    dispatcher_path: Path, project_id: str, request_id: str
) -> bool:
    """Return whether Response Consumer already durably handled request_id.

    The response decision ledger is a second-line tombstone independent of the
    dispatcher ledger and Bridge queue. This prevents retention/pruning or
    recovery of those transport-side files from re-emitting the same
    deterministic WORKER_DONE request after its response was already consumed.
    A present but malformed/conflicting tombstone fails closed instead of
    risking a duplicate Web Sol turn.
    """
    decisions_path = dispatcher_path.with_name(_RESPONSE_DECISIONS_FILE)
    if not decisions_path.exists():
        return False
    data = read_json(decisions_path, None)
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), dict):
        raise RuntimeError("response decision ledger is malformed")
    decisions = data["decisions"]
    if request_id not in decisions:
        return False
    record = decisions[request_id]
    if not isinstance(record, dict):
        raise RuntimeError("response decision tombstone is malformed for {0}".format(request_id))
    if _non_blank(record.get("request_id")) != request_id:
        raise RuntimeError("response decision tombstone request_id mismatch for {0}".format(request_id))
    if _non_blank(record.get("project_id")) != project_id:
        raise RuntimeError("response decision tombstone project_id mismatch for {0}".format(request_id))
    return True


def _project_occurrences(ledger: dict, project_id: str) -> dict:
    entries = ledger[_LEDGER_EVENT_KEY]
    project = entries.get(project_id)
    if not isinstance(project, dict):
        project = {"occurrences": {}}
        entries[project_id] = project
    occurrences = project.get("occurrences")
    if not isinstance(occurrences, dict):
        occurrences = {}
        project["occurrences"] = occurrences
    return occurrences


def _request_from_prepared(project_id: str, occurrence: dict) -> WebSolRequest:
    """Rebuild the exact request frozen before the first Bridge submit."""
    return WebSolRequest(
        project_id=project_id,
        request_id=str(occurrence["request_id"]),
        task_id=occurrence.get("task_id"),
        stage_id=occurrence.get("stage_id"),
        branch=str(occurrence["branch"]),
        head=str(occurrence["head"]),
        role=WebSolRole.REVIEWER,
        event=WebSolEvent.WORKER_DONE,
        nonce=str(occurrence["nonce"]),
    )


def _dispatch_result(
    project_id: str, run_id: str, occurrence: dict, adapter: str, binding_id: str
) -> WorkerDoneDispatch:
    return WorkerDoneDispatch(
        project_id=project_id,
        adapter=adapter,
        binding_id=binding_id,
        request_id=str(occurrence["request_id"]),
        run_id=run_id,
        branch=str(occurrence["branch"]),
        head=str(occurrence["head"]),
    )


def _delivery_allowed(store: BrowserBridgeStore, adapter: str, binding_id: str) -> bool:
    """Whether a submission into the exact binding may happen right now.

    Ordinary stores submit directly to the configured route. Live-binding
    stores (``require_live_binding=True``) require the exact binding to show
    live adapter presence inside its presence window — a conversation nobody is
    watching must not receive a request (unbound browser reliability).
    """
    require_live = bool(getattr(store, "require_live_binding", False))
    if not require_live:
        return True
    status = store.binding_status(adapter, binding_id)
    return status.get("state") == "bound"


def _keep_unbound(occurrence: dict, path: Path, ledger: dict) -> None:
    """Persist the observable prepared/unbound marker exactly once."""
    if occurrence.get("delivery_state") == _DELIVERY_UNBOUND:
        return
    occurrence["delivery_state"] = _DELIVERY_UNBOUND
    _save_ledger(path, ledger)


def _submit_prepared(
    project_id: str,
    run_id: str,
    occurrence: dict,
    adapter: str,
    binding_id: str,
    store: BrowserBridgeStore,
    path: Path,
    ledger: dict,
) -> WorkerDoneDispatch:
    """Submit one frozen prepared occurrence and mark the ledger submitted.

    The request identity (and the rendered prompt) come exclusively from the
    frozen occurrence, so a crash between freeze and submit — or a later
    binding/rebinding — always resumes the exact same request and never
    fabricates a new identity for the same completed Worker occurrence.
    """
    prompt = occurrence.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RuntimeError(
            "prepared dispatcher occurrence is missing its prompt for {0}/{1}".format(project_id, run_id)
        )
    request = _request_from_prepared(project_id, occurrence)
    store.submit(adapter, binding_id, request, prompt)
    occurrence["state"] = _STATE_SUBMITTED
    occurrence["dispatched_at"] = utc_now_iso()
    occurrence.pop("delivery_state", None)
    _save_ledger(path, ledger)
    return _dispatch_result(project_id, run_id, occurrence, adapter, binding_id)


# --------------------------------------------------------------------------
# one project
# --------------------------------------------------------------------------


def _dispatch_one(
    snapshot: Any, store: BrowserBridgeStore, ledger: dict, path: Path
) -> Optional[WorkerDoneDispatch]:
    """Dispatch one completed Worker occurrence, idempotently across crashes."""
    if not isinstance(snapshot, dict):
        return None
    project_id = _non_blank(snapshot.get("project_id"))
    repo_path = _non_blank(snapshot.get("repo_path"))
    if project_id is None or repo_path is None:
        return None

    worker = snapshot.get("worker")
    telemetry = snapshot.get("telemetry")
    if not isinstance(worker, dict) or not isinstance(telemetry, dict):
        return None
    if worker.get("kind") != "task" or worker.get("state") != "completed":
        return None

    run_id = _non_blank(telemetry.get("run_id"))
    task_id = _non_blank(telemetry.get("task_id"))
    stage_id = _non_blank(snapshot.get("stage_id"))
    if run_id is None or (task_id is None and stage_id is None):
        return None

    request_id = _request_id(project_id, run_id)
    if _response_already_consumed(path, project_id, request_id):
        return None

    occurrences = _project_occurrences(ledger, project_id)
    prepared = occurrences.get(run_id)
    if isinstance(prepared, dict):
        state = prepared.get("state")
        if state == _STATE_SUBMITTED:
            return None
        if state != _STATE_PREPARED:
            raise RuntimeError(
                "invalid dispatcher occurrence state for {0}/{1}".format(project_id, run_id)
            )
        # Resume an existing frozen occurrence. Delivery is gated on the
        # project being orchestration-ready, on a current browser-bridge
        # route, and (for live-binding stores) on live adapter presence. A
        # rebind between freeze and delivery therefore resumes the same frozen
        # request into the new conversation instead of fabricating a new one.
        if snapshot.get("orchestration_ready") is not True:
            _keep_unbound(prepared, path, ledger)
            return None
        route = _binding_route(snapshot.get("conversation_binding"))
        if route is None:
            _keep_unbound(prepared, path, ledger)
            return None
        _, adapter, binding_id = route
        if not _delivery_allowed(store, adapter, binding_id):
            _keep_unbound(prepared, path, ledger)
            return None
        return _submit_prepared(project_id, run_id, prepared, adapter, binding_id, store, path, ledger)

    route = _binding_route(snapshot.get("conversation_binding"))
    ready = snapshot.get("orchestration_ready") is True

    truth = read_repository_truth(repo_path)
    if not truth.valid:
        return None

    request = WebSolRequest(
        project_id=project_id,
        request_id=request_id,
        task_id=task_id,
        stage_id=stage_id,
        branch=truth.branch,
        head=truth.head,
        role=WebSolRole.REVIEWER,
        event=WebSolEvent.WORKER_DONE,
        nonce=_nonce(project_id, run_id),
    )
    context = _worker_done_evidence(snapshot, truth, worker, task_id, stage_id, run_id)
    prompt = render_websol_prompt(request, context)

    # Freeze the occurrence exactly once, before any delivery decision. The
    # frozen identity/prompt is what a later binding or live presence resumes;
    # a project that is not yet deliverable stays prepared/unbound (visible in
    # the dashboard) instead of silently disappearing.
    occurrence: dict[str, Any] = {
        "state": _STATE_PREPARED,
        "request_id": request.request_id,
        "nonce": request.nonce,
        "task_id": request.task_id,
        "stage_id": request.stage_id,
        "branch": request.branch,
        "head": request.head,
        "review_dirty": bool(truth.dirty),
        "review_status_hash": truth.status_hash,
        "prompt": prompt,
        "prepared_at": utc_now_iso(),
        "delivery_state": _DELIVERY_UNBOUND,
    }
    if route is not None:
        transport, adapter, binding_id = route
        occurrence["transport"] = transport
        occurrence["adapter"] = adapter
        occurrence["binding_id"] = binding_id
    occurrences[run_id] = occurrence
    _save_ledger(path, ledger)

    if route is not None and ready and _delivery_allowed(store, route[1], route[2]):
        return _submit_prepared(project_id, run_id, occurrence, route[1], route[2], store, path, ledger)
    return None


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------


def dispatch_worker_done_events(summary: Any, store: BrowserBridgeStore, runtime: Any) -> list:
    """Submit one WORKER_DONE request per newly completed Worker occurrence.

    ``summary`` is one monitor summary (``{"projects": [snapshot, ...]}``).
    ``store`` is the process-local :class:`BrowserBridgeStore`; ``runtime`` is
    the DevOrchestrator runtime root where the dispatcher ledger is persisted.

    Returns the list of :class:`WorkerDoneDispatch` outcomes for requests that
    were newly enqueued in this call — ``[]`` when there is nothing new
    (duplicate ticks, restarts, ineligible projects) or when delivery is
    withheld because the occurrence is frozen but not yet deliverable
    (``prepared``/``unbound``). Never claims, responds, or consumes a Web Sol
    response.
    """
    if not isinstance(summary, dict):
        return []
    projects = summary.get("projects")
    if not isinstance(projects, list):
        return []
    path = _ledger_path(runtime)
    ledger = _load_ledger(path)
    dispatched: list[WorkerDoneDispatch] = []
    for snapshot in projects:
        outcome = _dispatch_one(snapshot, store, ledger, path)
        if outcome is not None:
            dispatched.append(outcome)
    return dispatched
