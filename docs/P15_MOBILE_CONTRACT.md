# P15 Mobile Observability & Guarded Control Contract

Status: FROZEN
Authority: DevOrchestrator Architecture Policy (`docs/development-workflow.md`, `agent/staged/P15.md`)

---

## 1. Overview and Architecture

The P15 Mobile Observability and Guarded Control subsystem provides an Android-native, failure-independent observation and bounded-control surface for DevOrchestrator without creating a second lifecycle authority.

```text
+-----------------------------------------------------------+
|                      Android Client                       |
|  - Encrypted token storage (EncryptedSharedPreferences)   |
|  - SSE / Long-poll streams with resumable cursors         |
|  - Local notifications (presentation-only policy)         |
+-----------------------------------------------------------+
                             |  Tailscale (100.64.0.0/10 or fd7a:115c:a1e0::/48)
                             v
+-----------------------------------------------------------+
|                 Daemon: MobileGateway                     |
|  - Bound ONLY to verified local Tailscale interface       |
|  - Authenticates Bearer -> MobileDevicePrincipal          |
|  - Namespaces commands: mobile_gateway:<device_id>        |
|  - Read-only in-process composition (no lifecycle writes) |
+-----------------------------------------------------------+
            |                                  |
   Read-only composition             Mutation via ControlAdapterClient
            v                                  v
+------------------------+      +-------------------------------+
| MobileProjectionService|      | POST /api/v1/control/commands |
| - project_control_view |      | X-DevO-Control-Source:        |
| - project_status       |      |   mobile_gateway:<device_id>  |
| - watchdog (verbatim)  |      +-------------------------------+
+------------------------+                     |
                                               v
+---------------------------------------------------------------+
|             Authoritative Control Command Store               |
|  - Source-locked command replay and conflict detection        |
|  - Execution re-validates device_id via Authorizer            |
|  - ControlCommandCoordinator -> AIPlannerCoordinator          |
+---------------------------------------------------------------+
```

### Core Invariants

1. **Single Lifecycle Authority**: The daemon-owned coordinators (`ControlCommandCoordinator`, `TransitionExecutor`, `AIPlannerCoordinator`, `AIReviewerCoordinator`, `WatchdogCoordinator`) remain the sole mutation and state authority. Neither `MobileGateway` nor `MobileProjectionService` nor the Android app mutates lifecycle stores directly.
2. **Single Revocation Authority**: Canonical device registration, capabilities, and revocation are owned strictly by `ControlSecurity` persisting to `runtime/control/adapter-capabilities.json` under `InterProcessFileLock`. A monotonic `mobile_revocation_generation` tracks revocation events.
3. **Notification Boundary**: Notification thresholds and rules are presentation policy only. Alerts derive from authoritative progress and transport observations; they cannot reclassify running or unknown state to stalled.
4. **Durable Source-Locking**: `ControlCommandStore` enforces source-locking: any command replay with a mismatched source raises `ControlCommandConflictError`.
5. **No Bearer Propagation**: Mobile bearer tokens are validated at `MobileGateway` ingress and converted to non-secret `MobileDevicePrincipal(device_id, scope, ...)`. Bearer tokens never enter command records, audit logs, or coordinator calls.

---

## 2. Trust Boundaries and Authentication

### External Ingress: MobileGateway

- Bound exclusively to a verified local Tailscale IP address (`100.64.0.0/10` or `fd7a:115c:a1e0::/48`).
- Disabled by default unless configured.
- Reject all wildcard (`0.0.0.0`, `::`), loopback (`127.0.0.1`, `::1`), RFC1918 private LAN, and public addresses.
- All routes except `POST /api/v1/mobile/v1/pair` require an `Authorization: Bearer <device_token>` header.
- No browser session cookies, CSRF tokens, CORS, or `OPTIONS` handling on `MobileGateway`.

### Unauthenticated Exception: Pairing

