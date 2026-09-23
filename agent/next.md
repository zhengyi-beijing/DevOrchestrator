# P16.8 DevO Golden-Path Lifecycle Hardening

Status: **PENDING DESIGN**

## Goal
Turn the LineScanViewer P15 launch postmortem into a bounded DevOrchestrator hardening milestone. Prove that a fresh owner-authorized task can move from PENDING_DESIGN to DONE from one continue command, without manual lifecycle repair.

## Problems to close
1. Replace duplicated/free-text lifecycle interpretation with one canonical task-status parser/contract used by control, Planner, plan apply, transition and terminal handling.
2. Make AI artifact validation errors actionable: report field, expected constraint, actual value and required correction so retries perform remediation rather than blind replay.
3. Enforce terminal-state closure: DONE keeps immutable audit history but exposes no stale retry/rereview/reconcile/continue action.
4. Make operator telemetry distinguish orchestrator activity from implementation activity (stage, active role, Worker state, code-execution yes/no).
5. Preserve provider failover and existing fail-closed safety semantics.

## Persistent launch intent and bounded recovery
Treat one owner Start/Continue command as authorization to pursue the already-defined execution goal until it reaches implementation execution or a real stop condition. Persist a LaunchIntent/ExecutionIntent across Planner, review, remediation, reconciliation, provider failover, daemon restart and Worker-launch retries. Intermediate repair completion must never consume that intent.

Use a bounded recovery loop rather than repeated owner prompts. Initial acceptance defaults are 20 total recovery actions, 3 repeats of an identical recovery fingerprint, and 30 minutes elapsed. Distinct blockers that are making measurable forward progress do not consume the identical-failure budget. Detect no-progress and short-cycle livelock and terminate with structured evidence.

The only valid interruption outcomes are: genuine OWNER_GATE requiring new information/choice, explicit owner stop, terminal success, terminal/invariant failure, or exhausted recovery/livelock budget. Transient infrastructure and recoverable orchestration failures must remediate and automatically retry the original transition.

## Durable ExecutionContext and continuation dispatch

Persist a structured, machine-readable ExecutionContext for every active task. This is not a transcript or hidden chain-of-thought; it stores only auditable reasoning outcomes and orchestration state needed by the next invocation: current stage, latest conclusion/verdict and evidence references, completed actions, remaining actions, current blocker, explicit next_action, active ExecutionIntent, recovery fingerprint/budgets, and immutable project/task/head identities.

Every Planner, Reviewer, Worker, remediation, reconciliation, and provider-failover completion must atomically update this context before the role invocation is considered complete. The next role must consume the latest compatible context rather than reconstructing continuity from free-form Markdown or stale role files.

ExecutionContext answers "what happened and what is next"; ExecutionIntent answers "must execution continue". The dispatcher must automatically consume next_action while ExecutionIntent remains active. Intermediate AI completion, successful remediation, daemon restart, or provider failover must not consume the intent.

Enforce this invariant: when ExecutionIntent.active is true, next_action is non-null, no OWNER_GATE/explicit stop/terminal failure exists, and recovery budget remains, the project must not remain stably IDLE. Remaining in that condition for more than one daemon tick is itself an orchestration fault; watchdog/reconciler must redispatch the next action idempotently.

Add restart/replay tests proving the context survives daemon restart and that stale context is rejected by project/task/head/recovery-epoch identity. Preserve concise historical outcome records for audit, but never persist or require model hidden chain-of-thought.

## Golden-path E2E acceptance
Create an isolated synthetic self-test project/task whose bounded implementation is trivial and harmless. From a fresh clean repository and one continue command, require:
PENDING_DESIGN -> PLAN -> PLAN_REVIEW -> READY_TO_RUN -> EXECUTE -> TECHNICAL_REVIEW -> DONE.
At DONE assert clean worktree, no active execution/role, no owner gate, and retry/rereview/reconcile/continue unavailable.

## Fault-injection acceptance
Run bounded variants that deliberately cause:
- Planner schema violation (including oversized list) and verify actionable correction/recovery.
- Primary provider unavailable/exhausted and verify AIBroker failover without lifecycle corruption.
- Reviewer rejects once and verify one bounded remediation path reaches DONE.
- Daemon restart at Planner/Reviewer/Worker handoff boundaries and verify idempotent reconciliation: no duplicate AI actuation, no lost accepted verdict, no rollback to an incorrect lifecycle.
- Git-anchor invalidation regression: start a Planner on clean HEAD A with an active ExecutionIntent, advance the repository to clean HEAD B before completion, and require the stale Planner result to fail closed. Preserve the original ExecutionIntent, refresh repository truth, re-anchor to HEAD B, and automatically dispatch one replacement Planner without another owner Continue. Verify the stale HEAD-A plan is never applied and the project does not remain IDLE for more than one daemon tick while continuation remains authorized.

## Regression anchors from LineScanViewer P15
Cover the exact failures observed:
- annotated status such as PENDING DESIGN — READY FOR PLAN/EXECUTION is parsed consistently at both planning start and plan apply;
- 25 implementation steps against max 24 returns exact count/correction;
- approved plan can launch a Worker;
- DONE treats stale review descendants as audit history only.

## Deliverables
- Canonical lifecycle/task-status contract and parser with callers migrated away from ad-hoc string matching.
- Golden-path self-test harness plus deterministic fault-injection tests.
- Lifecycle invariant checks and concise stage/role/Worker/code-execution telemetry.
- Documentation of recovery semantics and failure classifications.
- Focused tests, full regression, compile checks and git diff --check; local clean commit, no push.

## Out of scope
- New UI product features, new AI providers, benchmark model ranking, LineScanViewer feature work, or weakening owner/safety gates.
