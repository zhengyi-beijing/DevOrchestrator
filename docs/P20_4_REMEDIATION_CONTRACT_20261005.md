# P20.4 failed-run remediation contract

Scope: repair D01–D07 in the preserved failed-run diagnosis. The original
`p20-live-20261004-a` remains FAILED; no timestamp, segment, failure, or acceptance
evidence is rewritten. This is bounded implementation/remediation within the
approved P20.4 design, followed by regression and independent Technical Review.

## Control-Plane Impact
- Invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION, PENDING_DESIGN_NOT_EXECUTING, SUCCESSOR_HANDOFF_LINEAGE_VALID, NEXT_TASK_WITHOUT_HANDOFF, SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: authority_reconciliation, worker_launch, owner_gate_transition, watchdog_recovery, successor_handoff, handoff_publication
- Fault scenarios: CPF-01, CPF-02, CPF-03, CPF-04, CPF-05, CPF-06, CPF-07, CPF-08, CPF-09, CPF-10
- Convergence evidence: task-scoped launch fences survive changed HEAD and restart; current role failure remains authoritative and recoverable through existing bounded Watchdog policy; blocked decisions and observation failure expose stable authority gates; old-task evidence cannot gate a successor.

Contracts to test before implementation:

1. Static readiness cannot relaunch an already executed task when its HEAD changes.
   Pending Review and unapplied decisions fence ordinary launch. Explicit validated
   remediation and retry retain their lineage and bounded permissions.
2. Latest current-task Planner failure projects as PLAN_FAILED/RECOVERY_REQUIRED,
   allowing existing recovery policy to act. Explicit Planner gates and same-task
   blocked review obligations persist in sole lifecycle authority.
3. Use an OS boot identifier independent of wall-clock estimates. Store the original
   supervisor PID/create-time; reject process replacement, reboot, and monotonic
   rollback. Preserve wall/monotonic deltas; the existing >10-second clock anomaly
   refusal remains. Small normal corrections must not imply reboot. Legacy FAILED
   runs cannot be promoted to qualification.
4. Compact observations retain the fields used by verifiers. Bounded append-only
   segments with a checked sealed-segment index retain the entire measurement;
   unavailable/corrupt/capacity-exhausted evidence is UNKNOWN and opens a visible
   stable gate, never silently discarded. All readers consume all segments.
5. Duplicate metrics include terminal launch history, different sources for the
   same task, interval overlap, and authenticated remediation/retry ancestry.
6. Generated roadmap preflight uses the native resolver, including checkout bytes.
7. Case success, timeout, exception, and supervisor exit stop only permitted owned
   daemons through the normal API with PID/create-time identity checks. Preserve
   ledgers. Cleanup failure is recorded; shutdown is bounded and idempotent.
8. Two failures of one provider/model prohibit further blind account rotation, but
   do not terminate eligible heterogeneous failover within the existing budget.
   Quality and independence constraints remain unchanged; exhaustion gates.

No P21, push, Broker configuration edits, manual lifecycle repair, or claims of
live acceptance from deterministic fixtures. A later live run must have its own
explicit boundary and normal durable owner/job/control authorization.

Independent Technical Review R1 returned CHANGES_REQUIRED. Bounded remediation
delta: preserve gate identity with a full-reason digest in compact observations;
ambiguous current-task recovery_required opens a stable explicit authority gate;
write bounded observations only to an explicit reliability run's own directory,
with capacity failure scoped to its project and closed runs immutable. Preserve
the legacy store for historical readers. Windows boot identity additionally
requires the successful-boot BootId counter (the GUID identifies a BCD entry).
Regression must demonstrate restart persistence, unrelated-project isolation,
post-restart gate verification, and a changed counter with an unchanged GUID.
