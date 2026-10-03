# P20.4 — Fault-Injection Multi-Task Unattended Soak

Status: **PENDING DESIGN**

Predecessor: P20.3
Successor: P20.5

## Objective and frozen direction

Build the isolated reproducible tracked software template/harness defined in
`agent/staged/P20.md` and `docs/P20_OWNER_REQUIREMENTS.md` sections 6, 8–11, 18.
Disposable generated Git repo and runtime must be outside the production
checkout. Local explicit soak permit only, defaults OFF, no remote enablement,
hardware, RDC, production fault injection or direct lifecycle ledger writes.

Run five real bounded feature/edge-case/refactor/parser/API tasks through real
Planner/Worker/tests/commits/independent Reviewer, AIBroker/resource routing,
durable jobs and lifecycle. Test/inject all 15 specified faults and record
actual incidents, stable family identities, recovery strategy/outcomes and
correct owner gates. Separate deterministic proof from live fault coverage.

## Hard duration gate and durable ownership

One real continuous unattended run >=21600 seconds; UTC/boot-aware monotonic
start/end/duration, restart/task/incident/recovery timelines. No fabricated
clock or summed old runs. A detached DevO-owned durable supervisor must survive
this session and daemon restarts. Keep P20.4 RUNNING until evidence satisfies
the real-duration gate. Launching a harness does not complete the phase.
Never count implementation preparation or sleep-only time as a live soak.
Do not permit automatic technical review/next_task while the soak is running.
If a foreground Worker cannot retain ownership, use the existing observable
durable external-wait contract rather than marking that Worker completed.

## Acceptance and checkpoint

TDD proves permit/path/project isolation and each fault. Live qualification
requires five accepted real tasks, six continuous hours, zero manual continue
or lifecycle repair, zero duplicates/escapes/stalls/loops/regression, 100%
fault disposition and incident coverage. Correct owner gates remain stable,
actionable and inhibit effects. Record run ID/start/elapsed/tasks/incidents/
recovery/current execution/next fault and exact resume command in HANDOFF.
Resume the same run after session loss, never reset its start. Only after
independent Technical Review ACCEPT may P20.5 activate. No push.

## Control-Plane Impact
- Invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION, PENDING_DESIGN_NOT_EXECUTING, SUCCESSOR_HANDOFF_LINEAGE_VALID, NEXT_TASK_WITHOUT_HANDOFF, SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: authority_reconciliation, watchdog_recovery, owner_gate_transition, successor_handoff, handoff_publication, worker_launch
- Fault scenarios: CPF-01, CPF-02, CPF-03, CPF-04, CPF-05, CPF-06, CPF-07, CPF-08, CPF-09, CPF-10
- Convergence evidence: Isolated real pipeline maintains sole authority across measured restart/fault timelines; unsafe faults gate, safe faults reconcile once; no fixture can enable production faults.
