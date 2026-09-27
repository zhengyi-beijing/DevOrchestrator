# P17 Handoff — Single-Authority Goal Convergence Baseline

Last updated: 2026-09-27. Branch `main`; implementation anchor `7f1430f`.

Status: **COMPLETE / REVIEW ACCEPTED**. Technical Review (`ai_review:ai_review:ai_review:ai_review:a04b4803-cc11-41fe-8ccd-38263decfb5d`) accepted all P17 deliverables with `decision: "next"`, `next_action: "next_task"`, `remediation_round: 3`, and zero blocking findings. Transition executor settled the review execution as `state: "settled"`, `outcome: "task_complete"`.

## 1. Executive Summary

P17 successfully designed, implemented, and validated the **Single-Authority Goal Convergence Baseline** (Migration Gates M0 through M4) without replacing the production controller or adding an unverified fourth runtime authority:

1. **Pure Convergence Model (`src/dev_orchestrator/convergence/`)**:
   - `work_record.py`: Target WorkRecord v0 schema (20 field groups, `OPEN` | `NEEDS_HUMAN` | `DONE`, active lease, current problem, attempts, acceptance union, wait, verification, successor, human request, handoff, CAS token), strict validation, canonical sorted-key JSON, and sha256 digests.
   - `evidence.py`: Immutable `EvidenceSnapshot`, `EvidenceItem`, `ConflictClaim`, `SharedCredentialLease`, typed source lookup, unresolved ambiguity detection, and deterministic snapshot digest.
   - `policy.py`: Immutable `Policy` and `ProblemBudget` with independent budgets, capability tiers, quota resets, wait bounds, and deterministic policy digest; reads neither configuration nor environment.
   - `evaluator.py`: Pure side-effect-free `decide()` returning one of 12 frozen `DecisionKind` values with reason, problem ID, parameters, invariant citations, idempotency key, and evidence digests.
   - `problems.py`: 11 frozen `FailureClass` values, `normalized_problem_fingerprint` rejecting volatile keys (HEAD, lifecycle phase, PID, timestamps, retry IDs), `ProblemTracker`, and deterministic next-problem selection.
   - `findings.py`: Closed-schema `Finding` model, `FindingSeverity` (`BLOCKING`, `NON_BLOCKING`, `INFO`), `OutputInvalidError`, and strict parser eliminating free-text prose scanning.
   - `verification.py`: `VerificationRecord`, `is_goal_satisfied` evaluator, and `validate_owner_override` enforcing non-waivable safety obligations (no unresolved blockers, no active lease, no unresolved ambiguity, emergency brake, safety authorization).
   - `invariants.py`: Stable 21-code convergence invariant registry, pure evaluators, and bidirectional mappings to 6 lifecycle invariants and `CPF-01` through `CPF-10`.
   - `human_request.py`: Stable question ID derivation, `HumanRequest`, active finder, and answer semantics CASing exclusively on `(question_id, question_revision)`.
   - `successor.py`: Deterministic successor publication transaction plan (`compute_handoff_idempotency_key`) and structured `build_resumable_handoff`.
   - `preflight.py`: Deterministic capability preflight (`CapabilityConstraint`) with seeded ZXZ-PC rules including `seed:p11b:rdc-powershell-5.1` (fingerprint `281bf686902bf4aa1cd966a92cab25c5d1a66bfec453323ed171726580c1b82c`), and recurrence classification emitting `LEARNING_REGRESSION` / `CONTROL_PLANE_DEFECT`.
   - `effects.py` & `actuator_guard.py`: In-memory 5-state effect port (`EffectState`), lease reconciliation enforcing `NO_ORPHAN_OWNER`, and non-writing `ActuatorGuard` revalidating revision, identity, anchor, lease uniqueness, emergency pause, authorization, and idempotency.
   - `replay.py`: Corpus loader, `ReplayHarness`, aggregate reporting, canonical `decision_trace_hash`, and crash injection between durable write boundaries proving trace-hash equality.
   - `shadow.py`: `ReadOnlyEvidenceRoot` raising `FenceViolation` on mutation, `validate_shadow_sink` strictly confining output to `runtime/p17-shadow/`, and `ShadowEvaluator` projecting legacy evidence with source digests.
   - `cli.py` & `__main__.py`: Isolated CLI entry point `python -m dev_orchestrator.convergence` supporting `replay` and `shadow` commands.
   - `roots.py` & `amendments.py`: Runtime root inventory and legacy contract amendments matrix.

