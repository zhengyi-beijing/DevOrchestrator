# P20.4 Fault Driver Terminal Contract (2026-10-06)

## Observed failure

Run `p20-live-20261006-c` completed all five primary tasks and attempted every
permitted fault case. The fault-driver thread then exited with eleven cases
still lacking verified dispositions. The supervisor continued to write
heartbeats because it only inspected the disposition snapshot and the global
twelve-hour deadline; it did not inspect whether the producer of future fault
evidence had terminated.

## Invariants

1. A finished fault driver with pending evidence cannot coexist with a
   `RUNNING` soak beyond the next supervisor poll.
2. Verified dispositions, pending fault IDs, and bounded error types remain
   durable evidence when the soak fails.
3. A driver stopped as part of normal supervisor shutdown is not independently
   classified as a fault-driver failure.
4. A driver that finishes with no pending faults may wait for the six-hour
   qualification boundary.
5. No recovery path resets the original clock or reuses the failed run ID.

## Transition boundary

After each fault snapshot is checkpointed, the supervisor inspects the fault
driver's terminal state. `finished + pending` transitions the original soak
from `RUNNING` to `FAILED` with the typed reason
`fault_driver_terminal:FAULT_DRIVER_EXHAUSTED_WITH_PENDING_EVIDENCE`. The
transition happens before qualification and maximum-duration evaluation.

The terminal evidence includes the pending fault IDs and sanitized exception
type names. It does not convert missing evidence into an owner gate or a
successful disposition.

## Regression proof

- A driver that exhausts its case list marks itself finished durably.
- A finished driver with pending evidence returns a typed terminal blocker.
- The supervisor consumes that blocker, checkpoints `FAILED`, emits a terminal
  event, and exits nonzero instead of continuing heartbeats.
- A finished driver with no pending evidence remains eligible for ordinary
  six-hour qualification.

This closes CPF-04 (fail closed on contradictory/incomplete evidence), CPF-06
(idempotent evidence snapshots), and CPF-07 (one terminal authority transition).
