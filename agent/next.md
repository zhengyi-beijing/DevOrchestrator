# P17 - Single-Authority Goal Convergence Baseline

Status: **COMPLETE**

Predecessor: P16.14

## Architecture freeze decision

P17 MUST NOT introduce an independent fourth lifecycle/controller authority.

The target architecture is a single durable Work/Goal authority evolved from the existing lifecycle authority. P17 builds a non-authoritative, pure Convergence Evaluator plus replay/shadow harness that models this target. Existing production lifecycle code remains the sole production writer during P17.

The target control loop is:

Goal -> Observe Evidence -> Decide -> Act Once -> Verify -> Satisfy / Recover / Wait / Ask Human / Handoff

Detailed planner, reviewer, provider, watchdog and process states are evidence and telemetry unless explicitly listed as fields of the canonical Work Record. They MUST NOT independently determine lifecycle advancement.

Markdown status files, including agent/next.md, agent/CURRENT.md and agent/HANDOFF.md, are specifications/projections for humans and agents. Editing a markdown status MUST NOT by itself settle a goal, publish a successor, discharge a human decision, reset a retry budget, or create lifecycle authority.

## Objective

Reconcile the DevOrchestrator backlog against implementation after P1-P16.14 and validate a simpler single-authority convergence architecture against the real P12-P16.14 incident corpus.

P17 must prove that autonomous progress can be expressed with:
1. one canonical Work Record,
2. one pure decision function,
3. one idempotent actuator boundary,
4. typed verification evidence,
5. typed failure/problem identity with bounded attempts,
6. explicit human-decision semantics,
7. deterministic successor/handoff semantics.

P17 is a prototype, replay and shadow-validation task. It does not replace the production controller.

## Canonical Work Record target

The target production authority is one single-writer durable record per project/current goal. Its eventual production migration MUST evolve the existing durable lifecycle authority in place (currently the transition-executor authority) rather than introduce a parallel work-authority file. P17 defines and tests the schema but does not make the prototype a second production writer.

Required fields:

- schema_version
- project_id
- goal_id
- goal_revision
- goal_spec_digest
- predecessor_goal_id
- repository_identity:
  - repo_path identity
  - branch
- status: OPEN | NEEDS_HUMAN | DONE
- active_lease, nullable:
  - role
  - attempt_id
  - execution_id
  - effect_id / broker_request_id when an external effect exists
  - host
  - pid when local process-backed
  - epoch
  - heartbeat_at
  - acquired_at
- current_problem, nullable:
  - problem_id
  - failure_class
  - criterion_or_invariant_id
  - normalized_fingerprint
  - first_seen_at
- attempts:
  - attempt_id
  - problem_id
  - strategy_id
  - requested_capability_tier
  - resource identity when known
  - input anchor
  - output anchor
  - typed outcome
  - evidence references
- acceptance:
  - exactly one of:
    - NONE
    - VERIFIED:
      - reviewer_verdict_id
      - anchor_head
      - independence
      - verification_artifact_ids
    - OWNER_OVERRIDE:
      - owner_id
      - command_id
      - scope
      - reason
      - waived_obligations
- wait, nullable:
  - not_before
  - reset_source
  - reason_code
  - wakeup_obligation_id
- verification, nullable:
  - verification_id
  - exact repository anchor
  - acceptance-criterion results
  - test evidence
  - reviewer evidence
  - unresolved blocking findings
- successor, nullable:
  - successor_goal_id
  - successor_spec_digest
  - publication state
  - handoff idempotency key
- human_request, nullable:
  - question_id
  - question_revision
  - request type
  - concrete question/authorization required
  - options, each naming the exact effect of answering
  - authorization_scope
  - resume_checkpoint
  - evidence
  - answer, nullable:
    - option_id
    - owner_id
    - command_id
    - at
- handoff, nullable:
  - complete resumable checkpoint
  - current blocker/problem
  - attempts and strategies tried
  - verification evidence
  - ruled-out approaches
  - recommended human action
- authority_revision / CAS token
- created_at / updated_at

HEAD, lifecycle phase, provider/account identity and timestamps are evidence/anchors. They MUST NOT be part of the stable problem identity merely because normal work changed them.

The emergency pause/stop interlock is intentionally orthogonal safety authority, not an alternate lifecycle truth.

## Single writer and component disposition

Target disposition:

- Existing lifecycle authority / transition executor:
  - current production authority during P17;
  - future migration target for Work Record semantics;
  - remains single writer until an explicit later migration gate switches authority.
- Convergence Evaluator v0:
  - PURE, NON-WRITING, NON-AUTHORITATIVE in P17;
  - input is a Work Record snapshot plus typed evidence;
  - output is one typed proposed decision.
- Planner / plan-review / technical-review ledgers:
  - evidence and role telemetry;
  - never independent lifecycle authority.
- Transition/execution ledgers:
  - execution/audit evidence and idempotency evidence;
  - must not contradict the Work Record once migration occurs.
- Control command history:
  - append-only audit of owner/system requests and settlement;
  - not lifecycle truth.
- Owner control:
  - emergency pause/stop and typed human authorization only.
- Watchdog / supervisor / incident obligations:
  - observers and recovery-signal producers;
  - may propose a decision but cannot independently advance authority.
- execution_intent budgets:
  - existing behavior is evidence only in P17;
  - target attempt/problem budgets live in the Work Record.
- failure memory:
  - environment/capability constraint evidence;
  - target preflight rules are deterministic and consumed before execution.
- staged roadmap and agent markdown:
  - specification/projection artifacts only;
  - never sufficient evidence for settlement or advancement.
- Meta/System Architecture Reviewer:
  - stateless analytical report;
  - no lifecycle role, no gate, no code mutation authority.

P17 MUST NOT add another runtime state file that acts as a fourth source of truth.

## Pure decide() contract

P17 defines a deterministic, side-effect-free function equivalent to:

decide(work_record, evidence_snapshot, policy) -> Decision

Decision is exactly one typed action such as:

- NOOP_ACTIVE
- WAIT_UNTIL
- EXECUTE
- VERIFY
- RETRY_SAME_STRATEGY
- RETRY_NEW_STRATEGY
- FAILOVER_RESOURCE
- ESCALATE_CAPABILITY
- REQUEST_HUMAN
- WRITE_HANDOFF
- SATISFY_GOAL
- PUBLISH_SUCCESSOR

The evaluator never writes files, starts processes, changes git, updates ledgers, or mutates the Work Record.

The actuator accepts a Decision only after revalidating:
- authority_revision,
- project/goal identity,
- exact repository anchor where required,
- active lease uniqueness,
- emergency pause/stop,
- required human authorization,
- idempotency key,
- safety fences.

Each accepted Decision creates at most one idempotent effect and one authority transition.

## Lease liveness and external-effect identity

NO_ORPHAN_OWNER: a lease is active progress only while its bound execution is demonstrably live.

For process-backed work, active_lease carries host, pid, epoch and heartbeat_at. On daemon startup/reconcile, a non-live lease is deterministically converted to exactly one typed outcome; it is never left as active progress.

Autonomous retry/replay after restart is permitted only when the external effect has reconcilable identity and state, such as broker_request_id/effect_id plus a liveness/status probe and conclusive cancel/terminal result. If effect state is ambiguous, fail closed rather than duplicate the effect.

P17 defines and tests this execution/effect reconciliation port in the shadow model. Transport-level implementation that cannot be safely added without broadening P17 is deferred to P18.

## Core convergence invariants

