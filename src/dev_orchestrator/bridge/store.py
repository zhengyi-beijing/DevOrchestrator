"""Persistent Browser Bridge transport store.

Each outbound Web Sol request lives under an exact ``(adapter, binding_id)``
queue with the transport state machine:

    PENDING -> CLAIMED -> RESPONDED
       ^          |
       +----------+  lease expiry / adapter loss

Contract (design ``docs/BROWSER_BRIDGE_CHATGPT_BINDING_DESIGN.md``):

- Exact ``(adapter, binding_id)`` queue isolation: no operation on one queue
  ever reads or mutates another queue's request.
- One global binding per ``request_id``: a request id already persisted in
  another ``(adapter, binding_id)`` fails closed with ``BridgeConflictError``.
  An identical re-submit into the same binding stays idempotent; reusing a
  ``request_id`` with a different nonce or any changed identity fails closed
  and never changes queue state.
- ``claim`` is filtered by the exact ``(adapter, binding_id)``, returns at most
  one request, and issues an opaque ``claim_token`` with a bounded lease. A
  leased claim blocks the queue until the lease expires, after which the same
  request may be reclaimed with a fresh token. The claim envelope carries the
  transport metadata ``lease_expires_at`` (UTC ISO-8601) so an adapter can
  reason only about transport authority.
- ``renew`` extends the lease of a still-valid exact active claim (same
  binding, request id, nonce and claim token, lease not yet expired) from the
  renewal moment and returns the claimed state with the fresh
  ``lease_expires_at``. A wrong or expired identity/token is rejected with
  ``BridgeConflictError`` and never mutates queue state.
- ``respond`` requires the same binding, request id, nonce and claim token of
  a claim whose lease has not yet expired; any mismatch — including an expired
  lease even when state/token/nonce still match — is rejected with
  ``BridgeConflictError`` and never changes queue state.
- Pending/claimed/responded state is persisted under the store root, so it
  survives a store reopen.

Binding presence: every valid adapter poll (``claim``) and every successful
``renew``/``respond`` records that the exact ``(adapter, binding_id)`` was
recently observed live. ``binding_status`` derives ``bound``/``unbound`` from
that observation against ``binding_presence_seconds``; a binding that was never
observed, or whose last observation is older than the window, is ``unbound``
(fail closed). Stores created with ``require_live_binding=True`` expose this
status to the dispatcher so a request is only handed to a conversation an
adapter is actually watching (unbound browser reliability); ordinary stores
keep submitting directly to the configured route.

The transport only stores and delivers the response. It never validates
structured response content and never applies any decision.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from dev_orchestrator.core.websol import WebSolRequest
from dev_orchestrator.storage.json_store import read_json, write_json

_DEFAULT_LEASE_SECONDS = 30
_DEFAULT_PRESENCE_SECONDS = 300

STATE_PENDING = "pending"
STATE_CLAIMED = "claimed"
STATE_RESPONDED = "responded"

STATE_BOUND = "bound"
STATE_UNBOUND = "unbound"

_IDENTITY_FIELDS = (
    "project_id",
    "task_id",
    "stage_id",
    "branch",
    "head",
    "role",
    "event",
    "nonce",
)


class BridgeConflictError(Exception):
    """The operation would violate transport identity/claim invariants.

    Raised (fail closed) when a request id is reused with changed identity,
    already belongs to another binding, or when a response/renewal does not
    present the exact binding/request/nonce/claim token. A conflict never
    mutates queue state.
    """


@dataclass(frozen=True)
class StoredSubmission:
    """Immutable view returned by :meth:`BrowserBridgeStore.submit`."""

    adapter: str
    binding_id: str
    project_id: str
    request_id: str
    nonce: str
    state: str


@dataclass(frozen=True)
class BridgeClaim:
    """One claimed Web Sol request delivered to an adapter.

    Carries the accepted Web Sol identity fields, the rendered prompt, the
    opaque claim token the adapter must present when responding, and the
    transport metadata ``lease_expires_at`` (UTC ISO-8601) bounding that
    claim's authority.
    """

    adapter: str
    binding_id: str
    project_id: str
    request_id: str
    task_id: Optional[str]
    stage_id: Optional[str]
    branch: str
    head: str
    role: str
    event: str
    nonce: str
    claim_token: str
    lease_expires_at: str
    prompt: str


@dataclass(frozen=True)
class StoredResponse:
    """Immutable view of an accepted response stored by the transport."""

    adapter: str
    binding_id: str
    request_id: str
    nonce: str
    response_text: str
    state: str = STATE_RESPONDED


@dataclass(frozen=True)
class StoredRenewal:
    """Immutable view of a renewed active claim returned by renew().

    Carries the exact claim identity plus the fresh ``lease_expires_at``
    (UTC ISO-8601) metadata bounding the renewed transport authority.
    """

    adapter: str
    binding_id: str
    request_id: str
    nonce: str
    lease_expires_at: str
    state: str = STATE_CLAIMED


def _as_utc(value: Optional[datetime]) -> datetime:
    """Normalize a ``now`` parameter to an aware UTC datetime."""
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _encode_segment(value: str) -> str:
    """Filesystem-safe, injective encoding for a queue segment."""
    return quote(str(value), safe="")


def _require_route(adapter: Any, binding_id: Any) -> None:
    if not isinstance(adapter, str) or not adapter.strip():
        raise ValueError("adapter must be a non-blank string")
    if not isinstance(binding_id, str) or not binding_id.strip():
        raise ValueError("binding_id must be a non-blank string")


class BrowserBridgeStore:
    """Persistent, process-local transport store for one bridge instance.

    Queue state is persisted as atomic JSON files under ``root/queues/`` keyed
    by the exact encoded ``(adapter, binding_id)``. All mutations are guarded
    by a process-local lock; queue files are (re)loaded from disk on each
    operation so state survives reopen.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        require_live_binding: bool = False,
        binding_presence_seconds: Optional[int] = None,
    ) -> None:
        """Open (creating when needed) the persistent transport store.

        ``require_live_binding=True`` makes the store advertise that delivery
        into a binding must be gated on live adapter presence (see
        :meth:`binding_status`); the dispatcher consults that flag before
        handing a request to a conversation no adapter is watching.
        ``binding_presence_seconds`` bounds how recent a claim poll/renewal
        must be for a binding to count as live (default 300 s).
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if lease_seconds is None or int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be a positive number")
        self.lease_seconds = int(lease_seconds)
        self.require_live_binding = bool(require_live_binding)
        if binding_presence_seconds is None:
            binding_presence_seconds = _DEFAULT_PRESENCE_SECONDS
        if int(binding_presence_seconds) <= 0:
            raise ValueError("binding_presence_seconds must be a positive number")
        self.binding_presence_seconds = int(binding_presence_seconds)
        self._queue_dir = self.root / "queues"
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        self._presence_dir = self.root / "presence"
        self._presence_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # internal queue persistence
    # ------------------------------------------------------------------

    def _queue_path(self, adapter: str, binding_id: str) -> Path:
        return self._queue_dir / _encode_segment(adapter) / (_encode_segment(binding_id) + ".json")

    def _load_queue(self, adapter: str, binding_id: str) -> dict[str, dict[str, Any]]:
        path = self._queue_path(adapter, binding_id)
        data = read_json(path, None)
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict) and isinstance(key, str)
        }

    def _save_queue(self, adapter: str, binding_id: str, queue: dict[str, dict[str, Any]]) -> None:
        write_json(self._queue_path(adapter, binding_id), queue)

    def _queue_files(self) -> list[Path]:
        if not self._queue_dir.is_dir():
            return []
        return sorted(path for path in self._queue_dir.glob("*/*.json"))

    def _presence_path(self, adapter: str, binding_id: str) -> Path:
        return self._presence_dir / _encode_segment(adapter) / (_encode_segment(binding_id) + ".json")

    def _touch_presence(self, adapter: str, binding_id: str, moment: datetime) -> None:
        """Record that the exact binding was just observed live by an adapter.

        Called for every valid claim poll (including polls that find nothing)
        and for every successful renew/respond, so presence reflects "an
        adapter is watching this exact conversation right now".
        """
        write_json(
            self._presence_path(adapter, binding_id),
            {
                "adapter": adapter,
                "binding_id": binding_id,
                "last_seen_at": _iso(moment),
            },
        )

    def _request_id_lives_elsewhere(
        self, adapter: str, binding_id: str, request_id: str
    ) -> bool:
        """Whether ``request_id`` is already persisted in another binding.

        Scans every persisted queue file except the exact
        ``(adapter, binding_id)`` route. A ``request_id`` belongs to exactly one
        binding so a cross-project response lookup can never be ambiguous.
        """
        target = self._queue_path(adapter, binding_id)
        for path in self._queue_files():
            if path == target:
                continue
            data = read_json(path, None)
            if not isinstance(data, dict):
                continue
            for record in data.values():
                if not isinstance(record, dict):
                    continue
                if record.get("request_id") == request_id:
                    return True
        return False

    @staticmethod
    def _identity_matches(record: dict[str, Any], request: WebSolRequest) -> bool:
        stored = {field: record.get(field) for field in _IDENTITY_FIELDS}
        incoming = {
            "project_id": request.project_id,
            "task_id": request.task_id,
            "stage_id": request.stage_id,
            "branch": request.branch,
            "head": request.head,
            "role": request.role.value,
            "event": request.event.value,
            "nonce": request.nonce,
        }
        return stored == incoming

    @staticmethod
    def _record_to_claim(record: dict[str, Any], claim_token: str) -> BridgeClaim:
        return BridgeClaim(
            adapter=str(record["adapter"]),
            binding_id=str(record["binding_id"]),
            project_id=str(record["project_id"]),
            request_id=str(record["request_id"]),
            task_id=record.get("task_id"),
            stage_id=record.get("stage_id"),
            branch=str(record["branch"]),
            head=str(record["head"]),
            role=str(record["role"]),
            event=str(record["event"]),
            nonce=str(record["nonce"]),
            claim_token=claim_token,
            lease_expires_at=str(record.get("lease_expires_at") or ""),
            prompt=str(record["prompt"]),
        )

    # ------------------------------------------------------------------
    # public transport API
    # ------------------------------------------------------------------

    def binding_status(
        self, adapter: str, binding_id: str, *, now: Optional[datetime] = None
    ) -> dict[str, Any]:
        """Report whether the exact binding currently has live adapter presence.

        ``bound`` means an adapter poll/renewal for the exact
        ``(adapter, binding_id)`` was observed within the last
        ``binding_presence_seconds``; anything else — never observed, or
        observed before the presence window — is ``unbound`` (fail closed, so
        a conversation nobody is watching never looks deliverable).
        """
        _require_route(adapter, binding_id)
        moment = _as_utc(now)
        with self._lock:
            data = read_json(self._presence_path(adapter, binding_id), None)
        last_seen_at: Optional[str] = None
        state = STATE_UNBOUND
        if isinstance(data, dict):
            last_seen = _parse_iso(data.get("last_seen_at"))
            if last_seen is not None:
                last_seen_at = _iso(last_seen)
                if (moment - last_seen).total_seconds() < self.binding_presence_seconds:
                    state = STATE_BOUND
        return {
            "state": state,
            "adapter": adapter,
            "binding_id": binding_id,
            "last_seen_at": last_seen_at,
            "binding_presence_seconds": self.binding_presence_seconds,
        }

    def submit(
        self,
        adapter: str,
        binding_id: str,
        request: WebSolRequest,
        prompt: str,
        *,
        now: Optional[datetime] = None,
    ) -> StoredSubmission:
        """Persist one outbound Web Sol request under the exact binding queue.

        Idempotent by ``(request_id, nonce)`` within one binding; any
        request-id reuse with a changed nonce or identity raises
        :class:`BridgeConflictError`. A request id already persisted in another
        ``(adapter, binding_id)`` also fails closed, so every request id has
        exactly one global binding.
        """
        _require_route(adapter, binding_id)
        if not isinstance(request, WebSolRequest):
            raise TypeError("request must be a WebSolRequest")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-blank string")
        moment = _as_utc(now)
        with self._lock:
            queue = self._load_queue(adapter, binding_id)
            existing = queue.get(request.request_id)
            if existing is not None:
                if not self._identity_matches(existing, request):
                    raise BridgeConflictError(
                        "request_id {0!r} reused with a changed nonce or identity "
                        "in binding {1!r}".format(request.request_id, binding_id)
                    )
                return StoredSubmission(
                    adapter=adapter,
                    binding_id=binding_id,
                    project_id=request.project_id,
                    request_id=request.request_id,
                    nonce=request.nonce,
                    state=str(existing.get("state") or STATE_PENDING),
                )
            if self._request_id_lives_elsewhere(adapter, binding_id, request.request_id):
                raise BridgeConflictError(
                    "request_id {0!r} is already persisted in another "
                    "(adapter,binding_id); one request id may belong to only "
                    "one binding".format(request.request_id)
                )
            record: dict[str, Any] = {
                "adapter": adapter,
                "binding_id": binding_id,
                "project_id": request.project_id,
                "request_id": request.request_id,
                "task_id": request.task_id,
                "stage_id": request.stage_id,
                "branch": request.branch,
                "head": request.head,
                "role": request.role.value,
                "event": request.event.value,
                "nonce": request.nonce,
                "prompt": prompt,
                "state": STATE_PENDING,
                "claim_token": None,
                "claimed_at": None,
                "lease_expires_at": None,
                "response_text": None,
                "responded_at": None,
                "created_at": _iso(moment),
            }
            queue[request.request_id] = record
            self._save_queue(adapter, binding_id, queue)
            return StoredSubmission(
                adapter=adapter,
                binding_id=binding_id,
                project_id=request.project_id,
                request_id=request.request_id,
                nonce=request.nonce,
                state=STATE_PENDING,
            )

    def claim(
        self, adapter: str, binding_id: str, *, now: Optional[datetime] = None
    ) -> Optional[BridgeClaim]:
        """Claim at most one request for the exact binding.

        Returns ``None`` when the queue holds nothing pending or expired.
        Claiming marks the request ``CLAIMED`` and issues a fresh opaque
        ``claim_token`` with a bounded lease.
        """
        _require_route(adapter, binding_id)
        moment = _as_utc(now)
        with self._lock:
            # Every claim poll is liveness evidence for the exact binding, even
            # an empty one: an adapter that polls and finds nothing is still
            # watching this conversation.
            self._touch_presence(adapter, binding_id, moment)
            queue = self._load_queue(adapter, binding_id)
            # Fresh pending work must not be head-of-line blocked by an older
            # request whose claim keeps expiring and being retried. Prefer all
            # never-claimed pending requests first; only then reclaim expired
            # claims. Within each class, preserve insertion order.
            for reclaim_expired in (False, True):
                for request_id, record in queue.items():
                    state = record.get("state")
                    if not reclaim_expired:
                        if state != STATE_PENDING:
                            continue
                    else:
                        if state != STATE_CLAIMED:
                            continue
                        expires_at = _parse_iso(record.get("lease_expires_at"))
                        if expires_at is None or moment < expires_at:
                            continue
                    claim_token = secrets.token_urlsafe(18)
                    record["state"] = STATE_CLAIMED
                    record["claim_token"] = claim_token
                    record["claimed_at"] = _iso(moment)
                    record["lease_expires_at"] = _iso(moment + timedelta(seconds=self.lease_seconds))
                    self._save_queue(adapter, binding_id, queue)
                    return self._record_to_claim(record, claim_token)
            return None

    def respond(
        self,
        adapter: str,
        binding_id: str,
        request_id: str,
        nonce: str,
        claim_token: str,
        response_text: str,
        *,
        now: Optional[datetime] = None,
    ) -> StoredResponse:
        """Store the raw assistant response for one claimed request.

        The first response must present the exact binding, request id, nonce and
        claim token of a claim whose lease has not yet expired. Once that exact
        response has been accepted, an identical replay with the same nonce,
        claim token and response text is idempotent even after the original
        lease window; it returns the already-stored response without mutating
        queue state. Any non-identical replay or identity mismatch raises
        :class:`BridgeConflictError` and never changes queue state.
        """
        _require_route(adapter, binding_id)
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-blank string")
        if not isinstance(response_text, str) or not response_text.strip():
            raise ValueError("response_text must be a non-blank string")
        moment = _as_utc(now)
        with self._lock:
            queue = self._load_queue(adapter, binding_id)
            record = queue.get(request_id)
            if record is None:
                raise BridgeConflictError(
                    "no request {0!r} exists in binding {1!r}".format(request_id, binding_id)
                )
            if record.get("state") == STATE_RESPONDED:
                identical_replay = (
                    record.get("nonce") == nonce
                    and record.get("claim_token") == claim_token
                    and record.get("response_text") == response_text
                )
                if identical_replay:
                    return StoredResponse(
                        adapter=adapter,
                        binding_id=binding_id,
                        request_id=request_id,
                        nonce=nonce,
                        response_text=response_text,
                    )
                raise BridgeConflictError(
                    "response for request {0!r} conflicts with the already "
                    "accepted response".format(request_id)
                )

            expires_at = _parse_iso(record.get("lease_expires_at"))
            valid = (
                record.get("state") == STATE_CLAIMED
                and record.get("nonce") == nonce
                and record.get("claim_token") == claim_token
                and expires_at is not None
                and moment < expires_at
            )
            if not valid:
                raise BridgeConflictError(
                    "response for request {0!r} does not match the claimed "
                    "binding/nonce/claim token or its lease has expired".format(request_id)
                )
            record["state"] = STATE_RESPONDED
            record["response_text"] = response_text
            record["responded_at"] = _iso(moment)
            self._save_queue(adapter, binding_id, queue)
            # A successful response proves the adapter is alive on this binding.
            self._touch_presence(adapter, binding_id, moment)
            return StoredResponse(
                adapter=adapter,
                binding_id=binding_id,
                request_id=request_id,
                nonce=nonce,
                response_text=response_text,
            )

    def renew(
        self,
        adapter: str,
        binding_id: str,
        request_id: str,
        nonce: str,
        claim_token: str,
        *,
        now: Optional[datetime] = None,
    ) -> StoredRenewal:
        """Extend the lease of one still-valid exact active claim.

        Renewal is allowed only while the claim is still active: the record
        must be present in the exact binding with the exact request id, nonce
        and claim token, and its lease must not yet have expired at ``now``.
        The lease is then extended from ``now`` (``now + lease_seconds``).
        Any wrong/expired identity or token raises
        :class:`BridgeConflictError` and never changes queue state.
        """
        _require_route(adapter, binding_id)
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-blank string")
        if not isinstance(nonce, str) or not nonce.strip():
            raise ValueError("nonce must be a non-blank string")
        if not isinstance(claim_token, str) or not claim_token.strip():
            raise ValueError("claim_token must be a non-blank string")
        moment = _as_utc(now)
        with self._lock:
            queue = self._load_queue(adapter, binding_id)
            record = queue.get(request_id)
            if record is None:
                raise BridgeConflictError(
                    "no request {0!r} exists in binding {1!r}".format(request_id, binding_id)
                )
            expires_at = _parse_iso(record.get("lease_expires_at"))
            active = (
                record.get("state") == STATE_CLAIMED
                and record.get("nonce") == nonce
                and record.get("claim_token") == claim_token
                and expires_at is not None
                and moment < expires_at
            )
            if not active:
                raise BridgeConflictError(
                    "request {0!r} is not a still-valid exact active claim "
                    "matching the binding/nonce/claim token".format(request_id)
                )
            renewed_lease = _iso(moment + timedelta(seconds=self.lease_seconds))
            record["lease_expires_at"] = renewed_lease
            self._save_queue(adapter, binding_id, queue)
            # A successful renew proves the adapter is alive on this binding.
            self._touch_presence(adapter, binding_id, moment)
            return StoredRenewal(
                adapter=adapter,
                binding_id=binding_id,
                request_id=request_id,
                nonce=nonce,
                lease_expires_at=renewed_lease,
                state=STATE_CLAIMED,
            )

    def has_active_claim(
        self, adapter: str, binding_id: str, *, now: Optional[datetime] = None
    ) -> bool:
        """Whether the exact binding currently has a still-valid claimed request."""
        _require_route(adapter, binding_id)
        moment = _as_utc(now)
        with self._lock:
            queue = self._load_queue(adapter, binding_id)
            for record in queue.values():
                if not isinstance(record, dict) or record.get("state") != STATE_CLAIMED:
                    continue
                expires_at = _parse_iso(record.get("lease_expires_at"))
                if expires_at is not None and moment < expires_at:
                    return True
        return False

    def list_responded(self, adapter: str, binding_id: str) -> list[dict[str, Any]]:
        """Return immutable copies of RESPONDED records for the exact binding.

        Transport-observation helper used by the Response Consumer: Core reads
        what an adapter delivered without ever mutating queue state here.
        Records are returned in deterministic ``request_id`` order so repeated
        consumers see one stable view.
        """
        _require_route(adapter, binding_id)
        with self._lock:
            queue = self._load_queue(adapter, binding_id)
            records: list[dict[str, Any]] = []
            for request_id in sorted(queue):
                record = queue[request_id]
                if not isinstance(record, dict):
                    continue
                if record.get("state") == STATE_RESPONDED:
                    records.append(dict(record))
            return records

    def get_response(self, request_id: str, nonce: str) -> Optional[StoredResponse]:
        """Return the stored response matching ``request_id``/``nonce``.

        Responses are keyed per binding queue, so a lookup without a binding
        scans all persisted queues. Returns ``None`` when no responded request
        matches.
        """
        with self._lock:
            for path in self._queue_files():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                for record in data.values():
                    if not isinstance(record, dict):
                        continue
                    if (
                        record.get("state") == STATE_RESPONDED
                        and record.get("request_id") == request_id
                        and record.get("nonce") == nonce
                    ):
                        return StoredResponse(
                            adapter=str(record.get("adapter") or ""),
                            binding_id=str(record.get("binding_id") or ""),
                            request_id=str(record["request_id"]),
                            nonce=str(record["nonce"]),
                            response_text=str(record["response_text"]),
                        )
            return None
