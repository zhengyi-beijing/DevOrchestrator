# P16.13 Successor Consistency and Zero-Touch Handoff Contract

Status: IMPLEMENTED / TECHNICAL REVIEW PENDING

## Incident and causal chain

The preserved P16.12 to P16.13 incident was an ordering and authority failure,
not a mislabeled Worker:

1. P16.12 Worker `wd-c945...` completed at approximately 2026-09-26 07:17.
2. Commit `2b326fd` closed P16.12 and published P16.13 as the repository task.
3. A still-valid P16.12 technical-review obligation then returned `REMEDIATE`.
4. Remediation `ai_review:wd-c945...` started at approximately 07:23 with
   `task_id=P16.12` and `source_task_id=P16.12`, after `2b326fd` was already
   HEAD. It completed at approximately 07:38 and produced `78dfc95`.
5. Managed-run overlay selected that active execution independently of the
   repository task and relabeled the top-level P16.13 projection as
   `EXECUTING`. The repository, reviewer, executor, and lifecycle projection
   were therefore competing authorities.
6. Execution-loss recovery correctly observed that the old Worker was alive,
   so it did not invalidate it. Watchdog had no cross-task ownership invariant
   and could not converge the disagreement.

P16.13 keeps that runtime evidence intact. The regression fixture in
`tests_py/test_p1613_successor_consistency.py` reproduces the material state:
the repository advertises the successor while predecessor remediation remains
running.

## One authority, explicit projections

The authoritative lifecycle record is
`runtime/transition-executor.json:lifecycle[project_id]`. The transition journal
is the sibling `transitions` map in that same existing ledger; it is not a
second state file or authority.

The boundaries are:

| Surface | Role |
| --- | --- |
| `lifecycle[project_id]` | Current task, lifecycle state, generation, active transition, active owner, and owner gate authority |
| `transitions[transition_id]` | Durable intent and recovery journal for one source-to-target generation |
| `executions` | Worker/remediation and handoff work-item evidence; never independently changes the current task |
| `agent/next.md`, `agent/CURRENT.md`, `agent/execution-state.json`, staged roadmap | Repository declaration/projection validated against authority |
| Planner and Reviewer ledgers | Role ownership and terminal evidence |
| Project status/UI | Read-only projection of lifecycle authority plus repository projection diagnostics |
| Watchdog | Shared-invariant validator and bounded recovery actuator; never task authority |

Legacy ledgers are migrated in place: missing `lifecycle` and `transitions`
maps are initialized while existing `executions` are retained byte-for-byte as
records. On the first reconciliation, a sole active role retains its task as
authoritative even when repository files already advertise a successor. One
terminal pre-P16.13 handoff may be upgraded on first safe successor launch only
when its reviewer is completed, no execution is active, and it is the sole
candidate; the historical row is annotated rather than replaced.

## Transition protocol

Every transition has:

- deterministic `transition_id` / `idempotency_key`;
- `project_id`, `source_task_id`, and `target_task_id`;
- monotonically consumed authority `generation`;
- durable `state`, timestamps, evidence, and source request identity;
- a replayable handoff record before successor publication is accepted.

The normal state sequence is:

```text
intent -> waiting_source -> ready -> published -> completed
                       \-> waiting_recovery / owner gate
```

`intent`, `waiting_source`, and `ready` do not make the target authoritative.
All Worker, Reviewer, remediation, and pending-review ownership for the source
must drain first. The Planner consumes the ready handoff, activates the staged
successor with its guarded repository commit, and only then atomically marks
the transition `published` and advances lifecycle authority. A restart can
recreate a missing handoff work item from the journal's `handoff_record` without
allocating another generation. A later exact branch/HEAD/status-anchored
accepted review may explicitly transfer older pending review obligations for
the same task because it reviewed the aggregate repository result; an actually
running reviewer with durable source execution remains a blocker.

The existing watchdog recovery epoch incorporates lifecycle generation and
transition identity. It is the recovery fence; P16.13 does not add an unrelated
epoch mechanism.

## Successor consistency and recovery

A pending staged task may declare `Predecessor:` or an explicit `Sequence:`.
The resolver cross-checks that evidence with `agent/staged/roadmap.json`:

- matching roadmap and staged claim: proceed;
- null/unlisted roadmap plus one unambiguous staged claim: emit
  `ROADMAP_SUCCESSOR_INCONSISTENT`, require a clean worktree, repair only the
  roadmap under an interprocess lock, validate it, and commit the repair;
- disagreement, multiple claims, malformed evidence, or changed staged bytes:
  fail closed;
- dirty worktree: preserve all changes and retry automatically on a later clean
  tick; do not consume the accepted review decision.

`NEXT_TASK_WITHOUT_HANDOFF` causes Watchdog to call the same idempotent
successor resolver and handoff writer. One unambiguous failure can therefore
heal with no owner `continue`. Repeated non-transient failure or contradictory
ownership becomes a durable `OWNER_GATE`.

## Central invariants

`core.lifecycle_authority.evaluate_lifecycle_invariants` is shared by lifecycle
reconciliation and Watchdog. It evaluates:

- `CURRENT_TASK_MATCHES_ACTIVE_EXECUTION`;
- `TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION`;
- `PENDING_DESIGN_NOT_EXECUTING`;
- `SUCCESSOR_HANDOFF_LINEAGE_VALID`;
- `NEXT_TASK_WITHOUT_HANDOFF`;
- `SINGLE_ACTIVE_LIFECYCLE_OWNER`.

Unambiguous missing-handoff violations are recoverable. Invalid lineage and
multi-owner ambiguity fail closed. Worker launch also fences on the authority:
a requested task must equal the current authoritative task, no owner gate may
exist, and `PENDING_DESIGN` cannot launch a Worker. After a consumed handoff,
the successor Worker inherits `task_id=target_task_id` and
`source_task_id=source_task_id`.

## Bounded remediation extension

The normal technical-remediation budget remains finite. At exhaustion, exactly
one additional remediation may be granted when all blocking findings are
structured, no more than the configured small bound, localized to files in the
reviewed diff, testable, and free of staged-task or cross-task scope expansion.
The grant and finding fingerprints are persisted on the review record. A
replayed review reuses the same grant; a descendant review sees that the one
extension was consumed. Missing/ambiguous findings, repeated fingerprints, or
another failure after the extension produce `OWNER_GATE`.

## Fault matrix and acceptance evidence

`tests_py/test_p1613_successor_consistency.py` maps the required injections:

| Fault | Deterministic assertion |
| --- | --- |
| A / H stale source Worker after successor publication | Source remains authoritative and executing; target is only a repository projection |
| B accepted review loses handoff | Exactly one handoff and transition are rebuilt automatically |
| C daemon restart mid-transition | Missing work item is replayed from the ready journal record |
| D dirty worktree | Repair waits without mutation, then advances after the tree becomes clean |
| E successor disagreement | Ambiguity returns conflict and writes no transition |
| F duplicate review/handoff | Stable request and transition identities produce one record |
| G exhausted remediation budget | One localized extension is granted; the next failure gates |
| I repeated ticks | Transition count and generation remain stable |
| J crash after intent, before publication | Source stays authoritative; recovery creates and consumes one handoff |

Focused lifecycle regression evidence: `122 passed, 10 subtests passed`.
Full Python regression evidence: `1281 passed, 92 subtests passed` in 494.49s.
Runtime smoke and independent Technical Review remain closure gates.
