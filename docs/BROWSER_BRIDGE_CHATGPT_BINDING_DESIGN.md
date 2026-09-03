# Browser Bridge + ChatGPT Web multi-project binding

Status: DESIGN FROZEN / TEST-FIRST
Baseline: `586c49b` on `feature/browser-bridge-multiproject`

## Goal

Extend the accepted one-process DevOrchestrator daemon with a browser-bridge transport and a dumb ChatGPT Web adapter. One daemon may manage many projects, and every orchestration-ready project binds independently to one ChatGPT Web conversation through `conversation_binding`.

The bridge is transport only. The Userscript is a ChatGPT Web adapter only. Neither decides workflow transitions, starts Workers, applies `next_action`, or bypasses the accepted repository-truth guard.

## Process topology

```text
1 DevOrchestrator OS process
  +-- monitor loop
  +-- read-only dashboard :8770
  +-- browser bridge :8765
        +-- binding A queue -> ChatGPT conversation A
        +-- binding B queue -> ChatGPT conversation B
```

`daemon.json`, `monitor.json`, `web.json`, and `bridge.json` must report the same live PID in daemon mode.## Project binding

Canonical project binding remains credential-free:

```json
{
  "conversation_binding": {
    "transport": "browser_bridge",
    "adapter": "chatgpt_web",
    "binding_id": "<opaque-chatgpt-conversation-id>"
  }
}
```

For `chatgpt_web`, `binding_id` is normally the conversation id parsed from the current ChatGPT URL. The Userscript must derive it dynamically; no project/conversation key is hard-coded in script source.

Among orchestration-ready projects, `(transport, adapter, binding_id)` must be unique. Duplicate bindings are a configuration error because they could cross-route two project workflows into one conversation.

Missing/malformed bindings remain monitor-only (`orchestration_ready=false`) as in the accepted Core contract.## Transport state machine

Each outbound Web Sol request is persisted under DevOrchestrator runtime and has exactly one binding.

```text
PENDING -> CLAIMED -> RESPONDED
   ^          |
   +----------+  lease expiry / adapter loss
```

Submission is idempotent by `(request_id, nonce)`. Reusing a `request_id` with a different nonce or identity fails closed. A `request_id` belongs to exactly one binding: submitting a request id already persisted in another `(adapter,binding_id)` is a conflict even if the nonce is identical, preventing ambiguous cross-project response lookup.

Claim is filtered by exact `(adapter, binding_id)`, returns at most one request, and issues an opaque `claim_token` with a bounded lease. A response must present the same binding, request id, nonce, and claim token. Mismatch is rejected and never changes queue state.

A claimed request may renew its lease while ChatGPT is still answering. `renew` requires the exact binding, request id, nonce and claim token of a still-valid active claim, and extends the lease from the renewal moment; wrong/expired identity/token fails closed without mutating state.

The transport only stores/delivers the response. It never calls `validate_websol_response`, never starts an AgentBackend, and never acts on `next_action`.## Bridge HTTP surface

Bridge uses a dedicated local listener (default `127.0.0.1:8765`) so the accepted dashboard remains GET/HEAD-only.

Adapter-facing v1 surface:

- `GET /v1/health`
- `POST /v1/claim` with `{adapter, binding_id}` -> `200` envelope or `204` when empty
- `POST /v1/renew` with `{adapter, binding_id, request_id, nonce, claim_token}` -> `200` claimed-state envelope or rejected acknowledgement
- `POST /v1/response` with `{adapter, binding_id, request_id, nonce, claim_token, response_text}` -> accepted/rejected acknowledgement

No generic shell endpoint, arbitrary prompt endpoint, Worker endpoint, or workflow-transition endpoint is exposed.

Bridge envelopes contain the accepted Web Sol identity fields: project_id, request_id, task_id/stage_id, branch, head, role, event, nonce, plus transport metadata and a rendered Web Sol prompt. High-frequency progress/heartbeat never enters this queue; only requests already admitted by Core reasoning-event policy may be submitted.## ChatGPT Web Userscript adapter

The Tampermonkey adapter is intentionally dumb:

1. Parse the active ChatGPT conversation id from the current URL and use it as `binding_id`.
2. Poll/claim only that exact binding from the bridge.
3. Insert the rendered request prompt into the ChatGPT composer and submit it.
4. Observe assistant messages until the matching `[DEVORCH_WEB_SOL_RESPONSE <request_id>]` marker appears, renewing the active claim (`POST /v1/renew`) often enough that another tab cannot reclaim it while ChatGPT is still answering.
5. Stop renewing as soon as the matching response marker appears or the wait times out.
6. POST the raw matching assistant text back with the original request identity/claim token.

It must not interpret `NEXT`, `NEXT_STAGE`, `OWNER_GATE`, repository state, Worker state, or decide what happens next. It may only perform DOM adaptation and transport acknowledgement.

The adapter must tolerate multiple ChatGPT tabs/conversations: each tab derives a different binding id and therefore claims only its own project queue. A tab without a stable conversation id remains idle.## Web Sol prompt/response marker

Rendered requests begin with:

```text
[DEVORCH_WEB_SOL_REQUEST <request_id>]
```

and include the canonical identity JSON plus role/event context. The prompt requires the final answer to contain:

```text
[DEVORCH_WEB_SOL_RESPONSE <same request_id>]
{ structured response JSON }
```

The rendered prompt states the complete response JSON schema: it must echo project_id, request_id, task_id/stage_id, branch, head, role, event, nonce and include the mandatory `decision` (`next`, `remediate`, `retry`, `owner_gate`, `stop`) and `next_action` (`continue_current_stage`, `next_task`, `next_stage`, `stop`) using the accepted Core wire enum values.

The Userscript uses the marker only to associate assistant output with the claimed request. Parsing/validating structured decision content belongs above transport in DevOrchestrator Core.

## Acceptance / non-goals

Acceptance is software-only: queue isolation, persistence/idempotency, lease/reclaim, identity rejection, bridge HTTP, same-PID daemon integration, and testable Userscript URL/marker/DOM-adapter helpers. No real ChatGPT message is sent during automated tests.

Live browser DOM acceptance is a separate deployment gate because the existing PoC host `ts-pc-zy` is currently offline. No PHASE_AUTO, no automatic Web Sol event emission, no response application, no Worker execution, no credentials in project config, no LabDemo/xray project special case, and no hardware action.
## Reviewer clarification — lease renewal and global request identity

A live ChatGPT response may take much longer than the base claim lease. The adapter therefore renews its active claim before expiry through transport-only `POST /v1/renew`. Renewal requires exact adapter, binding_id, request_id, nonce and claim_token, and only extends the lease; it performs no workflow action. Wrong/expired claims fail closed.

While waiting for the matching assistant marker, the Userscript must renew often enough that another tab cannot reclaim the same request. If the adapter disappears, renewal stops and normal lease expiry makes the request reclaimable.

A `request_id` belongs to exactly one binding. Submitting the same request id into another `(adapter,binding_id)` is a conflict even if the nonce is identical, preventing ambiguous cross-project response lookup.

Rendered Web Sol prompts must state the complete response JSON schema: echoed project/request/task/stage/branch/head/role/event/nonce plus mandatory `decision` and `next_action`, with the accepted enum values from Core.