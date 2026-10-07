# P20.4 Run G F03 remediation result

Date: 2026-10-08

## Outcome

Run G (`p20-live-20261007-g`) remains immutable failed evidence. It ended after
17,380.593 monotonic seconds with 14 of 15 fault dispositions. The sole pending
case was `F03_WORKER_KILLED`.

The F03 driver observed the Worker as running and called the Broker interrupt
API, but the API returned `status=running` and `interrupt_supported=false`.
The old driver recorded that response as a completed injection. The original
Worker then succeeded, so the independent F03 recovery verifier correctly
refused to certify a failed Worker followed by a reviewed retry. The case
exhausted its observation budget without a disposition.

Commit `d1917f4` repairs the injection boundary. F03 now records `injected`
only when the exact interrupt response has the same request ID, a terminal
`failed` or `cancelled` status, and `interrupt_supported=true`. Unsupported,
missing, mismatched, non-terminal, and transient-error responses remain in the
polling loop. If the Worker completes before terminal acknowledgement, the
reservation becomes `not_injected` and does not consume the injection attempt.
The confirmed response also records its dispatch and execution identities.

## Run G evidence

The preserved F03 case at
`C:\work\p20-soak\20261007-live-g-fresh\runtime\fault-cases\F03_WORKER_KILLED`
shows:

- the old `fault-effect.json` claimed `state=injected` with
  `interrupt_status=running` and no positive capability evidence;
- the same original Worker later completed successfully;
- no distinct reviewed recovery Worker existed, so
  `worker_interrupt_recovered()` returned no proof;
- the case closed with `observation_budget_exhausted`.

The terminal Run G file remains byte-identical with SHA-256
`dc73f6a223e5ced9d5349985d18668e529f87fbf724c126fa23360ff7f3c7fee`.

## Verification

- focused live-fault regressions: 20 passed;
- all P20.4 tests: 124 passed;
- adjacent P20.3, TransitionExecutor, Watchdog, and control regression:
  285 passed plus 23 subtests;
- compile, diff, and Graphify update checks: passed.

Independent read-only Technical Review request
`p20-run-g-f03-remediation-review-20261008-r5` ran on
`copilot/default/claude-sonnet-4.6` and returned **ACCEPT** with zero BLOCKING
and one NON_BLOCKING finding. The non-blocking finding is the pre-existing
five-second poller join edge case; the authoritative ledger verifier still
prevents false certification if that edge is reached. The review dispatch was
`b475cf75-d5f3-4e0f-a345-93a2cd485e82` and execution was
`25b16045-ba67-4b90-a82e-92eaff36afda`.

P20.4 is still incomplete. A qualifying attempt must use a fresh Run H
run/project/permit/path/job/source-request identity and independently satisfy
the full six-hour gate.