1. SINGLE_AUTHORITY: exactly one canonical current Work Record per project.
2. SINGLE_ACTIVE_LEASE: at most one active execution owner for one goal.
3. PROGRESS_TOTALITY:
   GOAL_NOT_SATISFIED + NO_ACTIVE_PROGRESS + NO_TRUE_HUMAN_DECISION_REQUIRED
   MUST produce a deterministic recovery decision or a typed control-plane fault.
4. ACCEPTANCE_BEFORE_ADVANCE: no successor can publish unless acceptance is VERIFIED or an explicit OWNER_OVERRIDE is durably recorded. acceptance=NONE can never advance.
5. OWNER_OVERRIDE_EXPLICIT: an owner override never fabricates verification evidence; it records exactly which obligations are waived and the authorization scope.
6. HUMAN_REQUEST_DISCHARGEABLE: every open human_request has at least one currently acceptable answer option; answering CASes only question_id + question_revision, never HEAD, lifecycle revision, projected gate ID, role, or conversation binding.
7. MARKDOWN_NON_AUTHORITY: markdown edits alone cannot change lifecycle authority.
8. ANCHOR_BINDING: verification/reviewer evidence is bound to exact project/task/branch/HEAD/worktree/test anchors.
9. IDEMPOTENT_REPLAY: replay of the same event/decision cannot duplicate execution or successor publication.
10. STABLE_PROBLEM_IDENTITY: normal HEAD/lifecycle/provider changes do not reset an unresolved problem budget.
11. BOUNDED_PROBLEM: one normalized problem cannot retry forever.
12. COMPLETE_EXHAUSTION: every exhausted problem writes a resumable structured HANDOFF.
13. HUMAN_TYPED: human intervention requires a typed reason and a concrete question/authorization.
14. EMERGENCY_BRAKE: authenticated pause/stop cannot be refused merely because lifecycle/HEAD/CAS changed.
15. FAIL_CLOSED_AMBIGUITY: conflicting identity/ownership/unsafe evidence blocks side effects.
16. LEARNED_CONSTRAINT_CONSUMPTION: a known incompatible operation is rejected at preflight before execution.
17. LEARNING_REGRESSION: recurrence of the same normalized failure after an applicable learned rule is loaded is a control-plane defect.
18. SUCCESSOR_DETERMINISM: successor identity and handoff idempotency key are deterministic from accepted source-goal evidence.
19. NO_HUMAN_CLOCK: manual continue is never required solely to make an otherwise safe deterministic transition happen.
20. WAIT_IS_NOT_PROGRESS: WAIT_UNTIL is an explicit bounded deferral with not_before/reset_source and a durable wakeup obligation; it is neither active progress nor exhaustion and consumes no attempt budget.
21. INVARIANT_CODE_STABILITY: invariant code names are stable identifiers; ordinal numbering is explanatory only and MUST NOT be persisted or referenced as identity.

## Typed verification and GoalSatisfied

Test counts, reviewer prose, markdown COMPLETE tokens and claimed commands are not acceptance evidence by themselves.

Verification evidence must be structured and include:
- verification_id
- project_id / goal_id
- branch
- exact HEAD
- clean/status fingerprint
- acceptance criterion IDs
- executed test/build/static-check commands or harness IDs
- exit status
- log/artifact digest
- reviewer request/resource identity when reviewer evidence is required
- structured reviewer decision
- structured finding severity and finding IDs
- verification timestamp

Reviewer findings MUST use typed severity, for example BLOCKING | NON_BLOCKING | INFO. Control flow MUST NOT infer severity by searching prose for strings such as "BLOCKING:".

A repository/markdown COMPLETE while acceptance=NONE is a fail-closed contradiction and is never an advance trigger.

An owner may close/advance without otherwise obtainable verification only through a durable typed OWNER_OVERRIDE command that identifies owner, scope, reason and every waived obligation. The override is acceptance provenance, not fake test/reviewer evidence.

waived_obligations may waive explicitly named evidence/acceptance obligations only. It can never waive: no unresolved BLOCKING finding, no active execution lease, no unresolved integrity/identity ambiguity, emergency-brake enforcement, or explicit safety/irreversible-action authorization.

GoalSatisfied is true only when:
- acceptance is VERIFIED, or an explicitly policy-permitted OWNER_OVERRIDE exists for the exact goal/scope;
- every non-waived required acceptance criterion has typed passing evidence;
- required tests/checks actually executed and passed;
- required independent review has an ACCEPT decision at the same accepted anchor;
- no unresolved BLOCKING finding exists;
- repository/worktree identity matches the accepted result;
- no active execution lease exists;
- no unresolved integrity ambiguity exists.

If a reviewer could not run required tests, the verification record says NOT_VERIFIED; prose test counts from another role do not substitute for evidence.

## Problem identity and failure classifier

A problem_id remains stable until the specific problem is verified resolved.

The normalized problem fingerprint is derived from:
- goal_id
- criterion/invariant or operation class
- typed failure_class
- normalized semantic error family
- relevant environment/tool capability selector
- stable semantic scope

It MUST exclude volatile values such as:
- HEAD changes produced while fixing the same problem
- lifecycle state names
- daemon restart / execution epoch changes
- PID
- timestamps
- retry/request IDs
- provider account unless the root cause is provider-specific

Typed failure classes must include at least:
- OUTPUT_INVALID
- RESOURCE_TRANSIENT
- PROVIDER_UNAVAILABLE
- AUTH_OR_PERMISSION
- ENVIRONMENT_CONSTRAINT
- IMPLEMENTATION_DEFECT
- REASONING_OR_STRATEGY_DEFECT
- VERIFICATION_FAILURE
- INTEGRITY_OR_IDENTITY_AMBIGUITY
- SAFETY_OR_IRREVERSIBLE_AUTHORIZATION
- CONTROL_PLANE_DEFECT

## Structured role output contract

No control decision may parse free-text prose to determine severity, scope, target, blocker identity, retryability or next action.

Structured findings/results must use a closed schema including:
- finding_id
- severity
- failure_class
- target:
  - file
  - symbol, nullable
  - test_id, nullable
  - invariant_code, nullable
  - blocker_code, nullable
- scope_claim
- machine-readable reason/code
- human-readable explanation as non-authoritative text

Malformed or partially invalid structured output is OUTPUT_INVALID. A rejected finding set and an absent finding set are different typed states. OUTPUT_INVALID receives bounded validator-feedback repair at the same requested capability tier before any reasoning-remediation budget is consumed.

Multiple simultaneous BLOCKING findings do not require a special len(blocking)==1 path. Each distinct normalized criterion/invariant + failure family forms its own problem_id unless the closed-schema target/fingerprint proves they are the same problem. The decider selects the next unresolved problem by deterministic stable ordering; each problem keeps its own budget.

## Retry, failover, escalation and exhaustion

Budgets are per problem_id, not per whole task and not reset by ordinary HEAD/lifecycle evolution.

Maintain separate bounded counters for:
- infrastructure/resource attempts
- strategy/implementation attempts
- capability escalations
- total attempts

Policy:
- resource/quota failure with a policy-known reset:
  emit WAIT_UNTIL with typed not_before/reset_source and a durable wakeup obligation; waiting consumes no attempt budget and is re-evaluated automatically at/after not_before.
- transient/resource/provider failure without a known wait boundary:
  retry or fail over at the same requested capability tier first;
  do not consume reasoning-remediation budget merely because one account/provider failed.
- OUTPUT_INVALID:
  run bounded schema/validator-feedback repair at the same capability tier; do not silently coerce malformed findings to an empty/absent set and do not charge the first formatting repair against reasoning-remediation budget.
