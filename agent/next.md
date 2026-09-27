# P17 - Single-Authority Goal Convergence Baseline

Status: **PENDING DESIGN**

Predecessor: P16.14

## Architecture freeze decision

P17 MUST NOT introduce an independent fourth lifecycle/controller authority.

The target architecture is a single durable Work/Goal authority evolved from the existing lifecycle authority. P17 builds a non-authoritative, pure Convergence Evaluator plus replay/shadow harness that models this target. Existing production lifecycle code remains the sole production writer during P17.

The target control loop is:

Goal -> Observe Evidence -> Decide -> Act Once -> Verify -> Satisfy / Recover / Ask Human / Handoff

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

## Retry, failover, escalation and exhaustion

Budgets are per problem_id, not per whole task and not reset by ordinary HEAD/lifecycle evolution.

Maintain separate bounded counters for:
- infrastructure/resource attempts
- strategy/implementation attempts
- capability escalations
- total attempts

Policy:
- transient/resource/provider failure:
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

Only one daemon may own the canonical state root.

P17 designs and tests this with isolated fixtures/simulation; production relocation/promotion is a later gated migration.

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
- human questions remain dischargeable across harmless HEAD/projection changes;
- malformed role output is OUTPUT_INVALID and cannot silently become empty evidence;
- restart reconciliation cannot leave a dead lease as active progress and cannot duplicate an ambiguous external effect;
- every legacy no-implicit-retry/gating assertion is explicitly classified SAFETY or POLICY with named coverage.

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
