# DevOrchestrator Backlog

Longer-horizon work intentionally outside the currently accepted portable
integration V1 slice.

## Machine-independent development environment

Status: **PORTABLE V1 BASELINE DELIVERED 2026-09-04 / FOLLOW-UPS REMAIN**

Delivered:
- clean clone on ZXZ-PC runs the normal automated suite without XLabServer;
- external repositories are registered through project configuration only;
- relative `repo_path` is resolved from the selected config directory;
- `validate-config` checks canonical config, adapter availability and Git truth;
- generic tracked example/configuration documentation no longer requires a
  LabDemo/XLabServer path;
- temporary/fake repositories cover normal Core integration tests.

Remaining candidates:
- verify macOS/Linux portability in CI or equivalent clean hosts;
- decide whether a non-editable packaged distribution is a supported product
  surface; current V1 deployment model is a standalone DevOrchestrator clone;
- isolate any remaining machine-specific browser/runtime deployment overrides;
- add a dynamic third-party ProjectAdapter registration/discovery surface only if a
  real target cannot adopt the existing `agent_files` contract;
- keep a separate opt-in real LabDemo/XLabServer + deployed-browser profile.

## Deployed ChatGPT browser POC

Adapter 0.1.2 real-browser deployment on TS-ZY_PC with XLabServer remains a
valuable integration gate, but it is not a prerequisite for normal Core
feature development or third-party project onboarding.

## Transition Executor / Worker actuation

Still owner-gated. Before implementation, freeze a new contract covering
execution authority, idempotency/replay, project isolation, stop conditions,
and stage/owner boundaries. Do not infer authorization from a valid Web Sol
disposition alone.

## Browser acceptance harness hardening

Follow-up from CCP7 live acceptance:
- add a preflight that verifies the expected process owns Bridge port 8765
  before a live browser case starts;
- fail fast if a stale acceptance Bridge or production daemon already owns the
  port, and report the owning PID/command line;
- keep this as test-harness hardening only; CCP7 transport acceptance itself is
  complete.

## P11 staged roadmap

- **P11-A — Bounded plan-review remediation loop (complete):** bootstrap lifecycle fix. Reviewer rejection auto-revises/re-reviews for at most 3 rounds, then OWNER_GATE.
- **P11-B — Execution accounting foundation (complete):** durable lifecycle/test/retry/accepted-work timing, Effective Development Ratio, plan-review churn, and failure-memory foundation.
- **P11-C — Provider/context/RDC evidence (complete):** context continuity, quota/failover latency, RDC invocation metrics and multi-project isolation probes with deterministic classification.
- **P11-D — Reporting and acceptance (complete):** 8770 dashboard/API, project/task/role timing, evidence-backed bottleneck diagnoses, original-hypothesis comparison, representative DevOrchestrator fixtures, and quantitative acceptance gates. xray-hw-platform remains paused unless separately authorized.

## P12 staged roadmap

- **P12 — Unified AI Control Surface (complete 2026-09-15):** port 8770 is the normal operator surface for project lifecycle, active AI roles, AIBroker resources/executions, conversation bindings, P11 evidence and guarded owner actions.
- Delivery is bounded into P12.1 unified reads, P12.2 authenticated/idempotent command transport and audit, P12.3 guarded actions plus conversation-control consolidation, and P12.4 dashboard/client acceptance.
- The daemon and existing `ControlCommandCoordinator` remain the only mutation authority. Existing GET routes remain compatible, standalone web remains read-only, and 8875 remains the broker-specialist configuration/diagnostic surface.
- Detailed frozen design: `docs/UNIFIED_AI_CONTROL_SURFACE_DESIGN.md`.
- Acceptance passed with atomic/replay, corruption quarantine, security, paired ChatGPT heartbeat, authenticated CLI, stale broker provenance, every-action revision, cross-project isolation, conversation, UI and representative 8770 fixtures plus the full 509-test/16-subtest regression. Actions without an exact safe existing authority adapter remain capability-advertised as unavailable rather than gaining a weaker fallback.
- Post-acceptance selective branch convergence added the P12-native exact Planner `approve_owner_gate` adapter without restoring CCP 8766 or `start_current_task`. All mutation paths now require the complete projected identity, and settlement recovery repairs a missing terminal audit before inbox removal. The old adjudicator remains intentionally retired because bounded plan remediation already exhausts to a durable fail-closed owner gate.

