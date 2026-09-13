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

## Operational roadmap after AIBroker integration

Status: **ACTIVE BACKLOG 2026-09-11**

The target operating model is: ChatGPT/mobile/CLI are stateless control inputs;
DevOrchestrator owns project/task lifecycle; AIResourceBroker owns provider,
account, model and execution selection; Git plus runtime state remain the
authoritative record.

### Adoption maturity gates

**Gate A - Controlled use: AVAILABLE NOW for migrated projects.**
A project using `execution.engine=aibroker`, direct Planner/Reviewer roles and
the `agent_files` contract can already be driven with `project-status` and
`project-continue`. Human observation is still recommended.

**Gate B - Daily use: next target.** Required before making DevO the default
entry point for normal development:
- finish role-aware resource routing (`dispatch_roles`, `role_priority`, quality,
  independence) and register the intended Sol/Terra/Opus/Web-Sol resources;
- add durable per-project context: goals, architecture constraints, protected
  scope, build/test commands, deployment assumptions and key decisions;
- migrate remaining production projects away from legacy browser-bound Worker
  routing to `execution.engine=aibroker`;
- expose one stable Control API used by dashboard, ChatGPT/RDC, CLI and later
  mobile clients;
- deliver one unified 8770 dashboard showing project lifecycle, current role
  runs, AI resources, executions, usage and OWNER_GATE events.

**Gate C - Unattended use: later target.** Required before trusting long-running
continuous execution without routine supervision:
- checkpoint role runs at meaningful boundaries and reconcile after restart;
- add task DAG/dependency support and bounded parallelism;
- make quota/cost/burn-rate constraints first-class scheduling inputs;
- add provider-independent review defaults for critical work;
- add automatic onboarding that scans a repository and proposes project config,
  context and validation commands;
- add explicit operating modes such as `economy`, `normal` and `critical`.

### Role model expansion

Evolve beyond Planner/Worker/Reviewer to a stable semantic set:
`Planner`, `Implementer`, `Reviewer`, `Remediator`, `Researcher`, and `Release`.
One AI resource may qualify for several roles with different per-role priority;
do not force one model into exactly one role. Critical reviews should prefer a
different provider from the implementation resource when possible.

### Unified management surface

Extend the 8770 dashboard into the primary control plane. It should show project
task/stage/lifecycle state, active Planner/Worker/Reviewer, next action, AI
resource health/auth/quota/session state, role routing, execution chains, usage,
events and OWNER_GATE conditions. Keep 8875 as the broker-specialist surface and
API, but avoid requiring operators to switch between pages for normal work.

### Mobile ChatGPT + ring + voice control

Use the same stateless Control API from the Android/ChatGPT entry point. The
Bluetooth ring is an input device only: center toggles voice capture; directional
buttons navigate/cancel/confirm. No authoritative project state lives on the
phone or in the ChatGPT conversation.

### Bounded self-evolution

Self-evolution means evidence-driven improvement, not unrestricted self-modifying
production code. Build a closed loop from telemetry and review outcomes to
candidate policy changes:
- learn role/resource success rate, latency, cost/usage, retry/remediation rate
  and review disagreement from durable telemetry;
- propose changes to `role_priority`, quality mapping, reserve thresholds,
  prompts/templates and operating-mode policy;
- version every candidate and evaluate it by replay/simulation plus regression
  before promotion;
- record why a candidate was proposed, expected benefit, evidence and rollback
  target;
- initially require OWNER_GATE approval for every promoted change;
- later allow bounded auto-promotion only for low-risk configuration changes
  with explicit limits, automatic rollback and no safety/authorization impact;
- never let the evolution loop expand repository scope, bypass review, weaken
  hardware/safety gates, modify credentials, or grant itself new authority.

Longer-horizon evolution may include project retrospectives, prompt A/B testing,
resource-policy tuning by subscription cycle, and reusable lessons across
projects, while keeping project-specific facts isolated unless explicitly
promoted into a shared rule.

### P10 - Active-project progress watchdog and automatic diagnostics

Add a daemon-owned watchdog for projects whose lifecycle is actively progressing
(`PLANNING`, `EXECUTING`, `REVIEWING`, `REMEDIATING`). Reuse existing durable
signals (`last_activity_at`, lifecycle/role state, broker execution state,
Progress Channel events, Git/agent-file changes) instead of adding a second
heartbeat system.