- implementation/test failure:
  diagnose and try a changed bounded strategy against the same problem_id.
- repeated reasoning/strategy failure:
  escalate requested capability tier after evidence shows the prior strategy/tier did not resolve the problem.
- ambiguity/integrity failure:
  fail closed; recover automatically only when evidence is deterministic and unambiguous.
- auth/credential/OS permission with no authorized alternative:
  request human action with exact missing requirement.
- safety/irreversible/cost-boundary action:
  require explicit human authorization.
- all budgets exhausted:
  write a complete resumable HANDOFF; if no safe automated strategy remains, set status=NEEDS_HUMAN with typed reason ATTEMPT_BUDGET_EXHAUSTED and stop automatic attempts for that problem.

Retry-budget exhaustion is not itself a reason to ask a human to "continue". It means the current strategy budget is exhausted; the controller must choose a typed escalation/failover/HANDOFF outcome.

## Human-decision boundary

Human input is required only when no safe deterministic automated action exists, including:
- ambiguous requirements or value/product choices;
- irreversible or high-risk operations;
- explicit real-hardware authorization boundaries;
- missing credentials/permissions only the user can supply;
- explicit cost/resource authorization above configured limits;
- irreconcilable identity/ownership evidence where choosing automatically could corrupt work;
- explicit policy/safety decisions.

Test failure, reviewer blocker, provider quota, process death, lost handoff, transient git/read error, remediation budget exhaustion, or no-progress state are not human decisions when a deterministic safe recovery exists.

## Emergency pause / stop

Pause/stop is an out-of-band safety interlock.

Authenticated pause/stop:
- does not require the current lifecycle revision, task HEAD, reviewer gate ID or active role to match;
- atomically prevents new actuator side effects;
- is checked immediately before every side effect;
- may request cancellation of active cancellable AI/process work;
- is append-only audited with command ID and caller identity.

Resume is also explicit and authenticated but MUST NOT rely on a stale lifecycle identity merely to clear the brake.

Ordinary lifecycle commands continue to use strict identity/CAS checks. Emergency brake semantics are intentionally stronger.

## Human request answer semantics

A human request is subordinate to the single Work Record, not a second gate authority.

- question_id is stable across HEAD changes, provider changes, projected lifecycle labels and conversation rebinding.
- question_revision changes only when the actual question/options/authorization scope changes.
- each option names the exact actuator effect authorized by that answer.
- an answer is accepted by CAS on question_id + question_revision only.
- current project HEAD, lifecycle revision, projected gate ID, active role and conversation binding are not answer identity.
- answering durably records owner_id, option_id, command_id and time, then resumes from resume_checkpoint.
- one canonical finder resolves the current open question; projections may not mint competing gate IDs.

Acceptance replay: open a question, advance through N harmless HEAD/projection changes, answer the original unchanged question, and prove it discharges exactly once without requiring manual continue.

## Deterministic successor and HANDOFF semantics

Successor publication is derived from accepted authority, not markdown.

After GoalSatisfied:
1. validate the configured successor specification and its digest;
2. create one deterministic handoff idempotency key from project, source goal, successor goal and acceptance digest;
3. in one single-writer transaction:
   - terminalize the source goal as SATISFIED,
   - persist acceptance evidence digest,
   - persist successor intent/handoff,
   - clear source execution ownership;
4. planner/next-goal activation consumes the same handoff idempotently.

A missing consumer or restart replays the same handoff. It does not manufacture a new transition.

If there is no successor, the goal remains terminal SATISFIED.

A document status change without accepted verification cannot invoke this path.

## Complete resumable HANDOFF

Every bounded-attempt exhaustion or NEEDS_HUMAN stop must persist, in structured form:
- goal/spec identity
- exact repository anchor
- acceptance criteria
- stable current problem_id/fingerprint
- failure class
- every attempt with strategy/resource/result
- test/reviewer evidence
- unresolved blocker
- ruled-out approaches
- learned constraints
- recommended next human decision/action
- safe resume checkpoint and idempotency identity

agent/HANDOFF.md may render this information for humans but is not the authority.

## Stable runtime vs development workspace

P17 first inventories actual path/runtime/config ownership before any deployment change.

Target separation:
- controller_root: stable controller code, intended role currently C:\work\github\DevOrchestrator
- workspace_root: development target, currently C:\work\github\DevOrchestrator-dev
- state_root: one explicit absolute runtime/checkpoint/ledger root, NOT derived from controller code location
- config_root: one explicit absolute configuration root, NOT silently selected from whichever repo executes
- logs/artifacts: children of explicit state_root or separately explicit absolute roots

P17 MUST NOT activate stable/dev split while REPO_ROOT-derived runtime/config behavior can fork state.

Promotion target:
1. accepted dev commit + typed verification evidence;
2. clean exact dev SHA;
3. explicit promotion transaction to stable controller revision;
4. same canonical external state/config roots;
5. controlled daemon restart;
6. health/replay check;
7. rollback to prior stable SHA on failure without changing durable state identity.

Only one daemon may own the canonical state root. Runtime-root resolution without an explicit configured/canonical root must fail closed rather than silently create a fresh state tree. The state-root ownership lock records host, pid and controller code identity.

P17 designs and tests this with isolated fixtures/simulation; production relocation/promotion is a later gated migration.

The migration inventory must explicitly identify retirement/consolidation targets for: execution_intent budgets, reviewer remediation/extension budgets, watchdog lifecycle_recovery_attempts, and label-keyed resolve_progress_obligation escalation tables. P17 does not delete them; M9 cannot claim simplification unless these duplicated decision/budget paths have an evidence-backed retirement disposition.

## Learned environment constraints

Define a deterministic CapabilityConstraint record:
- rule_id
- normalized failure fingerprint
- environment selector:
  - OS
  - shell family/version
  - tool/version or capability
- operation signature
- prohibition or deterministic rewrite/precondition
- evidence
- created_at
- recurrence_count
- last_verified_at

Flow:
failure -> normalize fingerprint -> classify environment capability -> create/update durable rule -> load rule in preflight -> reject/rewrite incompatible operation before execution.

Representative ZXZ-PC rules to seed as replay cases include:
- Windows PowerShell 5.1 does not support PowerShell 7-only operators/features used by prior commands;
- encoding mode such as Set-Content -Encoding utf8NoBOM is unsupported on this host;
- script execution policy may block npm .ps1 launchers; approved .cmd or process-scope bypass path must be selected deterministically;
- Codex CLI flags must be capability-discovered from the installed version before use;
- shell stdin/redirection syntax must match PowerShell rather than Bash semantics;
- .NET file APIs must receive absolute paths when PowerShell Set-Location does not update the process working directory used by that API.

If an applicable rule is loaded and the same incompatible operation is nevertheless executed, emit LEARNING_REGRESSION / CONTROL_PLANE_DEFECT rather than learning the same fact again.

## Historical replay corpus

P17 must build a deduplicated incident -> reproduction -> expected decision mapping.

