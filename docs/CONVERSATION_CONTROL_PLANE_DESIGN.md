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

## CCP4 implementation checkpoint

- ChatGPT Web adapter advanced to `0.2.0` and now heartbeats the current `/c/<conversation-id>` session to `127.0.0.1:8766` every 15 seconds.
- A per-tab `tab_instance_id` is persisted in `sessionStorage`; title, URL, adapter, binding id and tab identity are registered without any project id hard-coded in the userscript.
- The floating badge is now clickable and renders daemon-derived conversation state: `UNBOUND`, `<project> · BOUND`, `STALE`, or `CONTROL OFFLINE`.
- Clicking the badge opens a compact project-binding panel populated from `GET /v1/projects`; Bind/Rebind/Unbind are sent only from explicit button clicks.
- No `/v1/owner-action` call or workflow decision logic is present in CCP4; owner-gate actuation remains reserved for CCP6.
- Existing Browser Bridge claim/renew/response behavior is retained, including the current `0.1.6` composer-submit compatibility fix and stop-button rejection.
- ChatGPT SPA navigation is tolerated because every heartbeat and transport poll re-derives the binding id from the live URL.
- Live browser deployment/acceptance is intentionally deferred to CCP7; CCP4 does not restart or modify the currently running production daemon.
- Regression evidence on ZXZ-PC: CCP4/userscript targeted tests **12/12 PASS**; full `tests_py` **162/162 PASS**; `node --check` PASS; `git diff --check` PASS.

## CCP5 implementation checkpoint

- The read-only dashboard now exposes `GET /api/conversations`, projecting effective project-to-conversation mappings from monitor snapshots plus the runtime binding/session registries.
- Runtime binding records override stale static snapshot routes immediately; explicit runtime `unbound` tombstones remain authoritative and malformed runtime records fail closed.
- Each project is rendered as `BOUND`, `STALE`, or `UNBOUND` with binding source, conversation title, binding id, adapter, last-seen time, and active-tab count.
- For known ChatGPT bindings the dashboard provides a read-only `Open conversation` navigation link; binding mutations remain in the CCP4 ChatGPT panel and the dashboard still accepts only GET/HEAD.
- Static bindings with no live CCP heartbeat are shown as `STALE`, not falsely `BOUND`; live state is derived from the same CCP1 session-presence policy.
- CCP5 targeted dashboard/web tests **6/6 PASS**; full `tests_py` **165/165 PASS**; `node --check` PASS; `git diff --check` PASS.
- CCP5 is not deployed into the currently running production daemon yet.

## CCP6 implementation checkpoint

- `POST /v1/owner-action` now accepts only explicit loopback owner actions: `approve_next_stage`, `start_current_task`, and `stop`.
- Owner actions must originate from the project's currently bound, live ChatGPT conversation and echo fresh branch/HEAD identity; assistant prose alone has no authority.
- `approve_next_stage` records an exact approved owner gate but does not start a Worker. `start_current_task` reuses Transition Executor fresh-truth, clean-worktree, READY_TO_RUN, task-id, one-active-run, and backend policy guards.
- `stop` is a durable pause of future automatic launches; it deliberately does not kill an already-running Worker. Runtime owner state suppresses legacy static `owner_start`/`bootstrap` resurrection.
- Owner control state is persisted in `runtime/owner-control.json`; action ids are idempotent and project/action scoped.
- The ChatGPT Userscript exposes owner actions only as deliberate buttons in the bound conversation panel.
- The read-only Dashboard now projects owner pause and latest owner-gate identity/state alongside each conversation binding.
- Windows test cleanup now waits for the managed Worker thread to unwind after terminal ledger persistence before deleting temporary Git repositories, eliminating the WinError 32 race.
- CCP6 targeted control/daemon/transition/UI tests: **34/34 PASS**.
- Full `tests_py`: **170/170 PASS**; `node --check` for Userscript and Dashboard: PASS; `git diff --check`: PASS.
- CCP6 remains software-only and is not deployed into the currently running production daemon yet.
## Fresh technical review checkpoint — CCP4 + CCP5 + CCP6

- Final review scope covered the complete working-tree diff from `210b6dd`, including the four new files that are not represented by ordinary `git diff --stat` until staged.
- Review disposition: **ACCEPTED / READY TO COMMIT**. No unresolved HIGH/MEDIUM correctness or authority-boundary finding remains.
- The review hardened owner-action replay to require exact conversation/repository/task-or-gate identity, not only project/action identity.
- `start_current_task` now persists launch intent before actuation and reconciles the `owner-control:<action_id>` Transition ledger after a crash window without duplicate Worker launch.
- Gate approval rechecks fresh repository truth server-side and rejects dirty worktrees; pause barriers fail closed when decision timestamps are absent or invalid.
- `stop` remains available from the currently bound live conversation even if repository HEAD moved, while Start/Approve retain fresh branch/HEAD guards.
- Final Worker launch is serialized with owner-control state and rechecks pause/static-start suppression immediately before ledger insertion; active Web Sol claims block Start/Approve but never block Stop.
- Runtime owner-control adoption suppresses legacy static `owner_start`/`bootstrap` before a Start result is known, preventing legacy authority resurrection on failure/retry paths.
- Fresh targeted CCP4/CCP5/CCP6 verification: **41/41 PASS**; full `tests_py`: **178/178 PASS**.
- `node --check browser/chatgpt-web-adapter.user.js`: PASS; `node --check web/app.js`: PASS; changed Python modules compile cleanly.
- Final `git diff --check`: PASS. Only Git's existing LF→CRLF working-copy notices remain for two text files; they are non-blocking and introduce no whitespace error.
- Final status contains only the intended CCP4/CCP5/CCP6 source, UI, documentation, and test files; no temporary review/patch file remains.
