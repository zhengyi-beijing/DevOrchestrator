# P20.3 — Deterministic Recovery & Heterogeneous Escalation

Status: **PENDING DESIGN**

Predecessor: P20.2
Successor: P20.4

## Objective and frozen direction

Implement the pure bounded Recovery Policy in `agent/staged/P20.md` and
`docs/P20_OWNER_REQUIREMENTS.md` section 5. Decisions cover all eight required
actions; existing lifecycle/coordinators retain ownership and final safety
fences. Persist reservations/attempts/strategy/outcome before and after effects.
Replay reconciles existing effect IDs or owner-gates crash ambiguity.

Same strategy retry at most once, at most three recoveries per incident;
account rotation alone cannot bypass repetition. Require changed context,
strategy, model, provider or repair plan, with available eligible heterogeneous
escalation for semantic/high-risk/repeated failures. Preserve provider-level
reviewer independence. Unknown safety, stale HEAD, unsafe dirt, conflicting
authority or exhausted budget becomes durable actionable stable OWNER_GATE.

## Confirmed bootstrap recovery gaps

Read `docs/P20_BOOTSTRAP_REVIEW_TIMEOUT_FIX.md` and preserve its real failure
references. Carry semantic remediation/rejection budgets across review-resource
recovery and restart; after two remediation rounds adjudicate only the unresolved
blocking delta, read-only. Transport response loss while Broker remains running
requires exact-request bounded reconciliation or a durable actionable gate.
Bound role-tool child/harness cleanup, retain source/resource correlation and
never retry an ambiguously live Worker. These are required P20.3 integration
regressions, not permission to expand deadlines or reset budgets.

## Acceptance and stage gate

TDD RED/GREEN for deterministic decisions, durable restart attempts and replay,
anti-loop AGY rotation, quota/provider unavailable, Reviewer reject, stale HEAD,
lost Worker, no duplicate execution/job/handoff, resolved old incident no-op,
and owner gate inhibits background actuation after restart. Relevant full
regression, compileall/diff check/graphify update. Independent Technical Review
ACCEPT before normal P20.4 handoff. No new authority, weakened gates or push.

## Control-Plane Impact
- Invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION, PENDING_DESIGN_NOT_EXECUTING, SUCCESSOR_HANDOFF_LINEAGE_VALID, NEXT_TASK_WITHOUT_HANDOFF, SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: authority_reconciliation, watchdog_recovery, owner_gate_transition, successor_handoff, handoff_publication, worker_launch
- Fault scenarios: CPF-01, CPF-02, CPF-03, CPF-04, CPF-05, CPF-06, CPF-07, CPF-08, CPF-09, CPF-10
- Convergence evidence: Durable bounded attempts reconcile exactly one existing effect, unknown safety gates once, independent reviewers remain eligible, all CPF faults converge without manual continuation.
