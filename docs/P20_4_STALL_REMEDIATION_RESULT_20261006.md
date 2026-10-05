# P20.4 stalled-soak remediation result

The false-running P20.4 soak state is repaired in local commit `610f97e`.
Canonical execution was paused before mutation, the affected ledgers were
backed up under `runtime/backup-p20-stalled-soak-repair-20261006/`, and the
owned durable job was normally cancelled. Its job supervisor and soak child
were proved dead before settlement. All owned disposable daemons were stopped
through permit-scoped normal cleanup.

The software repair has three parts:

- generated final tasks now declare an explicit end-of-roadmap terminal
  settlement, so an accepted Review returns `NEXT` and native lifecycle writes
  `settled/task_complete` without inventing a successor;
- a primary completed owner gate with no active role causes the supervisor to
  fail with a typed terminal blocker instead of emitting indefinite RUNNING
  heartbeats; a newer `NEXT` Review supersedes an older historical gate;
- local `soak-settle-stopped` requires canonical pause, the exact
  `recovery_required` external binding, `SOAK_EVIDENCE_UNRESOLVED`, an exact
  failed/cancelled owned job, matching permit/run/process identity, and a dead
  original supervisor generation. It preserves measured evidence and is
  idempotent.

The command settled `p20-live-20261006-b` to `FAILED` with reason
`owned_soak_job_terminal:cancelled`. Its original start, monotonic segment,
P204.1–P204.4 evidence, 3/15 verified fault dispositions, and CPF evidence are
retained. Qualification returns `FAIL`; the run is not resumable or eligible
for acceptance. Its post-settlement SHA-256 is
`8a122a385e1f83296bb60fd93e6afcd11ba427f6ca764761f6354c54923ef7c9`.
The earlier failed run `p20-live-20261004-a` remains byte-identical at
`e782e815aa956f049ce765e67987b685d928011bad13189ecd8af081956b72c0`.

Verification:

- all P20.4 tests: 99 passed before the final stale-review regression; focused
  final suite: 5 passed;
- transition/control/daemon/job/CLI regression: 80 passed plus 10 subtests;
- independent Reviewer rerun: P20.4 100 passed; lifecycle/control/jobs/CLI 81
  passed;
- compileall, CLI help, diff check, reviewed source hashes, and Graphify update
  passed.

Independent Technical Review by `agy/agy-1/gemini-3.8-flash-high-adjudicator`
returned `ACCEPT` with no blocking findings. Receipt:
`runtime/p20-stall-remediation-review-attempt-1.json`.

P20.4 remains incomplete. A future measurement must use a fresh run id,
project, permit, disposable paths, job and six-hour clock through the normal
owner recovery workflow. Neither failed run may be resumed, reset, appended to,
or concatenated. No push or P21 action was performed.

