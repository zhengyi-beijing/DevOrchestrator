# NEXT — P14.6 Unattended Execution Stabilization Gate

Status: **NEXT / OWNER AUTHORIZED TO CONTINUE** (watchdog recovery-epoch cleanup closed 2026-09-20)

Sequence:
P14.5 (closed) -> Watchdog recovery-epoch cleanup (closed) -> P14.6 Unattended Execution Stabilization Gate -> P15

## Watchdog recovery-epoch cleanup closure

Closed after validating and extending the earlier 6b1e7f3 authoritative recovery-epoch implementation.

Acceptance evidence:
- Recovery epochs are derived from authoritative task/HEAD/plan/control/Worker execution evidence and now also include active review_id, so a new Reviewer run is an explicit epoch boundary.
- Epoch supersession resets watchdog-only attempts, attempt counts, recovery slots, cooldown, diagnosis, evidence hash, recovery result and watchdog owner_gate while incrementing the diagnostic fence.
- Genuine current lifecycle OWNER_GATE remains fail-closed and is not auto-cleared.
- Exact P14.5-era regression is covered: healthy newer WORKER_RUNNING/EXECUTING with historical watchdog owner_gate and attempts=20 advances epoch, clears stale budget/gate and projects watchdog state ok with attempts_this_run=0.
- Restart/replay and READY_TO_RUN recovery remain covered.
- Focused verification: test_watchdog_recovery.py 62 passed + 5 subtests; adjacent watchdog suites 40 passed.
- Full repository regression: 882 passed + 87 subtests.
- compileall and git diff --check passed.

## P14.6 goal

Prove that the existing DevO control/recovery stack can sustain real unattended development before adding another client surface.

Immediate implementation sequence:
1. Consolidate Planner protocol handling into Raw Capture -> JSON Extract -> Normalize -> Schema Validate -> Semantic Validate -> Reviewer, with bounded format repair for schema-only failures.
2. Verify provider/session/quota failures trigger resource failover without consuming semantic-remediation budget or requiring user continue.
3. Require lifecycle/health/watchdog projections to agree on the current recovery epoch.
4. Exercise automatic Review -> Remediation -> Re-review -> completion and automatic promotion/launch of the next eligible task.
5. Build the unattended fault-injection acceptance harness before the long qualification run.

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

## Available from P14.5 — review rule pack

`config/devorch_rules.json` (version `0.1.0-draft`, added in `5d39787`) holds
ten review rules derived from the blocking defects confirmed during P14.5. Each
carries a "Defect precedent" clause naming the concrete case.

Not yet usable for real review. Three prerequisites, all listed above:
- NB-1 — per-packet timeout semantics. This repository's implementation commits
  routinely touch 13-23 files, so multi-packet reviews are the normal case, not
  an edge case.
- NB-12 — `reviewer_harness` is absent from `config/projects.json`.
- `file_limits` tuning for realistic commit sizes.

The single P14.5 smoke run executed with `rule_pack_path: None` and therefore
reported zero findings; that result demonstrates the pipeline, not review
quality. Suggested first use is a parallel trial on this task: run the harness
for structured findings while independent deep review proceeds as normal, then
compare what each caught. Note that packet-scoped per-file review structurally
cannot find the F1/G1/H2 class of defect, which required fault injection and
cross-module reasoning; the rule pack raises the floor rather than replacing
adversarial review.

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
