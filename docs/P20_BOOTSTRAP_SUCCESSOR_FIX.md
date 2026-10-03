# P20 bootstrap: terminal successor with stale readiness

Observed 2026-10-04 after normal owner-authorized P19.2 -> P20.1 handoff:
the durable handoff exists, but ControlCommandCoordinator requires COMPLETE
Markdown when reviewed_ready is false. P19.2 has authoritative terminal
settlement while its historical Markdown remains READY_TO_RUN; repeated ticks
therefore launch no Planner. This is an activation recovery defect, not
permission to rerun or relabel P19.2.

The bounded repair reads the existing executor authority and exact task-scoped
task_complete settlement. Only a terminal matching predecessor with no owner,
gate or active project Worker can supply the missing completed-status evidence.
All existing pause, task match, staged successor, Git clean/anchor, spec hash,
activation and Planner gates still run. No runtime ledger is hand-edited.
The existing handoff is consumed normally and only once after actual Planner
startup. No new handoff, authority or recovery loop is introduced.

TDD reproduction before repair: the daemon-ordered real-component regression
`TerminalRoadmapSuccessorEndToEndTests.test_terminal_settlement_consumes_successor_with_stale_ready_markdown`
failed with zero successor plans. Focused repair GREEN: eight predicate tests plus one end-to-end test, including
exact project/task identity and rejection of absent settlement, wrong outcome,
wrong task, owner gate, active owner/Worker and non-terminal authority.
Independent technical review and relevant regression are required before this
bootstrap fix is committed and the daemon resumed. These startup interventions
precede the isolated live soak and must not be counted as soak qualification.

## Control-Plane Impact
- Invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION, PENDING_DESIGN_NOT_EXECUTING, SUCCESSOR_HANDOFF_LINEAGE_VALID, NEXT_TASK_WITHOUT_HANDOFF, SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: authority_reconciliation, successor_handoff, worker_launch
- Fault scenarios: CPF-01, CPF-02, CPF-03, CPF-04, CPF-05, CPF-06, CPF-07, CPF-08, CPF-09, CPF-10
- Convergence evidence: Actual daemon-ordered successor regression consumes one handoff and creates one Planner without owner commands; all invalid or active predecessors fail closed; registered CPF scenarios remain green.


## Independent acceptance

Technical review p20-bootstrap-successor-technical-review-20261004-r1 returned
ACCEPT through claude/default/opus (provider anthropic), independent from
author provider codex. No BLOCKING finding; six NON_BLOCKING notes preserved
in docs/evidence/P20_BOOTSTRAP_TECHNICAL_REVIEW.json. Root verification:
182 tests and 12 subtests passed in 101.24 seconds; compileall/diff check pass;
Graphify updated. This accepts the narrow bootstrap repair only, not P20.1
implementation or any soak. Normal existing handoff may now resume.
