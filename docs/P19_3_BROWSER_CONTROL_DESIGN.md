# P19.3 ChatGPT Plus Browser Control Bridge

P19.3 is a browser adapter for a bounded, read-only DevOrchestrator status flow:

```text
ChatGPT Web conversation
  -> Tampermonkey userscript
  -> loopback Browser Bridge (127.0.0.1:8765)
  -> authenticated WebBridge ingress (127.0.0.1:8770)
  -> authoritative DevO project/overview read models
  -> userscript
  -> the same ChatGPT conversation
```

It does not use native ChatGPT MCP, the OpenAI API, Desktop Commander/RDC, or a
public endpoint. Port 8770 remains the only control authority; port 8765 is a
fixed transport adapter and never implements lifecycle decisions.

## Implemented vertical slice

The browser surface accepts one action: `get_project_status`.

The result is a bounded projection containing:

- project ID and Git branch/HEAD/dirty state;
- current task, lifecycle state, paused state, and readiness;
- the project's active execution and AI roles;
- project-scoped active broker executions and current resource occupancy;
- watchdog state;
- exact conversation binding and live/stale session state.

There is intentionally no browser-facing filesystem, shell, generic URL proxy,
job execution, or lifecycle mutation operation. The older owner-authenticated
8770 browser-control API is not part of this 8765 slice and its capability is
not accepted by the read-only WebBridge ingress.

## Protocol

ChatGPT emits the existing versioned envelope:

```text
[DEVORCH_ACTION_V1]
{
  "protocol": "DEVORCH_ACTION_V1",
  "request_id": "req-20260930-001",
  "action": "get_project_status",
  "project_id": "devorchestrator",
  "parameters": {}
}
[/DEVORCH_ACTION_V1]
```

Before transport, the userscript derives the current conversation ID from the
`https://chatgpt.com/c/<id>` URL and adds:

```json
{
  "binding": {
    "adapter": "chatgpt_web",
    "binding_id": "<current conversation id>"
  },
  "issued_at": "<UTC timestamp>"
}
```

The userscript rejects an envelope that supplies a different binding. Port
8765 validates a closed schema, maps the action to WebBridge operation
`project_status`, and forwards only to the fixed loopback endpoint
`POST /api/v1/control/web-bridge/requests` on port 8770. It is not a general
proxy.

Port 8770 then enforces all authoritative guards:

- a revocable `web_bridge_control` capability bound to the exact project,
  adapter, and conversation ID;
- the project's durable `ConversationControlStore` binding;
- a currently live session for that binding;
- request freshness on every attempt;
- durable request-ID idempotency over semantic request fields. The transport-only `issued_at` freshness timestamp is deliberately excluded from the idempotency hash, so a fresh retry of the same semantic request replays the cached response instead of conflicting; changes to operation/project/binding/revision/payload still conflict.

The result is associated with the original request ID and formatted as:

```text
[DEVORCH_ACTION_RESULT_V1 req-20260930-001]
{
  "protocol": "DEVORCH_ACTION_RESULT_V1",
  "request_id": "req-20260930-001",
  "status": "success",
  "data": { "project_id": "devorchestrator" }
}
[/DEVORCH_ACTION_RESULT_V1]
```

The userscript inserts that result into the composer and submits it as a user
message in the same conversation. A request is marked processed only after the
message is observed in conversation history. Per-tab processed IDs remain
conversation-scoped in `sessionStorage`; retry identity and pending successful
formatted results are also stored in bounded Tampermonkey-private shared storage
so duplicate tabs see the same retry state. Background/hidden tabs maintain
heartbeat presence but do not consume actions, while the visible bound tab owns
dispatch/result submission. This prevents duplicate-tab races without placing
capabilities or result state in the page DOM.

## Presence and recovery

The existing userscript heartbeat is independent of action dispatch. It:

- derives the binding dynamically from the current URL;
- uses a `sessionStorage` tab instance ID, stable across reloads but different
  in another tab;
- heartbeats on startup, URL changes, visibility/focus changes, and its normal
  interval;
- re-establishes presence after a DevO restart without manual runtime edits;
- lets a stale durable binding return to `live` when the page is active again.

