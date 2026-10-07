# P20.4 Run H premeasurement recovery remediation

Date: 2026-10-08

## Outcome

Run H (`p20-live-20261008-h`) failed before measurement, but the canonical
owner-recovery path could only authorize replacement of a soak that already
had an `external_execution_binding` and a terminal `FAILED` run file. Run H
has neither: source execution `wd-0ceb9ed99045bfca` has an exact preparation
record and start intent for job `job-b88542ae1a5828b4`; that job exited 3; and
no Run H run file or measurement clock exists. The old recovery path therefore
returned `failed_soak_binding_missing` even though duplicate launch was safely
excluded by the terminal JobService record.

## Remediation

Commit `2e55084` adds a bounded premeasurement-failure branch to
`TransitionExecutor.authorize_soak_recovery`. It requires all of the following
before clearing `SOAK_EVIDENCE_UNRESOLVED`:

- the exact P20.4 recovery gate and source execution;
- no external binding, but matching preparation and start-intent records;
- a readable previous permit whose run and fingerprint match both records;
- a matching prepared HEAD, an expired preparation deadline, and the canonical
  bounded-deadline recovery reason;
- no run state and no run file;
- the exact reserved JobService job in `failed` or `cancelled` state, with
  matching project, task, source, command, transport, parameter digest, and
  idempotency key; and
- a new replacement run/project/fingerprint/repository/runtime identity.

The authorization records a stable SHA-256 digest of the proved job identity,
settles the failed execution lineage, publishes the replacement bootstrap, and
preserves idempotent command replay. The existing bound-run branch continues
to require a terminal `FAILED` run file and its immutable SHA-256.

## Verification

- owner-recovery suite: 20 passed;
- all P20.4 suites: 129 passed;
- adjacent TransitionExecutor, control, and watchdog suites: 144 passed plus
  23 subtests;
- compile, `git diff --check`, and Graphify update: passed.

Independent read-only Technical Review request
`p20-run-h-premeasurement-recovery-review-20261008-r1` ran on
`copilot/default/claude-sonnet-4.6`. Dispatch
`0d577dc0-fc09-4ae9-8914-c59bb5a7de26`, execution
`b4f6c0af-98c3-46d0-8bd2-a7b1e6447b08`, returned **ACCEPT** with zero
BLOCKING findings. Its same-session output recovery request
`p20-run-h-premeasurement-recovery-review-20261008-r1-output` used dispatch
`66a4736c-c207-4198-ac5f-710fc78daf11` and execution
`852e963d-7e9b-47c7-a4d3-9fcaf900c0a2` and restated the ten checked invariants.

Run J (`p20-live-20261008-j`) remains premeasurement. Its isolated Planner,
independent Plan Reviewer, Worker, and Technical Reviewer completed the first
real task, but no canonical soak job or run file has been created. Before
starting the supervisor, the isolated pipeline must again have an active role;
its Worker-produced `agent/execution-state.json` currently uses the invalid
value `done`, so the next isolated control correctly failed closed on
`READINESS_SCHEMA_INVALID`. Repair that disposable preflight artifact to the
schema value `completed`, advance the normal isolated pipeline, then perform
the canonical Run H to Run J recovery with a new command ID.
