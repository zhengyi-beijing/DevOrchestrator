# P12 Unified AI Control Surface

Status: **DESIGN FROZEN / OWNER START REQUIRED**

Design authorization: **DESIGN P12 / 2026-09-15**

Implementation authorization: **not yet granted**

## Outcome

Port 8770 becomes the normal operator surface for DevOrchestrator. It presents
one versioned view of project lifecycle, active AI work, broker resources,
watchdog state, P11 execution evidence, conversation bindings and available
operator actions. Port 8875 remains the AIBroker specialist surface for broker
configuration and low-level diagnostics.

P12 extends the current system; it does not create another lifecycle engine.
The daemon remains the only component allowed to accept lifecycle effects.
HTTP handlers, the dashboard, CLI, ChatGPT/RDC integrations and future mobile
clients are clients of the same Control API.

## Non-negotiable invariants

1. `project_runtime_status` and the existing Planner, Transition Executor,
   Reviewer, Watchdog and AIBroker ledgers remain repository/runtime truth.
2. An HTTP request never launches, interrupts or mutates workflow state in the
   request thread. It can only validate an envelope and durably enqueue it.
3. `ControlCommandCoordinator` is the single daemon-owned command consumer.
   Action adapters must call existing guarded lifecycle methods; they may not
   duplicate branch/HEAD, task, owner-gate, dirty-tree or active-run rules.
4. Every mutation is project-scoped, authenticated, idempotent, stale-state
   guarded and auditable. There is no arbitrary shell, prompt, patch or provider
   command endpoint.
5. AIBroker data is read through its supported interface. Broker failure
   degrades only the broker section of the 8770 view and never fabricates a
   healthy, zero-usage or unlimited-quota value.
6. Existing `/api/*` read endpoints remain compatible during P12. New stable
   contracts live under `/api/v1/control/*`.
7. The standalone `start-web` process remains read-only. Mutations are enabled
   only when the unified daemon supplies the command service.
8. Direct non-loopback mutation is disabled in P12. Remote/mobile use requires
   an owner-configured authenticated tunnel or ingress to the same API; public
   listener authentication and TLS termination are not invented here.
9. No hardware project is resumed and no hardware action is introduced by P12.

## Topology and authority

```text
Dashboard / CLI / ChatGPT / RDC / mobile-through-trusted-ingress
                            |
                  8770 Control API v1
                  | read          | enqueue only
                  v               v
          unified projection   control inbox + audit
                                      |
                              daemon tick / single consumer
                                      |
              Planner / Transition Executor / Reviewer / Watchdog
                                      |
                         AIBroker execution adapter

                  8875 AIBroker specialist UI/API
                     configuration + deep diagnostics
```

The 8770 projection aggregates already durable facts. It does not copy those
facts into a second database or become a source of lifecycle truth.

## Stable API v1

### Read routes

- `GET /api/v1/control/overview`
- `GET /api/v1/control/projects`
- `GET /api/v1/control/projects/{project_id}`
- `GET /api/v1/control/resources`
- `GET /api/v1/control/executions`
- `GET /api/v1/control/commands/{command_id}`
- `GET /api/v1/control/sessions`
- `GET /api/v1/control/bindings`
- `POST /api/v1/control/browser-sessions`
- `POST /api/v1/control/adapter-pairings`
- `POST /api/v1/control/adapter-pairings/redeem`
- `POST /api/v1/control/adapter-pairings/{pairing_id}/revoke`
- `POST /api/v1/control/session-heartbeats`

`overview` is the one-call normal-operation view. The narrower routes use the
same serializers so their values cannot disagree with the overview response.
Large event/log bodies remain paged or bounded; overview contains references
and recent summaries, not unbounded history.