The heartbeat capability and browser read capability remain separate. Neither
secret is inserted into the ChatGPT DOM, result text, or logs. The read token is
stored only by the userscript manager and is sent through `GM_xmlhttpRequest`.

### Owner-initiated local bootstrap

Normal pairing does not require copying the read token. After the heartbeat is
live and the project is authoritatively bound to that conversation, the owner
creates a pending permit from the local CLI:

```powershell
python -m dev_orchestrator.cli create-web-bridge-bootstrap devorchestrator <binding-id> --adapter chatgpt_web --ttl 60
```

The permit contains no client-disclosed secret, is bound to the exact project,
`chatgpt_web` adapter, and conversation ID, expires in at most 60 seconds, and
is consumed atomically. Heartbeats only report non-secret pending availability;
they never create a permit. The userscript may claim a pending permit using its
existing heartbeat capability only when 8770 confirms that the same capability
owns the exact live, owner-bound session. The resulting `web_bridge_control`
bearer is returned directly to the userscript and stored in Tampermonkey
storage. Neither it nor bootstrap state is inserted into the page DOM.

An expired, replayed, mismatched, wrong-origin, unverified-session, or
non-loopback claim fails closed. Pending permits remain bounded across daemon
restart; browser restart does not broaden them. The manual **Pair
DevOrchestrator read-only control** Tampermonkey command remains the fallback.

`ControlSecurity.capability_state()` uses the established non-authorizing
`UNKNOWN` verdict for expired rows because the repository vocabulary has no
`EXPIRED` member. Token validation also returns an explicit expiry reason and
continues to deny the bearer.

## Routes and storage

Port 8765:

- `GET /v1/control/health`
- `OPTIONS /v1/control/action`
- `POST /v1/control/action`

Port 8770 authority used by this slice:

- `POST /api/v1/control/session-heartbeats`
- `POST /api/v1/control/web-bridge/bootstrap/redeem`
- `POST /api/v1/control/web-bridge/requests`

Durable WebBridge idempotency records are stored under
`runtime/control/web_bridge_requests/`. Browser-control messages never enter the
Web Sol claim/response queue.

## Security invariants

1. The browser listener and its fixed upstream must both be loopback HTTP.
2. CORS accepts exact ChatGPT and loopback origins; suffix/prefix spoofing is
   rejected without reflected CORS headers.
3. Only `get_project_status` and empty parameters are accepted on 8765.
4. Unknown fields, arbitrary URLs, shell text, paths, and mutation actions fail
   closed before forwarding.
5. Port 8770 revalidates capability scope, runtime binding, live presence, and
   freshness. Port 8765 cannot manufacture authority.
6. Exact replay returns the durable response; a changed payload under the same
   request ID returns HTTP 409.
7. Returned broker/watchdog/control objects are field-allowlisted rather than
   forwarded wholesale.
8. Bootstrap creation is an explicit local owner action; a heartbeat cannot
   create authorization, and a claim requires its exact verified live session.

## Verification

Transport acceptance against a running daemon:

```powershell
python ops/p19_3_browser_control_acceptance.py
```

The script verifies health, exact CORS, authentication, the bounded status
shape, exact replay, changed-payload conflict, wrong binding, cross-project
routing, capability revocation, rejection of shell/filesystem/mutation inputs,
and zero Web Sol queue pollution. It deliberately labels browser UI round-trip
as not run; only real Chrome/Tampermonkey acceptance may establish that claim.

Focused checks:

```powershell
python -m unittest tests_py.test_p19_3_browser_control
python -m unittest tests_py.test_p13_web_bridge_adapter tests_py.test_p13_software_acceptance
node --check browser/chatgpt-web-adapter.user.js
python -m compileall -q src tests_py ops
git diff --check
```

Evidence artifacts:

- `docs/evidence/P19_3_BROWSER_CONTROL_ACCEPTANCE.json`
- `docs/evidence/P19_3_BROWSER_CONTROL_OPERATIONS.ndjson`
- live-browser observations in `agent/result.md` and the P19.3 handoff

## Deferred mutation phase

Pause/resume/continue/retry/rereview/reconcile are not exposed by the current
browser slice. A later mutation phase must reuse existing project-control
commands, advertised capabilities, expected identity/revision guards, stale
state protection, and human confirmation where appropriate. The userscript
must never decide lifecycle transitions itself.
