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
- P12.7 starts only after P12.6 closure and does not replace the P13-P15 roadmap; P13 establishes transport-independent control/execution, P14 adds durable recoverable jobs, P14.5 adds the Reviewer Harness/OpenCodeReview adapter, and P15 is the Android/mobile observability track.

## P13 staged roadmap

### P13 - Transport-Independent Control Bridge

Goal: remove RDC as the normal ChatGPT-to-DevO control dependency while preserving DevOrchestrator as the sole lifecycle authority.

- Add a Control Adapter boundary above the existing P12 Control API; adapters translate transport-specific requests only.
- Provide an MCPAdapter MVP for bounded semantic status/log/control operations; do not expose unrestricted remote shell.
- Stabilize WebBridgeAdapter as a secondary path with durable request identity, freshness, stale-binding detection and fail-closed response handling.
- Separate ExecutionTransport from control transport: LocalTransport first, SSHTransport over Tailscale for remote hosts, RDCTransport only as fallback/emergency GUI/debug.
- Preserve idempotency, expected-revision/repository-truth guards, authentication/origin protections, audit and explicit stale/unknown states.
- Acceptance: with RDC unavailable, ChatGPT through a supported adapter can inspect authoritative DevO state, issue bounded lifecycle controls, and DevO can execute through Local/SSH without changing lifecycle authority.

Sequence: P13 follows P12.7 and removes the current RDC communication bottleneck before durable remote-job expansion.

## P13.5 staged roadmap

### P13.5 - Canonical Dashboard Sidebar Migration

Goal: migrate the left-sidebar dashboard information architecture from `DevOrchestrator-dashboard-redesign` into the canonical web control surface without regressing P12.7 behavior.

- Treat `C:\\work\\github\\DevOrchestrator-dashboard-redesign` as read-only UI reference; do not merge that branch wholesale.
- Replace the canonical top-tab navigation with a persistent left sidebar.
- Preserve all current canonical project, run, resource, watchdog, accounting and control capabilities.
- Organize the sidebar around Overview, Projects, AI Resources, Usage/Accounting, Logs and System while keeping safety-critical controls discoverable.
- Preserve authentication, CSRF/origin/Host protections, stale/unknown rendering and fail-closed control behavior.
- Keep this phase bounded to UI/information architecture; lifecycle, AIBroker routing and P13 transport contracts remain unchanged.
- Acceptance: all pre-migration capabilities remain reachable and functional, P12/P12.7 regression stays green, and new navigation/layout tests cover the sidebar behavior.

Sequence: P13.5 follows P13 and precedes P14 so the canonical dashboard structure is settled before durable-job state expands the UI further.

## P14 staged roadmap

### P14 - Remote Execution Resilience & Recoverable Jobs

Goal: make long-running local/SSH/remote execution durable and recoverable across client, transport or tool-result interruption without duplicate side effects.

- Use durable asynchronous jobs with stable job_id/request_id and queued/running/completed/failed/cancelled/unknown-recovery states.
- Persist execution identity, project/task/stage correlation, process evidence, timestamps, bounded logs, heartbeat/progress and terminal result.
- Make retries idempotent and reconcile ambiguous timeouts before creating new work.
- Separate execution from message delivery; disconnect must not terminate a healthy job.
- Add bounded logs, long-operation classification, cancel/retry/reconcile, daemon-restart recovery and watchdog/accounting integration.
- Preserve DevO lifecycle authority; ExecutionTransport owns execution evidence only.
- Acceptance: interrupt a long build/test, recover the same job_id/log/result later, and prove retries create no duplicate side effects.

Sequence: P14 follows P13 and builds recoverable jobs on the transport-independent boundary established by P13.

## P14.5 staged roadmap

### P14.5 - Reviewer Harness & OpenCodeReview Adapter

Goal: standardize code-review preparation, project rule enforcement, model delegation and structured findings without making any review engine or model the DevO lifecycle authority.

