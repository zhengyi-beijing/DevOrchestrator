# P20.4 Run F F02 remediation result

Date: 2026-10-07

## Outcome

Run F (`p20-live-20261007-f`) remains immutable failed evidence. It ended after
15,172.203 monotonic seconds with 14 of 15 fault dispositions; the sole pending
case was `F02_PLANNER_TIMEOUT`. Investigation found that F02 had recovered at
the Planner boundary, but its evidence collector waited for a complete
Worker/Reviewer acceptance. The recovered Worker later became ambiguous when
the broker status endpoint was unavailable, so the case exhausted its
observation budget without recording the already durable recovery.

Commits `6d826ec` and `2d90b24` repair the evidence boundary. F02 now accepts
either of the two real automatic recovery paths:

1. a later successful provider attempt in the same Planner cycle; or
2. an accepted watchdog recovery command, its project-scoped recovery
   milestone, a later Planner success, and the resulting durable Worker launch.

Both paths verify dispatch identity, attempt ordering, timestamp ordering, plan
and project lineage, and the exact Worker launch boundary. The watchdog path
also validates the command ID before reading its durable record. Final
qualification recomputes the proof instead of trusting the initially declared
evidence.

## Run F evidence

The preserved F02 case shows this causal sequence:

- fault injected: `2026-10-07T01:02:50.256Z`;
- recovery command requested: `2026-10-07T01:33:45.523846Z`;
- recovery milestone: `2026-10-07T01:33:45.536068Z`;
- replacement plan started: `2026-10-07T01:33:51.096419Z`;
- Planner recovered: `2026-10-07T01:35:12.489311Z`;
- Worker durably launched: `2026-10-07T01:36:01.570042Z`.

The repaired verifier returns a non-null proof on the untouched Run F case and
rejects unrelated, reordered, cross-project, unsafe-path, or tampered evidence.
The Run F run file remains byte-identical with SHA-256
`b24ae34e1ab0e51fa8e448100d0ef7e8cc44318055a99a2749ee5dce771d37f6`.

## Verification

- focused F02 regression: 18 passed;
- all P20.4 tests: 122 passed;
- adjacent P20.3, transition executor, watchdog, and control regression:
  281 passed plus 23 subtests;
- compile and diff checks: passed;
- Graphify updated after both code changes.

Two independent read-only Technical Review rounds used
`claude/default/opus`. The final review request
`p20-run-f-f02-remediation-review-20261007-r2` returned **ACCEPT** with zero
blocking findings and independently reproduced every test count above, the
real Run F proof, ordering constraints, path safety, and falsification cases.
Its durable dispatch was `92add219-d6db-4aba-a7e5-97d188b4e13b` and execution
was `39d3b3ee-5253-4383-a08a-b5543fdbf9d5`.

P20.4 is still incomplete. A qualifying attempt must use a fresh Run G
run/project/permit/path/job/source-request identity and independently satisfy
the full six-hour gate.
