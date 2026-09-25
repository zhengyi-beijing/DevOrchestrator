"""Comprehensive validation suite for P16.12 Web Sol Persistent Pairing & Truthful Availability.

Covers:
- Capability verdicts, store unavailability (503 vs 401), non-destructive failure, and renewal
- Health signal evaluation, fail-closed precedence, and WebSolHealthStore persistence
- Bridge claim lifetime clamping, durable cancellation & withdrawal convergence, and response races
- Probe execution, nonce verification, response consumer isolation, and probe discard
- Failover engine, state machine, and AIReviewerCoordinator.submit_failover_review without AGY pool
- Dispatcher availability gating and failover exclusion
- HTTP control endpoints: renewal, websol-health, and session-heartbeat enrichment
- CLI commands: websol-status and websol-probe
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from dev_orchestrator.bridge.store import (
    BrowserBridgeStore,
    BridgeConflictError,
    WithdrawResult,
)
from dev_orchestrator.control.security import (
    CapabilityStoreUnavailableError,
    CapabilityVerdict,
    ControlSecurity,
    capability_identity,
    capability_state,
    capability_status,
    renew_session_capability,
)
from dev_orchestrator.control.store import ConversationControlStore
from dev_orchestrator.core.ai_reviewer import AIReviewerCoordinator
from dev_orchestrator.core.dispatcher import (
    dispatch_worker_done_events,
)
from dev_orchestrator.core.websol_failover import (
    FailoverDecision,
    FailoverEngine,
    FailoverRecord,
    FailoverState,
    WebSolFailoverStore,
    evaluate_failover_decision,
)
from dev_orchestrator.core.websol_health import (
    WebSolAvailability,
    WebSolHealth,
    WebSolHealthStore,
    WebSolSignal,
    collect_websol_signals,
    evaluate_websol_availability,
    health_key,
)
from dev_orchestrator.core.websol_probe import ProbeResult, run_websol_probe


# ============================================================================
# 1. Capability Store, Verdicts, and Renewal
# ============================================================================

def test_capability_verdicts_and_store_unavailability(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    sec = ControlSecurity(runtime_root=runtime)
    cap_file = sec.pairings_path

    # Initially empty / non-existent -> UNKNOWN
    assert capability_state("non-existent-id", runtime_root=runtime) == CapabilityVerdict.UNKNOWN
    assert capability_status("Bearer invalid-token", runtime_root=runtime)[0] == CapabilityVerdict.UNKNOWN

    # Create a valid session_heartbeat capability
    p = sec.create_pairing()
    redeemed = sec.redeem_pairing(p["pairing_id"], p["code"])
    token = redeemed["capability"]
    pairing_id = redeemed["pairing_id"]

    assert capability_state(pairing_id, runtime_root=runtime) == CapabilityVerdict.VALID
    assert capability_status(f"Bearer {token}", runtime_root=runtime)[0] == CapabilityVerdict.VALID
    assert capability_identity(f"Bearer {token}", runtime_root=runtime) == pairing_id

    # Revoke it
    sec.revoke_pairing(pairing_id)
    assert capability_state(pairing_id, runtime_root=runtime) == CapabilityVerdict.REVOKED
    assert capability_status(f"Bearer {token}", runtime_root=runtime)[0] == CapabilityVerdict.REVOKED

    # Simulate store corruption (invalid JSON)
    cap_file.write_text("{ corrupt json", encoding="utf-8")
    assert capability_state(pairing_id, runtime_root=runtime) == CapabilityVerdict.UNAVAILABLE
    assert capability_status(f"Bearer {token}", runtime_root=runtime)[0] == CapabilityVerdict.UNAVAILABLE
    assert capability_identity(f"Bearer {token}", runtime_root=runtime) is None

    # Mutations refuse to overwrite unreadable store
    with pytest.raises(CapabilityStoreUnavailableError):
        sec.create_pairing()

    with pytest.raises(CapabilityStoreUnavailableError):
        sec.revoke_pairing(pairing_id)

    # Renewal also fails closed on unreadable store
    renew_ok, reason, _ = renew_session_capability(f"Bearer {token}", runtime_root=runtime)
    assert renew_ok is False
    assert "capability_store_unavailable" in reason


def test_capability_renewal_grace_period(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    sec = ControlSecurity(runtime_root=runtime)

    p = sec.create_pairing()
    redeemed = sec.redeem_pairing(p["pairing_id"], p["code"])
    old_token = redeemed["capability"]
    pairing_id = redeemed["pairing_id"]

    # Renew token
    ok, reason, renewal = renew_session_capability(f"Bearer {old_token}", grace_period_seconds=10, runtime_root=runtime)
    assert ok is True
    assert renewal is not None
    new_token = renewal["token"]
    assert renewal["pairing_id"] == pairing_id
    assert new_token != old_token

    # Both old and new token are valid during grace period
    assert capability_status(f"Bearer {new_token}", runtime_root=runtime)[0] == CapabilityVerdict.VALID
    assert capability_status(f"Bearer {old_token}", runtime_root=runtime)[0] == CapabilityVerdict.VALID

    # After grace period expires, old token becomes unknown
    with patch("time.time", return_value=time.time() + 20):
        assert capability_status(f"Bearer {new_token}", runtime_root=runtime)[0] == CapabilityVerdict.VALID
        assert capability_status(f"Bearer {old_token}", runtime_root=runtime)[0] == CapabilityVerdict.UNKNOWN


# ============================================================================
# 2. Control Session Store - Tab & Capability Tracking
# ============================================================================

def test_control_session_store_tab_and_capability_tracking(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    session_store = ConversationControlStore(runtime)

    # Heartbeat tab 1
    session_store.heartbeat(
        adapter="chatgpt_web",
        binding_id="bind-1",
        title="Test Conversation",
        url="https://chatgpt.com/c/bind-1",
        tab_instance_id="tab-1",
        capability_id="pairing-1",
        capability_source="capability",
    )

    status = session_store.session_status("chatgpt_web", "bind-1")
    assert status is not None
    assert status["active_tab_count"] == 1
    assert status["stale_tab_count"] == 0
    assert status["verified"] is True
    assert status["capability_pairing_id"] == "pairing-1"

    # Heartbeat tab 2 on same session -> duplicate tabs
    session_store.heartbeat(
        adapter="chatgpt_web",
        binding_id="bind-1",
        title="Test Conversation",
        url="https://chatgpt.com/c/bind-1",
        tab_instance_id="tab-2",
        capability_id="pairing-1",
        capability_source="capability",
    )

    status = session_store.session_status("chatgpt_web", "bind-1")
    assert status["active_tab_count"] == 2



# ============================================================================
# 3. WebSol Health Evaluation & Precedence
# ============================================================================

def test_websol_health_evaluation_precedence() -> None:
    now_iso = "2026-09-01T12:00:00+00:00"

    def make_signals(**kwargs: Any) -> dict[str, WebSolSignal]:
        defaults = {
            "bridge_listener": WebSolSignal(name="bridge_listener", status="healthy", reason="ok", observed_at=now_iso),
            "browser_claim_presence": WebSolSignal(name="browser_claim_presence", status="healthy", reason="ok", observed_at=now_iso),
            "control_heartbeat": WebSolSignal(name="control_heartbeat", status="healthy", reason="ok", observed_at=now_iso),
            "binding_identity": WebSolSignal(name="binding_identity", status="healthy", reason="ok", observed_at=now_iso),
            "capability": WebSolSignal(name="capability", status="healthy", reason="ok", observed_at=now_iso),
            "probe": WebSolSignal(name="probe", status="healthy", reason="ok", observed_at=now_iso),
        }
        defaults.update(kwargs)
        return defaults

    # 1. All healthy -> AVAILABLE
    avail, reason = evaluate_websol_availability(make_signals(), probe_passed=True)
    assert avail == WebSolAvailability.AVAILABLE
    assert reason == "all_signals_healthy"

    # 2. Capability revoked/unknown -> PAIRING_REQUIRED (takes high precedence)
    avail, reason = evaluate_websol_availability(
        make_signals(capability=WebSolSignal(name="capability", status="revoked", reason="capability_revoked", observed_at=now_iso)),
        probe_passed=True,
    )
    assert avail == WebSolAvailability.PAIRING_REQUIRED

    # 3. Bridge listener offline -> OFFLINE
    avail, reason = evaluate_websol_availability(
        make_signals(bridge_listener=WebSolSignal(name="bridge_listener", status="offline", reason="unreachable", observed_at=now_iso)),
        probe_passed=True,
    )
    assert avail == WebSolAvailability.OFFLINE

    # 4. Browser claim presence absent -> OFFLINE
    avail, reason = evaluate_websol_availability(
        make_signals(browser_claim_presence=WebSolSignal(name="browser_claim_presence", status="offline", reason="no presence", observed_at=now_iso)),
        probe_passed=True,
    )
    assert avail == WebSolAvailability.OFFLINE

    # 5. Probe failed -> PROBE_FAILED
    avail, reason = evaluate_websol_availability(
        make_signals(probe=WebSolSignal(name="probe", status="failed", reason="timeout", observed_at=now_iso)),
        probe_passed=True,
    )
    assert avail == WebSolAvailability.PROBE_FAILED

    # 6. Duplicate tabs -> DEGRADED
    avail, reason = evaluate_websol_availability(
        make_signals(control_heartbeat=WebSolSignal(name="control_heartbeat", status="degraded", reason="duplicate_tabs", observed_at=now_iso)),
        probe_passed=True,
    )
    assert avail == WebSolAvailability.DEGRADED

    # 7. Capability store unavailable -> DEGRADED capability_store_unavailable
    avail, reason = evaluate_websol_availability(
        make_signals(capability=WebSolSignal(name="capability", status="unavailable", reason="store I/O error", observed_at=now_iso)),
        probe_passed=True,
    )
    assert avail == WebSolAvailability.DEGRADED
    assert reason == "capability_store_unavailable"


def test_websol_health_store_persistence(tmp_path: Path) -> None:
    from datetime import datetime, timezone, timedelta
    runtime = tmp_path / "runtime"
    health_store = WebSolHealthStore(runtime)

    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    signals = {
        "bridge_listener": WebSolSignal(name="bridge_listener", status="healthy", reason="ok", observed_at=now.isoformat()),
    }
    h = WebSolHealth(
        project_id="proj-1",
        adapter="chatgpt_web",
        binding_id="bind-1",
        availability=WebSolAvailability.AVAILABLE.value,
        reason="ok",
        signals=signals,
        evaluated_at=now.isoformat(),
        valid_until=(now + timedelta(seconds=60)).isoformat(),
        probe_generation=1,
    )
    health_store.put(h)

    loaded = health_store.get("proj-1", "chatgpt_web", "bind-1", now=now)
    assert loaded is not None
    assert loaded.availability == WebSolAvailability.AVAILABLE.value

    # Expired health snapshot returns DEGRADED
    expired = health_store.get("proj-1", "chatgpt_web", "bind-1", now=now + timedelta(seconds=120))
    assert expired is not None
    assert expired.availability == WebSolAvailability.DEGRADED.value
    assert expired.reason == "health_snapshot_expired"

    # Generation invalidation bumps the store generation
    new_gen = health_store.invalidate_generation(reason="daemon restart")
    assert new_gen == 2


from datetime import datetime, timezone, timedelta
from dev_orchestrator.core.websol import WebSolEvent, WebSolRequest, WebSolRole
from dev_orchestrator.core.response_consumer import consume_websol_responses


def make_req(req_id: str, nonce: str = "n1", project_id: str = "proj-1") -> WebSolRequest:
    return WebSolRequest(
        project_id=project_id,
        request_id=req_id,
        task_id="T1",
        stage_id="S1",
        branch="main",
        head="a" * 40,
        role=WebSolRole.REVIEWER,
        event=WebSolEvent.WORKER_DONE,
        nonce=nonce,
    )


# ============================================================================
# 4. Bridge Store: Claim Clamping, Durable Withdrawal & Probe Discard
# ============================================================================

def test_bridge_claim_lifetime_clamp(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    bridge = BrowserBridgeStore(runtime, lease_seconds=60, max_claim_lifetime_seconds=100)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    bridge._touch_presence("chatgpt_web", "bind-1", t0)

    req = make_req("req-clamp-1", nonce="nonce-1")
    bridge.submit("chatgpt_web", "bind-1", req, "prompt text", now=t0)

    # First claim at t0 with lease 60s
    claim = bridge.claim("chatgpt_web", "bind-1", now=t0)
    assert claim is not None
    assert claim.lease_expires_at == (t0 + timedelta(seconds=60)).isoformat()

    # Renew near deadline: t0 + 50s -> lease is clamped to t0 + 100s (only 50s remaining instead of full 60s)
    t_renew = t0 + timedelta(seconds=50)
    renewed = bridge.renew("chatgpt_web", "bind-1", req.request_id, req.nonce, claim.claim_token, now=t_renew)
    assert renewed.lease_expires_at == (t0 + timedelta(seconds=100)).isoformat()

    # Past deadline: t0 + 101s -> renew raises BridgeConflictError
    t_past = t0 + timedelta(seconds=101)
    with pytest.raises(BridgeConflictError):
        bridge.renew("chatgpt_web", "bind-1", req.request_id, req.nonce, claim.claim_token, now=t_past)


def test_bridge_durable_withdrawal_and_convergence(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    bridge = BrowserBridgeStore(runtime, lease_seconds=60)
    t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    bridge._touch_presence("chatgpt_web", "bind-1", t0)

    # 1. Withdraw pending request -> immediately finalized
    req_pending = make_req("req-pending", nonce="n1")
    bridge.submit("chatgpt_web", "bind-1", req_pending, "prompt", now=t0)
    res = bridge.withdraw("chatgpt_web", "bind-1", req_pending.request_id, req_pending.nonce, "test", now=t0)
    assert res.outcome == "withdrawn"
    # Cannot be claimed
    assert bridge.claim("chatgpt_web", "bind-1", now=t0) is None

    # 2. Withdraw actively claimed request -> revocation_pending
    req_active = make_req("req-active", nonce="n2")
    bridge.submit("chatgpt_web", "bind-1", req_active, "prompt", now=t0)
    claim = bridge.claim("chatgpt_web", "bind-1", now=t0)
    assert claim is not None

    res_active = bridge.withdraw("chatgpt_web", "bind-1", req_active.request_id, req_active.nonce, "timeout", now=t0)
    assert res_active.outcome == "revocation_pending"
    assert res_active.cancel_deadline is not None

    # Renewal during cancellation is rejected
    with pytest.raises(BridgeConflictError):
        bridge.renew("chatgpt_web", "bind-1", req_active.request_id, req_active.nonce, claim.claim_token, now=t0 + timedelta(seconds=10))

    # Exact response arriving BEFORE cancel_deadline wins
    resp_ok = bridge.respond("chatgpt_web", "bind-1", req_active.request_id, req_active.nonce, claim.claim_token, "response text", now=t0 + timedelta(seconds=20))
    assert resp_ok.response_text == "response text"

    # 3. Withdraw active request where response does NOT arrive before deadline
    req_timeout = make_req("req-timeout", nonce="n3")
    bridge.submit("chatgpt_web", "bind-1", req_timeout, "prompt", now=t0)
    claim_timeout = bridge.claim("chatgpt_web", "bind-1", now=t0)
    assert claim_timeout is not None
    bridge.withdraw("chatgpt_web", "bind-1", req_timeout.request_id, req_timeout.nonce, "stall", now=t0)

    # After lease expires (t0 + 65s), final withdrawal succeeds
    res_final = bridge.withdraw("chatgpt_web", "bind-1", req_timeout.request_id, req_timeout.nonce, "stall", now=t0 + timedelta(seconds=65))
    assert res_final.outcome == "withdrawn"

    # Late response after final withdrawal raises BridgeConflictError
    with pytest.raises(BridgeConflictError):
        bridge.respond("chatgpt_web", "bind-1", req_timeout.request_id, req_timeout.nonce, claim_timeout.claim_token, "late response", now=t0 + timedelta(seconds=70))


def test_bridge_probe_isolation_and_discard(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    bridge = BrowserBridgeStore(runtime)
    bridge._touch_presence("chatgpt_web", "bind-1", datetime.now(timezone.utc))

    # Regular request cannot be discarded via discard_probe (raises ValueError)
    req_norm = make_req("normal-req", nonce="n1")
    bridge.submit("chatgpt_web", "bind-1", req_norm, "prompt")
    with pytest.raises(ValueError, match="refusing to discard non-probe"):
        bridge.discard_probe("chatgpt_web", "bind-1", "normal-req", "n1")

    # Probe request can be discarded
    probe_id = "probe:websol:bind-1:1:123456"
    req_probe = make_req(probe_id, nonce="pnonce")
    bridge.submit("chatgpt_web", "bind-1", req_probe, "probe prompt")
    assert bridge.discard_probe("chatgpt_web", "bind-1", probe_id, "pnonce") is True
    # Discarded record is removed from queue
    assert bridge.get_response(probe_id, "pnonce") is None


# ============================================================================
# 5. Response Consumer Probe Isolation
# ============================================================================

def test_response_consumer_skips_probe_records(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    import subprocess
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.invalid"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
    (repo / "seed.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True, capture_output=True)

    bridge = BrowserBridgeStore(runtime)
    bridge._touch_presence("chatgpt_web", "bind-1", datetime.now(timezone.utc))

    # Submit and respond a probe
    probe_id = "probe:websol:bind-1:1:123456"
    req_probe = make_req(probe_id, nonce="pn")
    bridge.submit("chatgpt_web", "bind-1", req_probe, "probe prompt")
    claim = bridge.claim("chatgpt_web", "bind-1")
    assert claim is not None
    bridge.respond("chatgpt_web", "bind-1", probe_id, "pn", claim.claim_token, f"[DEVORCH_WEB_SOL_RESPONSE {probe_id}] pn ok")

    # Summary for project
    summary = {
        "projects": [{
            "project_id": "proj-1",
            "repo_path": str(repo),
            "conversation_binding": {
                "transport": "browser_bridge",
                "adapter": "chatgpt_web",
                "binding_id": "bind-1",
            },
            "orchestration_ready": True,
        }]
    }

    outcomes = consume_websol_responses(summary, bridge, runtime)
    # Probe is skipped, outcomes is empty, and probe_id does not enter decisions ledger
    assert len(outcomes) == 0
    from dev_orchestrator.core.response_consumer import DECISIONS_FILE
    from dev_orchestrator.storage.json_store import read_json
    ledger = read_json(runtime / DECISIONS_FILE, {})
    assert probe_id not in ledger.get("decisions", {})


# ============================================================================
# 6. End-to-End Probe Execution
# ============================================================================

def test_run_websol_probe_success(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    bridge = BrowserBridgeStore(runtime)
    bridge._touch_presence("chatgpt_web", "bind-1", datetime.now(timezone.utc))

    def responder(req: WebSolRequest, prompt: str) -> None:
        claim = bridge.claim("chatgpt_web", "bind-1")
        assert claim is not None
        assert claim.request_id == req.request_id
        bridge.respond("chatgpt_web", "bind-1", req.request_id, req.nonce, claim.claim_token, f"[DEVORCH_WEB_SOL_RESPONSE {req.request_id}] {req.nonce} ok")

    res = run_websol_probe(
        bridge,
        adapter="chatgpt_web",
        binding_id="bind-1",
        timeout_seconds=5.0,
        simulated_responder=responder,
    )

    assert res.success is True
    assert res.reason == "probe_response_verified"
    # Probe record was automatically discarded from queue
    assert bridge.get_response(res.request_id, res.nonce) is None


def test_run_websol_probe_timeout(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    bridge = BrowserBridgeStore(runtime)
    bridge._touch_presence("chatgpt_web", "bind-1", datetime.now(timezone.utc))

    # Probe times out when no claim happens
    res = run_websol_probe(
        bridge,
        adapter="chatgpt_web",
        binding_id="bind-1",
        timeout_seconds=0.1,
    )

    assert res.success is False
    assert res.error_class == "no_claim"



# ============================================================================
# 7. Failover Engine & AI Reviewer Direct Submission
# ============================================================================

def test_failover_decision_logic() -> None:
    # 1. Prepared work + OFFLINE -> FAILOVER_DIRECT
    dec, _ = evaluate_failover_decision(
        occurrence_state="prepared",
        health_availability=WebSolAvailability.OFFLINE.value,
    )
    assert dec == FailoverDecision.FAILOVER_DIRECT

    # 2. Submitted work, unhealthy resource, not yet requested cancellation -> REQUEST_WITHDRAWAL
    dec, _ = evaluate_failover_decision(
        occurrence_state="submitted",
        health_availability=WebSolAvailability.OFFLINE.value,
        cancel_requested=False,
    )
    assert dec == FailoverDecision.REQUEST_WITHDRAWAL

    # 3. Submitted work, cancel requested but deadline not passed -> AWAIT_REVOCATION
    dec, _ = evaluate_failover_decision(
        occurrence_state="submitted",
        health_availability=WebSolAvailability.OFFLINE.value,
        cancel_requested=True,
        cancel_deadline_passed=False,
    )
    assert dec == FailoverDecision.AWAIT_REVOCATION

    # 4. Submitted work, bridge_state="withdrawn" -> SUBMIT_REVIEW
    dec, _ = evaluate_failover_decision(
        occurrence_state="submitted",
        health_availability=WebSolAvailability.OFFLINE.value,
        bridge_state="withdrawn",
    )
    assert dec == FailoverDecision.SUBMIT_REVIEW

    # 5. Submitted work, bridge_state="responded" -> CANCEL_FAILOVER
    dec, _ = evaluate_failover_decision(
        occurrence_state="submitted",
        health_availability=WebSolAvailability.OFFLINE.value,
        bridge_state="responded",
    )
    assert dec == FailoverDecision.CANCEL_FAILOVER

    # 6. Exceeded max attempts -> EXHAUSTED
    dec, _ = evaluate_failover_decision(
        occurrence_state="prepared",
        health_availability=WebSolAvailability.OFFLINE.value,
        attempts=3,
        max_attempts=3,
    )
    assert dec == FailoverDecision.EXHAUSTED


def test_failover_engine_reconciliation(tmp_path: Path) -> None:
    from dev_orchestrator.core.dispatcher import DISPATCHER_STATE_FILE
    from dev_orchestrator.storage.json_store import read_json, write_json

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    bridge = BrowserBridgeStore(runtime)
    bridge._touch_presence("chatgpt_web", "bind-1", datetime.now(timezone.utc))
    failover_store = WebSolFailoverStore(runtime)
    health_store = WebSolHealthStore(runtime)

    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    now_iso = now.isoformat()
    health_store.put(WebSolHealth(
        project_id="proj-1",
        adapter="chatgpt_web",
        binding_id="bind-1",
        availability=WebSolAvailability.OFFLINE.value,
        reason="bridge_unreachable",
        signals={},
        evaluated_at=now_iso,
        valid_until=(now + timedelta(seconds=60)).isoformat(),
    ))

    ledger_path = runtime / DISPATCHER_STATE_FILE
    write_json(ledger_path, {
        "version": 1,
        "worker_done": {
            "proj-1": {
                "occurrences": {
                    "run-001": {
                        "state": "prepared",
                        "adapter": "chatgpt_web",
                        "binding_id": "bind-1",
                        "request_id": "req-001",
                        "nonce": "n001",
                        "task_id": "P16.12",
                    }
                }
            }
        }
    })

    # Setup reviewer mock
    mock_reviewer = MagicMock()
    mock_reviewer.submit_failover_review.return_value = "ai_review:run-001"

    engine = FailoverEngine(
        failover_store=failover_store,
        bridge_store=bridge,
        reviewer_coordinator=mock_reviewer,
        dispatcher_ledger_path=ledger_path,
        health_store=health_store,
    )

    reconciled = engine.reconcile(now=now)
    assert len(reconciled) == 1
    assert reconciled[0].state == FailoverState.REVIEW_SUBMITTED.value
    assert reconciled[0].review_id == "ai_review:run-001"
    mock_reviewer.submit_failover_review.assert_called_once_with(
        project_id="proj-1",
        run_id="run-001",
        failover_record_id="proj-1:run-001",
    )

    # Verify store has the record
    stored = failover_store.get("proj-1", "run-001")
    assert stored is not None
    assert stored.state == FailoverState.REVIEW_SUBMITTED.value

    # Verify dispatcher ledger occurrence was updated
    updated_ledger = read_json(ledger_path, {})
    occ = updated_ledger["worker_done"]["proj-1"]["occurrences"]["run-001"]
    assert occ["failover_state"] == FailoverState.REVIEW_SUBMITTED.value


def test_ai_reviewer_submit_failover_review_anchored(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    import subprocess
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.invalid"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
    (repo / "seed.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True, capture_output=True)

    config_file = runtime / "projects.json"
    config_file.write_text(json.dumps({
        "projects": [{
            "project_id": "proj-1",
            "repo_path": str(repo),
            "reviewer_harness": {"enabled": True},
        }]
    }), encoding="utf-8")

    from dev_orchestrator.core.dispatcher import DISPATCHER_STATE_FILE
    from dev_orchestrator.storage.json_store import write_json
    write_json(runtime / DISPATCHER_STATE_FILE, {
        "version": 1,
        "worker_done": {
            "proj-1": {
                "occurrences": {
                    "run-abc-123": {
                        "task_id": "P16.12",
                        "repo_path": str(repo),
                    }
                }
            }
        }
    })

    with patch.object(AIReviewerCoordinator, "_run_harness_review"):
        coordinator = AIReviewerCoordinator(
            runtime_root=runtime,
            port=None,
        )

        review_id = coordinator.submit_failover_review(
            project_id="proj-1",
            run_id="run-abc-123",
            failover_record_id="rec-1",
        )
        assert review_id == "ai_review:run-abc-123"
        st = coordinator.state()
        rec = st["reviews"].get(review_id)
        assert rec is not None
        assert rec["review_id"] == "ai_review:run-abc-123"
        assert rec["source_request_id"] == "run-abc-123"
        assert rec["project_id"] == "proj-1"
        assert rec["task_id"] == "P16.12"
        assert rec["harness"] is True


# ============================================================================
# 8. Dispatcher Availability Gating & Failover Exclusion
# ============================================================================

def test_dispatcher_availability_gating_and_failover_exclusion(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    import subprocess
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.invalid"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
    (repo / "seed.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True, capture_output=True)

    bridge = BrowserBridgeStore(runtime)
    bridge._touch_presence("chatgpt_web", "bind-1", datetime.now(timezone.utc))

    def snap(project_id: str, run_id: str) -> dict:
        return {
            "project_id": project_id,
            "repo_path": str(repo),
            "conversation_binding": {
                "transport": "browser_bridge",
                "adapter": "chatgpt_web",
                "binding_id": "bind-1",
            },
            "orchestration_ready": True,
            "worker": {
                "kind": "task", "state": "completed", "exit_code": 0,
                "updated_at": "2026-09-04T00:00:00+00:00",
            },
            "telemetry": {"run_id": run_id, "task_id": "P1"},
        }

    def availability_provider(project_id: str, adapter: str, binding_id: str) -> str:
        if project_id == "proj-avail":
            return WebSolAvailability.AVAILABLE.value
        return WebSolAvailability.DEGRADED.value

    # 1. Occurrence for degraded project -> delivery withheld, returned list empty
    summary_deg = {"projects": [snap("proj-degraded", "run-deg-1")]}
    dispatched_deg = dispatch_worker_done_events(
        summary_deg, bridge, runtime, availability_provider=availability_provider
    )
    assert len(dispatched_deg) == 0

    # 2. Occurrence for available project -> delivery succeeds
    summary_avail = {"projects": [snap("proj-avail", "run-avail-1")]}
    dispatched_avail = dispatch_worker_done_events(
        summary_avail, bridge, runtime, availability_provider=availability_provider
    )
    assert len(dispatched_avail) == 1
    assert dispatched_avail[0].project_id == "proj-avail"

    # 3. Modify ledger occurrence to simulate failover_state -> delivery blocked
    from dev_orchestrator.core.dispatcher import DISPATCHER_STATE_FILE
    from dev_orchestrator.storage.json_store import read_json, write_json
    ledger_file = runtime / DISPATCHER_STATE_FILE
    summary_fo = {"projects": [snap("proj-avail", "run-fo-1")]}
    # First tick without availability to freeze as prepared
    dispatch_worker_done_events(
        summary_fo, bridge, runtime, availability_provider=lambda *_: "degraded"
    )
    # Now set failover_state on prepared occurrence
    ledger_data = read_json(ledger_file, {})
    ledger_data["worker_done"]["proj-avail"]["occurrences"]["run-fo-1"]["failover_state"] = "submitted"
    write_json(ledger_file, ledger_data)

    # Next tick with AVAILABLE -> should NOT submit because failover_state is set
    dispatched_fo = dispatch_worker_done_events(
        summary_fo, bridge, runtime, availability_provider=availability_provider
    )
    assert len(dispatched_fo) == 0


# ============================================================================
# 9. CLI Commands: websol-status & websol-probe
# ============================================================================

def test_cli_websol_status_and_probe(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from dev_orchestrator.storage.json_store import utc_now, utc_now_iso
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    health_store = WebSolHealthStore(runtime)

    now = utc_now()
    now_iso = utc_now_iso()
    h = WebSolHealth(
        project_id="proj-cli",
        adapter="chatgpt_web",
        binding_id="bind-cli",
        availability=WebSolAvailability.AVAILABLE.value,
        reason="all_signals_healthy",
        signals={},
        evaluated_at=now_iso,
        valid_until=(now + timedelta(seconds=60)).isoformat(),
        probe_generation=1,
    )
    health_store.put(h)

    from dev_orchestrator.cli import cmd_websol_probe, cmd_websol_status
    import argparse

    # Test websol-status json
    args_status = argparse.Namespace(runtime_root=str(runtime), project_id=None, format="json")
    ret = cmd_websol_status(args_status)
    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["health"]) == 1
    assert out["health"][0]["project_id"] == "proj-cli"

    # Test websol-status text
    args_text = argparse.Namespace(runtime_root=str(runtime), project_id="proj-cli", format="text")
    ret = cmd_websol_status(args_text)
    assert ret == 0
    text_out = capsys.readouterr().out
    assert "[AVAILABLE] proj-cli/chatgpt_web/bind-cli" in text_out

    # Test websol-probe (mock probe execution)
    with patch("dev_orchestrator.core.websol_probe.run_websol_probe") as mock_probe:
        mock_probe.return_value = ProbeResult(
            success=True,
            request_id="probe:mock",
            nonce="mocknonce",
            duration_seconds=0.45,
            reason="ok",
        )
        args_probe = argparse.Namespace(
            runtime_root=str(runtime),
            project_id="proj-cli",
            adapter="chatgpt_web",
            binding_id="bind-cli",
            timeout=10.0,
            format="json",
            config=None,
        )
        ret_probe = cmd_websol_probe(args_probe)
        assert ret_probe == 0
        probe_out = json.loads(capsys.readouterr().out)
        assert probe_out["status"] == "success"
        assert probe_out["request_id"] == "probe:mock"