## P12.7 Web Control Surface Visual Refresh

Status: **FUTURE / design constraint frozen 2026-09-17**

- Refresh the port 8770 Operations Console using https://opencode.ai/data as a visual/information-architecture reference, not a branding or component-copy target.
- Direction: high information density, restrained surfaces/dividers, strong numeric/status hierarchy, explicit freshness, useful trends plus compact ranked tables, and substantially less decorative card elevation.
- Preserve P12 lifecycle authority and all guarded control semantics; P12.7 is a client-side information-architecture/visual refresh, not a control-plane redesign.
- Adapt reference concepts to DevO: execution trends, project health, provider/model utilization, bottleneck/recovery ranking, watchdog incidents and accounting evidence. Do not reproduce irrelevant OpenCode metrics.
- Begin with a fixture-backed low-cost prototype; validate hierarchy before production styling changes.
- Frozen design: docs/P12_7_WEB_UI_DESIGN.md.
- P12.7 starts only after P12.6 closure and does not replace the P13/P14 roadmap; P13 is the remote-execution resilience foundation and P14 is the Android/mobile client track.

## P13 staged roadmap

### P13 - Remote Execution Resilience & Recoverable Jobs

Goal: make long-running RDC/remote-shell work recoverable when the ChatGPT message/tool-result path times out, reconnects, or loses a synchronous response, without duplicating side effects or losing authoritative execution state.

- Treat `Message delivery timed out` as an ambiguous transport/orchestration outcome, not proof that the remote command failed. DevO must distinguish remote process state, RDC transport state, and ChatGPT/tool-result delivery state.
- Replace long synchronous remote-shell calls with durable asynchronous jobs where execution can exceed the bounded interactive-call window. `START` must return a durable `job_id`; later `STATUS`, `TAIL`, and `RESULT` operations recover state without re-running the command.
- Persist job identity, command/request identity, project/task/stage correlation, PID/process handle where available, start/end timestamps, exit code, bounded stdout/stderr references, heartbeat/progress evidence, and terminal result before acknowledging completion.
- Make mutating remote actions idempotent. Retrying a ChatGPT turn, RDC request, or DevO command with the same semantic `command_id`/`job_id` must reconcile to the existing execution rather than launching a duplicate side effect.
- Separate remote execution from message delivery: a ChatGPT/RDC delivery timeout must not terminate a healthy background job, and reconnect must permit status recovery from durable DevO state.
- Add bounded-output behavior: long stdout/stderr is written to durable log/evidence storage; interactive responses return a capped tail/summary instead of transporting unbounded build/test output through one tool call.
- Add long-operation classification/heartbeat so builds, tests, package operations, and provider runs can report `running` with evidence rather than being mislabeled as stalled merely because no terminal output has arrived.
- Prefer short single-purpose shell steps over large chained commands. Build/configure/test/git/diagnostic phases should be separately attributable and recoverable; Windows PowerShell 5.1 `&&`/`||` remains prohibited by verified failure memory.
- Define explicit states at minimum: `queued`, `running`, `completed`, `failed`, `cancel_requested`, `cancelled`, `unknown/recovery_required`, plus transport connectivity/freshness evidence independent of job lifecycle.
- Integrate with watchdog/accounting: distinguish expected long-running work, remote-process hang, RDC disconnect/reconnect, lost tool-result delivery, and true no-progress stalls; expose the classification on the 8770 control surface and future P14 mobile client.
- `Retry` after an ambiguous timeout must be fail-safe: first reconcile the prior command/job and only create new work when durable evidence proves no equivalent execution exists.
- Preserve DevOrchestrator as lifecycle authority. RDC/remote agent owns process execution evidence only and must not independently advance project task/review/remediation state.

