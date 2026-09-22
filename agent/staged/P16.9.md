# P16.9 Watchdog Execution-Loss Detection & Recovery

Status: **PENDING DESIGN**

## Goal

Fix the watchdog blind spot exposed by the xray-hw-platform P1 incident: an accepted execution reached WORKER_RUNNING, produced no provider output, then disappeared and the project returned to READY_TO_RUN without a terminal execution outcome, while watchdog still reported state=ok and performed no diagnosis or recovery.

## Incident regression

Observed sequence:
1. P1 continue was accepted.
2. Project entered WORKER_RUNNING / EXECUTING.
3. active_execution existed and process_alive=true.
4. provider_output_observed remained false.
5. The Worker/execution later disappeared: active_execution=null, active_roles=[], worker=not_started.
6. Project projected READY_TO_RUN again with no completed/failed/cancelled terminal outcome for the vanished execution.
7. A later continue/recovery epoch refreshed activity/recovery context.
8. watchdog reported state=ok, last_diagnosis=null, attempts=0, last_recovery=null.

The defect is not merely a long no-progress threshold. A new control/recovery epoch or agent-file activity can mask an unterminated prior execution.

## Required invariant

Every accepted execution that reaches launch/running must have a durable lifecycle outcome:

accepted -> launched/running -> {completed | failed | cancelled | explicitly_reconciled}

The following transition is invalid unless accompanied by durable failure/reconciliation evidence:

WORKER_RUNNING / EXECUTING
-> active_execution absent
-> READY_TO_RUN

If this occurs, classify it as an execution-loss condition such as:
WORKER_VANISHED_WITHOUT_TERMINAL_STATE.

## Required outcomes

- Persist enough execution lineage/history that a new recovery epoch cannot erase or mask an unterminated prior execution.
- Watchdog must compare current state against prior accepted/launched execution records, not only current no-progress/activity timestamps.
- Agent-file touches, control requests, new recovery epochs, or READY_TO_RUN projection must not count as proof that a prior execution terminated correctly.
- Detect at least: vanished worker, missing terminal outcome, running-without-provider-output beyond bounded startup grace, broker/execution record disappearance, and inconsistent active role/execution state.
- Emit structured diagnosis containing project_id, task_id, prior execution/control IDs, launch/head anchor, last known worker state, provider_output_observed, expected terminal states, observed state, and evidence sources.
- For safe recoverable cases, automatically reconcile/retry with bounded attempts/backoff and preserve the failed execution audit trail.
- Never launch duplicate Workers: recovery must verify that no supported backend execution is still alive before retry.
- Escalate only when recovery is unsafe, identity/HEAD changed ambiguously, retry budget is exhausted, or a genuine Owner Gate is required.
- Watchdog health must not remain `ok` while an unresolved execution-loss invariant exists.

## Acceptance tests

Automate the exact P1 failure:
- accept a continue at a clean immutable HEAD;
- mark Worker launched/running;
- keep provider_output_observed=false;
- remove/drop the active Worker/execution without writing completed/failed/cancelled;
- inject a new continue/control epoch and touch agent/next.md;
- prove watchdog still detects the prior execution loss rather than resetting to ok;
- prove diagnosis is durable and references the vanished execution;
- prove one bounded safe recovery occurs;
- prove no duplicate Worker launches;
- prove successful recovered execution closes the diagnosis with audit evidence;
- prove exhausted/unsafe recovery becomes degraded/blocked with a precise reason rather than silently returning READY_TO_RUN.

Also test the legitimate cases: normal completion, explicit cancellation, broker-declared failure, daemon restart with durable running execution, slow provider startup within grace, and a live backend execution whose local projection temporarily disappears.

## Relationship to P16.7

P16.7 covers broader self-healing project activation/readiness, structured blockers, transient recovery, orphan registration and idempotent controls.

P16.9 is narrower and deeper: watchdog must enforce durable execution lifecycle invariants across recovery epochs so an execution cannot vanish without diagnosis. P16.9 must not be considered solved merely by adding readiness aliases, reducing the stale threshold, or issuing another continue.

## Safety / non-goals

- Do not weaken Owner Gates or Git/head identity checks.
- Do not treat provider silence alone as proof that a process is dead; verify backend/process state before retry.
- Do not auto-start real X-ray, conveyor/VFD, or other physical hardware.
- Do not discard historical execution evidence when creating a new recovery epoch.
