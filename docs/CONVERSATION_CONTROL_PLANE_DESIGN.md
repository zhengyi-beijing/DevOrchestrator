# Conversation Control Plane V1

Status: DESIGN FROZEN / TEST-FIRST
Baseline: `0ca7fe1` on `feature/conversation-control-plane`

## Goal

Remove the operational requirement to copy a ChatGPT conversation URL/ID into `config/projects.json` by hand. DevOrchestrator must discover live ChatGPT sessions, let the owner pair one session with one project, persist that pairing outside Git project configuration, and expose explicit owner-gate controls without weakening existing Browser Bridge transport boundaries.

## Invariants

- Browser Bridge `:8765` remains transport-only for Web Sol claim/renew/response.
- Project/repository truth and workflow decisions remain owned by DevOrchestrator Core.
- Binding mutation is explicit owner action; ChatGPT assistant text is never authority to bind, approve, start, stop, or rebind.
- Runtime bindings are machine/browser state and therefore live under `runtime/`, not tracked project config.
- Static `conversation_binding` remains a migration fallback until a runtime binding exists.
- One live ChatGPT conversation may bind to at most one project and one project to at most one conversation.
- Rebind/unbind fails closed while the project has an active claimed Web Sol request.
- No hardware action is introduced by this feature.

## Topology

```text
ChatGPT tab + Userscript
  |-- Web Sol transport ------------------> :8765 Browser Bridge
  |-- session heartbeat / owner controls -> :8766 Control Plane
                                             |
DevOrchestrator daemon ----------------------+-- runtime session registry
                                             +-- runtime binding registry
                                             +-- explicit owner commands
                                             +-- :8770 read-only dashboard/status
```

## Runtime session registry

Each ChatGPT tab with a stable `/c/<conversation-id>` URL periodically registers:

```json
{
  "adapter": "chatgpt_web",
  "binding_id": "<conversation-id>",
  "title": "<best-effort document title>",
  "url": "https://chatgpt.com/c/<conversation-id>",
  "tab_instance_id": "<ephemeral random id>"
}
```

Daemon persists `first_seen_at`, `last_seen_at`, title/url metadata and active tab instances. Session liveness is derived from heartbeat age; stale sessions remain visible for recovery/history but cannot silently acquire a new project binding.

## Runtime binding registry

Canonical runtime file: `runtime/conversation-bindings.json`.

Each record contains project id, adapter, binding id, title/url snapshot, `bound_at`, `updated_at`, and provenance (`owner_control`). Binding lookup uses runtime first, then the existing static project binding only when no runtime record exists.

Static config remains valid for migration and fail-safe rollback. Control Plane mutations never rewrite `config/projects.json`.

## Binding state

```text
UNBOUND -> BOUND -> STALE
   ^         |
   +---------+  owner unbind/rebind
```

`BOUND` requires a valid runtime/static route plus live session presence. `STALE` means the route exists but the conversation heartbeat has expired. Web Sol dispatch remains gated on live adapter presence as today.

## Control Plane HTTP surface

Dedicated local listener, default `127.0.0.1:8766`:

- `GET /v1/health`
- `GET /v1/sessions`
- `GET /v1/bindings`
- `GET /v1/projects`
- `POST /v1/session/heartbeat`
- `POST /v1/bind`
- `POST /v1/unbind`
- `POST /v1/rebind`
- `POST /v1/owner-action`

Mutation endpoints accept only loopback requests and validate complete project/session identity. No generic shell or arbitrary prompt endpoint is added.

## Owner gate control

`POST /v1/owner-action` carries an explicit owner intent such as `approve_next_stage`, `stop`, or `start_current_task`, plus project id and the expected current branch/HEAD/gate identity. The daemon re-reads fresh repository/project state immediately before accepting it.

Assistant text such as `Owner approve` is informational only. A visible owner click in the Userscript panel or Dashboard is required to create an owner action.

For V1, owner actions reuse Transition Executor guards; they do not bypass repository truth, allowed-next-action policy, phase gates, dirty-worktree protection, or hardware gates.

## Rebind safety