- Route: `POST /api/v1/mobile/v1/pair`
- Request body: `{"pairing_id": "<str>", "code": "<str>", "device_label": "<str>"}`
- Authenticated by owner-minted single-use `pairing_id` and high-entropy secret `code`.
- Time-to-live: 300 seconds default. Max redemption attempts: 3.
- Rate-limited and compared using constant-time comparison (`hmac.compare_digest`).
- Failure responses on unknown, expired, consumed, or incorrect credentials are indistinguishable.
- Success returns `device_id`, `token`, `scope="mobile_device"`, and `expires_in_seconds`. The token is returned only once.

### Mobile Device Identity

```python
@dataclass(frozen=True)
class MobileDevicePrincipal:
    device_id: str
    scope: str  # 'mobile_device'
    device_label: str
    created_at: str
    expires_at: str | None
    revoked: bool
```

- Bearer tokens are hashed with SHA-256 before storage in `adapter-capabilities.json`.
- `MobileDeviceAuthorizer` provides:
  - `validate_mobile_bearer(header_or_token)` -> `(bool, str, MobileDevicePrincipal | None)`
  - `lookup_mobile_device(device_id)` -> `(bool, str, MobileDevicePrincipal | None)`
  - `mobile_revocation_generation()` -> `int`

---

## 3. Command Provenance and Source-Locking

### Internal Loopback Source Admission

- When `MobileGateway` receives a mutation command from an authenticated mobile device, it delegates to `ControlAdapterClient.submit_control(..., source=f"mobile_gateway:{principal.device_id}")`.
- `ControlAdapterClient` sends header `X-DevO-Control-Source: mobile_gateway:<device_id>`.
- The loopback `POST /api/v1/control/commands` endpoint admits `mobile_gateway:<device_id>` only when:
  1. The request has master bearer authentication.
  2. The source matches `^mobile_gateway:[A-Za-z0-9_-]{1,64}$`.
  3. `lookup_mobile_device(device_id)` confirms the device exists, is not revoked, and is not expired.
- Any unauthorized or invalid source claim is rejected with HTTP 403/400.

### Store Source-Locking Semantics

In `ControlCommandStore.submit(value, *, source)`:
1. `source` is normalized and validated before acquiring the store lock.
2. If an existing record exists for `command_id`:
   - If `existing.get("source") != source`: raise `ControlCommandConflictError`.
   - If `existing.get("source") == source` and `existing.get("request_hash") == digest`: return `existing` (idempotent replay).
   - If `existing.get("source") == source` and `existing.get("request_hash") != digest`: raise `ControlCommandConflictError` (changed body conflict).

---

## 4. Mobile Owner-Gate Approval

### Shared Eligibility Predicate

`mobile_owner_gate_eligibility(project, snapshot, runtime_root, device_id, mobile_device_authorizer, conversation_store, bridge_store)`:
- Verifies a pending planner owner gate exists.
- Verifies project, task, and repo match.
- Verifies repo is clean and repository truth unchanged.
- Verifies `mobile_device_authorizer.lookup_mobile_device(device_id)` succeeds.
- Verifies no active claim exists on any bound conversation.
- Does **not** require a live bound ChatGPT/WebSol conversation.

### Execution Path

1. `ControlCommandCoordinator._approve_owner_gate` detects `source.startswith("mobile_gateway:")`.
2. Extracts `device_id` and revalidates via `lookup_mobile_device(device_id)`.
3. Verifies no active claim on any bound conversation.
4. Invokes `AIPlannerCoordinator.approve_owner_gate(project, snapshot, gate_id, command_id, approval_channel="mobile_device", approving_device_id=device_id)`.
5. `AIPlannerCoordinator` validates repository truth, pending gate state, records `"approved_via": "mobile_device"`, `"approving_device_id": device_id`, and sets state `"owner_approved"`.
6. Plan apply and Worker launch still require an explicit subsequent `continue`.

---

## 5. Read-Only Mobile Projection Service

