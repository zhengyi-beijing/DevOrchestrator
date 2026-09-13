# P11 Execution Accounting and Bottleneck Profiling

Status: **PENDING DESIGN**

Owner authorization: **START P11 / 2026-09-13**

Goal: quantify DevOrchestrator development wall-clock cost before changing scheduling policy, using P10 watchdog data as one input and adding finer-grained execution accounting.

Scope:
- Persist queue/provider/context/model/test/review/retry/quota/owner/transport/idle timing per project/task/role.
- Track provider, model and session continuity; compute Context Hit Rate and identify context churn caused by resource/model/session switching.
- Measure failover latency from quota/rate-limit/provider-unhealthy detection to fallback execution start and expose avoidable waiting/retry.
- Measure RDC/transport invocation count, command size, first-output latency, duration, output size and failure/retry count.
- Audit multi-project RDC execution isolation before attributing stalls to RDC itself: determine whether projects share a command shell, request queue, transport lock, or interactive Windows session; distinguish true deadlock from head-of-line blocking, starvation, session coupling, and transport serialization.
- Add bounded concurrency probes: while project A runs a long blocking/streaming command, projects B/C must still complete short commands promptly; cancelling A must not terminate or corrupt B/C; reconnect/recovery must not cross-contaminate project execution state. Record queue wait, start latency, execution identity, cancellation scope, and reconnect effects as P11 evidence.
- If shared-session serialization is observed, treat per-project execution context/process isolation, asynchronous long-command handling, and scoped timeout/cancel as remediation candidates derived from evidence; do not assume an RDC product limitation or perform a broad execution-layer rewrite before the audit establishes the failure boundary.
- Detect lifecycle overhead: repeated planning, unresolved reviews, prolonged REVIEWING/BLOCKED, repository-truth churn and no-progress intervals.
- Compute Effective Development Ratio and longest no-progress interval.
- Add 8770 views for time breakdown, top bottlenecks, context hit rate, provider switches, failover latency, RDC statistics and longest stall.
- Produce evidence-backed diagnoses such as reviewer_stall, quota_failover_delay, context_churn, transport_overhead and lifecycle_churn.

Execution Lessons / Failure Memory foundation:
- Persist structured verified lessons independently of chat memory, including failure fingerprint, scope, environment predicates, symptom, root cause, preferred and avoided actions, confidence, verification state, timestamps, and occurrence count.
- Support global and project-scoped lessons; match deterministically from host, transport, shell, project, and runtime context.
- Inject only relevant verified lessons into Planner, Worker/Remediator, and Reviewer context before dispatch, with bounded size and provenance.
- Detect recurrence of a verified fingerprint as repeated_known_failure and include its wasted wall-clock, transport, and retry cost in P11 accounting.
- Seed a verified lesson for the observed RDC PowerShell command-chaining incompatibility so a fresh execution context receives the shell-specific rule before constructing commands.
- Keep automatic remediation conservative; broad scheduler and policy optimization remains outside P11.

Acceptance:
- Analyze a representative DevO + xray-hw-platform development day.
- Quantitatively confirm or reject: RDC large-command overhead, AI context churn, quota-without-timely-failover, and lifecycle/reviewer stall hypotheses.
- Quantitatively classify multi-project RDC behavior as isolated concurrency, head-of-line blocking/serialization, session coupling, starvation, or true deadlock where evidence supports it; demonstrate the A-long/B-C-short, scoped-cancel, and reconnect-isolation probes and identify whether the limiting boundary is DevO, the RDC transport/API, or the Windows interactive session.
- Demonstrate that a fresh execution context receives the applicable verified shell/transport lesson before command construction and does not repeat the seeded known failure.
- Demonstrate that a synthetic or replayed recurrence is classified as repeated_known_failure and appears in accounting/dashboard evidence.
- Instrument and measure first. Do not perform broad scheduler optimization in P11; use measured bottlenecks to define later remediation.
- Preserve self-hosting isolation: modify only `C:\work\github\DevOrchestrator-dev`; do not modify/restart/replace the stable controller except through the established promotion flow after acceptance.

Planner schema guard for this task: keep every planner list at 20 entries or fewer and every list entry under 800 characters.

Execution instruction: begin P11 now through the normal DevOrchestrator lifecycle. Continue automatically through planning, implementation, verification, review and bounded remediation until P11 reaches an accepted phase gate or an OWNER_GATE condition.
Required redesign constraints from independent plan review (must be resolved in the next plan, not deferred):
- Define concrete observable sources and schemas for test timing, accepted/rejected work, queue wait, retry, planning rounds and review outcomes; do not claim a metric that cannot be observed from current runtime signals.
- Add durable session-continuity identity and request/result correlation for Context Hit Rate, or explicitly narrow the metric until such identity exists.
- Specify deterministic interval mathematics: overlap union/deduplication, category precedence, clock-skew handling, missing-end handling, window clipping and data-quality flags.
- Define an explicit event taxonomy including test, retry, planning, review, queue/provider/model/transport/owner/idle events and the timestamps needed to derive each interval.
- Make event persistence concurrency-safe for daemon and Worker threads, with corruption recovery and scalable time-window reads that are not silently capped at 100 records.
- Fresh-context acceptance must exercise the real dispatch/context-injection path and observe command construction; a fake Worker assertion is insufficient.
- Real representative-day DevO + xray-hw-platform evidence is required to confirm/reject production bottleneck hypotheses; synthetic/replay data may validate analysis mechanics only.
- Keep the design implementable in bounded phases: instrument missing observables first, then derive metrics, then dashboard/reporting, then representative-day acceptance.

Plan review/remediation loop requirement:
- Treat plan-review rejection with actionable redesign feedback as a non-terminal lifecycle transition, not generic `failed/blocked`: `PLANNING -> REVIEWING_PLAN -> REMEDIATING_PLAN -> REVIEWING_PLAN` until approval, OWNER_GATE, provider/system failure, or a bounded retry limit.
- Feed the exact reviewer rejection, prior plan, task identity, repository HEAD, and prior planner resource/session context into each plan-remediation round; avoid restarting from zero when continuity is available.
- Distinguish design rejection from infrastructure/provider/schema failure. Only owner decisions go to OWNER_GATE; bounded design defects should auto-remediate.
- Instrument plan-review reject count, remediation-round count, reject-to-remediation latency, remediation duration, repeated-planning wall time, planner/reviewer resource switches, context continuity, and manual-intervention count.
- Diagnose repeated rejection without convergence as `plan_review_churn`; include its wall-clock and AI/resource cost in lifecycle-overhead accounting.
- Use a configurable bounded retry policy; exhausting the bound must preserve the full rejection chain and escalate to OWNER_GATE rather than loop indefinitely.
- Acceptance: force at least one deterministic plan rejection in an isolated test and prove DevO automatically revises and re-reviews without a new user `project-continue`; verify approval proceeds to READY_TO_RUN and retry exhaustion reaches OWNER_GATE with complete provenance.
