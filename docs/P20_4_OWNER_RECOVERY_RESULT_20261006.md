# P20.4 owner recovery implementation result

The owner-authorized recovery path for the preserved FAILED soak is implemented
and independently accepted. It creates a new measurement identity and never
restarts, extends, or rewrites `p20-live-20261004-a`.

Implemented behavior:

- `soak-recover` is a local-only control action. It requires the canonical
  project to be owner-paused, clean, quiescent, and at the exact P20.4
  `SOAK_EVIDENCE_UNRESOLVED` gate.
- Recovery correlates the old execution binding, permit, terminal durable job,
  FAILED run, run bytes, and execution lineage before clearing the gate.
- The replacement permit must use new run/project/path identities and cannot
  already contain a run record.
- Bootstrap history is archived and replay-safe across crashes. Control-command
  settlement recognizes an already committed authorization without accepting a
  changed command or permit.
- Authorization launches no Worker or job. A later explicit resume/continue
  starts a fresh Worker at the exact reviewed HEAD with explicit recovery ancestry.
  HEAD drift and unsafe fallback retry both fail closed.

Regression evidence (XML case counts include subtests where pytest emits them):

- owner recovery: 15 passed;
- P20.4 related: 57 passed;
- transition/control/daemon: 94 passed;
- policy/external control: 44 passed;
- daemon integration: 11 passed.

Independent Technical Review initially encountered one provider timeout and two
quota failures, none of which produced a verdict. A heterogeneous AGY Gemini
review then ACCEPTED the exact hashes of eight source/contract/test files.
Dispatch `76297f27-f4f5-4910-b4c8-feee8889f50d`, execution
`05d325d3-94ae-44d9-af1e-7713f2ba7c31`. The only finding was NON_BLOCKING:
the local CLI correctly fails closed when the canonical daemon snapshot is absent.

This is software readiness for a fresh run, not P20.4 live acceptance. The new
run must independently satisfy the six-hour, five-task, fault-disposition,
hard-zero, CPF, and final independent-review gates.

