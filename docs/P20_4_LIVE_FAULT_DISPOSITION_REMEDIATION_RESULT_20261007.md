# P20.4 Live Fault-Disposition Remediation Result (2026-10-07)

## Run D result

Fresh measurement `p20-live-20261006-d` ran from 2026-10-06 18:06:02 to
22:18:28 Asia/Shanghai on source `e2b3e28277cb24c598fabdf2af3f5e9231f1f28d`.
The terminal-driver guard worked as designed: after all cases were attempted it
failed the run with exit code 7 and
`FAULT_DRIVER_EXHAUSTED_WITH_PENDING_EVIDENCE` instead of remaining falsely
RUNNING. The durable job was `job-dec4eb620fb4593d`.

Five fault dispositions verified: F01, F07, F08, F09, and F12. Ten remained
pending: F02, F03, F04, F05, F06, F10, F11, F13, F14, and F15. The six-hour
gate failed secondarily because the run ended after about 4 hours 12 minutes.
All canonical lifecycle invariants held, no active owner remained, and the
canonical project converged to `OWNER_GATE / SOAK_EVIDENCE_UNRESOLVED`.

## Root causes and corrections

Commits `d7bae80`, `354ba69`, and `c4b2a59` correct four shared causes:

- Injection and recovery now have separate bounded 45-minute phases. A case no
  longer spends nearly all of its recovery allowance waiting for the watchdog.
- F03 polls its exact Broker request for the lifetime of the dispatch; F04
  targets Technical Review rather than Plan Review; response-boundary faults do
  not consume their injection budget when the real upstream request fails.
- Single-task cases delete all successor specifications, reduce the manifest to
  P204.1, and run the native successor preflight before launch.
- F11 verifies stable dirty `wait_without_mutation` across restart and records
  `fail_closed`; F13 verifies reconstruction of the exact backed-up transition,
  production `published/completed` handoff state, target authority, invariants,
  and single owner; F14 verifies one authoritative idempotent retry record.
  Runtime disposition and final qualification share these evidence functions.

F14 preserves its one-attempt budget across the structural duplicate request,
so it cannot reinject and extend the deadline. F11 cannot fall through to a
generic accepted-task or owner-gate disposition. F13 cannot accept a skipped
target or a transition state that production never emits.

## Verification

- focused live-fault regressions and registry: 13 passed;
- all P20.4 suites: 112 passed;
- transition/control/watchdog/recovery suites: 185 passed and 23 subtests;
- independent final remediation review: `ACCEPT`, zero BLOCKING findings;
- reviewer reproduction: 285 related tests passed and 18 subtests;
- compileall, diff checks, and Graphify update passed.

Review receipts are
`runtime/p20-live-disposition-remediation-review-attempt-1.json`,
`runtime/p20-live-disposition-remediation-review-attempt-2.json`, and
`runtime/p20-live-disposition-remediation-review-attempt-3.json`. The final
review used `claude/default/opus` and accepted commit `c4b2a59`.

Run D remains immutable failed evidence and must not be resumed or combined with
another clock. P20.4 is still incomplete. The next measurement must use fresh
run, project, permit, disposable path, job, source-request, and clock identities.