`MobileProjectionService` composes read-only views for mobile:
- Inputs: `project_control_view`, `build_project_status`, `project_runtime_status`, `watchdog.json`, `transition-executor.json`, `ai-planner.json`, `ai-reviewer.json`.
- Exposes only `MOBILE_CONTROL_ACTIONS = {'continue', 'pause', 'resume', 'stop', 'retry', 'reconcile', 'approve_owner_gate'}`.
- Rereview and conversation binding controls are excluded.
- Watchdog state and recovery epoch are copied verbatim.
- Surfaces `progress_observation_state` as `"authoritative"`, `"stale"`, or `"unavailable"`.

---

## 6. HTTP API Surface on MobileGateway

Prefix: `/api/v1/mobile/v1`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/pair` | Pairing Code | Redeem pairing code for device token |
| `GET` | `/projects` | Mobile Bearer | List active projects with high-level status |
| `GET` | `/projects/{project_id}` | Mobile Bearer | Detailed project view with mobile-available controls |
| `POST` | `/projects/{project_id}/controls` | Mobile Bearer | Submit a guarded control command |
| `GET` | `/commands/{command_id}` | Mobile Bearer | Poll status of a submitted command |
| `GET` | `/events` | Mobile Bearer | SSE stream with `Last-Event-ID` support |
| `GET` | `/events/poll` | Mobile Bearer | Long-poll fallback with cursor |
| `GET` | `/alert-policy` | Mobile Bearer | Get current mobile alert policy |
| `PUT` | `/alert-policy` | Mobile Bearer | Update mobile alert policy |
| `POST` | `/alerts/{alert_key}/ack` | Mobile Bearer | Acknowledge an alert |
| `POST` | `/alerts/{alert_key}/snooze` | Mobile Bearer | Snooze an alert for duration |

### Streaming and Long-Polling Contract

- Cursors are opaque monotonic tokens.
- Reconnection with `Last-Event-ID` or `?cursor=` resumes without gaps within retention (last 100 events).
- If cursor is outside retention, returns event `resync_required` with a full snapshot.
- Stream sends keepalive heartbeats every 15 seconds.
- Every event and heartbeat revalidates device authorization; on revocation, stream terminates immediately with 401.

---

## 7. Alert Policy and Notification Boundary

- Evaluator: `evaluate_notification(progress_obs, transport_obs, policy, now, ack_state)`
- **Disjoint alert families**:
  1. `ProgressAlert` (`stall`, `owner_gate`): Can fire only when progress observation is authoritative; a stall alert fires only when watchdog reports `agent_stalled`.
  2. `TransportAlert` (`disconnected`, `degraded`): Evaluates connectivity to daemon/Tailscale; can fire when progress is `None` or unavailable.
- Thresholds, quiet hours, acknowledgement, snooze, and escalation control notification dispatch only; they never modify or reclassify daemon lifecycle state.

---

## 8. Loopback Administration Routes

Available only via authenticated loopback Control API (`:8770`):
- `POST /api/v1/control/mobile/pairings` -> mint single-use pairing code
- `GET /api/v1/control/mobile/devices` -> list authorized devices (non-secret metadata)
- `POST /api/v1/control/mobile/devices/{device_id}/revoke` -> revoke specific device
- `POST /api/v1/control/mobile/devices/revoke-all` -> revoke all mobile devices

Revocation advances `mobile_revocation_generation` and immediately invalidates active streams.

---

## 9. Manual Acceptance Procedure

1. Start DevOrchestrator daemon with MobileGateway enabled on Tailscale interface.
2. Mint pairing code via loopback admin CLI: `dev-orchestrator mobile-pair`.
3. On Android device connected to Tailscale network, enter pairing code.
4. Verify pairing succeeds, storing token in EncryptedSharedPreferences.
5. In Android app, verify projects list loads and live SSE event stream connects.
6. Verify watchdog and recovery epoch match web dashboard.
7. Trigger an `OWNER_GATE` in DevOrchestrator; verify notification arrives on Android.
8. Approve `OWNER_GATE` from Android; verify plan is marked `owner_approved` without launching worker.
9. Continue from Android; verify worker launches.
10. Revoke device from CLI: verify active SSE stream closes within 15 seconds and subsequent requests return 401.