Minimum replay classes:
1. P16.14 remediation-budget / owner-continue deadlock.
2. P16.13/P16.14 lost or missing successor handoff.
3. markdown COMPLETE/status edit without accepted verification.
4. stale reviewer anchor after HEAD changes.
5. replayed/duplicate command or event.
6. Worker/process death with stale active ownership.
7. provider quota/timeout/OAuth/resource failure with alternatives available.
8. owner-gate identity mismatch/discharge failure.
9. emergency pause requested after HEAD/task revision changes.
10. multi-owner / contradictory authority ambiguity.
11. transient git/read failure versus genuinely missing declaration/evidence.
12. known PowerShell/tool incompatibility after the rule has already been learned.
13. dirty worktree at a transition/verification boundary.
14. reviewer unable to run tests while another role claims passing counts.
15. runtime/config split caused by code-location-derived roots.
16. malformed/untyped reviewer finding or severity prose.
17. exhausted problem requiring complete HANDOFF rather than diagnosis_unknown/continue.
18. normal happy-path goal completion and deterministic successor publication.
19. malformed/partial role output whose findings must become OUTPUT_INVALID, never empty evidence.
20. multiple simultaneous BLOCKING findings under the same stable problem family.
21. restart during a local or broker effect: live, conclusively dead, conclusively cancelled, and ambiguous external-effect states.
22. stable problem budget across fix commits, provider failover and daemon restart.
23. timed quota reset producing WAIT_UNTIL rather than churn when policy knows the reset.
24. human question opened before harmless HEAD/projection changes and discharged exactly once afterward.
25. shared-credential mutual exclusion where one authenticated session/refresh lease must serialize access rather than create retry churn.
26. crash injection between every pair of durable writes in successor/acceptance/handoff paths, with deterministic replay and trace-hash equality.

Each replay records:
- legacy result;
- v0 Decision;
- expected invariant verdict;
- human intervention count;
- attempt count;
- duplicate-execution count.

## Meta/System Architecture Reviewer

The Meta Reviewer is not a lifecycle role and owns no durable workflow state.

It consumes bounded read-only telemetry and produces a report/backlog proposal.

Trigger candidates:
- same normalized failure family recurring after remediation;
- same subsystem repeatedly receiving local recovery patches;
- manual continue repeatedly acting as a progress clock;
- authority/projection divergence;
- sustained growth in lifecycle states/invariants/special cases;
- learned constraint recurrence after preflight rule exists.

It cannot:
- mutate production code;
- open/close a goal;
- approve verification;
- clear a human request;
- publish a successor;
- change model/provider selection directly.

## P17 prototype scope

P17 SHALL implement/produce only:
1. evidence-backed backlog/capability reconciliation;
2. normalized P12-P16.14 incident corpus;
3. Work Record v0 schema/model for simulation;
4. pure ConvergenceEvaluator.decide();
5. typed failure/problem fingerprint model;
6. typed verification/acceptance model;
7. deterministic environment-constraint preflight model;
8. replay harness over historical incidents;
9. shadow adapter that can read existing legacy evidence and emit proposed v0 decisions without writing production state;
10. legacy-vs-v0 comparison metrics;
11. explicit runtime/config-root inventory and target stable/dev deployment design;
12. migration plan and gates.

The prototype may write only test fixtures, reports and explicitly non-authoritative shadow/replay artifacts inside the development repository. Shadow artifacts must be fully rebuildable from authoritative evidence and MUST NOT be read by any actuator. It never becomes a second runtime authority.

## P17 explicit non-goals

P17 MUST NOT:
- replace or bypass the current production controller;
- add an authoritative fourth state store/controller;
- migrate production runtime/config roots;
- switch the stable daemon to a new writer;
- delete legacy safety tests merely because target behavior changes;
- silently weaken fail-closed identity/ownership/safety checks;
- make Meta Reviewer findings self-executing;
- auto-start P18;
- activate real X-ray sources, conveyors or other explicitly human-authorized hardware.

Legacy tests that encode obsolete orchestration policy must be classified as:
- enduring safety invariant, or
- legacy behavior to preserve only until migration.

P17 does not require the v0 shadow model to reproduce obsolete owner-gate/implicit-retry policy. It must instead prove equivalent or stronger underlying safety invariants.

## Migration gates

M0 - Freeze evidence:
- current legacy behavior, incidents, contracts and replay corpus captured.

M1 - Pure model:
- Work Record schema and decide() deterministic;
- unit/property tests for invariants and idempotence pass.

M2 - Historical replay:
- required incident matrix passes with expected v0 decisions;
- zero duplicate execution in simulation;
- no markdown-only advancement.

M3 - Shadow live evaluation:
- v0 reads production evidence and emits decisions only;
- no production writes;
- divergence from legacy is classified and reviewed.

M4 - Architecture acceptance:
- safety invariants equivalent/stronger;
- human-clock interventions materially reduced in replay/shadow;
- no hidden source of truth;
- environment-preflight recurrence test passes;
- stable/dev root design validated in isolated fixtures.

P17 STOPS HERE for production authority.

Shadow output uses one named non-authoritative namespace, runtime/p17-shadow/, and every shadow record includes source-evidence digests. A differential test proves deleting/rebuilding the namespace yields the same decision trace hash from the same evidence. No actuator may read this namespace.

Future gated migration, not automatically started by P17:

M5 - external canonical state/config root migration rehearsal.
M6 - single-project canary with legacy still rollback authority.
M7 - dual-run comparison with new authority writer disabled by default.
M8 - explicit owner-approved authority switch with rollback.
M9 - retire redundant legacy decision paths only after sustained acceptance.

## Backlog reconciliation scope

Classify retained capabilities exactly one of:
IMPLEMENTED | PARTIAL | NOT_IMPLEMENTED | OBSOLETE | MERGED.

Include:
- provider/model/account routing;
- quota/retry/failover;
- lifecycle/handoff;
- watchdog/activity recovery;
- invariant/fault injection;
- post-Worker acceptance continuation;
- persistent context;
- API/web management;
- multi-project;
- onboarding;
- DAG/parallel;
- multi-node;
- mobile;
- operating/resource modes;
- incident-to-regression learning.

## Required outputs

1. Canonical evidence-backed backlog/capability matrix.
2. Historical failure corpus with incident -> reproduction -> expected v0 decision -> existing/missing regression.
3. Work Record v0 schema and pure decision contract.
4. Executable replay/shadow harness.
5. Legacy-vs-v0 convergence/safety/human-intervention comparison.
6. Environment constraint/preflight model and recurrence regression tests.
7. Stable/dev runtime-root inventory and migration design.
8. Legacy contract-amendment matrix classifying every conflicting assertion in TRANSITION_EXECUTOR_CONTRACT, development-workflow and P16.13/P16.14 tests as SAFETY (must remain) or POLICY (may change only at a later migration gate), with named covering tests.
9. Residual risks/debt and future migration gates.

## Acceptance criteria

P17 is accepted only if:
- no fourth runtime authority is introduced;
- one Work Record model can represent every replayed case without contradictory lifecycle truth;
- all required historical replay classes pass;
- markdown status edits cannot satisfy or advance a goal;
- typed verification is required for GoalSatisfied;
- structured finding severity replaces prose parsing in the v0 model;
- one unresolved problem keeps one stable budget across HEAD/lifecycle/provider changes;
- safe provider/resource failures fail over without consuming reasoning budget;
- capability escalation is evidence-driven and bounded;
- all exhaustion paths yield complete resumable HANDOFF;
- emergency pause/stop semantics are independent of lifecycle revision;
- learned constraints are consumed preflight and recurrence is classified as a control-plane defect;
- shadow mode performs no production mutation;
- legacy safety fences are preserved or represented by equivalent/stronger explicit invariants;
- P17 migration does not pass beyond M4;
- policy-known timed waits produce WAIT_UNTIL/not_before, consume no attempt budget, and wake without manual continue;
- invariant code names, not ordinals, are the stable identities;
- human questions remain dischargeable across harmless HEAD/projection changes;
- malformed role output is OUTPUT_INVALID and cannot silently become empty evidence;
- restart reconciliation cannot leave a dead lease as active progress and cannot duplicate an ambiguous external effect;
- every legacy no-implicit-retry/gating assertion is explicitly classified SAFETY or POLICY with named coverage;
- shadow replay is rebuildable and yields identical decision trace hashes after crash/restart fixtures.