- Introduce a provider-neutral `ReviewerHarness` boundary owned by DevO; OpenCodeReview is the first adapter/backend, not a hard architectural dependency.
- Use OpenCodeReview for deterministic diff/full-scan preparation, file selection, rule packs, review sessions, coverage and structured finding localization.
- Keep reviewer model selection in AIBroker by role/quota/cost policy; OCR must not directly own provider selection when running under DevO.
- Support delegated review so Codex/Claude/Web Sol can perform semantic reasoning while OCR supplies bounded context and project-specific rules.
- Normalize results into a DevO finding contract with file/line/severity/category/rule/evidence/reviewer/status plus JSON/SARIF artifact retention.
- Preserve independent build/test/static-analysis gates; OCR findings complement compiler/tests/clang-tidy/cppcheck and do not replace them.
- Run review as a durable P14 job with stable job/session identity, resumable evidence and idempotent retry/reconcile semantics.
- First qualification target: LabDemo, with rules covering Service-only hardware authority, fail-safe X-ray OFF convergence, manual-vs-transactional scan separation, state-machine reachability and protection against false dead-code deletion of compatibility paths such as `scan.run`.
- Acceptance: a representative repository can run diff review and bounded full scan without RDC as the normal transport, delegate to an AIBroker-selected reviewer, persist structured findings/coverage, survive transport interruption via P14 recovery, and feed evidence to DevO without allowing OCR to issue lifecycle decisions directly.

Sequence: P14.5 follows P14 because review sessions must rely on durable/recoverable jobs, and precedes P15 so mobile observability can consume normalized review/job/finding state from the start.

## NEXT - Watchdog recovery-epoch / stale OWNER_GATE cleanup

Status: **COMPLETE 2026-09-20**

Goal: prevent a historical watchdog-generated OWNER_GATE/recovery budget from contaminating a newer valid execution epoch, while preserving genuine owner-decision gates.

- Define a durable recovery-epoch identity from current task/plan/control command/HEAD/execution evidence.
- When newer valid execution evidence supersedes the evidence that created a watchdog-generated OWNER_GATE, invalidate the stale watchdog gate, attempts, diagnosis and evidence hash automatically.
- Never auto-clear an explicit owner-decision OWNER_GATE; only watchdog/recovery-generated stale gates are eligible.
- Project health/watchdog projection must not report historical owner_gate/unknown while a newer authoritative execution is healthy.
- Reset attempts_this_run and recovery budget only on a proven epoch transition, not merely because a worker process appeared.
- Add regression coverage for stale historical gate cleanup, genuine owner gate preservation, restart/replay, and new Worker/Reviewer epochs.
- Acceptance: reproduce the current P14.5 case (new WORKER_RUNNING/EXECUTING while watchdog retains old owner_gate/attempts=20), advance to a newer epoch, and prove automatic cleanup without a manual watchdog-clear or user continue.

Sequence: execute immediately after P14.5 closes, before Stabilization Gate and P15.

## P14.6 - Unattended Execution Stabilization Gate

Status: **NEXT / REQUIRED BEFORE P15**

Goal: prove that the existing DevO control/recovery stack can sustain real unattended development before adding another client surface.

- Consolidate Planner protocol handling into Raw Capture -> JSON Extract -> Normalize -> Schema Validate -> Semantic Validate -> Reviewer, with bounded format repair for schema-only failures.
- Verify provider/session/quota failures trigger resource failover without consuming semantic-remediation budget or requiring user continue.
- Require canonical lifecycle/health/watchdog projections to agree on the current recovery epoch; historical failure evidence must not poison a newer task/plan/execution.
- Exercise automatic Review -> Remediation -> Re-review -> completion and automatic promotion/launch of the next eligible task.
- Run a real 4-6 hour unattended development workload and inject at least: malformed AI schema output, provider quota/unavailable, reviewer rejection, execution interruption, and daemon restart.
- Acceptance requires automatic recovery/failover/remediation/re-review/next-task progression with manual continue count = 0.
- Repeat representative unattended runs 2-3 times before declaring the gate stable enough for P15.
- Preserve durable evidence for every injected fault, recovery decision, provider switch and lifecycle transition so failures are diagnosable rather than hidden by retries.

