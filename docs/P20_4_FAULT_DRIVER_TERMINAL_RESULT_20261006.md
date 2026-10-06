# P20.4 Fault Driver Terminal Remediation Result (2026-10-06)

## Run C disposition

Fresh run `p20-live-20261006-c` started at 2026-10-06 07:53:13 Asia/Shanghai.
It completed all five primary tasks and all ten deterministic CPF checks. All
fifteen isolated fault cases were attempted, but only F07, F08, F09, and F12
produced verified dispositions. Eleven cases remained pending after the
fault-driver thread exhausted its case list.

The supervisor had no terminal check for that producer state. From the end of
the case list until diagnosis it continued to write job heartbeats with
`current_execution: null`, even though no future fault evidence could be
created. This was a silent terminal stall, not a healthy soak wait.

Canonical DevOrchestrator was paused normally. Durable job
`job-4f5634aea52f8ece` was cancelled with reason
`p20_soak_fault_driver_exhausted_with_pending_evidence`; job supervisor PID
7724 and soak child PID 40304 were verified dead. Canonical converged to
`OWNER_GATE / SOAK_EVIDENCE_UNRESOLVED` with no active Worker and all lifecycle
invariants holding. Exact stopped-run settlement preserved the measured
evidence and marked run C `FAILED` at 2026-10-06 17:44:54 Asia/Shanghai. Its
post-settlement SHA-256 is
`0d53c0578d07aa580e910993828265ea823299e6bbf2e2450b3f36b66cbd1308`.

## Software correction

Commit `03323a2` gives `LiveFaultDriver` an explicit running/stopped/finished
state and durable terminal timestamp. When a finished driver still owns pending
fault evidence, the supervisor now checkpoints the latest evidence, transitions
the run to `FAILED`, records
`FAULT_DRIVER_EXHAUSTED_WITH_PENDING_EVIDENCE`, emits a typed timeline event,
and exits with code 7 on the next poll. Normal shutdown is recorded as stopped,
and a finished driver with no pending faults remains eligible for the six-hour
qualification boundary.

Regression evidence:

- focused stall-remediation suite: 8 passed;
- all P20.4 suites: 103 passed;
- transition/control/durable-job/CLI suites: 71 passed and 10 subtests passed;
- compileall and diff checks passed;
- Graphify updated.

Independent read-only Technical Review by `claude/default/opus` returned
`ACCEPT` with zero blocking findings and reproduced the test evidence. Review
receipt: `runtime/p20-fault-driver-terminal-review-attempt-1.json`.

P20.4 remains incomplete. Run C is immutable failed evidence and must never be
resumed, reset, or combined with another run. Any further qualification attempt
requires a fresh run/project/permit/path/job/clock after a normal daemon reload.
