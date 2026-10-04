# P20.4 scoped implementation notes (read-only inspection, not qualification)

P20.3 ac2cf329, 2026-10-04. No P20.4 code is implemented by these notes.

Reuse JobService's allowlisted detached supervisor and PID/start-token heartbeat.
Reuse TransitionExecutor as sole execution/lifecycle authority. Its current
_run_ai_worker marks a succeeded Broker request completed immediately and emits
WORKER_DONE. _recover_interrupted_runs also marks Broker success completed on
restart. Both paths must honor a validated external-job binding before completing
P20.4. A harness launch or a Worker output label cannot satisfy the duration gate.

The normal attach API needs current project/task/source execution identity,
clean current descendant HEAD, local explicit default-OFF soak permit, the
owned durable supervisor job ID and exact soak run ID/start. Persist that binding
inside the existing execution record; preserve active owner while external job
runs. JobService owns process liveness, not a new lifecycle authority. Unknown
job identity/liveness must gate and never duplicate a supervisor. Broker result
metadata may finish while the execution waits; suppress Worker completion,
Technical Review, successor activation and Watchdog false-stall detection until
validated terminal supervisor evidence. Recover the same binding after daemon
restart, without re-dispatch or resetting run start. Keep Worker timeout 14400s.

Terminal evidence must bind job/run/project/start and show >=21600 real seconds,
>=5 real AI task acceptances with normal isolated pipeline identities, all 15
actual fault dispositions, incident/family/recovery evidence and known hard-zero
metric coverage. Unknown metric values cannot be silently coerced to zero.
Then use normal completion/review/settlement paths, never direct runtime edits.

The supervisor's permit and every process/filesystem/fault target must remain
inside the generated disposable software project/runtime outside canonical
checkout. No remote/hardware/RDC fault or production Broker/daemon interruption.
Production normal quiescent reload is preparation, not an injected fault.

Session-end checkpoint is allowed only after real durable soak RUNNING with
exact resume/status command retaining run ID/start. Task counts, faults, timing
and acceptance are not supplied by fixture output or these notes.

## Current implementation entry points (8cbc30df; read-only preparation)

- TransitionExecutor._run_aibroker_worker success near lines 2676-2730 must check
  the exact external binding before terminal update, WORKER_DONE, or next_action.
- TransitionExecutor._recover_interrupted_runs near line 689 must recover owned
  supervisor binding before interpreting a succeeded Broker result as completed.
- The daemon ordered tick (_run_orchestration_tick) must poll/reconcile external
  execution before authority projection, Reviewer advancement and dispatch.
- JobService.submit already validates an allowlisted local command and supplies
  idempotent job identity; use it instead of a separate ad hoc background process.
- Progress obligation legal_wait alone does not suppress evaluate_stall.breached;
  integrate actual job heartbeat/semantic progress and bounded deadline, rather
  than globally turning watchdog off or inflating Worker timeout.
- Unknown process/job ownership must retain evidence and gate, not dispatch a
  second job. Restart must preserve original run ID/start and supervisor PID
  creation/start-token identity.
- Local permit binds canonical P20.4 execution to disposable target paths;
  canonical source is allowed only for ordinary reviewed implementation, never
  as a fault target. This cannot be exposed as a general remote control action.
- Dedicated Broker unavailability must use a disposable service or isolated
  transport fault boundary. Never stop/quarantine production Broker or its other
  sessions merely to manufacture fault coverage.
- Five accepted task evidence must correlate actual Planner, Worker commit and
  independent Reviewer identities. A deterministic fault-case result remains
  labeled deterministic; real AI task evidence does not make all synthetic faults
  live. Report each fault's own injection and normal disposition accurately.

These are Worker verification notes, not a new design gate or implementation.

## Hard-zero producer coverage

reliability_telemetry.py currently requires escaped_to_authority evidence on
normal authority invariant observations. No production producer is present at
8cbc30df; absence yields UNKNOWN by design. P20.4/P20.5 must wire trustworthy
actuation/authority observation coverage before treating this count as a zero.
Do not modify historical runtime values or coerce None/UNKNOWN to 0. Distinguish
an unsafe action fenced before effect from an actual invariant escape. Persist
run-window evidence rather than reading only the last current invariant sample.

## Local isolated service preparation

At 2026-10-04T06:53Z, candidate localhost ports 19765 (bridge), 19770
(daemon/web), 19775 (dedicated Broker if used) have no TCP listener and are
outside current Windows IPv4 excluded ranges. Recheck/bind atomically at launch;
this observation does not reserve a port or authorize an existing process kill.

## Fresh-runtime ordering

The running daemon imports code once. Installing an external binding in a new
CLI process while the old daemon still uses unconditional Worker-success
completion is not sufficient. Plan a normal controlled reload into the new
binding-aware code before the foreground Worker can publish completion, or a
bounded implementation-preparation hold that lets root reload and launch the
same authoritative P20.4 execution through supported APIs. A production reload
for new code is preparation, not an isolated fault event. Do not directly edit
an execution record, inflate the Worker timeout, or claim attachment to an old
in-memory authority proves it will defer completion. Verify fresh runtime and
same-source execution ownership at launch and after restart.

## Accepted P20.3 review inputs (15abd1a)

The independent Review accepted P20.3 and left these NON_BLOCKING inputs for
P20.4: ControlCommandStore.get rejects colon/slash non-control effect IDs before
lineage reconciliation (not reachable from the current production reserver);
Harness resource status probing is not deadline bounded and config loading sits
outside its exception fence (harness disabled in current project config);
provider-native Graphify children are not registered, so timeout gates provide
the actionable conservative outcome; dead planner recovery-loop wrapper remains.
These must not reopen full planning or delay safe implementation by themselves.
Do not claim native child cleanup coverage absent actual registration evidence.
