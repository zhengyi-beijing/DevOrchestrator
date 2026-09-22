# P16.7 Self-Healing Project Activation & Readiness

Status: **PENDING DESIGN**

## Goal

Use the 2026-09-23 xray-hw-platform activation failure as the concrete regression case to make DevOrchestrator diagnose and recover project-activation/readiness failures without requiring repeated owner messages such as "继续", "目前状态", or "修复这个问题".

The observed failure chain was:
1. repository-local `.devorch/status.json` still showed an old P4.1/REVIEW_FAILED state while the running daemon did not have `xray-hw-platform` registered at all;
2. `project-status xray-hw-platform` returned `not_found`;
3. after project registration and P1 task/contract anchoring, `continue` still failed with `project is not READY_TO_RUN`;
4. the task projection used human-readable `READY / OWNER_GOAL_DEFINED / NOT_STARTED`, while `resolve_monitor_state()` accepted only `READY_TO_RUN`, `DESIGN READY`, or `EXECUTABLE`, so the project was projected as `IDLE`;
5. changing the token to `READY_TO_RUN` caused the daemon to auto-launch the P1 AIBroker Worker successfully;
6. transient RDC inspection timeouts also caused orchestration work to stop and require another owner message even though no true owner decision was required.

## Required outcomes

### 1. Authoritative state and orphan detection
- Explicitly distinguish daemon-authoritative runtime state from repository-local historical/projection state.
- If local DevO state exists but the project is absent from the active registry, surface a structured `ORPHANED_PROJECT_STATE`/equivalent diagnosis rather than presenting stale lifecycle state as current truth.
- Where project identity/path/config are unique and validation succeeds, support bounded register/reconcile recovery; otherwise surface a precise blocker rather than guessing.

### 2. Structured readiness contract
- Remove critical lifecycle authority from free-form Markdown regex matching.
- Introduce a machine-readable readiness/execution state contract (for example `execution_state=ready_to_run`) with schema/version validation.
- Markdown such as `agent/next.md` is a human projection of structured state, not the authoritative protocol.
- Preserve a bounded legacy parser/migration path so existing projects do not break abruptly.
- Invalid/ambiguous readiness values fail closed with an actionable diagnostic.

### 3. Structured blocker diagnostics
- A rejected transition such as `continue` must return the failed predicate(s), expected value, observed value, evidence source, and bounded remediation.
- Add one canonical blocker/explain surface usable by CLI, API, Web UI, mobile and watchdog (for example `explain-block` or `GET /projects/{id}/blockers`).
- For the reproduced incident, diagnostics must identify the readiness-token/state mismatch directly instead of only returning `project is not READY_TO_RUN`.

### 4. Continuous self-recovery
- Classify failures into at least transient infrastructure, recoverable orchestration, owner gate, and terminal outcomes.
- Transient failures such as RDC/SSH/broker timeouts and recoverable conditions such as stale registration/readiness projection must not silently return control to the owner.
- Apply bounded retry/backoff and safe alternate inspection/reconciliation where available.
- Persist the original Start/Continue execution intent across remediation. A successful remediation is not terminal: DevO must retry the original forward transition automatically.
- Implement a bounded auto-recovery loop with independent budgets for total recovery actions, repeated identical failures, and elapsed launch time. Initial defaults for acceptance are 20 recovery actions, 3 repeats of the same failure fingerprint, and 30 minutes elapsed; keep them configurable and tune from replay evidence.
- Track a recovery fingerprint containing at least project/task, lifecycle state, blocker code, Git anchor, Worker state, and broker state. Detect no-progress and short cycles (for example A->A and A->B->A->B) and fail closed rather than spinning.
- While an execution intent targets EXECUTING, and there is no OWNER_GATE, explicit stop, terminal failure, or exhausted recovery budget, DevO must eventually attempt another forward transition without another owner message.
- Stop only for a genuine owner-only decision, explicit stop, terminal success, exhausted bounded recovery budget/livelock, or an invariant violation that cannot be recovered safely.

### 5. Idempotent/race-safe control
- If an execution is already active, a duplicate `continue` must resolve deterministically as an idempotent no-op such as `NOOP_ALREADY_EXECUTING`, not create an ambiguous pending command.
- Bind transition commands to project/task/head/recovery epoch/control identity and fail closed on stale identity.
- Prove that monitor auto-start racing with an explicit continue cannot launch duplicate Workers.

### 6. Git-truth diagnostics
- Keep clean immutable Git execution anchors as the launch authority.
- Distinguish expected owner-approved task artifacts from unexpected dirty changes in diagnostics.
- Never silently commit or discard unexpected changes solely to obtain a clean anchor.

## Acceptance regression

Build an automated regression that recreates the exact incident:
- stale repository-local P4.1/REVIEW_FAILED projection;
- project absent from active registry;
- valid P1 task contract exists;
- readiness initially represented as legacy `READY / OWNER_GOAL_DEFINED / NOT_STARTED`;
- transient inspection failure is injected;
- project is safely registered/reconciled;
- readiness is normalized/migrated to structured `ready_to_run`;
- blocker diagnostics identify every failed predicate during recovery;
- exactly one Worker is launched;
- duplicate `continue` during/after auto-launch returns an idempotent no-op;
- no owner interaction is requested unless a true owner-gate condition is injected.

Acceptance evidence must include lifecycle/command audit records proving the transition sequence and demonstrating that stale local state was never treated as daemon-authoritative truth.

## Priority

Within P16.7, prioritize:
1. structured readiness instead of Markdown regex authority;
2. structured blocker/explain diagnostics;
3. transient/recoverable automatic recovery;
4. orphan registration reconciliation;
5. richer Git dirty classification.

## Non-goals

- Do not weaken owner gates for architecture, public interfaces, resource ownership, threading, protocol/replay semantics, destructive Git operations, or physical safety.
- Do not auto-start real hardware, X-ray sources, conveyor/VFD, or other safety-sensitive resources.
- Do not make stale local files authoritative over the daemon/control ledger.
- Do not solve the problem by adding more permissive regex aliases alone; the objective is to remove free-form text as lifecycle authority.
