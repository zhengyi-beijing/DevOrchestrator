# P19.3 ChatGPT Plus Browser Control Bridge

P19.3 provides an interactive, secure browser-based control bridge enabling desktop Chrome
ChatGPT Plus web conversations to inspect and operate local DevOrchestrator instances
via Tampermonkey (`browser/chatgpt-web-adapter.user.js`), port 8765 bridge, and port 8770 control plane.

It achieves this with zero RDC calls, no OpenAI API billing, no Business workspace,
no public exposure, and without weakening or replacing P19.2 (remote MCP).

---

## 1. Architectural Boundaries and Roles

```
+-----------------------------------------------------------------------------------+
| ChatGPT Plus Web Interface (https://chatgpt.com)                                  |
|   - Assistant emits structured envelope: [DEVORCH_ACTION_V1] { ... }              |
|   - Tampermonkey Adapter (browser/chatgpt-web-adapter.user.js):                   |
|       * Scans assistant DOM for [DEVORCH_ACTION_V1] envelopes                     |
|       * Read-only actions: dispatches automatically via loopback HTTP             |
|       * Effectful actions: renders DOM confirmation modal (Approve / Reject)       |
|       * Submits formatted [DEVORCH_ACTION_RESULT_V1] back to prompt textarea      |
+------------------------------------------+----------------------------------------+
                                           | HTTP POST (Origin: https://chatgpt.com)
                                           v
+-----------------------------------------------------------------------------------+
| DevOrchestrator Bridge Listener (127.0.0.1:8765)                                   |
|   - OPTIONS /v1/control/action: CORS preflight for https://chatgpt.com            |
|   - GET /v1/control/health: health check and protocol version                     |
|   - POST /v1/control/action: token validation, envelope parsing, and dispatch     |
+------------------------------------------+----------------------------------------+
                                           | In-Process Service Dispatch
                                           v
+-----------------------------------------------------------------------------------+
| BrowserControlService & Shared Operations Layer (src/dev_orchestrator/control/)   |
|   - Idempotency Store: runtime/control/browser_control_requests/ (locking & cache)|
|   - Confirmation Enforcement: blocks unconfirmed effectful actions (400)          |
|   - Shared Operations: spawn_job, poll_job, cancel_job, read_file                 |
|   - Hardware fence protection: hardware commands marked non-selectable            |
+-----------------------------------------------------------------------------------+
```

### Critical Boundaries
1. **Adapter / Transport Only**: Neither `chatgpt-web-adapter.user.js` nor the port 8765 bridge server is a lifecycle authority or state machine. Authoritative state remains strictly inside DevOrchestrator core and daemon stores.
2. **Channel Separation**: DevOrchestrator control requests and results are strictly isolated from the Web Sol inference queue. Control actions never enter `BrowserBridgeStore` Web Sol queues, nor do they trigger automated AI decision loops.
3. **Structured Protocol**: ChatGPT interactions use structured JSON envelopes (`DEVORCH_ACTION_V1`), explicitly prohibiting arbitrary shell execution or natural language execution.

---

## 2. Protocol Specification

### Action Envelope (`DEVORCH_ACTION_V1`)
Emitted by the ChatGPT model in conversation:
```json
[DEVORCH_ACTION_V1]
{
  "protocol": "DEVORCH_ACTION_V1",
  "request_id": "req-20260930-001",
  "action": "start_task",
  "project_id": "devorchestrator",
  "parameters": {
    "command_ref": "transport_compileall"
  }
}
[/DEVORCH_ACTION_V1]
```
Supported envelope formats:
- Bracket marker: `[DEVORCH_ACTION_V1] { ... } [/DEVORCH_ACTION_V1]` or `[DEVORCH_ACTION_V1 { ... }]`
- Code fence: ```` ```devorch_action { ... } ``` ````
- Fallback marker: `DEVORCH_ACTION_V1 { ... }`

### Result Envelope (`DEVORCH_ACTION_RESULT_V1`)
Submitted into the prompt textarea by the userscript after dispatch:
```json
[DEVORCH_ACTION_RESULT_V1 req-20260930-001]
{
  "protocol": "DEVORCH_ACTION_RESULT_V1",
  "request_id": "req-20260930-001",
  "action": "start_task",
  "project_id": "devorchestrator",
  "status": "success",
  "data": {
    "job_id": "job-0aac714c069b32bb",
    "status": "queued",
    "selected_transport": "local"
  }
}
[/DEVORCH_ACTION_RESULT_V1]
```

---

## 3. Closed Action Surface & Confirmation Policy