## Control-Plane Impact
- Invariants: CURRENT_TASK_MATCHES_ACTIVE_EXECUTION, TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION, PENDING_DESIGN_NOT_EXECUTING, SUCCESSOR_HANDOFF_LINEAGE_VALID, NEXT_TASK_WITHOUT_HANDOFF, SINGLE_ACTIVE_LIFECYCLE_OWNER
- Transition boundaries: plan_freeze, worker_launch, special_gate_reconciliation, owner_gate_transition, successor_handoff, handoff_publication, authority_reconciliation, watchdog_recovery
- Fault scenarios: CPF-01, CPF-02, CPF-03, CPF-04, CPF-05, CPF-06, CPF-07, CPF-08, CPF-09, CPF-10
- Convergence evidence: Unambiguous convergence to a single authoritative owner without manual continue, zero duplicate execution or successor publication, deterministic bounded recovery, and fail-closed behavior on contradictory authority or unsafe ambiguity.

## Current accidental P17 activation handling

The local commit e3eeb29 ("lifecycle(P16.14): activate staged successor P17") is preserved as incident evidence; do not erase history merely to make status look cleaner.

The P16.14 -> P17 legacy handoff has acceptance=null/NONE and is therefore recorded in the P17 replay corpus as a known historical ACCEPTANCE_BEFORE_ADVANCE violation, not as evidence that the target architecture permits markdown-only closure. P17 proceeds as an explicitly owner-authorized migration/bootstrap exception; that authorization must be recorded as an OWNER_OVERRIDE fixture/evidence item before the edge is used as a positive target-semantics example.

Before implementation:
1. keep owner pause asserted;
2. verify no live Planner/Worker/Reviewer process owns P17;
3. reconcile the stale P17 planner record through an existing authoritative recovery/cancel path, never by editing runtime JSON;
4. update both agent/next.md and agent/staged/P17.md to the same frozen P17 specification because P17 has already been activated and they are currently byte-identical;
5. commit and push the architecture-freeze revision, preserving e3eeb29 as its parent;
6. restart/reconcile the daemon while still paused and verify one coherent P17 authority;
7. only then use the normal authenticated resume path to allow P17 planning/implementation.

No P17 Worker may start before these checks pass.

## Successor policy

P17 does not auto-invent or auto-start P18. Any successor requires explicit staged specification plus accepted P17 completion evidence through the then-authoritative lifecycle path.

## Approved executable design

Deliver P17 in two ordered phases. G0 is a fail-closed pre-launch gate completed while the authoritative owner pause remains asserted: commit the canonical Control-Plane Impact declaration, verify liveness and bootstrap evidence, reconcile stale records only through existing authoritative commands, restart and reconcile the daemon, commit the complete pre-resume evidence artifact, then revalidate declaration and authority at that new exact HEAD. Authenticated resume is the final gate action; its expected/observed identity and accepted settlement in authoritative control-command history are the durable post-commit evidence, and no repository artifact is written after resume. Phase W then implements the additive, pure, non-authoritative convergence model, typed evidence and policy, replay corpus, read-only shadow adapter, documentation, and tests without changing production writers or progressing beyond M4.

