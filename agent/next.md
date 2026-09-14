# P12 Unified AI Control Surface

Status: **DESIGN COMPLETE / OWNER START REQUIRED**

Design authorization: **DESIGN P12 / 2026-09-15**

Implementation authorization: **not yet granted**

Goal: make port 8770 the primary day-to-day project and AI-resource control surface while keeping AIBroker port 8875 as the broker-specialist configuration and diagnostic surface.

## Approved executable design

- Keep the daemon as the only lifecycle authority. Port 8770 handlers may project state or durably enqueue a command; only the existing daemon-owned `ControlCommandCoordinator` consumes commands and invokes guarded Planner, Transition Executor, Reviewer, Watchdog or AIBroker adapters.
- Introduce stable `/api/v1/control/*` envelopes and one-call overview projection across project/task/lifecycle, active AI roles, watchdog/diagnostics, owner gates, P11 evidence, conversation bindings, broker resources and executions. Preserve available/stale/unavailable and unknown provenance.
- Preserve current `/api/*` GET routes. Standalone `start-web` stays read-only; command POST is wired only by the unified daemon.
- Require a loopback peer plus bearer or same-origin browser-session authentication, CSRF/origin/Host checks, bounded JSON schemas and no wildcard CORS. P12 does not expose a public remote control service.
- Make command submission cross-process atomic. A command id is bound to one canonical request hash: exact replay returns the existing record and different content conflicts. Persist request before acknowledgement, use deterministic downstream ids, persist result before inbox removal and append an always-on redacted audit trail.
- Require the exact server-returned project state revision and action-specific branch/HEAD/task/lifecycle/gate identity. Capabilities drive client UX but the daemon always re-reads and revalidates fresh truth.
- Implement only closed lifecycle actions (`continue`, `pause`, `resume`, `stop`, `retry`, `reconcile`, `approve_owner_gate`) and closed binding actions (`bind_conversation`, `unbind_conversation`, `rebind_conversation`). An action remains unavailable until its existing authority adapter supports safe, idempotent semantics and deterministic tests.
- Selectively forward-port conversation session/binding and owner-control safety contracts from `feature/conversation-control-plane` under 8770. Do not merge that branch wholesale and do not restore a second 8766 lifecycle authority.

## Execution sequence

1. **P12.1 - Contract and unified reads:** add pure serializers and versioned read routes; aggregate current runtime/P11/AIBroker evidence; isolate broker failures; keep all mutation disabled.
2. **P12.2 - Command transport/auth/audit:** harden the current inbox/history into an atomic store; add exact replay/conflict, bearer/browser session, CSRF/origin guards, status reads and daemon-only POST; migrate `continue` unchanged first.
3. **P12.3 - Actions and conversations:** add action adapters one by one; consolidate prior session/binding work under 8770; update CLI and ChatGPT/RDC clients to the shared contract.
4. **P12.4 - Operator UI and acceptance:** render capabilities, resources/executions, bindings, gates, P11 evidence and command results; run deterministic UI/API tests and one representative software-only DevOrchestrator scenario.

Each phase must pass its focused tests and a compatibility regression before the next begins. P12 is COMPLETE only after all four phase exit gates and the full suite pass.

## Interfaces and contracts

- Read routes: `/api/v1/control/overview`, `/projects`, `/projects/{project_id}`, `/resources`, `/executions`, `/commands/{command_id}`, `/sessions`, `/bindings`; `POST /browser-sessions` creates the same-origin CSRF session, short-lived pairing create/redeem/revoke routes provision a hash-only heartbeat capability, and adapter telemetry uses `POST /session-heartbeats`.
- Mutation route: `POST /api/v1/control/commands` with `schema_version`, client-generated `command_id`, `project_id`, closed `action`, complete `expected` identity and an action-specific closed `target`.
- Successful read envelope: `schema_version`, `generated_at`, `data`, `warnings`, `sources`.
- Command dispositions: `pending`, `accepted`, `blocked`, `failed`. Accepted means the operator effect was accepted; linked Planner/Worker completion is reported separately.
- Authentication secrets never appear in overview, logs, audit, command history or errors.
- Detailed frozen contract: `docs/UNIFIED_AI_CONTROL_SURFACE_DESIGN.md`.

## Validation plan

- Schema snapshots and legacy GET compatibility.
- Broker unavailable/timeout/malformed/stale/unknown fixtures.
- Thread and subprocess submission races plus every queue/result crash boundary.
- Loopback/auth/session/CSRF/Origin/Fetch-Metadata/Host/body/content-type/method cases.
- Fresh and stale branch/HEAD/task/lifecycle/gate identity for every action.
- Replay/conflict, restart, unsupported action and cross-project isolation for every advertised capability.
- Conversation uniqueness, tombstone, liveness and claimed-request rebind guards.
- Dashboard DOM behavior, confirmations, pending polling and failure recovery.
- `python -m pytest tests_py -q`, Python compilation, JavaScript syntax, `git diff --check`, and `graphify update .`.

## Out of scope

- Public exposure, built-in TLS, multi-user RBAC or general remote access.
- AIBroker provider/account configuration or replacement of 8875 diagnostics.
- Arbitrary prompt, shell, file, patch or generic state-mutation endpoints.
- Scheduler/provider-policy redesign, inferred quota/cost, PID killing, weakened owner/hardware gates, or resuming `xray-hw-platform`.
- Wholesale merges from old feature branches.

## Blocking rule

Only a contradiction to single-daemon authority, a non-idempotent irreversible action contract, or a required product decision beyond the frozen loopback/trusted-ingress boundary may stop implementation. Local schema layout, names, pagination, exceptions, UI details and edge tests are non-blocking and must converge through implementation and tests.