A project cannot rebind/unbind while an exact Web Sol request is `CLAIMED`. `PENDING` requests may remain frozen and resume on the newly bound conversation only if the request has not been delivered to a previous conversation. `RESPONDED` history is immutable and retains its original binding identity.

## Userscript UX

The existing status badge becomes interactive and reports project identity, not merely transport health:

```text
DevOrch · UNBOUND
DevOrch · labdemo · BOUND
DevOrch · labdemo · STALE
```

Clicking opens a compact panel showing current conversation title/id, live projects, current binding, and explicit `Bind`, `Unbind`, `Rebind`, and owner-gate buttons. The panel never invents workflow state; it renders daemon responses only.

## Dashboard UX

The existing dashboard stays primarily observational but may link to the Control Plane UI. Each project shows configured/runtime binding source, conversation title/id, live/stale state, last seen time, and the current owner gate. Binding mutation remains routed through the Control Plane.

## Delivery plan

1. CCP1 — session registry + runtime binding store + tests. **IMPLEMENTED / GREEN**
2. CCP2 — effective-binding resolver and Core integration; static-config migration fallback. **IMPLEMENTED / GREEN**
3. CCP3 — dedicated `:8766` Control Plane HTTP API + daemon lifecycle integration.
4. CCP4 — interactive Userscript badge/panel + heartbeat/session discovery.
5. CCP5 — dashboard binding visibility and navigation.
6. CCP6 — explicit owner-gate action path and transition integration.
7. CCP7 — recovery/conflict/multi-tab tests, full regression, live software-only acceptance.

## CCP2 implementation checkpoint

- Effective route resolution is runtime-first; static `conversation_binding` is used only when no runtime record exists.
- A malformed runtime record suppresses static fallback and leaves the project monitor-only.
- Effective route uniqueness is rechecked after overlay, including runtime-vs-static collisions.
- Monitor snapshots carry `conversation_binding_source` and therefore feed Dispatcher and Response Consumer the effective route without changing their transport contracts.
- Regression evidence on ZXZ-PC: CCP1+CCP2 targeted tests 12/12 PASS; full `tests_py` 147/147 PASS; `node --check` PASS; `git diff --check` PASS.
- Software-only live smoke against the existing three-project config confirmed static fallback for all projects and a temporary runtime override for `labdemo` while the other projects remained on static routes.

## Acceptance

- New conversation can be discovered and bound to `labdemo` without editing `projects.json`.
- Restarting the daemon preserves the runtime binding.
- Opening the bound conversation restores live presence automatically.
- Duplicate project/conversation bindings are rejected atomically.
- Claimed Web Sol work cannot be cross-routed by rebind.
- Owner approval requires a deliberate owner UI action and cannot be triggered by assistant prose.
- Existing Web Sol queue, Decision Guard, Transition Executor, and all prior tests remain green.

## CCP3 implementation checkpoint

- Added a dedicated loopback-only Control Plane listener, default `127.0.0.1:8766`.
- Read surface: `GET /v1/health`, `/v1/sessions`, `/v1/bindings`, `/v1/projects`.
- Mutation surface: `POST /v1/session/heartbeat`, `/v1/bind`, `/v1/rebind`, `/v1/unbind`.
- `POST /v1/owner-action` is deliberately reserved and returns `501` until CCP6; assistant prose still has no owner authority.
- The unified daemon owns Web `:8770`, Browser Bridge `:8765`, and Control Plane `:8766` in the same PID and persists `control.json` plus control address/port in `daemon.json`.
- Control listener configuration is restricted to explicit loopback addresses; no shell, Worker, prompt, or generic workflow endpoint exists.
- Rebind/unbind re-read the effective project route and fail closed while the current Browser Bridge route has a still-valid `CLAIMED` Web Sol request.
- Explicit unbind now persists a runtime `unbound` tombstone so legacy static config cannot silently reappear as the effective route.
- Runtime/static route collisions, unknown projects, undiscovered/stale target conversations, and duplicate effective routes fail closed.
- Regression evidence on ZXZ-PC: CCP control/daemon targeted tests **8/8 PASS**; full `tests_py` **156/156 PASS**; `node --check` PASS; `git diff --check` PASS.

CCP3 remains software-only and is not deployed into the currently running production daemon yet.