2. **Corpus & Documentation Deliverables**:
   - `tests_py/data/p17_corpus/`: 26 schema-valid historical replay fixtures covering all required incident classes from P12 through P16.14.
   - `docs/P17_ARCHITECTURE_CONTRACT.md`: Target architecture, pure control loop, WorkRecord v0, non-waivable safety rules, and legacy vs v0 metrics.
   - `docs/P17_BACKLOG_RECONCILIATION.md`: Canonical reconciliation matrix covering all 15 backlog capability areas.
   - `docs/P17_INCIDENT_CORPUS.md`: Deduplicated mapping of all 26 replay classes to CPF scenarios, legacy behavior, expected decisions, and covering regressions.
   - `docs/P17_LEGACY_CONTRACT_AMENDMENTS.md`: Classification of legacy conflicting assertions as SAFETY vs POLICY, and inventory of 4 duplicated budget paths scheduled for retirement at M9.
   - `docs/P17_RUNTIME_ROOT_INVENTORY.md`: Complete root inventory, stable/dev deployment architecture, state-root ownership lock, and 7-step promotion protocol.
   - `docs/P17_MIGRATION_GATES.md`: Full specification of M0-M4 completed gates, P17 authority boundary freeze, and deferred M5-M9 gates.

## 2. Technical Review Remediation Summary

Three bounded review rounds resolved all findings:
- **Round 1 (8 findings)**: Acceptance-before-advance on DONE status, stale verification anchor checks, integrity ambiguity ordering, quota reset wake versus elapsed time, active lease liveness default, ActuatorGuard anchor and lease coverage, durable write crash injection harness, and contract amendments module.
- **Round 2 (6 findings)**: Honest decision trace-hash comparison and guard key verification in crash simulation, ActuatorGuard dead lease recovery liveness awareness, stable `PUBLISH_SUCCESSOR` idempotency keys across churn, `IDEMPOTENT_REPLAY` invariant enforcement, required evidence source read status filtering, and exact assertion pinning in planner singleflight tests.
- **Round 3 (2 findings)**: Dead lease recovery deadlock with answered human requests resolved by admitting `EXECUTE` in `ActuatorGuard` and checking lease liveness in Section 4 of `evaluator.py`; contradictory/ambiguous process probe and broker effect liveness resolution unified in `effects.resolve_lease_liveness`.
- **Round 3 Acceptance**: Clean `decision: "next"`, `next_action: "next_task"`, 0 blocking findings.

## 3. Validation State

- **19 dedicated P17 test suites (96 tests)**: 100% passed.
- **Historical incident corpus replay**: 26/26 passed (0 failures).
- **Touched regression suites**: 113 passed, 10 subtests passed (`test_p12_planner_singleflight.py`, `test_p1614_invariant_workflow.py`, `test_p1613_successor_consistency.py`, `test_workflow_policy.py`).
- **Core control plane suites**: 136 passed, 8 subtests passed (`test_transition_executor_aibroker.py`, `test_staged_roadmap.py`, `test_staged_handoff.py`, `test_lifecycle_projection.py`, `test_control_commands.py`, `test_ai_reviewer.py`, `test_ai_planner.py`).
- **Shadow mode CLI**: Clean execution, 0 mutations, verified via `python -m dev_orchestrator.convergence shadow --evidence-root . --stdout --json`.
- **Python AST compilation & formatting**: `python -m compileall -q src ops tests_py` clean (0 errors); `git diff --check` clean.
- **Structured readiness authority**: `agent/execution-state.json` set to `completed` and `agent/next.md` set to `COMPLETE`.

## 4. Operational & Successor Status

- P17 is terminal `COMPLETE`.
- P17 stops strictly at Migration Gate M4. Migration Gates M5-M9 (external root migration, single-project canary, dual-run comparison, authority switch, and legacy decision path retirement) are explicitly deferred.
- Staged task P18 (`agent/staged/P18.md`) defines "Native Execution Transport & RDC Dependency Reduction".
- In accordance with the successor policy, P18 starts only through the normal authoritative lifecycle handoff when authorized; it is not auto-started.
