# P20.1 — Reliability Telemetry & Failure Taxonomy

Status: **PENDING DESIGN**

Predecessor: P19.2
Successor: P20.2

## Objective

Implement only the incident contract, stable normalized failure taxonomy and
restart-safe telemetry/storage foundation defined in `agent/staged/P20.md`
and the authoritative owner request `docs/P20_OWNER_REQUIREMENTS.md` sections
3, 11–16. Read both before planning or execution. Reuse FailureMemory,
ExecutionRecorder and IncidentStore; never create a second lifecycle authority.

## Required implementation and acceptance

- All required FailureIncident fields and 19 classes with explicit validation.
- Stable signatures exclude volatile IDs/times/paths and account rotation.
- Existing transactional store provides concurrent append/replay idempotency,
  restart identity, persistent corruption fencing and bounded retention or
  fail-closed backpressure. Preserve all legacy evidence and compatibility.
- Sanitize ingress before packet/index/journal/candidate persistence; cover
  tokens, embedded credentials, nested environment/prompt content and PEM.
- Define all 16 run-scoped telemetry counters with durable source-event
  identities, honest unknowns, and the execution-affecting escape definition.
- Tests first and real RED evidence for normalization, restart, replay,
  concurrent capture, capacity, corrupt restart and secret sanitization.
- Focused GREEN, relevant incidents/accounting/lifecycle/watchdog regression,
  compileall, diff check, graphify update and independent Technical Review.
- Only after ACCEPT may P20.2 activate through normal successor APIs. Local
  commits on main only; no push. Keep the staged successor PENDING DESIGN.

## Control-Plane Impact
- Invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION, PENDING_DESIGN_NOT_EXECUTING, SUCCESSOR_HANDOFF_LINEAGE_VALID, NEXT_TASK_WITHOUT_HANDOFF, SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: authority_reconciliation, watchdog_recovery, owner_gate_transition, successor_handoff, worker_launch
- Fault scenarios: CPF-01, CPF-02, CPF-03, CPF-04, CPF-05, CPF-06, CPF-07, CPF-08, CPF-09, CPF-10
- Convergence evidence: Telemetry observes sole authority without actuation; replay preserves counters and owner/task identities; corrupt evidence gates without relaunch; registered CPF tests remain green.
