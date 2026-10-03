# P19.3 Handoff — Read-only Browser Control Bridge

Last updated: 2026-10-03.
Branch: `main`.
Initial HEAD: `d17dd3e478703377fc15821ae61f0eacb8a1ea6c`.
Push status: do not push.

## Current verdict

- Design complete: **yes**.
- Read-only implementation complete: **yes**.
- Automated unit/integration/regression verification complete: **yes**.
- Real ChatGPT Web same-conversation E2E complete: **yes — live-proven with `e2e-007`**.
- RDC replacement proven for read-only DevO status: **yes — browser transport round trip, multi-tab isolation, and daemon-restart recovery are live-proven**.
- Mutation workflows: **not implemented by this slice**.

These verdicts are based on real ChatGPT Web same-conversation browser round trips, not only HTTP acceptance or unit tests.

## P19.3 live browser closeout (2026-10-03)

- Userscript `0.1.26` is installed in Chrome Profile 4. The accepted `e2e-007`/`e2e-008` browser round trips were captured on `0.1.25`; `0.1.26` is the post-acceptance final-review remediation.
- `req-20261003-p193-e2e-007` completed a real same-conversation `DEVORCH_ACTION_V1 -> userscript -> 8765 -> 8770 -> userscript -> composer -> user turn` round trip with `status=success`, branch `main`, HEAD `d17dd3e478703377fc15821ae61f0eacb8a1ea6c`, project state `READY_TO_RUN`, `current_task=P19.2`, and `active_execution=null`.
- Multi-tab isolation was live-proven with two active tabs: the visible bound tab dispatched exactly once and confirmed exactly one result submission; the other active tab dispatched zero actions.
- Daemon restart recovery was live-proven: daemon PID `11444` restarted as PID `27944`; the durable binding required no repair, heartbeat returned to `live` automatically, and `req-20261003-p193-e2e-008` completed another successful same-conversation round trip.
- Browser reliability fixes through `0.1.26`: stable per-request retry state, shared Tampermonkey-private pending-result state across duplicate tabs, preservation of the first successful result while Send is unavailable, safe local composer Send fallback with an explicit Stop guard, visible-tab-only action consumption, and roleless composer self-injection detection through the unified `findComposer()` helper. Final review also moved `issued_at` out of the authoritative WebBridge semantic idempotency hash while retaining freshness validation on every attempt.
- Final related regression after the review remediation: **109 passed in 23.59 s** across P19.3, ChatGPT adapter, P16.12 pairing/availability, and P13 WebBridge tests; `node --check` and `git diff --check` passed (line-ending warnings only). A live runtime idempotency probe through 8765 returned `200` then `200` for the same request ID with different fresh `issued_at` values, identical responses, and no conflict.
- P19.3 is complete for the defined **read-only** scope. Mutation/browser effectful workflows remain outside this slice.

## P19.3 bootstrap/expiry remediation (2026-10-01)

Root causes:

- `capability_state()` checked only existence/revocation, so a capability row
  could remain reported `VALID` after its bearer had expired.
- Read-only control pairing disclosed a freshly minted token for manual paste;
  there was no owner-created, browser-claimable local permit.
- An intermediate bootstrap implementation created permits from normal
  heartbeats, which was ambient authorization rather than an explicit owner
  action. That behavior was removed.

Implemented protocol:

1. The local owner runs `create-web-bridge-bootstrap` for the exact live durable
   `project_id + chatgpt_web + binding_id` tuple.
2. `ControlSecurity` persists one non-secret pending permit for at most 60
   seconds. The CLI prints metadata only—never a ticket secret or bearer.
3. Heartbeat reports only whether that exact permit is pending; it never creates
   one. The project is resolved from the owner-controlled durable binding.
4. The userscript claims directly from loopback 8770 with its existing
   heartbeat capability. 8770 requires the exact verified live session and
   authoritative project binding, then consumes the permit and mints the
   existing scoped `web_bridge_control` bearer in one locked store mutation.
5. The bearer is written only to Tampermonkey storage. The manual **Pair
   DevOrchestrator read-only control** menu remains available as fallback.