Every successful read has this envelope:

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-15T00:00:00Z",
  "data": {},
  "warnings": [],
  "sources": []
}
```

Each source-backed section declares `availability` as `available`, `stale` or
`unavailable`, includes its observation time when known, and lists
`unknown_fields`. An unavailable value is `null`; it is never rewritten to
zero, false, healthy or unlimited. Derived values retain evidence IDs or source
references.

The project view contains:

- project/repository identity: project id, repo path, branch, HEAD and dirty
  state;
- staged task, lifecycle state, next action and exact state revision;
- active Planner, Implementer, Reviewer and Remediator identities and timing;
- watchdog health, diagnostic/recovery state and current OWNER_GATE;
- P11 EDR, phase loss, retries, owner wait, context/provider switching,
  failover and RDC evidence;
- conversation binding/liveness when present;
- command capabilities computed from fresh server state.

The resource/execution view contains provider, account, model, eligible roles,
health, quota/token/reset facts, session continuity, current execution,
correlation IDs, usage/cost and recent retry/remediation chains where supplied
by AIBroker or P11 evidence.

### Capability contract

Clients never infer whether a button is safe. Each project response includes a
closed action list with:

```json
{
  "action": "continue",
  "available": true,
  "reason": "project has a pending staged design",
  "required_expected_fields": ["revision", "branch", "head", "task_id"]
}
```

The capability is advisory UX, not authorization. The daemon rechecks the same
facts after dequeue. Unknown or unsupported actions are reported unavailable.

### Mutation route

`POST /api/v1/control/commands` is the only project/workflow owner-mutation
route and accepts only this bounded shape:

```json
{
  "schema_version": 1,
  "command_id": "client-generated-id",
  "project_id": "dev-orchestrator",
  "action": "continue",
  "expected": {
    "revision": "sha256:...",
    "branch": "feature/self-hosted-dev",
    "head": "...",
    "task_id": "P12",
    "lifecycle_state": "PENDING_DESIGN",
    "gate_id": null
  },
  "target": {}
}
```

`command_id` is mandatory for API clients. `expected.revision` is a hash of the
canonical, action-relevant project identity returned by the read model. The
human-readable expected fields are also retained in the audit record and are
checked where the action requires them. `target` has an action-specific closed
schema; extra keys and arbitrary text are rejected.

The HTTP layer validates size, content type, JSON shape, authentication and
project/action names, then durably enqueues the request. A new command returns
`202`; an exact replay returns the existing representation; reuse of a command
id with a different canonical request returns `409`. Lifecycle eligibility and
fresh repository truth are daemon decisions, so an accepted HTTP request may
later settle as `blocked`.

Command dispositions are `pending`, `accepted`, `blocked` or `failed`.
`accepted` means the bounded operator effect was accepted, not that the linked
Planner/Worker execution completed. Result records carry stable references to
the plan, execution, review, gate or interruption they affected.

## Authentication and browser safety

- On daemon startup, the control service creates or loads a random 256-bit
  bearer secret below `runtime/control/`; the value is never returned by normal
  status/overview routes or written to logs/audit.
- Local CLI/RDC clients read the secret from the configured runtime root and
  send `Authorization: Bearer ...`.
- The same-origin dashboard obtains a short-lived browser session from
  `POST /api/v1/control/browser-sessions`. The server returns a CSRF value and sets an
  `HttpOnly`, `SameSite=Strict` session cookie. Mutation requires both the cookie
  and matching CSRF header.
- Browser-session issuance and browser mutation require a loopback peer, a
  valid `Host`, and `Origin`/Fetch-Metadata consistent with the served 8770
  origin. No wildcard CORS is enabled.
- ChatGPT userscript heartbeat support uses explicit, narrowly allowed ChatGPT
  origins and a separate adapter capability. An authenticated dashboard action
  creates a short-lived, single-use pairing code; the userscript redeems it and
  stores the returned capability in userscript-private storage. The server
  durably stores only its hash and supports explicit revocation. That capability
  can submit session heartbeat/liveness only; it never receives the lifecycle
  bearer secret. Binding and lifecycle actions still pass through the
  authenticated 8770 dashboard/session or another authenticated client.
- Request bodies are JSON-only and capped. Responses use `no-store`,
  `nosniff`, frame denial and a restrictive Content Security Policy.
- A non-loopback 8770 listener remains readable according to existing policy,
  but command and browser-session routes return unavailable. P12 does not claim
  that loopback alone is identity; it uses loopback plus a secret/session and
  stale-state guards.

Authentication session state protects transport access only. Workflow inputs
remain stateless and complete in each durable command envelope.

## Idempotency, crash recovery and audit

- Submission is serialized across threads/processes using the repository's
  proven cross-platform file-lock pattern. Under that lock it checks both inbox
  and history, compares a canonical request hash, and creates at most one record.
- The immutable command request is stored before the API acknowledges it. The
  daemon derives deterministic downstream request IDs from `command_id`.
- The inbox item is removed only after a durable terminal command result exists.
  Restart repeats the same deterministic effect or observes its existing
  lifecycle ledger record; it never launches a duplicate Worker.
- Command history stores the canonical request hash, timestamps, disposition,
  reason, expected/observed identity and effect references. Secrets and raw
  provider credentials are excluded.
- An always-on append-only control audit records request acceptance, dequeue,
  guard rejection, effect acceptance and recovery. P11 accounting may mirror
  correlation events when enabled, but audit correctness does not depend on the
  optional accounting feature.
- Corrupt/torn queue or audit data is quarantined and surfaced as degraded
  control health. It is not silently discarded and does not block unrelated
  read routes.

## Bounded action semantics

All actions are capability-advertised and stale-state guarded:

| Action | Required effect |
| --- | --- |
| `continue` | Reuse current Planner/Transition behavior for the exact pending-design or READY_TO_RUN state. |
| `pause` | Persist a launch barrier. It does not terminate an active execution. |
| `resume` | Clear the exact pause barrier; it does not implicitly start work. |
| `stop` | Persist the pause barrier, then request interruption of the exact active broker execution only when that backend advertises interruption. No PID kill or guessed execution is allowed. |
| `retry` | Retry one exact terminal/recovery target through its existing guarded recovery path. |
| `reconcile` | Reconcile one exact recovery-required ledger item; it is not a generic daemon tick or bypass. |
| `approve_owner_gate` | Approve one exact, current gate after branch/HEAD/task/gate revalidation; approval does not imply arbitrary next-stage authority. |
| `bind_conversation` | Bind one exact live session to one exact unbound project after uniqueness and claimed-request checks. |
| `unbind_conversation` | Write an explicit unbound tombstone for the exact current binding after claimed-request checks. |
| `rebind_conversation` | Atomically move one exact project binding to one exact live session after current/target identity, uniqueness and claimed-request checks. |

If an underlying coordinator does not yet expose a safe adapter for an action,
the action stays unavailable until that adapter and deterministic tests exist.
P12 does not implement a weaker fallback to satisfy UI completeness.

## Conversation-control consolidation

The prior `feature/conversation-control-plane` branch is reference material,
not a branch to merge wholesale. P12 selectively ports its proven concepts:

- runtime-first session and binding registries with static-config migration
  fallback;
- one-project/one-conversation uniqueness and explicit unbound tombstones;
- live-session heartbeat and claimed-request rebind guards;
- exact action identity, persisted intent before actuation and crash
  reconciliation;
- deliberate owner UI actions and no authority from assistant prose.

Those capabilities move under 8770 `/api/v1/control/*`; P12 does not restore a
second 8766 lifecycle-control authority. Any P12-relevant correctness fix that
exists only on `feature/browser-bridge-multiproject` is forward-ported as a
reviewed patch with a regression test, not by merging unrelated branch history.

## Delivery sequence

### P12.1 - Contract and unified read model

- Add pure projection/serialization code and the v1 read routes.
- Aggregate project lifecycle, active roles, watchdog, P11 and AIBroker data.
- Add capability projection, availability/staleness/unknown provenance and
  bounded history.
- Preserve and regression-test all existing read routes.

Exit gate: deterministic fixtures prove one-call state comprehension and broker
failure isolation; no mutation is enabled.

### P12.2 - Command transport, authentication and audit

- Refactor the current control inbox/history behind an atomic command store.
- Add canonical hashing, exact replay/conflict behavior, browser sessions,
  bearer auth, origin/CSRF checks, request bounds and command status reads.
- Wire POST only in the unified daemon; standalone web remains read-only.
- Keep `continue` behavior compatible through the new contract before adding
  more actions.

Exit gate: concurrency and crash tests prove no overwrite, duplicate effect or
silent audit loss; HTTP security tests prove mutations fail closed.

### P12.3 - Guarded actions and conversation integration

- Add lifecycle action adapters one at a time in the order pause/resume,
  approve-owner-gate, retry/reconcile, stop, and add binding actions only after
  the session/binding store is present.
- Forward-port session/binding functionality from the prior control-plane work
  and place it under 8770 without a second authority listener.
- Update CLI and ChatGPT/RDC clients to use the same API contract.

Exit gate: every advertised action has success, stale identity, unsupported,
replay, restart and cross-project isolation tests. Unimplemented actions remain
unavailable.

### P12.4 - Operator UI and acceptance

- Add dashboard sections for projects, active roles, resources/executions,
  P11 evidence, bindings, gates and command history.
- Render controls solely from server capabilities; require confirmation for
  stop and owner-gate approval; show queued and settled command outcomes.
- Run a representative software-only DevOrchestrator scenario through 8770 and
  verify an operator does not need the 8875 page for normal observation/control.

Exit gate: focused UI/API fixtures, full Python/JavaScript regression and a
representative run pass. P12 is complete only after all four exit gates pass.

## Required tests

- Schema snapshots and compatibility tests for all v1 envelopes and legacy GETs.
- Broker unavailable, timeout, malformed response, stale data and unknown-field
  fixtures.
- Atomic same-id submission from threads and subprocesses; exact replay versus
  body conflict; daemon restart at every queue/result crash boundary.
- Loopback, non-loopback, bearer, session cookie, CSRF, Origin, Fetch-Metadata,
  Host, body-size, content-type and unsupported-method cases; pairing tests
  cover expiry, single use, scope, hash-only storage and revocation.
- Fresh versus stale branch/HEAD/task/lifecycle/gate revisions for every action.
- Cross-project command, binding and execution isolation.
- Capability/action agreement: the UI never enables an action the fresh daemon
  will reject under the same snapshot, while revalidation still catches races.
- DOM tests for unavailable/unknown evidence, confirmations, pending command
  polling and error recovery.
- Full `python -m pytest tests_py -q`, Python compilation, JavaScript syntax,
  `git diff --check` and Graphify update.

## Completion acceptance

P12 is COMPLETE only when, from port 8770 alone, an operator can:

1. identify every configured project's repository, task, lifecycle, next
   action, active AI role, watchdog/diagnostic state and owner gate;
2. understand resource/provider/session health and current/recent execution,
   with quota/usage/cost unknowns explicitly preserved;
3. inspect P11 time-loss and failure evidence with durable correlation IDs;
4. see conversation binding/liveness and normal blocking conditions;
5. issue every action that is advertised as available and observe its audited
   result without bypassing daemon authority; and
6. complete a deterministic software-only representative scenario without
   switching to 8875 for normal operations.

## Out of scope

- Public internet exposure, built-in TLS termination, multi-user RBAC or a
  general remote-access product.
- AIBroker provider/account configuration or replacement of its 8875 specialist
  diagnostics.
- Arbitrary prompts, shell commands, file writes, patches or generic workflow
  state mutation through HTTP.
- Scheduler/provider selection redesign, automatic cost optimization or new
  quota inference.
- Force-killing OS processes, weakening owner/hardware gates, or resuming
  `xray-hw-platform`.
- Wholesale branch merges from the old conversation-control or stable branches.

## BLOCKING versus NON-BLOCKING during implementation

Implementation pauses only if fresh repository evidence contradicts the single
daemon authority model, if an action cannot be made crash-idempotent without an
irreversible interface change, or if safe remote authority would require owner
product decisions beyond the loopback/trusted-ingress boundary above.

Serializer layout, local naming, bounded pagination defaults, presentation,
individual exception mapping and additional edge tests are NON-BLOCKING. They
are resolved in implementation, test and remediation without reopening this
overall design.