### Implementation steps
- G0.1 PRE-LAUNCH GATE, before any Worker launch or src/ change. Keep the owner pause asserted. Add one canonical byte-identical '## Control-Plane Impact' section to agent/next.md and agent/staged/P17.md declaring the six lifecycle invariants, eight transition boundaries, CPF-01 through CPF-10, and convergence evidence. Commit and push the architecture-freeze revision while preserving e3eeb29 in ancestry. Abort if the files differ or the committed declaration fails the existing contract validator.
- G0.2 PRE-LAUNCH GATE. While paused, verify the authoritative owner store reports paused; no live Planner, Worker, or Reviewer owns P17 according to incidents/liveness.py and the planner, reviewer, and execution ledgers; both task files still contain the same frozen specification; and agent/evidence/P17_BOOTSTRAP_OWNER_OVERRIDE.json records the bootstrap OWNER_OVERRIDE. Collect observed identities, revisions, digests, and verdicts for the pre-resume evidence artifact. Abort without clearing state if any check fails.
- G0.3 PRE-LAUNCH GATE. Reconcile any stale P17 planner or execution record only through an existing production recovery, cancel, or reconcile command. Never edit runtime JSON, delete a ledger, or bypass CAS or the emergency interlock. Capture the command ID, authoritative path, before/after revisions, and typed outcome. If no deterministic authoritative path exists, leave the pause asserted, record FAIL_CLOSED_AMBIGUITY, and escalate instead of assigning the ambiguity to the Worker.
- G0.4 PRE-LAUNCH GATE. With the pause still asserted, restart and reconcile the daemon through the normal supervised path. Verify one coherent P17 authority: current_task_id=P17, at most one lifecycle owner, no orphaned lease or contradictory successor, and all six authoritative lifecycle invariants holding. Capture the authority generation, snapshot digest, invariant verdicts, and restart/reconciliation command identities. Abort and retain the pause on any failure.
- G0.5 PRE-LAUNCH GATE, repository-finalization step. While still paused, write agent/evidence/P17_PRELAUNCH_GATE.json containing immutable results for G0.1-G0.4, declaration and bootstrap digests, command IDs, authority revisions, restart evidence, abort conditions, and pre_resume_status=PASS. Commit and push this M0 artifact. Do not encode its own commit SHA inside the file. At the resulting clean M0 HEAD, rerun the declaration gate, liveness check, pause check, and centralized authority invariants; require the authority's archived declaration-gate evidence to bind to that exact HEAD. Make no further repository change after this validation.
- G0.6 PRE-LAUNCH GATE, final action. From the final M0 snapshot, submit exactly one normal authenticated resume command whose expected identity includes the exact M0 HEAD and current authoritative revision, task, lifecycle, gate, and paused state. Do not write or commit any post-resume repository evidence. The authoritative control inbox/history/audit and owner-control state are the durable evidence: require the command to settle accepted with matching observed identity and effect resume_future_launches or the narrowly proven resume_exact_remediation. A blocked or mismatched command does not authorize Worker work. Once accepted, the daemon may launch the Worker; subsequent proof uses the settled command ID and archived exact-HEAD gate evidence.
- Create src/dev_orchestrator/convergence/ as a pure, non-authoritative package with an explicit public surface. Document that it never writes production state and is imported by no production module. Only the shadow adapter may use read-only lifecycle-authority parsers and repository readers; convergence modules must not import daemon, transition-executor write paths, or control-command mutators.
- Implement Work Record v0 with the frozen field groups, OPEN|NEEDS_HUMAN|DONE status, active lease, current problem, attempts, acceptance union, wait, verification, successor, human request, handoff, authority_revision, and timestamps. Provide typed validation, canonical sorted-key JSON, stable digesting, and a model-only CAS helper. Keep HEAD, lifecycle phase, provider identity, and timestamps out of stable problem identity.
- Implement immutable typed EvidenceSnapshot and Policy inputs. Evidence must model read outcome, freshness, exact anchors, digests, typed conflicts, source precedence, shared-credential leases, and missing/ambiguous required sources. Policy must carry independent problem budgets, capability tiers, quota-reset rules, wait bounds, OUTPUT_INVALID repair bounds, deterministic ordering, human-authorization boundaries, required sources, and a stable digest. decide() must read neither configuration nor environment.
- Implement typed failure/problem identity, closed-schema findings, verification, and acceptance. Fingerprints use stable semantic components and reject HEAD, lifecycle, epoch, PID, timestamp, and retry identifiers following incidents/fingerprint.py's volatile-key discipline. Malformed role output becomes OUTPUT_INVALID, not absent evidence. GoalSatisfied and OWNER_OVERRIDE validation must preserve all non-waivable safety conditions.
- Implement the stable twenty-one-code convergence invariant registry and mappings to authoritative lifecycle invariants and CPF-01 through CPF-10. Implement a total, deterministic, side-effect-free decide(work_record, evidence, policy, now) returning exactly one of the twelve frozen DecisionKind values with reason, problem identity, parameters, idempotency key, invariant citations, and evidence digests. PROGRESS_TOTALITY must produce recovery or a typed control-plane fault rather than silence.
- Encode deterministic multi-problem selection and bounded retry, wait, failover, escalation, and exhaustion behavior. Known quota resets produce budget-free WAIT_UNTIL with a durable wakeup obligation; transient resource failures remain at the same tier before reasoning escalation; OUTPUT_INVALID receives bounded validator feedback; ambiguity fails closed; true credential, safety, cost, or product choices request human action; exhaustion writes a complete HANDOFF and transitions the model to NEEDS_HUMAN.
- Implement human-request and successor model helpers. Question identity remains stable across harmless HEAD/provider/projection changes, answers CAS only question_id plus question_revision, and every request exposes an acceptable option with an exact authorized effect. Successor identity and handoff keys derive deterministically from accepted evidence; successor publication is represented only as a proposed single-writer transaction shape and never executed by P17.
- Implement deterministic capability preflight from read-only failure-memory projections, including provenance seed:p11b:rdc-powershell-5.1 and the ZXZ-PC PowerShell, encoding, execution-policy, CLI-capability, redirection, and absolute-path rules. An applicable learned rule must ALLOW, REWRITE, or REJECT before execution; recurrence becomes LEARNING_REGRESSION or CONTROL_PLANE_DEFECT.
- Implement an in-memory-only execution-effect reconciliation port and non-writing actuator guard. Reconcile LIVE, TERMINAL_SUCCESS, TERMINAL_FAILURE, CANCELLED, and AMBIGUOUS states without orphaning ownership or duplicating ambiguous effects. Revalidate authority revision, identities, anchor, lease uniqueness, emergency brake, authorization, idempotency, and safety fences, with the brake checked immediately before every modeled side effect.
- Build tests_py/data/p17_corpus/ with one schema-valid JSON fixture per required replay class, covering all 26 classes and mapping each applicable case to CPF scenarios, source commits/runtime evidence, legacy result, expected v0 decision, invariant verdict, human interventions, attempts, and duplicate executions. Treat the P16.14-to-P17 edge solely as a historical ACCEPTANCE_BEFORE_ADVANCE violation discharged by the bootstrap OWNER_OVERRIDE fixture.
- Implement the replay harness, canonical decision trace hashing, aggregate comparison metrics, and crash injection between modeled durable writes in successor, acceptance, and handoff paths. Repeated and truncated sequences must converge to the same trace hash with no duplicate effect or successor publication.
- Implement a read-only shadow adapter with a ReadOnlyEvidenceRoot, typed legacy-to-v0 evidence projection, and fail-closed output fencing. Permit stdout or an explicit directory resolving inside the development repository's runtime/p17-shadow namespace; reject unset, evidence-root-contained, canonical-state-root-contained, or live-state-root-contained outputs. Every emitted record must include source-evidence digests.
- Expose replay and shadow only through python -m dev_orchestrator.convergence. Do not register commands in src/dev_orchestrator/cli.py or import convergence from outside the package. Shadow requires an explicit evidence root and stdout or an explicitly fenced sink and exits nonzero on fence violations.
- Produce the P17 architecture contract, backlog reconciliation, incident corpus, legacy contract-amendment matrix, runtime-root inventory, and migration-gates documents. Classify retained capabilities and conflicting legacy assertions with file/test citations; inventory duplicated budget paths without deleting them; describe M0-M4 evidence and residual risk; and explicitly defer M5-M9 production migration and retirement.
- After implementation, run focused tests, the complete tests_py suite, manual shadow non-mutation comparison, and graphify update . Keep all commands compatible with Windows PowerShell 5.1 by using separate commands or explicit $LASTEXITCODE checks and never placing && or || directly in a PowerShell command.

### Interfaces / contracts
- Composite P17 launch prerequisite: a committed agent/evidence/P17_PRELAUNCH_GATE.json with pre_resume_status=PASS for G0.1-G0.4, archived declaration validation bound to the later exact M0 HEAD, and one authenticated resume command in authoritative control history settled accepted against that M0 identity. The repository artifact is pre-resume evidence, not lifecycle authority; control history and owner-control state are the durable resume evidence.
- agent/evidence/P17_PRELAUNCH_GATE.json: schema version, task ID, ordered G0.1-G0.4 results, declaration and bootstrap digests, command IDs, authority revisions/generation, restart evidence, invariant verdicts, timestamps, abort reasons, and pre_resume_status PASS|ABORT. It intentionally excludes authenticated-resume outcome and its own commit SHA so it can be committed before the final action.
- Existing ControlCommandStore and OwnerControlStore are reused unchanged for G0.6. The resume request carries full expected identity including the final M0 HEAD; history/<command_id>.json and control/audit.jsonl record the accepted settlement, while owner-control.json records the same command as the latest resume. No P17-specific authoritative store or launch guard is added.
- dev_orchestrator.convergence.work_record: WorkRecord model, validate_work_record(payload), canonical_json(record), work_record_digest(record), and model-only authority-revision CAS helper with no persistence.
- dev_orchestrator.convergence.evidence: EvidenceItem, ConflictClaim, SharedCredentialLease, EvidenceSnapshot, typed required-source lookup, unresolved-ambiguity detection, explicit precedence, and snapshot digest.
- dev_orchestrator.convergence.policy: immutable Policy carrying per-problem budgets, ordered capability tiers, quota resets, wait and repair bounds, problem ordering, human-authorization rules, required sources, and policy_digest; no file or environment reads.
- dev_orchestrator.convergence.evaluator: twelve-member DecisionKind, Decision record, and decide(work_record, evidence, policy, now) returning one deterministic proposed action without I/O or mutation.
- dev_orchestrator.convergence.problems: eleven-member FailureClass, normalized_problem_fingerprint, ProblemBudget, and deterministic next-problem selection with volatile identity fields rejected.
- dev_orchestrator.convergence.findings and verification: closed-schema Finding with BLOCKING|NON_BLOCKING|INFO; accepted, absent, and OUTPUT_INVALID result states; VerificationRecord; NONE|VERIFIED|OWNER_OVERRIDE acceptance; GoalSatisfied verdict; and non-waivable-obligation validation.
- dev_orchestrator.convergence.invariants: stable CONVERGENCE_INVARIANT_CODES tuple, pure evaluators, LIFECYCLE_INVARIANT_MAP, and CPF_SCENARIO_MAP; ordinals are explanatory and never persisted or used for branching.
- dev_orchestrator.convergence.human_request and successor: stable question derivation and revision semantics, answer CAS, canonical open-question lookup, deterministic successor validation and handoff key, proposed publication plan, and complete resumable handoff builder.
- dev_orchestrator.convergence.preflight: CapabilityConstraint, read-only constraint projection, ALLOW|REWRITE|REJECT verdict, and recurrence classification preserving verified failure-memory provenance.
- dev_orchestrator.convergence.effects and actuator_guard: ExecutionEffectPort, five-state effect model, lease reconciliation, and a non-writing guard returning typed ACCEPT or REJECT after all authority and safety checks.
- dev_orchestrator.convergence.replay: validated corpus loader, ReplayHarness, per-case and aggregate reports, canonical decision_trace_hash, and crash-injection replay report.
- dev_orchestrator.convergence.shadow: ReadOnlyEvidenceRoot, fenced stdout/directory sinks, legacy snapshot builder, ShadowEvaluator, FenceViolation, and SHADOW_NAMESPACE='p17-shadow' under the development workspace only.
- dev_orchestrator.convergence.cli and __main__: replay [--corpus PATH] [--json] and shadow --evidence-root PATH (--stdout | --out DIR) [--json], deliberately isolated from the production CLI and import graph.

