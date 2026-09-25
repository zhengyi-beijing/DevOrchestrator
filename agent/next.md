# P16.13 Successor Consistency & Zero-Touch Handoff Recovery

Status: **PENDING DESIGN**

## Goal
Eliminate silent task-chain stalls caused by roadmap/staged-task/reviewer successor disagreement, and guarantee zero-touch recovery without owner continue.

## Scope
- Validate roadmap successor links against staged task predecessor/sequence metadata.
- Fail closed on NEXT/next_task when roadmap advertises no successor but staged evidence indicates one.
- Add automatic successor reconcile/rebuild before terminal settlement.
- Add Watchdog invariant NEXT_TASK_WITHOUT_HANDOFF as final recovery layer.
- Correct handoff lineage: completed source task becomes source_task_id; active execution task_id must be the successor.
- Reconcile/close obsolete execution-loss findings when successor handoff is authoritative.
- Add end-to-end fault injection for missing/incorrect successor metadata.
- Add bounded remediation-budget extension: when review findings are explicit, localized, testable, and do not expand task scope, allow exactly one automatic extension instead of stopping at OWNER_GATE; fail closed to OWNER_GATE when eligibility is ambiguous or the extension is exhausted.

## State-machine convergence strategy
P16.13 must solve the lifecycle-consistency class, not only patch the current P16.12 -> P16.13 incident.

- Establish one authoritative lifecycle state; CURRENT/next/execution/reviewer/transition/watchdog are derived projections or validators, not competing task authorities.
- Make lifecycle transitions atomic and idempotent with source_task_id, target_task_id, and transition generation/epoch.
- Do not publish a successor until all source-task Worker/Reviewer/Remediation ownership is terminal or explicitly transferred.
- Enforce successor execution lineage and forbid pending-design successors from appearing EXECUTING without an authorized planning/design transition.
- Persist a transition journal and recover/replay from it after daemon restart instead of inferring state from contradictory projections.
- Drive Watchdog from invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION, PENDING_DESIGN_NOT_EXECUTING, SUCCESSOR_HANDOFF_LINEAGE_VALID, NEXT_TASK_WITHOUT_HANDOFF, SINGLE_ACTIVE_LIFECYCLE_OWNER.
- Build a P15-P16 fault-injection matrix covering stale Worker, lost review handoff, restart mid-transition, dirty worktree, successor disagreement, duplicate remediation/review, exhausted remediation budget, and old-task execution surviving successor publication.
- Zero-touch recovery is allowed only for unambiguous evidence; ambiguous ownership or repeated invariant failure must fail closed to OWNER_GATE.
- Preserve the live P16.12 -> P16.13 mismatch as a required regression fixture.

## Acceptance
1. Inject: reviewer returns NEXT/next_task, roadmap says current successor=null, while next staged task declares itself after current.
2. DevO detects ROADMAP_SUCCESSOR_INCONSISTENT and does not terminal-settle as project complete.
3. DevO automatically reconciles successor metadata and creates exactly one handoff.
4. Successor Planner/Worker starts automatically with no owner continue.
5. Active execution lineage uses successor task_id and prior task as source_task_id.
6. Stale execution-loss state converges to resolved after authoritative handoff.
7. Repeated ticks are idempotent and do not duplicate handoffs/recovery.
8. Regression/fault-injection test passes end-to-end with zero manual intervention.
9. When the normal remediation budget is exhausted but the reviewer returns only explicit localized blockers with bounded fixes and required regression tests, DevO grants at most one automatic bounded extension and re-reviews without owner intervention.
10. Ambiguous, scope-expanding, repeated, or still-failing findings after that extension stop at OWNER_GATE; repeated ticks never grant duplicate extensions.