Sequence: P14.5 -> watchdog recovery-epoch cleanup -> P14.6 Stabilization Gate -> P15.

## P15 staged roadmap

### P15 - Mobile Observability & Guarded Control

Goal: provide an Android-native, failure-independent observation and bounded-control client without creating a second lifecycle authority.

- Connect Android directly to DevO over Tailscale and consume authoritative P12-P14.5 lifecycle, durable-job and normalized review/finding state.
- Show project/task/stage, worker/reviewer/provider, current/next action, job progress, freshness, watchdog/diagnostics and OWNER_GATE.
- Provide reconnectable event updates and guarded continue/pause/resume/stop/retry/reconcile/OWNER_GATE controls through DevO authority.
- Raise deduplicated vibration/optional sound alerts for confirmed no-progress; disconnected/unknown remains a separate alert class.
- Support per-project/global alert policy, thresholds, quiet hours, acknowledgement and snooze.
- Initial scope: project list/detail, event stream, reconnect, stall alerts and minimum guarded controls; voice/Bluetooth-ring interaction is later scope.
- Acceptance: with ChatGPT/browser/RDC unavailable, Android can observe authoritative execution, distinguish running/stalled/disconnected states, receive alerts and execute supported guarded controls.

Sequence: P15 follows P14.5 and consumes existing lifecycle, transport, durable-job and reviewer-harness contracts.

## P16 staged roadmap

### P16 - AI Capability Benchmark Project

Goal: create an isolated benchmark project that measures the real capability, efficiency and reliability of the AI resources available to DevO/AIBroker, and separates model capability from retrieval/tooling effects.

- Build a frozen benchmark repository with seeded defects, architecture/cross-file reasoning questions, implementation tasks, review tasks and root-cause/debugging tasks with known ground truth.
- Evaluate resources by role: Planner, Reviewer, Worker/Implementer and Debugger/Root-cause Analyst.
- Discover the active resource pool at runtime and include representative Codex, Claude, ChatGPT Web Sol, AGY Gemini Pro/Flash and Copilot resources when available.
- Run repeated trials under fixed task/prompt/revision/tool policy/timeout/scoring conditions.
- Capture correctness, evidence completeness, false findings, missed findings, patch validity, regressions, tool calls, reported tokens, wall time, retries/failover and quota/session metadata.
- Keep benchmark execution isolated from production repositories.

#### zvec-grep Repository Retrieval A/B track

- Track A uses native project tools only: rg/grep, file reads, git, tests and normal agent tools.
- Track B uses the same AI and task plus zvec-grep for semantic/BM25/vector discovery, followed by exact source verification.
- Hold model, prompt, task, repository revision, timeout and scoring constant between A/B pairs.
- Measure whether zvec-grep reduces discovery time, tool calls and context/token consumption while preserving or improving correctness and evidence quality.
- Use semantic retrieval for unknown-location/cross-file/architecture/design-rationale tasks; keep known identifiers, regex and exhaustive occurrence searches on native rg/grep.
- Treat zvec-grep as a discovery/ranking layer only; repository truth, exact source reads, tests and lifecycle authority remain independent.

#### Promotion gate into Agent Harness

- Do not make zvec-grep a mandatory DevO dependency from a single positive run.
- Promote it into Agent Harness as the shared Repository Retrieval layer only after repeated representative A/B runs show meaningful benefit without unacceptable stale-index, false-retrieval, latency, privacy or maintenance costs.
- If promoted, Planner/Reviewer/Worker may consume one shared workspace index through a bounded Harness interface, reducing repeated repository discovery after model failover.
- Preserve a native-tool fallback whenever zvec-grep or its index is unavailable.

Acceptance: the benchmark can reproducibly compare at least three AI resources across multiple role/task classes, produce machine-readable and human-readable results, execute at least one native-vs-zvec-grep paired experiment, and provide evidence sufficient for a documented promotion/no-promotion decision.

Sequence: P16 follows P15. It is a benchmark/test-project phase rather than a production DevO feature phase.