### Validation plan
- Before resume, verify agent/evidence/P17_PRELAUNCH_GATE.json is committed and pushed with pre_resume_status=PASS, contains G0.1-G0.4 evidence, and no convergence source file exists. At the resulting clean M0 HEAD, rerun declaration validation, pause/liveness checks, and all six centralized authority invariants. Verify archived declaration-gate evidence binds to this exact HEAD. Then submit resume as the final action and verify its authoritative history record settled accepted with expected.head and observed.head equal to M0; do not create a post-resume repository evidence commit.
- Add a gate regression using isolated runtime/repository fixtures that proves pause remains asserted through the P17_PRELAUNCH_GATE commit and exact-HEAD revalidation, a mismatched/stale resume request remains blocked without authorizing launch, and an accepted exact-M0 resume is durably represented by existing command history/audit and owner-control state. The test must not add a P17 launch guard or change production resume behavior.
- python -m pytest tests_py/test_p17_control_plane_declaration.py -q validates exactly one four-field declaration in both frozen task files, authoritative invariant and boundary names, CPF-01 through CPF-10 coverage, and successful declaration evaluation at the candidate HEAD.
- python -m pytest tests_py/test_p17_cpf_scenarios_part1.py tests_py/test_p17_cpf_scenarios_part2.py -q covers CPF-01 through CPF-10 with named replay cases and expected decisions/invariant verdicts, including stale ownership, missing handoff, restart replay, ambiguity, dirty worktree, repeated ticks, intent-publication crash, missing declaration, repaired-HEAD replay, and same-HEAD launch stability.
- python -m pytest tests_py/test_p17_work_record.py tests_py/test_p17_evaluator_purity.py -q covers schema validation, digest stability, total deterministic decisions, and sentinels proving no filesystem, process, git, network, implicit clock, or RNG access.
- python -m pytest tests_py/test_p17_evidence_and_policy.py tests_py/test_p17_invariants.py -q covers required-source failures, freshness, conflicts, precedence, credential leases, immutable policy digest, all twenty-one invariant codes, and valid lifecycle/CPF mappings without ordinal identity.
- python -m pytest tests_py/test_p17_acceptance_model.py -q proves acceptance=NONE and markdown COMPLETE cannot advance, OWNER_OVERRIDE cannot waive safety conditions, same-anchor typed evidence is required, and unexecuted reviewer tests remain NOT_VERIFIED.
- python -m pytest tests_py/test_p17_problem_identity.py tests_py/test_p17_findings_and_output_invalid.py -q covers stable per-problem identity and budgets across volatile changes, deterministic selection, closed-schema findings, absent versus rejected results, OUTPUT_INVALID repair, and multiple simultaneous blockers.
- python -m pytest tests_py/test_p17_retry_wait_escalation.py -q covers budget-free timed waits with automatic wakeup, same-tier resource failover, bounded evidence-driven escalation, and exhaustion producing a complete handoff and NEEDS_HUMAN.
- python -m pytest tests_py/test_p17_human_and_emergency.py -q covers question discharge exactly once across harmless anchor/projection changes, actionable options, and emergency pause/stop enforcement independent of stale lifecycle identity.
- python -m pytest tests_py/test_p17_lease_and_effects.py -q covers process-backed lease reconciliation, all five effect states, fail-closed ambiguity without duplicate effects, and shared-credential serialization.
- python -m pytest tests_py/test_p17_preflight_constraints.py -q covers each ZXZ-PC rule, including provenance seed:p11b:rdc-powershell-5.1 and fingerprint 281bf686902bf4aa1cd966a92cab25c5d1a66bfec453323ed171726580c1b82c, plus recurrence classification instead of relearning.
- python -m pytest tests_py/test_p17_replay_corpus.py tests_py/test_p17_replay_determinism.py -q covers all 26 classes, expected decisions and invariant verdicts, zero duplicate executions and markdown-only advancement, the bootstrap OWNER_OVERRIDE exception, and equal trace hashes after crash/rebuild.
- python -m pytest tests_py/test_p17_shadow_fence.py -q proves every write API on the evidence root fails, unsafe output targets fail closed, stdout writes no file, fenced output touches only the p17-shadow namespace, and outputs retain evidence digests.
- python -m pytest tests_py/test_p17_architecture_boundaries.py -q proves no outside module imports convergence, the production CLI has no P17 registration, and non-shadow convergence modules import no production write path.
- python -m pytest tests_py/test_p17_contract_amendments.py -q validates SAFETY/POLICY classifications, named covering tests, runtime-root inventory, and retirement dispositions without claiming production deletion or migration.
- python -m pytest tests_py -q keeps all existing lifecycle, owner-control, watchdog, command-history, transition, and safety behavior green without deleting or weakening legacy tests.
- Manually run shadow against a read-only copy of live evidence with --stdout and then a fenced workspace sink; compare pre/post digests and mtimes to prove zero evidence or production-state mutation and record classified legacy-versus-v0 divergences. Use PowerShell-safe sequencing with explicit $LASTEXITCODE checks.
- Run graphify update . after code and documentation changes, then use scoped graph queries/import inspection to confirm the convergence package remains isolated and the knowledge graph reflects new tests and contracts.

