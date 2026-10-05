# P20.4 stalled-soak remediation contract

## Scope

Repair the live P20.4 defect where the fifth isolated task passed Technical
Review but the generated `Successor: TBD / owner decision required` wording
routed the final review to an owner gate. The durable supervisor then kept a
`RUNNING` heartbeat indefinitely with no active role and never reached the
remaining fault cases or an honest terminal result.

The repair is bounded to the disposable soak template, supervisor terminal
detection, and an explicit local settlement command for a stopped owned soak.
It does not change production task-roadmap semantics, ordinary owner-gate
authority, or qualification thresholds.

## Affected invariants

1. `SINGLE_ACTIVE_LIFECYCLE_OWNER`: terminal detection is read-only and never
   launches, retries, or fabricates a lifecycle owner.
2. `NEXT_TASK_WITHOUT_HANDOFF`: P204.5 remains an actual end-of-roadmap task;
   an accepted `next` review must settle as `task_complete`, without publishing
   a successor handoff.
3. `SUCCESSOR_HANDOFF_LINEAGE_VALID`: the template must keep roadmap successor
   `null`; it may clarify terminal intent but must not invent P204.6.
4. `TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION`: a terminal owner gate with no
   active role cannot be reported as active soak progress.
5. Immutable measurement identity: a stopped run can only move from `RUNNING`
   to `FAILED` when its exact owned job is terminal and its exact original
   supervisor process generation is dead. Start time, monotonic segments,
   task/fault evidence, run id, and permit identity remain unchanged.
6. Genuine owner decisions remain fail-closed. No owner gate is converted to
   acceptance, `next`, or `task_complete` by the supervisor or settlement CLI.

## Transition boundaries

- Final isolated Review ACCEPT + end-of-roadmap -> native
  `settled/task_complete` -> fifth task becomes source-correlated evidence.
- Any primary owner gate + no active role -> supervisor `FAILED` with a typed
  primary-pipeline reason; never an indefinite `RUNNING` heartbeat.
- Exact terminal owned job + dead matching original supervisor generation ->
  explicit local settlement `RUNNING -> FAILED`. A live or mismatched process,
  nonterminal job, wrong permit/job identity, or already incompatible terminal
  run is refused.

## Registered CPF coverage

- `CPF-03` and `CPF-04`: end-of-roadmap settlement must not fabricate, lose, or
  replay a successor handoff.
- `CPF-06`: settlement and terminal detection are byte-stable/idempotent under
  duplicate requests.
- `CPF-07`: terminal convergence retains a single authority owner and never
  relaunches work.

## Convergence evidence

- RED/GREEN regression proving the generated fifth task explicitly advertises
  end-of-roadmap terminal settlement while native roadmap resolution stays
  `end_of_roadmap`.
- RED/GREEN regression proving a primary completed owner gate with no active
  role becomes a typed supervisor blocker and a genuine gate is not accepted.
- RED/GREEN regression proving exact dead-process settlement preserves all
  measurement evidence, is idempotent, and refuses a live process generation.
- Related P20.4, transition, control, job, and daemon regression; compile and
  diff checks; Graphify refresh; independent Technical Review before runtime
  application.