- Configure a default no-progress threshold plus per-project/per-lifecycle
overrides; initial default target: 15 minutes, with policy configuration rather
than hard-coded behavior.
- When the threshold is exceeded, create exactly one bounded diagnostic task for
the current project/task/stage. Deduplicate by project + task + lifecycle run and
apply a cooldown so every daemon tick cannot create another diagnosis.
- Diagnostic work is read-only by default: inspect DevO lifecycle state, AIBroker
dispatch/resource/quota state, process liveness, Git status/HEAD/diff summary,
recent worker/reviewer/planner output and relevant agent files.
- Return a structured diagnosis such as `healthy_slow`, `agent_stalled`,
`process_dead`, `provider_or_quota_blocked`, `state_desync`, `external_wait`, or
`unknown`, with evidence and a recommended next action.
- Safe recovery may be dispatched only through the normal DevO lifecycle
(`retry`, `reconcile`, bounded remediation). Process termination, destructive
Git changes, credential changes, hardware actions, scope expansion, or ambiguous
recovery require OWNER_GATE.
- Emit Progress Channel milestones for `STALL_DETECTED`, `DIAGNOSTIC_STARTED`,
`DIAGNOSTIC_RESULT`, `RECOVERY_STARTED`, and `OWNER_GATE` as applicable.
- Persist last diagnosis, evidence hash, cooldown/attempt count, and recovery
result so restart does not duplicate work and the 8770 dashboard can surface it.

### P11 - Execution accounting and bottleneck profiling

Goal: quantify development wall-clock cost before changing scheduling policy.

- Persist queue/provider/context/model/test/review/retry/quota/owner/transport/idle timing per project/task/role.
- Track provider, model and session continuity and compute Context Hit Rate; identify context churn caused by resource/model/session switching.
- Measure failover latency from quota/rate-limit/provider-unhealthy detection to fallback execution start and expose avoidable waiting/retry.
- Measure RDC/transport invocation count, command size, first-output latency, duration, output size and failure/retry count.
- Detect lifecycle overhead: repeated planning, unresolved reviews, prolonged REVIEWING/BLOCKED, repository-truth churn and no-progress intervals.
- Compute Effective Development Ratio and longest no-progress interval.
- Add 8770 dashboard views for time breakdown, top bottlenecks, context hit rate, provider switches, failover latency, RDC statistics and longest stall.
- Produce evidence-backed diagnoses such as reviewer_stall, quota_failover_delay, context_churn, transport_overhead and lifecycle_churn.
- Acceptance: analyze a representative DevO + xray-hw-platform development day and quantitatively confirm/reject RDC large-command overhead, AI context churn, quota-without-timely-failover, and lifecycle/reviewer stall hypotheses.
- Instrument and measure first; defer broad scheduler optimization to a subsequent phase based on measured top bottlenecks.

### P12 - Unified AI Control Surface

Goal: make DevOrchestrator port 8770 the primary day-to-day control plane for projects and AI execution resources, while keeping AIResourceBroker port 8875 as the broker-specialist surface.

- Expose one stable Control API for ChatGPT/RDC/CLI/dashboard/mobile clients; control inputs remain stateless and DevOrchestrator remains the lifecycle authority.
- Show project/task/stage/lifecycle state, next action, active Planner/Implementer/Reviewer/Remediator runs, watchdog/diagnostic state and OWNER_GATE conditions.
- Aggregate AIResourceBroker resource state: provider, account, model, eligible roles, health, quota/token/reset information, session continuity and current execution.
- Show execution chains, recent events/logs, retries/remediation, usage/cost where available, and preserve `unknown` rather than inventing quota or usage values.
- Provide guarded controls for continue, pause/resume where supported, stop, retry/reconcile and OWNER_GATE approval; all mutations must use normal DevOrchestrator authority and audit paths.
- Integrate P11 telemetry into the same 8770 surface: time breakdown, context hit rate, provider switches, failover latency, RDC/transport statistics and longest no-progress interval.
- Avoid normal-operation dependence on the separate 8875 page; retain 8875 for AIBroker-specific configuration and diagnostics.
- Acceptance: an operator can understand project state, AI-resource state, current/next execution and blocking conditions, and perform normal bounded control actions from 8770 without switching between DevO and AIBroker pages.

Sequence: P12 follows P11. P11 instruments and measures bottlenecks first; P12 consumes that telemetry while completing the unified control experience.