Acceptance: intentionally interrupt or time out the ChatGPT/RDC response path during a long-running test/build, verify the remote job continues, reconnect from a later turn, recover the same `job_id` and bounded logs/result, and prove repeated retry/reconcile operations create no duplicate side effects. Also verify deterministic classification for remote process still running, process completed but result delivery lost, RDC disconnected, command genuinely failed, and state genuinely unknown.

Sequence: P13 follows P12.7 in the staged roadmap and establishes the durable job/idempotency contract before mobile work. P14 must consume this authoritative execution state rather than introducing a second monitoring model.

## P14 staged roadmap

### P14 - Mobile Control & Observability

Goal: provide an Android-native, failure-independent observation and bounded-control client for DevOrchestrator so project execution remains visible and controllable even when a ChatGPT conversation, browser session, or RDC interaction is stalled or unavailable.

- Build the Android client on the stable P12 Control API; do not create a second lifecycle/control authority in the mobile application.
- Connect Android directly to DevOrchestrator over the private Tailscale path; do not require RDC or a ChatGPT conversation in the normal mobile data/control path.
- Show all configured projects with lifecycle/task/stage state, current role/worker, AI provider/model, current and next action, last-progress age, watchdog/diagnostic state and OWNER_GATE conditions.
- Provide a project event timeline with execution/review/remediation/retry milestones and enough evidence to distinguish a genuinely running worker from stale orchestration state.
- Use a push-style event channel (SSE or WebSocket, selected during P12 API design) for near-real-time updates; reconnect with bounded backoff and reconcile from authoritative DevO state after disconnects.
- Provide guarded mobile controls for continue, pause/resume where supported, stop, retry/reconcile and OWNER_GATE actions; all mutations must use P12 DevO authority, idempotency and audit paths.
- Treat ChatGPT, Android, Web Dashboard and CLI as stateless control/observation clients. No authoritative project state may live in the Android application or a ChatGPT conversation.
- Preserve explicit `unknown`, disconnected and stale states instead of presenting cached data as live execution.
- Initial POC should stay small: project list/detail, event stream, connection/reconnect handling, stall alerting, and the minimum bounded controls needed to validate the architecture before expanding UI scope.
- Stall alerting is a P14 core requirement, not a later enhancement. Consume authoritative DevO/P11-P12 progress timestamps and stall/watchdog state rather than inferring progress only from Android-side timers.
- When an actively executing development project exceeds its configured no-progress threshold, raise an Android notification with vibration and an optional audible alarm; the alert must identify project, task/stage, last-progress age and the best-known stall/diagnostic reason.
- Support per-project/global alert policy including enable/disable, no-progress threshold, sound/vibration mode, quiet-hours behavior and acknowledgement/snooze. Deduplicate repeated alerts for the same stall episode and re-arm only after authoritative progress resumes or the stall state materially changes.
- Do not alert merely because a legitimate long-running build/test/review has produced no UI event: DevO should expose heartbeat/expected-long-operation evidence so the client can distinguish expected waiting from loss of progress. Disconnected/unknown state is a separate connectivity alert class and must not be mislabeled as project stall.
- Later P14 increments may add voice capture/control and Bluetooth-ring navigation/confirm/cancel, reusing the same Control API rather than introducing a separate command channel.
- Acceptance: with the ChatGPT/browser path intentionally unavailable, an operator on Android over Tailscale can determine whether a selected project is actively progressing or stalled, inspect recent authoritative events, execute supported bounded controls without RDC or direct shell access, and receive a vibration/audible notification when an active development project enters a confirmed no-progress condition.

Sequence: P14 follows P13. P12 owns the lifecycle/control contract; P13 adds durable recoverable remote-job state; P14 consumes both contracts and must not duplicate DevOrchestrator lifecycle or execution authority.