### Risks / failure modes
- Pre-launch race: clearing pause before committing evidence could allow the unchanged daemon to launch a Worker prematurely. Mitigation: keep pause asserted through the M0 evidence commit and exact-M0 revalidation; make authenticated resume the final action; store its durable proof only in existing authoritative command history, audit, owner-control state, and declaration-gate archive, with no post-resume repository commit.
- Commit-induced identity drift: committing P17_PRELAUNCH_GATE changes HEAD after earlier checks. Mitigation: treat earlier results as pre-resume evidence, rerun declaration, liveness, pause, and authority checks at the exact clean M0 HEAD, and bind resume expected/observed identity to that HEAD. A stale or blocked resume cannot authorize launch.
- Resume may immediately start narrowly recoverable work before a caller can write additional evidence. Mitigation: require every repository artifact and validation prerequisite to be complete before command submission and perform no repository writes after resume. Existing command settlement and audit are the only post-action evidence.
- Gate abort ambiguity: a failed pause check, live role owner, conflicting authority, or unreconcilable stale record is not a Worker repair task. Mitigation: do not submit resume, retain or reassert the authoritative pause through the normal command path, and escalate with typed evidence rather than hand-editing runtime state.
- Scope breadth remains substantial. Mitigation: production behavior stays untouched; modules are additive, pure, and independently testable; corpus entries must cite evidence and may explicitly mark a missing legacy regression instead of inventing a pass.
- Declaring all six lifecycle invariants requires the full CPF-01 through CPF-10 set. Mitigation: retain the superset declaration and map every scenario to a named replay and existing legacy regression where available.
- Shadow output could be directed at live or evidence state. Mitigation: canonical path resolution, read-only wrappers, explicit safe sinks, namespace isolation, and whole-root digest/mtime comparisons fail closed.
- A prototype could become a second authority if imported by production code. Mitigation: isolate its entry point, prohibit production imports, model transitions without persistence, and enforce architecture boundaries by import-graph tests.
- Problem fingerprint reuse may couple P17 to P16 field names. Mitigation: reuse only volatile-key rejection principles while defining and testing an independent allowed component set.
- Legacy prose-severity sites remain in production. P17 classifies them as POLICY and supplies typed v0 behavior but does not modify protected production paths; later migration gates own replacement.
- Windows PowerShell 5.1 incompatibility can break gate and validation commands. Mitigation: preserve failure-memory provenance, use explicit exit-code checks or separate invocations, and never use && or || directly in PowerShell.
- Hidden clocks, RNG, unordered serialization, or absolute paths can destabilize replay hashes. Mitigation: inject time and policy, canonicalize JSON, normalize repository-relative paths, and compare hashes across crash and rebuild fixtures.
- Corpus evidence may be incomplete or reconstructed. Mitigation: cite concrete anchors, label reconstructed fields, separate observed legacy behavior from expected v0 behavior, and do not promote undocumented assumptions into authority.
- Full-suite and crash-fixture runtime may be high on Windows. Mitigation: keep most replay/effect fixtures in memory and reserve real repositories for anchor, dirty-worktree, gate, and fence integration cases.
- A pause may be asserted again after resume. Mitigation: production emergency-brake semantics remain authoritative and are checked immediately before effects; Worker and shadow code never clear or bypass the brake.

### Out of scope
- Changing or bypassing production lifecycle authority, transition execution, declaration gates, successor consistency, watchdog, daemon, control-command, owner-control, or launch-barrier behavior; existing production components remain the sole writers.
- Adding a P17-specific authoritative launch guard, fourth state store, controller, runtime record, or resume protocol. The pre-launch artifact is audit evidence; the existing pause barrier and authenticated control-command path provide enforcement.
- Writing a repository artifact or commit after authenticated resume merely to record resume success; existing control history, audit, owner-control state, and archived gate evidence are the durable sources.
- Reconciling runtime state by editing JSON, deleting ledgers, bypassing CAS, forging command history, or clearing the emergency interlock outside existing authoritative commands.
- Registering convergence commands in src/dev_orchestrator/cli.py or importing convergence from production modules.
- Writing shadow output into the live canonical state root, the evidence root, or any canonical/live child; only stdout or a fenced development-workspace namespace is allowed.
- Migrating production runtime/config roots, changing REPO_ROOT-derived production defaults, activating the stable/development split, or relocating runtime state; P17 inventories and simulates these only.
- Executing M5-M9, enabling a new production writer, canarying authority, switching authority, or retiring legacy decision and budget paths.
- Deleting or weakening fail-closed identity, ownership, declaration, acceptance, emergency-brake, or legacy safety tests.
- Replacing production prose-severity parsing; typed severity applies to the v0 prototype and conflicting legacy behavior is classified for a later gate.
- Implementing real process/broker transport for effect probing or cancellation; P17 defines the port and uses in-memory fakes.
- Inventing or auto-starting P18, publishing a real successor, or treating markdown status as successor authority.
- Renaming, redefining, or extending the existing CPF registry; P17 maps to CPF-01 through CPF-10 as registered.
- Giving the Meta/System Architecture Reviewer lifecycle, mutation, verification, gate, or durable-state authority.
- Activating real X-ray sources, conveyors, hardware, irreversible operations, or changing provider/model selection behavior.
- Rewriting git history to hide e3eeb29 or treating the P16.14-to-P17 acceptance=NONE edge as a valid VERIFIED transition.
- Web, mobile, API-management, multi-node, or reporting feature implementation beyond backlog classification and architecture documentation.

### Independent plan review
- Approved: Plan is execution-ready and faithful to the frozen P17 architecture. Verified against the repo: it introduces no fourth authority (new src/dev_orchestrator/convergence/ package is pure, non-writing, not imported by production, no cli.py registration, enforced by import-graph tests); production writers (lifecycle_authority, transition_executor, successor_consistency, control_plane_contract/faults, watchdog, staged_roadmap) are untouched and explicitly out of scope; shadow output is fenced to runtime/p17-shadow with evidence digests and no actuator reads; it stops at M4 and defers M5-M9. Core contracts are frozen concretely enough to execute without design guessing: 12 DecisionKinds, 11 FailureClasses, 21 stable invariant codes (ordinals non-identity), Work Record v0 field groups, EvidenceSnapshot/Policy immutability, NONE|VERIFIED|OWNER_OVERRIDE acceptance with non-waivable safety obligations, question_id+question_revision CAS, deterministic successor/handoff keys, preflight ALLOW|REWRITE|REJECT, five-state effect reconciliation, and all 26 replay classes plus crash-injection trace-hash equality. Verification path is real and named (per-area pytest files, full tests_py suite, shadow non-mutation digest/mtime comparison, graphify), and reused APIs exist as described (ControlCommandStore, OwnerControlStore, validate_expected with observed identity, effects resume_future_launches / resume_exact_remediation, CPF-01..CPF-10 registered, incidents/liveness.py, incidents/fingerprint.py, agent/evidence/P17_BOOTSTRAP_OWNER_OVERRIDE.json present). G0 sequencing is fail-closed and safe: pause held through the M0 evidence commit, revalidation at the exact clean M0 HEAD, authenticated resume as the final action with durable proof kept in existing control history/audit/owner-control rather than a post-resume repository write, which correctly avoids the prior chicken-and-egg evidence race. agent/next.md and agent/staged/P17.md are currently byte-identical with no declaration section, HEAD is clean and pushed, and e3eeb29 is in ancestry, so G0.1 is coherent. NON_BLOCKING acceptance notes, none of which justify delay: (1) runtime/owner-control.json currently reports paused=false (resumed by command p17-bootstrap-fix-resume-20260927), so G0.2's literal abort-if-not-paused would stall; the Worker must first re-assert pause through the normal authoritative pause command and record that command ID, as plan risk 4 already implies, rather than abort or hand-edit state. (2) The requirement that archived declaration-gate evidence bind to the exact M0 HEAD is unsatisfiable as written while no gate is open (authority for devorchestrator is current_task_id=P17, lifecycle_state=PLANNING, owner_gate=null) because archiving only occurs on gate resolution during a launch that the pause prevents; treat that clause as conditional and make direct evaluation of the committed declaration at the exact M0 HEAD the operative check. (3) Declaring the full superset of six invariants, eight boundaries and CPF-01..CPF-10 self-classifies P17 as control-plane and imposes required-scenario coverage; the committed declaration and its validation-plan cross-reference must pass the existing validator and fault-registry checks. (4) M4's stable/dev root design validation in isolated fixtures is covered only by the documentation step and the contract-amendment test; add an explicitly named fixture-backed check for runtime/config-root fail-closed resolution and state-root ownership lock. (5) Corpus entries that lack a legacy regression must be marked missing rather than asserted as passing.
