# NEXT — Watchdog recovery-epoch / stale OWNER_GATE cleanup

Status: **NEXT AFTER P14.5** (P14.5 closed at `e2fce11` on 2026-09-20)

Sequence per `docs/backlog.md`:
`P14.5 (closed) -> Watchdog recovery-epoch cleanup -> P14.6 Unattended Execution Stabilization Gate -> P15`

Goal: prevent a historical watchdog-generated OWNER_GATE/recovery budget from
contaminating a newer valid execution epoch, while preserving genuine
owner-decision gates.

Scope:
- Define a durable recovery-epoch identity from current task/plan/control command/HEAD/execution evidence.
- When newer valid execution evidence supersedes the evidence that created a watchdog-generated OWNER_GATE, invalidate the stale watchdog gate, attempts, diagnosis and evidence hash automatically.
- Never auto-clear an explicit owner-decision OWNER_GATE; only watchdog/recovery-generated stale gates are eligible.
- Project health/watchdog projection must not report historical owner_gate/unknown while a newer authoritative execution is healthy.
- Reset attempts_this_run and recovery budget only on a proven epoch transition, not merely because a worker process appeared.
- Add regression coverage for stale historical gate cleanup, genuine owner gate preservation, restart/replay, and new Worker/Reviewer epochs.

Acceptance:
- Reproduce the P14.5-era case (new WORKER_RUNNING/EXECUTING while the watchdog retains an old owner_gate with attempts=20), advance to a newer epoch, and prove automatic cleanup without a manual watchdog-clear or user continue.

## Carried forward from P14.5 — residual NON_BLOCKING items

None of these blocked P14.5 closure. They are recorded here so they are not
lost; schedule them against the task where each fits rather than treating them
as a single unit.

| id | Item | Suggested home |
| --- | --- | --- |
| NB-1 | `timeout_seconds` serves as both the whole-review coordinator budget (`ai_reviewer.py`) and a fresh per-packet `AIRoleRequest` timeout (`review/runner.py`); multi-packet reviews can outlive the coordinator budget. Separate the two, or propagate a bounded remaining budget. | P14.6 |
| NB-2 | Accounting idempotency: a crash after decision persistence but before `end_interval`/`record_attempt_outcome` leaves a right-censored interval. Replaying the current APIs is unsafe because accounting events lack deterministic ids. Add idempotent accounting identities or a repair projection. | P14.6 |
| NB-3 | `quarantined_decisions` has no consumer, operator surface or retention bound. | Watchdog/operator surface work |
| NB-4 | `recovery_identity_error` durably blocks a review with a conflicting embedded id and no operator exit path. | This task (operator visibility) |
| NB-5 | `_emit_terminal_milestone` swallows all emit exceptions without diagnostics; persistent transport failure is invisible. | This task (operator visibility) |
| NB-6 | `_transition_supports_recovery_retry` is stricter than `resolve_retry_candidate`, so some genuinely retryable records may be classified `terminal_source_unreachable`. Fail-closed, but worth aligning. | This task |
| NB-7 | `advance()`-level test coverage for degenerate worker resource contexts; the implementation validates, only the test exercises the helper directly. | P14.6 |
| NB-8 | `AIRoleRequest.previous_resource_context` accepts a plain `Mapping` despite its `ResourceContext` annotation. Cosmetic type-contract alignment. | P14.6 |
| NB-9 | Web Sol bridge lease duration: the `chatgpt_web` adapter claims a request then stops renewing its 30s lease, so a multi-minute review cannot return through the bridge. Transport defect, outside P14.5. | Separate bridge task |
| NB-10 | The reviewer-remediation convergence policy developed during P14.5 is not yet encoded in `docs/development-workflow.md` (frozen contract artifact, blocking-finding admission rules, per-set and per-task remediation budgets, multi-reviewer disagreement policy). | Process work, before the next long review cycle |
| NB-11 | `review/runner.py:195` forbids the delegated model from emitting lifecycle tokens, but `review/runner.py:505-514` has the runner itself emit a field named `disposition` with values `next`/`remediate`. The only consumer is the reporting field at `review/store.py:115`; DevO does not read it, and AC-3 independence was proven by discriminating test. Still a contract smell inviting future misuse — rename to something like `evidence_summary`. | P14.6 |
| NB-12 | `reviewer_harness` remains absent from `config/projects.json`. The harness is demonstrated but not enabled in production; enabling it is a deliberate future decision, not P14.5 scope. | Owner decision before P14.6 |

## P14.5 closure references

- Decision packet: `.devorch/forensics/p145-closure-decision-packet-e2fce11-v2.md`
- Reproduction oracles (all closed at `e2fce11`):
  `p145-repro-cad7cf5-findings.py`, `p145-repro-bb2a7a4-findings.py`,
  `p145-repro-6b5d5ba-findings.py`
- Live demonstration outputs: `smoke-a-output.txt`, `smoke-b-output.txt`,
  `smoke-c-output.txt`
- Review verdicts: `p145-websol-final-verdict-a74ed3f.json`,
  `p145-review-verdict-cad7cf5.md`, `p145-review-verdict-bb2a7a4.md`,
  `p145-audit-verdict-9922480.md`

## Operational state at handoff

- Anchor `e2fce11`, worktree clean, **not pushed**.
- DevOrchestrator is **owner-paused** (`p145-freeze-a74ed3f-extreview-20260920`).
  Resuming it is a separate owner decision and is not implied by P14.5 closure.
- No successor task has been started.
