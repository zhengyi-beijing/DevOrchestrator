# P20.4 Run E boundary-evidence remediation result (2026-10-07)

## Run E result

Fresh run `p20-live-20261007-e` used project `p20-soak-20261007-e`, durable job
`job-68ff41834bb9f0c9`, and source
`4cb1842c3c28e63977edf84961d37f7e0b67b695`. It ran from
2026-10-07 00:17:40 to 06:18:21 Asia/Shanghai, completed all five primary
tasks, preserved clock continuity, and verified 12 of 15 fault dispositions.
It terminated honestly with exit code 7 and
`FAULT_DRIVER_EXHAUSTED_WITH_PENDING_EVIDENCE`; F03, F07, and F08 remained
pending. Canonical authority converged to
`OWNER_GATE / SOAK_EVIDENCE_UNRESOLVED` with no active owner and all invariants
holding.

Run E is immutable failed evidence. Its clock, job, permit, project, and
disposable runtime must not be resumed, rewritten, or combined with a later
measurement.

## Root causes and repairs

Commits `1c713fc`, `bed7d5d`, and `62dc8ec` repair the three boundary-evidence
failures:

- F03 now proves the exact interrupted Broker Worker request, a distinct
  post-injection Worker on the same task, provider-independent Technical Review,
  review decision head agreement, and Git ancestry.
- F07 now proves the injected transport attempt and a distinct later successful
  Planner request. Blank or reused request identities fail closed.
- F08 now proves a changed daemon process through the durable boot whose PID and
  timestamp fall inside the actual `reserved_at < boot.started_at <= injected_at`
  restart window. Three coherent post-boot authority observations are required.
  Final qualification revalidates that frozen evidence after normal case-daemon
  cleanup; it no longer depends on a live PID or a running `daemon.json`.
- `observed_faults` recomputes F03/F07/F08 semantic evidence and requires exact
  equality with the driver's declaration. Altered, missing, or additional proof
  fields do not qualify.

The F08 proof rejects stale authority, `PLAN_FAILED`, `OWNER_GATE`, task or
generation drift, violated invariants, the wrong daemon PID, a boot outside the
reserved/injected window, insufficient observations, and no-restart identity.
The owner-gate fallback remains available for a genuine terminal gate.

## Verification

- focused live-fault regressions: 15 passed;
- all P20.4 suites: 119 passed;
- P20.3 recovery suites: 87 passed;
- transition integration: 71 passed and 18 subtests;
- watchdog/control: 136 passed and 5 subtests;
- local total: 413 passed and 23 subtests;
- compileall, diff checks, and Graphify update passed;
- independent final Technical Review by `claude/default/opus`: `ACCEPT`, zero
  BLOCKING findings; reviewer reproduced 255 P20 tests and verified the real
  production restart window, cleanup-safe byte-identical proof, and tamper
  rejection.

Review receipts are
`runtime/p20-run-e-boundary-remediation-review.json`,
`runtime/p20-run-e-boundary-remediation-r2-review.json`, and
`runtime/p20-run-e-boundary-remediation-r3-review.json`. The final review
accepted commit `62dc8ec`.

P20.4 remains incomplete until a fresh run independently satisfies the actual
continuous six-hour minimum, five accepted tasks, all 15 live fault
dispositions, hard-zero invariants, and final Technical Review. The replacement
must use new run, project, permit, path, job, source-request, and clock
identities. Do not activate P20.5 or P21 and do not push, reset, rebase, or clean.
