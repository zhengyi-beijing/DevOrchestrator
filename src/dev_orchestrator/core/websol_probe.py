"""Deterministic end-to-end inference probe for Web Sol availability."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from dev_orchestrator.bridge.store import BrowserBridgeStore
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole
from dev_orchestrator.storage.json_store import utc_now

PROBE_PREFIX = "probe:websol:"
DEFAULT_PROBE_TIMEOUT_SECONDS = 900.0  # Aligned with browser wait deadline (15 minutes)


@dataclass(frozen=True)
class ProbeResult:
    success: bool
    error_class: Optional[str] = None
    reason: Optional[str] = None
    request_id: Optional[str] = None
    nonce: Optional[str] = None
    duration_seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


def is_probe_request_id(request_id: str) -> bool:
    return isinstance(request_id, str) and request_id.startswith(PROBE_PREFIX)


def make_probe_request(
    binding_id: str,
    generation: int,
    *,
    nonce: Optional[str] = None,
    project_id: str = "devorchestrator",
) -> tuple[WebSolRequest, str]:
    """Generate a probe WebSolRequest and matching prompt with reserved identity."""
    token = secrets.token_hex(6)
    gen_nonce = nonce or f"prb-{token}"
    req_id = f"{PROBE_PREFIX}{binding_id}:{generation}:{token}"
    request = WebSolRequest(
        project_id=project_id,
        request_id=req_id,
        task_id="probe",
        stage_id="health",
        branch="main",
        head="0" * 40,
        role=WebSolRole.REVIEWER,
        event=WebSolEvent.WORKER_DONE,
        nonce=gen_nonce,
    )
    prompt = (
        f"[DEVORCH_WEB_SOL_REQUEST {req_id}]\n"
        f'{{"project_id": "{project_id}", "nonce": "{gen_nonce}", "type": "probe"}}\n'
        f"Web Sol availability probe.\n"
        f"Reply with [DEVORCH_WEB_SOL_RESPONSE {req_id}] {gen_nonce} ok\n"
    )
    return request, prompt


def run_websol_probe(
    bridge_store: BrowserBridgeStore,
    adapter: str,
    binding_id: str,
    *,
    generation: int = 1,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    simulated_responder: Optional[Callable[[WebSolRequest, str], None]] = None,
    now: Optional[datetime] = None,
    poll_interval_seconds: float = 0.1,
) -> ProbeResult:
    """Execute a bounded real probe covering submit -> claim -> response -> discard.

    Classifies:
    - not_bound: binding has no live adapter presence
    - no_claim: request was submitted but never claimed within timeout
    - response_timeout: request was claimed but response was not received within timeout
    - malformed_response: response received but missing exact marker or nonce
    - transport_failure: submit or query failed
    """
    start_time = time.monotonic()
    moment = now or utc_now()

    # Check initial presence
    try:
        status = bridge_store.binding_status(adapter, binding_id, now=moment)
        if status.get("state") != "bound":
            return ProbeResult(
                success=False,
                error_class="not_bound",
                reason="conversation has no live browser presence",
                duration_seconds=time.monotonic() - start_time,
            )
    except Exception as exc:
        return ProbeResult(
            success=False,
            error_class="transport_failure",
            reason=f"failed checking binding status: {exc}",
            duration_seconds=time.monotonic() - start_time,
        )

    # Submit probe request
    request, prompt = make_probe_request(binding_id, generation)
    try:
        bridge_store.submit(adapter, binding_id, request, prompt, now=moment)
    except Exception as exc:
        return ProbeResult(
            success=False,
            error_class="transport_failure",
            reason=f"failed submitting probe request: {exc}",
            request_id=request.request_id,
            nonce=request.nonce,
            duration_seconds=time.monotonic() - start_time,
        )

    # If a simulated responder is provided (e.g. for deterministic testing), invoke it
    if simulated_responder is not None:
        try:
            simulated_responder(request, prompt)
        except Exception as exc:
            bridge_store.discard_probe(adapter, binding_id, request.request_id, request.nonce)
            return ProbeResult(
                success=False,
                error_class="transport_failure",
                reason=f"simulated responder error: {exc}",
                request_id=request.request_id,
                nonce=request.nonce,
                duration_seconds=time.monotonic() - start_time,
            )

    # Poll for response or claim
    claimed = False
    deadline = start_time + max(0.5, float(timeout_seconds))

    while time.monotonic() < deadline:
        # Check if response arrived
        resp = bridge_store.get_response(request.request_id, request.nonce)
        if resp is not None:
            # Verify marker and nonce
            marker = f"[DEVORCH_WEB_SOL_RESPONSE {request.request_id}]"
            text = resp.response_text or ""
            if marker in text and request.nonce in text:
                # Successfully verified! Discard probe record so it doesn't clutter queue
                bridge_store.discard_probe(adapter, binding_id, request.request_id, request.nonce)
                return ProbeResult(
                    success=True,
                    reason="probe_response_verified",
                    request_id=request.request_id,
                    nonce=request.nonce,
                    duration_seconds=time.monotonic() - start_time,
                    details={"response_text": text},
                )
            else:
                bridge_store.discard_probe(adapter, binding_id, request.request_id, request.nonce)
                return ProbeResult(
                    success=False,
                    error_class="malformed_response",
                    reason="response missing exact marker or nonce",
                    request_id=request.request_id,
                    nonce=request.nonce,
                    duration_seconds=time.monotonic() - start_time,
                    details={"response_text": text},
                )

        # Check claim state
        with bridge_store._lock:
            queue = bridge_store._load_queue(adapter, binding_id)
            rec = queue.get(request.request_id)
            if rec and rec.get("state") == "claimed":
                claimed = True

        time.sleep(poll_interval_seconds)

    # Timed out
    bridge_store.discard_probe(adapter, binding_id, request.request_id, request.nonce)
    error_class = "response_timeout" if claimed else "no_claim"
    reason = "claimed but timed out awaiting assistant response" if claimed else "probe request was never claimed"
    return ProbeResult(
        success=False,
        error_class=error_class,
        reason=reason,
        request_id=request.request_id,
        nonce=request.nonce,
        duration_seconds=time.monotonic() - start_time,
    )