| Action | Kind | Confirmation Required | Description |
|---|---|---|---|
| `status` | Read-Only | No | Authoritative project status, lifecycle state, owner gates, git status |
| `commands_catalog` | Read-Only | No | Allowlisted commands, parameter schemas, hardware non-selectable flags |
| `job_status` | Read-Only | No | Durable execution job status, exit code, terminal outcome |
| `read_log` | Read-Only | No | Paginated, redacted logs from allowlisted sources (`events`, `runs`, etc.) |
| `read_file` | Read-Only | No | Path-contained file reading (text content or base64) |
| `start_task` | Effectful | **Strictly Yes** | Spawns allowlisted execution job via native transport |
| `cancel_job` | Effectful | **Strictly Yes** | Cancels specific running execution job |

### Confirmation Gate Mechanism
1. **Unconfirmed Attempt**: If an effectful action (`start_task`, `cancel_job`) arrives at the bridge without `confirmed: true`, `BrowserControlService` raises `BrowserControlConfirmationRequiredError`, returning HTTP 400:
   ```json
   {
     "status": "error",
     "status_code": 400,
     "reason": "Confirmation Required",
     "confirmation_required": true,
     "action": "start_task",
     "project_id": "devorchestrator",
     "request_id": "req-20260930-001",
     "details": {
       "command_ref": "transport_compileall",
       "job_id": null,
       "parameters": {}
     }
   }
   ```
2. **Browser Modal**: The userscript displays a prominent floating DOM modal detailing the Action, Project, Command/Job, and Parameters.
3. **User Decision**:
   - **Approve**: Dispatches the action with `confirmed: true`.
   - **Reject**: Submits a `status: "rejected"` result envelope into the prompt textarea explaining human rejection.

---

## 4. Security & Isolation Invariants

1. **Capability Tokens**: Every request requires a valid bearer token minted via CLI:
   ```powershell
   python -m dev_orchestrator browser-token mint --label "chrome-chatgpt" --ttl 86400
   python -m dev_orchestrator browser-token list
   python -m dev_orchestrator browser-token revoke <capability_id>
   ```
   Stored hashed in `runtime/control/browser-control-capabilities.json`. Tokens are compared via constant-time HMAC and never leaked in logs.
2. **CORS Restrictions**: Port 8765 responds to preflight `OPTIONS` requests allowing only `Origin: https://chatgpt.com` and loopback.
3. **Path Traversal Protection**: `read_file` validates canonical containment within configured project roots; relative path escapes (e.g. `../../windows/win.ini`) fail closed with HTTP 400.
4. **Hardware Fence**: In `commands_catalog`, hardware-effect commands have `is_hardware: true` and `selectable: false`. Attempting to start hardware commands via the bridge fails closed.
5. **Idempotency Protection**: `BrowserControlRequestStore` tracks all requests by `request_id` under `runtime/control/browser_control_requests/`. Identical resends return cached results without re-executing; mismatched payloads on the same `request_id` fail with HTTP 409 Conflict.
6. **Zero-RDC Constraint**: All dispatched operations strictly assert native transport (`local` or `ssh`). RDC transport count must be 0.

---

## 5. Verification & Acceptance Evidence

### Separate-Process Acceptance Test
The automated acceptance script verifies the complete live bridge and daemon flow:
```powershell
python ops/p19_3_browser_control_acceptance.py
```
Verified steps:
1. Bridge control health check (`GET /v1/control/health`).
2. CORS preflight check from `Origin: https://chatgpt.com`.
3. Unauthorized request rejection (missing and invalid token).
4. Capability token minting via `create_browser_control_capability`.
5. Authoritative `status` inspection.
6. `commands_catalog` discovery & hardware non-selectable assertion.
7. `read_file` text inspection on `pyproject.toml`.
8. Path traversal attempt fail-closed verification.
9. Effectful `start_task` confirmation gate (HTTP 400 requirement).
10. Confirmed `start_task` execution returning durable `job_id`.
11. Idempotency exact replay returning identical job without re-spawn.
12. Idempotency conflict rejection on altered payload (HTTP 409).
13. `job_status` polling until terminal completion.
14. Cancellable job lifecycle: spawn -> confirmation gate -> cancel gate -> cancel confirmed.
15. Redacted `read_log` querying.
16. Web Sol inference queue isolation (zero control actions in Web Sol queues).
17. Capability revocation verification (HTTP 401).
18. Transport audit: 0 RDC invocations, 100% native local transport.

Evidence artifacts:
- `docs/evidence/P19_3_BROWSER_CONTROL_ACCEPTANCE.json`
- `docs/evidence/P19_3_BROWSER_CONTROL_OPERATIONS.ndjson`

### Focused & Regression Test Suites
```powershell
python -m unittest tests_py/test_p19_3_browser_control.py
python -m unittest tests_py/test_p19_2_remote_mcp.py
python -m unittest tests_py/test_p19_web_console.py
python -m unittest tests_py/test_p18_external_control.py
python -m unittest tests_py/test_bridge_http.py
python -m unittest tests_py/test_chatgpt_web_adapter.py
node --check browser/chatgpt-web-adapter.user.js
```