Expired capability IDs now return the established non-authorizing
`CapabilityVerdict.UNKNOWN` (there is no `EXPIRED` enum member). Bearer
validation remains fail-closed and returns an explicit expiry reason.

Remediation delta files:

- `src/dev_orchestrator/control/security.py`
- `src/dev_orchestrator/web/server.py`
- `src/dev_orchestrator/cli.py`
- `browser/chatgpt-web-adapter.user.js`
- `tests_py/test_p19_3_browser_control.py`
- `tests_py/test_chatgpt_web_adapter.py`
- `tests_py/test_p1612_websol_pairing_and_availability.py`
- `docs/P19_3_BROWSER_CONTROL_DESIGN.md`
- `agent/HANDOFF.md`
- `agent/result.md`

Pre-existing dirty changes not authored by this remediation remain in
`agent/CURRENT.md`, `docs/P13_CONTROL_BRIDGE_CONTRACT.md`, both tracked P19.3
evidence files, `ops/p19_3_browser_control_acceptance.py`,
`src/dev_orchestrator/bridge/server.py`, `control/adapter.py`,
`control/browser_control.py`, `control/web_bridge.py`, and `daemon.py`. Several
remediation delta files were already dirty with earlier P19.3 work; that work
was preserved rather than reset or rewritten.

## What changed

The previous 8765 control route directly used `BrowserControlService` and a
project-only browser token. It exposed job/file operations and bypassed the
existing 8770 WebBridge conversation/capability boundary. It also did not send
the current ChatGPT conversation ID. That server-only flow was insufficient for
the requested P19.3 acceptance.

The remediated flow is:

```text
ChatGPT /c/<binding>
  -> userscript 0.1.25 (`get_project_status` only)
  -> 127.0.0.1:8765 strict translator
  -> 127.0.0.1:8770 `/api/v1/control/web-bridge/requests`
  -> bounded project/overview read projection
  -> userscript composer submission
  -> same `/c/<binding>` conversation
```

Port 8770 validates the exact project, adapter, binding, live session,
`web_bridge_control` capability, freshness, and durable request ID. Port 8765
contains no job, lifecycle, shell, filesystem, or generic URL dispatch.

## Runtime state and live-browser closeout

- Durable project binding remains `devorchestrator + chatgpt_web + 6ab3b7e4-d398-83eb-901a-e50d5975f7bf`.
- The bound Chrome conversation is live and successfully auto-redeems the
  owner-created short-lived bootstrap into private Tampermonkey control storage.
- Same-conversation read-only E2E passed with `e2e-007`.
- Two-active-tab isolation passed: one visible bound tab consumed the request,
  the other active tab consumed zero actions.
- Daemon restart recovery passed: PID `11444` restarted as PID `27944`, the
  heartbeat automatically returned the binding to `live`, and `e2e-008`
  succeeded without rebinding or manual re-pairing.
- No further owner/browser setup is required for the completed read-only scope.

## Verification already completed

- Final required P19.3 three-file suite after live-browser reliability fixes:
  **97 passed in 19.64 seconds**:
  `python -m pytest tests_py/test_p19_3_browser_control.py tests_py/test_chatgpt_web_adapter.py tests_py/test_p1612_websol_pairing_and_availability.py -q`.
- Earlier bootstrap/expiry remediation suite: **85 passed** in 18.33 seconds.
- New expiry/bootstrap subset: **8 passed** in 1.50 seconds.
- `node --check browser/chatgpt-web-adapter.user.js`: passed.
- `python -m compileall -q src tests_py`: passed.
- `git diff --check`: passed.
- `python -m unittest discover -s tests_py -p "test_*.py"`: 1569 passed in
  863.439 seconds before this narrow remediation; it was not rerun here.

## Security/architecture limits

- No arbitrary shell, filesystem, PowerShell, URL proxy, local-port proxy, job
  execution, or lifecycle mutation is accepted on 8765.
- Heartbeat and WebBridge capabilities are distinct and hash-stored; no bearer
  token may enter source, logs, or ChatGPT messages.
- The old owner-authenticated 8770 browser-control endpoints remain for
  compatibility but are not accepted by or reachable through the remediated
  8765 read-only slice.
- Mutation remains a later phase and must reuse existing project-control
  commands and stale identity/revision/gate checks.
