# P20.4 owner-authorized fresh-soak recovery contract

Scope: materialize the owner decision to replace the preserved FAILED
`p20-live-20261004-a` measurement with one fresh, independently identified live
measurement. The old run, job, permit, timing, failure, execution record, and
owner-gate evidence remain immutable history. Recovery does not retry or resume
the old execution.

## Control-plane declaration

- Invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION,
  TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION, PENDING_DESIGN_NOT_EXECUTING,
  SUCCESSOR_HANDOFF_LINEAGE_VALID, NEXT_TASK_WITHOUT_HANDOFF,
  SINGLE_ACTIVE_LIFECYCLE_OWNER.
- Transition boundaries: local_control_ingress, owner_gate_transition,
  failed_execution_settlement, bootstrap_publication, worker_launch,
  durable_soak_job_binding.
- Registered fault scenarios: CPF-01, CPF-02, CPF-03, CPF-04, CPF-05, CPF-06,
  CPF-07, CPF-08, CPF-09, CPF-10.
- Convergence evidence: the exact failed run and job are terminal; the project is
  owner-paused and quiescent; a clean committed HEAD is bound to a new permit;
  authorization is replay-idempotent across bootstrap/ledger crash windows; the
  old execution lineage settles as failed; the fresh Worker carries explicit
  recovery ancestry; any identity, permit, HEAD, or ownership mismatch fails
  closed without launching.

## Required behavior

1. `soak_recover` is local-only. Remote control schemas cannot submit it.
2. Authorization requires the exact P20.4 `SOAK_EVIDENCE_UNRESOLVED` gate,
   `active_owner = null`, no active execution, owner pause, and a clean repository.
3. The old binding must match its permit and terminal durable job. Its run must be
   `FAILED`. The old run bytes are hashed before authorization and never rewritten.
4. The replacement uses a different run ID, project ID, permit fingerprint,
   repository path, and runtime path, and must not already contain a run record.
5. Bootstrap replacement archives the previous pointer and is exactly replayable.
   Changed command/permit identity is rejected.
6. Authorization itself launches neither a Worker nor a durable job. It clears
   only the exact failed-soak gate while the project remains paused.
7. A later explicit resume/continue creates a new Worker identity, links it to
   the failed execution lineage, and refuses launch if HEAD changed.
8. Normal `soak-start` binds exactly one idempotent fresh durable job. The new
   supervisor opens a new clock only after the disposable primary pipeline is
   normally active.

No old-clock accumulation, duplicate launch, manual ledger repair, acceptance
claim, P20.5 activation, P21, push, reset, rebase, clean, or history rewrite.
The new run must independently satisfy the full six-hour P20.4 acceptance gate.
