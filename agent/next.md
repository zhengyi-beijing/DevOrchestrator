# NEXT — P16 AI Capability Benchmark Project

Status: **PENDING DESIGN**

Sequence:
P14.5 (closed) -> Watchdog recovery-epoch cleanup (closed) -> P14.6 Unattended Execution Stabilization Gate (closed) -> P15 Mobile Observability & Guarded Control (closed) -> P16

## P15 Mobile Observability & Guarded Control closure

Closed after implementing and verifying all P15 Mobile Observability and Guarded Control capabilities:

Acceptance evidence:
1. **Durable Source-Locking & Replay Conflict Safety**:
   - `ControlCommandStore.submit` normalizes `source` before locking and rejects replays with mismatched sources via `ControlCommandConflictError`.
   - Replay of same command_id with matching source and body is an idempotent replay; same source with changed body conflicts.
2. **Device Identity, Pairing, & Single Revocation Authority**:
   - Single source of truth in `runtime/control/adapter-capabilities.json` managed under `InterProcessFileLock`.
   - Monotonic `mobile_revocation_generation` tracks revocation events.
   - `validate_mobile_bearer` returns non-secret `MobileDevicePrincipal` at gateway ingress; `lookup_mobile_device` re-validates device identity without bearer token. Zero bearer token propagation into command records, coordinators, or audit logs.
   - `POST /api/v1/mobile/v1/pair` is single-use, TTL-bounded, attempt-bounded, and returns device token once.
3. **Guarded Mobile Control & Mobile Owner-Gate Channel**:
   - Loopback `POST /api/v1/control/commands` admits `X-DevO-Control-Source: mobile_gateway:<device_id>` only under master bearer and successful tokenless `lookup_mobile_device`.
   - `approve_owner_gate` via `approval_channel='mobile_device'` skips conversation-binding requirement, preserves pending-gate and repository-truth checks, and durably records `approved_via` and `approving_device_id`.
   - Approval never launches a Worker; progress still requires explicit `continue`.
4. **Read-Only Mobile Projection & Bounded Controls**:
   - `MobileProjectionService` composes read-only views directly from `runtime_root` without coordinator invocation or lifecycle mutations.
   - Exposes strictly `MOBILE_CONTROL_ACTIONS` (`continue`, `pause`, `resume`, `stop`, `retry`, `reconcile`, `approve_owner_gate`).
   - Copies watchdog state and recovery epoch verbatim.
5. **Tailscale Bind Policy & Streaming Infrastructure**:
   - `verify_tailscale_bind_address` restricts gateway to verified local Tailscale `100.64.0.0/10` and `fd7a:115c:a1e0::/48` addresses, refusing wildcard, loopback, and RFC1918.
   - Reconnectable SSE and long-poll streams with 15-second mid-stream authorization rechecks; mid-stream revocation immediately terminates connections.
6. **Notification-Only Alert Boundary**:
   - Disjoint progress-family and transport-family alert classes. Stall alerts derive strictly from authoritative watchdog state.
7. **Client Implementations & Acceptance Suite**:
   - Headless Python `MobileContractClient` and Kotlin/Jetpack Compose Android application skeleton in `android/`.
   - 13 comprehensive P15 test suites in `tests_py/test_p15_*.py` (61 tests passed).
   - Full repository regression: 969 passed, 87 subtests passed in 356.33s.
   - `compileall`, `git diff --check`, and `graphify update .` all passed cleanly.

## P16 goal

Create a separate, reproducible benchmark project that measures the real capability, efficiency and reliability of the AI resources available to DevO/AIBroker, and determines when shared repository retrieval such as zvec-grep should become a standard Agent Harness capability.

Refer to `agent/staged/P16.md` for initial scope and acceptance criteria.
