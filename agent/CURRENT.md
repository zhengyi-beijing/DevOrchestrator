# DevOrchestrator Self-Hosted Development State

- Canonical worktree: C:\work\github\DevOrchestrator-dev.
- Canonical branch: main.
- Runtime mode: self-hosted canonical daemon on 8770 with AIBroker diagnostics on 8875.
- Historical detached worktree C:\work\github\DevOrchestrator is not an active controller.

Current task: **P18 Native Execution Transport & RDC Dependency Reduction** (Status: **REMEDIATION_COMPLETE**). Previous task: **P17 Single-Authority Goal Convergence Baseline** (Status: **COMPLETE**).

P18 Technical Review Round 1 Remediation Evidence (2026-09-28):
- Addressed all 9 Technical Review findings from `runtime/ai-reviewer.json`:
  1. Finding 1 (Remote helper poll signature): Updated `op_poll` in `src/dev_orchestrator/ai/remote_helper.py` to call `store.get(job_id)` instead of non-existent `store.get_record(job_id)`.
  2. Finding 2 (validate_transport_capability argument mismatch): In `src/dev_orchestrator/web/server.py`, called `security.validate_transport_capability` using keyword arguments (`project_id`, `host_id`, `operation`, `command_ref`, `path`) and separated nonce verification.
  3. Finding 3 (stage_write_content signature mismatch): In `src/dev_orchestrator/transport/local.py` and `remote_helper.py`, passed `WriteContentUpload` instance as first positional argument to `store.stage_write_content(upload, max_bytes=max_bytes)`.
  4. Finding 4 (write_file argument and target path forwarding): In `src/dev_orchestrator/transport/local.py` and `remote_helper.py`, constructed `FileWriteRequest` with resolved `target_path`, passed `allowed_roots=[str(r) for r in allowed_roots]`, passed `host_identity`, and excluded raw `content_bytes` from API response payloads.
  5. Finding 5 (Re-evaluation of claimed state write intents): In `src/dev_orchestrator/transport/write_store.py`, inspected target file digest when intent is `claimed`; if matching content digest (post-atomic replace crash), promotes intent to `applied`, records post-digest, preserves `pre_digest`, and returns successful result. If matching pre-digest, proceeds with write; if matching neither, marks `ambiguous_requires_human`.
  6. Finding 6 (Missing single-use nonce validation): In `src/dev_orchestrator/web/server.py` POST transport routes, validated capability token and called `security.consume_request_nonce(row["capability_id"], nonce)` returning 403 Forbidden on missing or replayed nonces.
  7. Finding 7 (sha256 digest format normalization): Introduced `canonical_sha256(digest_or_bytes)` in `src/dev_orchestrator/transport/contracts.py` ensuring `"sha256:"` lowercase hex prefix across read, stat, staging, and CAS writes in `local.py`, `remote_helper.py`, `write_store.py`, and `cli.py`.
  8. Finding 8 (read_transport_operations signature and NDJSON logging): In `src/dev_orchestrator/web/server.py`, invoked `read_transport_operations(runtime, cursor=cursor, limit=limit)`. In `src/dev_orchestrator/transport/observability.py`, serialized records directly as NDJSON with secret redaction and bounded log compaction.
  9. Finding 9 (Race condition in reconcile_file_write): In `src/dev_orchestrator/transport/write_store.py`, read intent under `InterProcessFileLock(lock_file)` before evaluating or mutating state to prevent clobbering concurrent terminal writes.
- HTTP Transport Route Authorization:
  - In `src/dev_orchestrator/web/server.py`, exempted `/api/v1/control/transport/*` endpoints from master-only `_owner_authorized()` gate, allowing transport capability tokens with single-use nonces while preserving loopback requirement and master bearer token authorization.
  - Added missing `from uuid import uuid4` import and implemented `_authorized_control_read` on `_DashboardHandler` supporting both master authorization and scoped capability tokens.
- Acceptance Readiness:
  - Codebase is fully prepared for live Windows acceptance exercise for ZXZ-PC without RDC/GUI. Routine command, file read/write/stat, job spawn/poll/cancel operations run native without Remote Desktop escalation.
- Verification Summary:
  - 43/43 P18 tests passed across all 5 test suites (`test_p18_binary_staging_and_write.py`, `test_p18_canonical_identity.py`, `test_p18_config_and_parameters.py`, `test_p18_selector_and_security.py`, `test_p18_transports.py`).
  - 210/210 regression tests passed in P13, P14, P145 suites (0 failures).
  - Python AST and syntax compilation clean (`compileall src ops tests_py`).
  - `git diff --check` clean with 0 warnings or whitespace errors.
  - Knowledge graph updated cleanly via `graphify update .`.
     - Added `consumed_idempotency_keys: tuple[str, ...]` to `WorkRecord` dataclass, canonical serialization (`to_dict` serialized conditionally when non-empty to preserve digest stability for existing fixtures), and `validate_work_record()`.
     - Implemented `model_durable_decision_transition(record, decision, now=...)` in `work_record.py` applying CAS updates for mutative decisions (`EXECUTE`, `RETRY_*`, `FAILOVER_RESOURCE`, `ESCALATE_CAPABILITY`, `VERIFY`, `PUBLISH_SUCCESSOR`, `SATISFY_GOAL`, `WRITE_HANDOFF`), recording active lease with idempotency key, appending consumed idempotency keys, and advancing authority revision.
     - Updated `ActuatorGuard.seed_from_work_record()` to restore consumed keys comprehensively from `successor.handoff_idempotency_key`, `active_lease["idempotency_key"]`, `attempts[*]["idempotency_key"]`, `handoff["idempotency_key"]`, and `consumed_idempotency_keys`.
     - Updated `ReplayHarness.run_case` in `replay.py`: for admitted mutative decisions, models the durable state via `model_durable_decision_transition`, instantiates a restarted `ActuatorGuard` seeded from durable state, and confirms both re-evaluating the original decision and re-evaluating from durable state reject duplicate mutative actuation.
     - Added regression test `tests_py/test_p17_replay_determinism.py::TestP17ReplayDeterminism::test_execute_replay_after_guard_restart_is_rejected_from_durable_state`.
  2. Finding 2 (Contradictory Terminal Broker Outcomes Fail-Closed):
     - In `effects.py`, updated `resolve_lease_liveness` to preserve distinct terminal broker outcomes (`TERMINAL_SUCCESS`, `TERMINAL_FAILURE`, `CANCELLED`).
     - When conflicting terminal broker outcomes coexist (e.g. `succeeded` and `failed` for the same execution ID), marks `lease_ambiguous=True` and `terminal_success=False`, failing closed in `evaluator.py` to `REQUEST_HUMAN` citing `FAIL_CLOSED_AMBIGUITY` and rejecting in `ActuatorGuard`.
     - Added regression test `tests_py/test_p17_lease_and_effects.py::TestP17LeaseAndEffects::test_conflicting_terminal_success_and_failure_fails_closed_evaluator_and_guard`.
- Verification Summary:
  - 19 dedicated P17 test suites (102 tests): 100% passed.
  - Full historical incident corpus replay: 26/26 passed (0 failures, trace hash deterministic, 0 duplicate executions).
  - Touched regression suites (`test_p12_planner_singleflight.py`, `test_p1614_invariant_workflow.py`, `test_p1613_successor_consistency.py`, `test_workflow_policy.py`, `test_transition_executor_aibroker.py`): 138 passed.
  - Convergence replay CLI (`python -m dev_orchestrator.convergence replay --corpus tests_py/data/p17_corpus`): 26/26 passed.
  - Shadow mode CLI (`python -m dev_orchestrator.convergence shadow --evidence-root . --stdout --json`): clean execution, 0 mutations.
  - Python AST and syntax compilation: clean (`compileall` 0 errors).
  - Whitespace check (`git diff --check`): clean.
  - Knowledge graph updated cleanly via `graphify update .`.

P17 Technical Review Round 4 remediation evidence (2026-09-27):
- Addressed all 4 Technical Review findings and observations from review `ai_review:wd-e7b2c8466f922be2` at HEAD `9188611`:
  1. Finding 1 (Wall-clock dependency in replay corpus):
     - Added explicit `now: str = "2026-09-27T00:00:00Z"` parameter to `ReplayCase` (with `from_dict`/`to_dict`).
     - Updated `ReplayHarness.run_case` to pass `now=case.now` to `decide(...)`.
     - In `tests_py/test_p17_retry_wait_escalation.py::test_known_quota_reset_produces_budget_free_wait`, passed explicit `now="2026-09-27T12:00:00Z"` to eliminate wall-clock dependency against 16:00:00Z quota reset.
     - Added regression test `tests_py/test_p17_replay_determinism.py::TestP17ReplayDeterminism::test_corpus_replay_is_wall_clock_independent` proving identical replay results, trace hashes, and zero duplicates across time.
  2. Finding 2 (Missing failure classes in Section 9 & case_12 fixture):
     - In `evaluator.py` Section 9, added explicit bounded recovery branches:
       - `ENVIRONMENT_CONSTRAINT`: emits `RETRY_SAME_STRATEGY` with preflight `rewrite_template` while attempts remain within budget.
       - `CONTROL_PLANE_DEFECT`: emits bounded `RETRY_NEW_STRATEGY` then `ESCALATE_CAPABILITY`.
       - `INTEGRITY_OR_IDENTITY_AMBIGUITY`: fails closed to `REQUEST_HUMAN` citing `FAIL_CLOSED_AMBIGUITY`.
     - Reclassified `tests_py/data/p17_corpus/case_12_known_powershell_incompatibility_after_rule_learned.json` with `failure_class: "ENVIRONMENT_CONSTRAINT"`, attempt carrying prohibited `&&` command under PowerShell 5.1, `expected_v0_decision: "RETRY_SAME_STRATEGY"`, and `expected_invariant_verdicts: {"BOUNDED_PROBLEM": true, "LEARNED_CONSTRAINT_CONSUMPTION": false, "LEARNING_REGRESSION": false}`.
     - In `invariants.py`, updated `LEARNING_REGRESSION` to scan `work_record.attempts` via `classify_recurrence` with seeded rules so executing a prohibited command sets `lr_holds = False`.
     - Added regression test `tests_py/test_p17_preflight_constraints.py::TestP17PreflightConstraints::test_environment_constraint_problem_retries_with_rewrite_before_handoff`.
  3. Finding 3 (Completed execution with active lease / terminal success deadlocks on human):
     - In `evaluator.py` Section 4, added check for `liveness.terminal_success` before dead-lease fall-through; emits `DecisionKind.VERIFY` for independent verification (`parameters={"role": role, "attempt_id": attempt_id, "terminal_success": True}`).
     - In `actuator_guard.py`, `is_verify_on_terminal_success` admits `VERIFY` on demonstrably non-live terminal success lease.
     - In `invariants.py`, updated `PROGRESS_TOTALITY` to resolve lease liveness via `resolve_lease_liveness` and verify progress totality holds (`pt_holds = True`) on `terminal_success`.
     - In `tests_py/data/p17_corpus/case_14_reviewer_unable_to_run_tests_with_claimed_counts.json`, added active lease and succeeded broker_effect so terminal success is exercised across all 26 replay cases.
     - Added regression test `tests_py/test_p17_lease_and_effects.py::TestP17LeaseAndEffects::test_terminal_success_lease_emits_verify_and_guard_admits_it`.
  4. Finding 4 (Duplicate execution count measured not declared):
     - In `replay.py`, `run_case` evaluates `decide()` twice through a single `ActuatorGuard` seeded from `work_record`; if admitted twice for the same idempotency key, records `measured_duplicates = 1`, else `0`. Added `duplicate_matches = (measured_duplicates == case.duplicate_execution_count)`.
     - In `replay.py`, `run_corpus` calculates `aggregate_duplicate_executions = sum(r.measured_duplicate_executions for r in results)` rather than summing fixture constants.
     - Added regression test `tests_py/test_p17_replay_corpus.py::TestP17ReplayCorpus::test_duplicate_execution_count_is_measured_not_declared`.
  5. Non-blocking doc corrections:
     - Updated `docs/P17_INCIDENT_CORPUS.md` row 04 (`ANCHOR_BINDING: False`) and row 12 (`RETRY_SAME_STRATEGY`, `LEARNED_CONSTRAINT_CONSUMPTION: False`, `LEARNING_REGRESSION: False`).
- Verification Summary:
  - 19 dedicated P17 test suites (100 tests): 100% passed.
  - Full historical incident corpus replay: 26/26 passed (0 failures).
  - Touched regression suites (`test_p12_planner_singleflight.py`, `test_p1614_invariant_workflow.py`, `test_p1613_successor_consistency.py`, `test_workflow_policy.py`, `test_transition_executor_aibroker.py`): 138 passed.
  - Convergence replay CLI (`python -m dev_orchestrator.convergence replay --corpus tests_py/data/p17_corpus`): 26/26 passed.
  - Shadow mode CLI (`python -m dev_orchestrator.convergence shadow --evidence-root . --stdout --json`): clean execution, 0 mutations.
  - Python AST and syntax compilation: clean (`compileall` 0 errors).
  - Whitespace check (`git diff --check`): clean.

P17 final closure and technical review acceptance evidence (2026-09-27):
- Independent Technical Review (`ai_review:ai_review:ai_review:ai_review:a04b4803-cc11-41fe-8ccd-38263decfb5d`) on clean HEAD `7f1430f` accepted all P17 deliverables with `decision: "next"`, `next_action: "next_task"`, `remediation_round: 3`, and `review_findings: []`:
  - Verified Finding 1 (dead lease / answered human request): `actuator_guard.py` includes `DecisionKind.EXECUTE` in `is_recovery_decision`, and `evaluator.decide` evaluates active lease liveness in Section 4 ahead of human requests, admitting `EXECUTE` on demonstrably dead leases with an answered request while emitting `NOOP_ACTIVE` on live leases.
  - Verified Finding 2 (contradictory liveness): shared `effects.resolve_lease_liveness` scans all matching `process_probe` and `broker_effect` evidence rows, sets `lease_ambiguous = True` on disagreements or indeterminate rows, fails closed in `evaluator.py` to `REQUEST_HUMAN` citing `FAIL_CLOSED_AMBIGUITY`, and rejects in `ActuatorGuard`.
  - Confirmed all 8 Round-1 + 6 Round-2 + 2 Round-3 findings closed with load-bearing regressions; crash injection simulation and `IDEMPOTENT_REPLAY` invariant non-vacuous; `PUBLISH_SUCCESSOR` idempotency keys stable across churn; all 26 replay cases and 19 dedicated test suites preserved.
  - One non-blocking observation recorded for deferred M5-M9 wiring (unconfirmed/contradictory lease owner request consumption without transport-level actuator).
- Authoritative transition executor settlement: recorded execution `ai_review:ai_review:ai_review:ai_review:a04b4803-cc11-41fe-8ccd-38263decfb5d` as `state: "settled"`, `outcome: "task_complete"`, `reason: "review accepted current READY_TO_RUN task and no next executable task is advertised"`.
- Verification Summary:
  - 19 dedicated P17 test suites (96 tests): 100% passed.
  - Full historical incident corpus replay: 26/26 passed (0 failures).
  - Touched regression suites (`test_p12_planner_singleflight.py`, `test_p1614_invariant_workflow.py`, `test_p1613_successor_consistency.py`, `test_workflow_policy.py`): 113 passed, 10 subtests passed.
  - Core control plane suites (`test_transition_executor_aibroker.py`, `test_staged_roadmap.py`, `test_staged_handoff.py`, `test_lifecycle_projection.py`, `test_control_commands.py`, `test_ai_reviewer.py`, `test_ai_planner.py`): 136 passed, 8 subtests passed.
  - Shadow mode CLI (`python -m dev_orchestrator.convergence shadow --evidence-root . --stdout --json`): clean execution, 0 mutations.
  - Python AST and syntax compilation: clean (`compileall` 0 errors).
  - Whitespace check (`git diff --check`): clean.
  - Structured readiness authority: `agent/execution-state.json` set to `completed` and `agent/next.md` set to `COMPLETE`.
  - Knowledge graph updated cleanly via `graphify update .`.

P17 Technical Review Round 3 remediation evidence (2026-09-27):
- Addressed all Technical Review findings from `ai_review:ai_review:ai_review:a04b4803-cc11-41fe-8ccd-38263decfb5d`:
  1. Finding 1 (Dead lease recovery deadlock with answered human request):
     - In `effects.py`, implemented `LeaseLiveness` and `resolve_lease_liveness(active_lease, evidence)` to resolve liveness deterministically across all matching probes and effects.
     - In `actuator_guard.py`, included `DecisionKind.EXECUTE` in `is_recovery_decision`, allowing `ActuatorGuard.validate` to admit `EXECUTE` when an active lease is demonstrably dead (`found_liveness and not lease_live and not lease_ambiguous`).
     - In `evaluator.py`, reordered active lease liveness evaluation to Section 4 before human request handling in Section 5. Evaluator now checks lease liveness first:
       - When lease is LIVE, emits `NOOP_ACTIVE` (preventing concurrent execution).
       - When lease lacks liveness evidence, fails closed to `REQUEST_HUMAN` citing `NO_ORPHAN_OWNER`.
       - When lease has ambiguous or contradictory liveness, fails closed to `REQUEST_HUMAN` citing `FAIL_CLOSED_AMBIGUITY`.
       - When lease is demonstrably dead and human request is answered, emits `DecisionKind.EXECUTE`, which `ActuatorGuard` admits without deadlock.
       - When lease is demonstrably dead, human request is None, and no current problem exists, fails closed to `REQUEST_HUMAN` ("dead lease but no failure problem recorded").
     - In `invariants.py`, updated `PROGRESS_TOTALITY` to account for `work_record.human_request` when checking dead lease progress totality.
     - Covered by `tests_py/test_p17_lease_and_effects.py::TestP17LeaseAndEffects::test_dead_lease_with_answered_human_request_emits_and_admits_execute` and `test_live_lease_with_answered_human_request_emits_noop_and_rejects_execute`.
  2. Finding 2 (Contradictory process_probe and broker_effect liveness resolution):
     - In `effects.py`, `resolve_lease_liveness` inspects all matching `process_probe` and `broker_effect` items instead of using first-match-wins `break`.
     - If multiple probes/effects for the same role/execution contradict each other (e.g. one claims `alive: True` and another claims `alive: False`, or differing states), or if any item has an ambiguous state, sets `lease_ambiguous = True`.
     - In `evaluator.py`, ambiguous/contradictory lease liveness fails closed to `REQUEST_HUMAN` citing `FAIL_CLOSED_AMBIGUITY` with problem `ambiguous_lease_liveness`.
     - In `actuator_guard.py`, `liveness.lease_ambiguous` triggers rejection with `rejection_code="LEASE_ALREADY_ACTIVE"`.
     - Covered by `tests_py/test_p17_lease_and_effects.py::TestP17LeaseAndEffects::test_contradictory_process_probes_fails_closed_evaluator_and_guard`.
- Verification Summary:
  - All 19 P17 test suites (96 tests): 100% passed.
  - Full historical incident corpus replay: 26/26 passed (0 failures).
  - Regression suites (`test_p12_planner_singleflight.py`, `test_p1614_invariant_workflow.py`, `test_p1613_successor_consistency.py`, `test_workflow_policy.py`): 113 passed.
  - Python AST and syntax compilation: clean (`compileall` 0 errors).
  - Whitespace check (`git diff --check`): clean.
  - Knowledge graph updated cleanly via `graphify update .`.

P17 Technical Review Round 2 remediation evidence (2026-09-27):
- Addressed all 6 Technical Review findings from `ai_review:ai_review:a04b4803-cc11-41fe-8ccd-38263decfb5d`:
  1. Finding 1 (Crash injection simulation honest trace-hash comparison & guard key verification):
     - In `replay.py`, removed conditional assignment fallback (`if c_dec.kind == baseline_decision.kind else baseline_hash`), computing `c_hash = decision_trace_hash([c_dec])` unconditionally.
     - Seeded ActuatorGuard with the exact deterministic idempotency key (`handoff_idempotency_key` or computed successor key) at post-publish points.
     - Validated evaluator's actual decision key `c_dec.idempotency_key` through `guard.validate()`, verifying that duplicate execution is rejected by the guard with `rejection_code == "DUPLICATE_IDEMPOTENCY_KEY"` when crash occurs after successor is published, ensuring `duplicate_publications == 0`.
     - In `tests_py/test_p17_replay_determinism.py`, updated `test_truncated_and_repeated_durable_write_sequences_yield_one_publication` to assert honest divergence on truncated crash points (`before_verification`, `after_verification`, `before_acceptance` -> `EXECUTE`, `after_successor_publish` -> `NOOP_ACTIVE`), honest convergence on prepared points (`after_acceptance`, `before_successor_publish` -> `PUBLISH_SUCCESSOR`), and verified that `after_handoff` evaluates to `PUBLISH_SUCCESSOR` matching baseline trace hash while the guard rejects duplicate publication.
  2. Finding 2 (ActuatorGuard dead lease recovery deadlock):
     - In `actuator_guard.py`, made lease uniqueness fence liveness-aware by checking `process_probe` and `broker_effect` in evidence.
     - When lease is demonstrably dead/terminal (e.g. `process_probe.data['alive'] == False`), admits recovery decisions (`RETRY_NEW_STRATEGY`, `RETRY_SAME_STRATEGY`, `FAILOVER_RESOURCE`, `ESCALATE_CAPABILITY`) and terminal-success `VERIFY`. Rejects on live, missing liveness, and ambiguous effect.
     - Covered by `tests_py/test_p17_lease_and_effects.py::TestP17LeaseAndEffects::test_dead_lease_recovery_decision_is_admitted_by_guard`.
  3. Finding 3 (PUBLISH_SUCCESSOR idempotency key stability across churn):
     - In `evaluator.py`, derived `PUBLISH_SUCCESSOR` idempotency key stably from `work_record.successor["handoff_idempotency_key"]` or `compute_handoff_idempotency_key(project_id, goal_id, successor_goal_id, acceptance_digest)`, and `WRITE_HANDOFF` from stable goal/problem identity, eliminating volatile digests `(w_digest, ev_digest)`.
     - In `tests_py/test_p17_replay_determinism.py`, added `test_publish_successor_idempotency_key_stable_across_evidence_and_revision_churn` demonstrating that authority revision bump and evidence churn produce identical idempotency key and trigger `DUPLICATE_IDEMPOTENCY_KEY` rejection.
  4. Finding 4 (IDEMPOTENT_REPLAY invariant enforcement):
     - In `invariants.py`, hardened `IDEMPOTENT_REPLAY` to verify that `work_record.successor["handoff_idempotency_key"]` matches `compute_handoff_idempotency_key(project_id, goal_id, successor_goal_id, acceptance_digest)` and that `PUBLISHED` state cannot exist on an `OPEN` goal.
     - Updated corpus fixtures `case_02`, `case_18`, and `case_26` to use exact computed deterministic keys.
     - Covered by `tests_py/test_p17_invariants.py::TestP17Invariants::test_idempotent_replay_fails_on_volatile_or_mismatched_handoff_key`.
  5. Finding 5 (Required evidence source read status):
     - In `evaluator.py`, updated `present_sources` to require `it.read_status == "OK"`. Evidence items with `read_status == "MISSING"` or `"ERROR"` fail closed to `REQUEST_HUMAN` citing `FAIL_CLOSED_AMBIGUITY`.
     - Covered by `tests_py/test_p17_preflight_constraints.py::TestP17PreflightConstraints::test_required_source_present_with_missing_read_status_fails_closed`.
  6. Finding 6 (Exact assertion in planner singleflight test):
     - In `tests_py/test_p12_planner_singleflight.py`, tightened relaxed substring assertion to exact assertion: `"PENDING DESIGN continue requires IDLE, PLAN_FAILED, or PENDING_DESIGN lifecycle"`.
- Verification Summary:
  - All 19 P17 test suites (93 tests): 100% passed.
  - Full historical incident corpus replay: 26/26 passed (0 failures).
  - Regression suites (`test_p12_planner_singleflight.py`, `test_p1614_invariant_workflow.py`, `test_p1613_successor_consistency.py`, `test_workflow_policy.py`): 113 passed, 10 subtests passed.
  - Python AST and syntax compilation: clean (`compileall` 0 errors).
  - Whitespace check (`git diff --check`): clean.

P17 Technical Review Round 1 remediation evidence (2026-09-27):
  1. Finding 1 (Acceptance-before-advance on DONE status):
     - In `evaluator.py` Section 3, required `is_goal_satisfied(work_record, evidence=evidence)` before allowing `PUBLISH_SUCCESSOR`; fails closed to `REQUEST_HUMAN` citing `ACCEPTANCE_BEFORE_ADVANCE` if acceptance is not verified or overridden.
     - In `work_record.py`, updated `validate_work_record` to reject `status == "DONE"` when `acceptance.kind == "NONE"`.
     - In `actuator_guard.py`, added acceptance precondition for `PUBLISH_SUCCESSOR` (requires `VERIFIED` or `OWNER_OVERRIDE`).
     - Covered by `tests_py/test_p17_acceptance_model.py::TestP17AcceptanceModel::test_done_status_with_acceptance_none_cannot_publish_successor`.
  2. Finding 2 (Stale verification anchor comparison):
     - In `verification.py`, added `evidence` and `current_anchor_head` parameters to `is_goal_satisfied()`; fails with `NOT_VERIFIED` when `acceptance.anchor_head` or `verification.exact_head` differs from `evidence.exact_anchors['head']`.
     - Threaded `evidence` snapshot through `evaluator.py` sections 3, 7, and 8.
     - Rebuilt `tests_py/data/p17_corpus/case_04_stale_reviewer_anchor_after_head_change.json` with stale verification anchor (`old-c0293ab`), `expected_v0_decision: "VERIFY"`, and `expected_invariant_verdicts: {"ANCHOR_BINDING": false}`.
     - Updated case 18 (`case_18_normal_happy_path_goal_completion.json`) with matching anchor `head-p17-clean` and `status: "DONE"`.
     - Covered by `tests_py/test_p17_acceptance_model.py::TestP17AcceptanceModel::test_stale_verification_anchor_blocks_goal_satisfaction`.
  3. Finding 3 (Integrity ambiguity ordering):
     - In `evaluator.py`, moved `has_unresolved_ambiguity(evidence)` check to Section 2 (above goal satisfaction and above DONE checks), ensuring conflicts and CORRUPT/AMBIGUOUS reads fail closed to `REQUEST_HUMAN` citing `FAIL_CLOSED_AMBIGUITY` before side effects or acceptance are considered.
     - Covered by `tests_py/test_p17_invariants.py::TestP17Invariants::test_unresolved_ambiguity_blocks_satisfy_and_publish`.
  4. Finding 4 (Quota reset wake vs elapsed time):
     - In `evaluator.py`, quota-reset branch only emits `WAIT_UNTIL` when parsed `reset_at` is strictly in the future relative to `now`; when elapsed, falls through to `FAILOVER_RESOURCE` within budget without requiring manual continue. Dropped `now_dt.isoformat()` zero-length wait fallback.
     - Covered by `tests_py/test_p17_retry_wait_escalation.py::TestP17RetryWaitEscalation::test_elapsed_quota_reset_wakes_to_failover_without_manual_continue`.
  5. Finding 5 (Active lease liveness default):
     - In `evaluator.py`, active lease branch defaults to not-live when liveness evidence is absent, failing closed to `REQUEST_HUMAN` citing `NO_ORPHAN_OWNER` and `PROGRESS_TOTALITY` rather than `NOOP_ACTIVE`.
     - Rebuilt `tests_py/data/p17_corpus/case_06_worker_process_death_stale_active_ownership.json` with active lease, dead process probe (`alive: false`), and `expected_v0_decision: "RETRY_NEW_STRATEGY"`.
     - Covered by `tests_py/test_p17_lease_and_effects.py::TestP17LeaseAndEffects::test_lease_without_liveness_evidence_is_not_active_progress`.
  6. Finding 6 (ActuatorGuard anchor and lease coverage):
     - In `actuator_guard.py`, added `PUBLISH_SUCCESSOR` to anchor-required decisions (`VERIFY`, `SATISFY_GOAL`, `PUBLISH_SUCCESSOR`), and added `FAILOVER_RESOURCE`, `ESCALATE_CAPABILITY`, and `VERIFY` to lease-uniqueness checks (`LEASE_ALREADY_ACTIVE`). Fails closed when required anchor is missing from evidence or expectation.
     - Covered by `tests_py/test_p17_lease_and_effects.py::TestP17LeaseAndEffects::test_guard_rejects_failover_and_escalation_while_lease_active`.
  7. Finding 7 (Durable write crash injection and shadow differential):
     - In `replay.py`, `simulate_crash_injection()` models ordered durable write boundaries (`before/after_verification`, `before/after_acceptance`, `before/after_successor_publish`, `after_handoff`), truncates state at crash points, resumes through `ActuatorGuard`, and asserts trace-hash convergence with zero duplicate publications (`duplicate_publications == 0`).
     - Covered by `tests_py/test_p17_replay_determinism.py::TestP17ReplayDeterminism::test_truncated_and_repeated_durable_write_sequences_yield_one_publication` and `test_shadow_delete_and_rebuild_differential_trace_hash`.
  8. Finding 8 (Contract amendments module, AST verification, and preflight constraints):
     - Implemented `src/dev_orchestrator/convergence/roots.py` (`resolve_runtime_root`, `acquire_state_root_lock`) and `src/dev_orchestrator/convergence/amendments.py` (`LEGACY_CONTRACT_AMENDMENTS`, `RETIREMENT_DISPOSITIONS`, `load_legacy_contract_amendments`, `load_retirement_dispositions`, `validate_amendment_test_references` via AST).
     - In `preflight.py`, implemented matchers for all 6 seeded constraint rules (`contains_tokens`, `cmdlet + parameter`, `extension`, `cli_flags`, `redirection`, `path_style`) with deterministic derived fingerprints (`derive_constraint_fingerprint`).
     - In `evaluator.py`, consumed `Policy.required_sources`, failing closed with `REQUEST_HUMAN` citing `FAIL_CLOSED_AMBIGUITY` when any required source is missing.
     - In `docs/P17_LEGACY_CONTRACT_AMENDMENTS.md`, aligned test reference for LCA-01 to `ControlPlaneGateLifecycleTests`.
     - In `agent/evidence/p17-g0-final-resume-20260927.json`, tracked authoritative resume command history unconditionally.
     - Covered by `tests_py/test_p17_contract_amendments.py`, `tests_py/test_p17_control_plane_declaration.py`, and `tests_py/test_p17_preflight_constraints.py::TestP17PreflightConstraints::test_every_seeded_rule_is_reachable_and_classified`.
- Verification Summary:
  - All 19 P17 test suites (85 tests): 100% passed.
  - Full historical incident corpus replay: 26/26 passed (0 failures).
  - Regression suites (`test_p1614_invariant_workflow.py`, `test_p1613_successor_consistency.py`, `test_workflow_policy.py`): 102 passed, 10 subtests passed.
  - Python AST and syntax compilation: clean (`compileall` 0 errors).
  - Whitespace check (`git diff --check`): clean.
  - Knowledge graph updated cleanly via `graphify update .`.


P17 Single-Authority Goal Convergence Baseline implementation evidence (2026-09-27):
- Pre-launch Gate M0 completed under authoritative owner pause: canonical 4-field Control-Plane Impact declaration committed, liveness and bootstrap override verified, stale records reconciled, daemon restarted and reconciled with one coherent P17 authority, and M0 evidence recorded in `agent/evidence/P17_PRELAUNCH_GATE.json` (`pre_resume_status: PASS`). Authenticated resume command `p17-g0-final-resume-20260927` settled accepted against exact clean M0 HEAD.
- Phase W pure convergence model implemented in `src/dev_orchestrator/convergence/`:
  - `work_record.py`: Target WorkRecord v0 schema (20 fields/groups, `OPEN` | `NEEDS_HUMAN` | `DONE`, active lease, current problem, attempts, acceptance union, wait, verification, successor, human request, handoff, CAS token), strict validation, sorted-key canonical JSON, sha256 digest, and model-only CAS helper.
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
- Corpus and Documentation Deliverables:
  - `tests_py/data/p17_corpus/`: 26 schema-valid historical replay fixtures covering all required incident classes from P12 through P16.14.
  - `docs/P17_ARCHITECTURE_CONTRACT.md`: Target architecture, pure control loop, WorkRecord v0, non-waivable safety rules, and legacy vs v0 metrics.
  - `docs/P17_BACKLOG_RECONCILIATION.md`: Canonical reconciliation matrix covering all 15 backlog capability areas.
  - `docs/P17_INCIDENT_CORPUS.md`: Deduplicated mapping of all 26 replay classes to CPF scenarios, legacy behavior, expected decisions, and covering regressions.
  - `docs/P17_LEGACY_CONTRACT_AMENDMENTS.md`: Classification of legacy conflicting assertions as SAFETY vs POLICY, and inventory of 4 duplicated budget paths scheduled for retirement at M9.
  - `docs/P17_RUNTIME_ROOT_INVENTORY.md`: Complete root inventory, stable/dev deployment architecture, state-root ownership lock, and 7-step promotion protocol.
  - `docs/P17_MIGRATION_GATES.md`: Full specification of M0-M4 completed gates, P17 authority boundary freeze, and deferred M5-M9 gates.
- Verification Evidence:
  - 19 dedicated P17 test suites (78 tests): 100% passed (`test_p17_control_plane_declaration.py`, `test_p17_cpf_scenarios_part1.py`, `test_p17_cpf_scenarios_part2.py`, `test_p17_work_record.py`, `test_p17_evaluator_purity.py`, `test_p17_evidence_and_policy.py`, `test_p17_invariants.py`, `test_p17_acceptance_model.py`, `test_p17_problem_identity.py`, `test_p17_findings_and_output_invalid.py`, `test_p17_retry_wait_escalation.py`, `test_p17_human_and_emergency.py`, `test_p17_lease_and_effects.py`, `test_p17_preflight_constraints.py`, `test_p17_replay_corpus.py`, `test_p17_replay_determinism.py`, `test_p17_shadow_fence.py`, `test_p17_architecture_boundaries.py`, `test_p17_contract_amendments.py`).
  - Touched regression suites: 102 passed, 10 subtests passed (`test_p1614_invariant_workflow.py`, `test_p1613_successor_consistency.py`, `test_workflow_policy.py`).
  - Replay CLI (`python -m dev_orchestrator.convergence replay --corpus tests_py/data/p17_corpus`): 26/26 passed, 0 duplicate executions, deterministic trace hash.
  - Shadow CLI (`python -m dev_orchestrator.convergence shadow --evidence-root . --stdout --json`): Clean decision emission with valid source and policy digests, zero production mutations.
  - `python -m compileall -q src ops tests_py`: 0 errors.
  - `git diff --check`: 0 errors.
  - `graphify update .`: Clean update (7310 nodes, 19986 edges, 310 communities).



P16.14 final closure evidence (2026-09-26):
- Worker's first commit `cb1648e` committed canonical declaration to `agent/next.md` and `agent/staged/P16.14.md` prior to any source modifications, establishing historical proof of impact declaration before implementation.
- Implemented `core/control_plane_faults.py` with durable fault registry (`CPF-01` through `CPF-10`) covering all six lifecycle invariants with resolvable AST test references and strict boundary validation.
- Implemented `core/control_plane_contract.py` providing deterministic declaration parsing (`declared`, `absent`, `invalid`, `ambiguous`, `unavailable`), task-level classification (`control_plane`, `ordinary`, `invalid`, `unevaluable`), launch declaration evaluation against committed Git HEAD, prompt injection blocks, review scope-gap analysis, and read-only convergence/traversal evidence projections.
- Implemented authoritative gate transition and lazy schema v2 upgrade in `core/lifecycle_authority.py`: `open_declaration_gate` atomically asserts `OWNER_GATE` lifecycle state with stable `gate_id` and resume state, and `resolve_declaration_gate` archives resolved gate evidence into bounded `resolved_owner_gates` (max 50, deduplicated) without polluting the transition journal.
- Integrated declaration gate reconciliation, replay authorization for different-HEAD retries, and gate evaluation into `core/transition_executor.py` before generic owner-gate fences across both DSH and AIBroker execution paths.
- Integrated invariant-driven contract prompt injection and validation into `core/ai_planner.py` (materializing canonical declaration section on plan freeze) and `core/ai_reviewer.py` (flagging undeclared control-plane scope gaps).
- Created CLI projection script `ops/p1614_evidence.py` and documented comprehensive invariant-driven method contract in `docs/CONTROL_PLANE_INVARIANT_METHOD_CONTRACT.md` and `docs/development-workflow.md`.
- Verification:
  - Focused test suite `tests_py/test_p1614_invariant_workflow.py`: 38/38 tests passed (including 6 remediation tests for Technical Review findings).
  - Touched regressions:
    - `test_p1613_successor_consistency.py`: 49 passed, 10 subtests passed.
    - `test_transition_executor_aibroker.py`: 24 passed, 8 subtests passed.
    - `test_lifecycle_projection.py`: 3 passed.
    - `test_ai_planner.py`: 38 passed.
    - `test_ai_reviewer.py`: 12 passed.
    - `test_workflow_policy.py`: 10 passed.
  - `python ops/p1614_evidence.py`: clean output with valid fault registry.
  - `python -m compileall src ops tests_py`: 0 errors.
  - `git diff --check`: 0 errors.
  - `graphify update .`: knowledge graph updated cleanly.

P16.14 Technical Review remediation evidence (2026-09-26):
- Addressed all 5 Technical Review findings from `ai_review:rereview:p1614-live-recover-20260926`:
  1. Finding 1 (Task mismatch before gate reconciliation):
     - In `core/transition_executor.py` (`_launch` and `_launch_aibroker`), reordered checks so authoritative task mismatch check executes before declaration gate reconciliation.
     - In `core/lifecycle_authority.py`, updated `resolve_declaration_gate` to require and enforce `task_id` matching on both `current_gate` and `authority`.
  2. Finding 2 (Bare declaration parser & grammar teaching):
     - In `core/control_plane_contract.py`, `parse_control_plane_declaration` with `allow_bare=True` terminates `convergence_evidence` continuation upon blank/non-indented lines and ignores leading unrelated interfaces.
     - In `inject_control_plane_contract`, added canonical contract requirement prompt block for `planner` and `plan_reviewer` roles when declaration is undeclared/missing.
  3. Finding 3 (Review scope-gap check on declared branch):
     - In `core/control_plane_contract.py`, defined `PROTECTED_SURFACE_BOUNDARIES` mapping protected surfaces to required transition boundaries; updated `declared_scope_gap` to detect under-scoped declarations where modified protected surfaces are omitted from transition boundaries.
  4. Finding 4 (Transient git failure & same-HEAD recovery):
     - In `core/control_plane_contract.py`, `evaluate_launch_declaration` fast-paths ordinary repositories without protected surfaces and escapes transient failures without false owner gating.
     - In `core/transition_executor.py`, `_is_declaration_gate_replayable` permits same-HEAD replay for transient/unevaluable blocks.
  5. Finding 5 (Substring false-positives on bare symbols):
     - In `core/control_plane_contract.py`, `_matches_protected_surface` uses word-boundary regex `(?<![A-Za-z0-9_]){re.escape(surface)}(?![A-Za-z0-9_])` preventing false positive matches on bare symbols.
- Added targeted tests in `tests_py/test_p1614_invariant_workflow.py` (`ControlPlaneRemediationTests`, 6 tests covering all 5 findings).

P16.14 Technical Review second-round remediation evidence (2026-09-26):
- Addressed all 3 Technical Review findings from `ai_review:ai_review:rereview:p1614-live-recover-20260926`:
  1. Finding (a) (Reviewer prompt injection wiring):
     - In `core/ai_reviewer.py`, updated all 5 call sites of `_review_prompt` (`advance`, `reconcile_stale`, `rereview_descendant`, `retry_failed`, and `_launch_web_sol_failover_reviewer`) to pass `repo_path=repo_path` and `worker_launch_head` in metadata.
     - Hardened `_review_prompt` to extract `head` from `truth` whether `truth` is a `RepositoryTruth` instance or a dictionary.
     - In `core/dispatcher.py`, wired control-plane contract prompt injection into WebSol reviewer dispatch.
  2. Finding (b) (Review scope-gap backstop on live review path):
     - In `core/ai_reviewer.py`, extracted `AIReviewerCoordinator._detect_scope_gaps` static method using safe diff inspection (`hidden_subprocess_kwargs`) and exact launch head comparison.
     - Replaced inline scope gap code in `_finalize_harness_review` with `_detect_scope_gaps`.
     - Integrated `_detect_scope_gaps` into `_handle_review_completion` (the live production review path), overriding accepting `decision="next"` to `decision="remediate"`, setting `next_action="continue_current_stage"`, and appending `CONTROL_PLANE_SCOPE_GAP` findings.
  3. Finding (c) (Worker and remediator contract prompt injection):
     - In `core/transition_executor.py` (`_launch`), loaded committed declaration at `head` and injected the control plane contract for `policy_role` (`worker` and `remediator`) into `effective_worker_prompt`.
- Verification:
  - Focused test suite `tests_py/test_p1614_invariant_workflow.py`: 41/41 passed (added `test_finding_a_reviewer_prompt_injection_wired_at_launch`, `test_finding_b_live_review_scope_gap_overrides_acceptance_to_remediate`, and `test_finding_c_worker_and_remediator_prompt_injection_wired_at_launch`).
  - Touched regressions: 133 passed, 18 subtests passed across `test_p1613_successor_consistency.py`, `test_transition_executor_aibroker.py`, `test_ai_reviewer.py`, `test_ai_planner.py`, and `test_workflow_policy.py`.
  - `python ops/p1614_evidence.py`: valid output with clean registry.
  - `python -m compileall src ops tests_py`: 0 errors.
  - `git diff --check`: 0 errors.
  - `graphify update .`: updated cleanly.

P16.14 Technical Review third-round remediation evidence (2026-09-27):
- Addressed all Technical Review findings from `ai_review:ai_review:ai_review:rereview:p1614-live-recover-20260926`:
  1. Regression Evidence and Full Validation Coverage:
     - Executed full 10-suite dispatcher regression suite (93/93 passed): `test_worker_done_dispatcher.py` (6), `test_worker_done_dispatcher_slice.py` (3), `test_bridge_reviewer_regressions.py` (5), `test_remediation_flow.py` (9), `test_binding_presence_gate.py` (3), `test_daemon_transition_integration.py` (4), `test_response_consumer.py` (11), `test_accounting_instrumentation.py` (4), `test_chatgpt_web_adapter.py` (14), and `test_p1612_websol_pairing_and_availability.py` (34).
     - Executed touched suites (136/136 passed): `test_p1613_successor_consistency.py` (49), `test_transition_executor_aibroker.py` (24), `test_lifecycle_projection.py` (3), `test_ai_planner.py` (38), `test_ai_reviewer.py` (12), and `test_workflow_policy.py` (10).
     - Executed full repository regression test suite: 1,363/1,363 passed in 526.69s.
  2. Failover Reviewer Launch Head Propagation:
     - In `core/ai_reviewer.py` (`_launch_web_sol_failover_reviewer`), extracted `worker_launch_head` from transitions or dispatcher occurrence and passed `"worker_launch_head": worker_launch_head` in `AIRoleRequest.metadata`. This guarantees `_detect_scope_gaps` receives the exact launch head on failover review paths without falling back to `HEAD~1..HEAD`.
  3. Cleaned Redundant Variable Rebinding:
     - In `core/dispatcher.py`, removed redundant `repo_path = snapshot.get("repo_path")` assignment.
  4. Repaired Roadmap UTF-8 BOM:
     - Stripped accidental UTF-8 BOM from `agent/staged/roadmap.json` (resolving strict UTF-8 JSON parsing in `staged_roadmap.py`).
- Verification:
  - Focused test suite `tests_py/test_p1614_invariant_workflow.py`: 42/42 passed (added `test_dispatcher_prompt_injection_and_failover_launch_head`).
  - Touched regressions: 136 passed across 6 suites.
  - Dispatcher regressions: 93 passed across 10 suites.
  - Full suite regression: 1,363 passed in 526.69s.
  - `python ops/p1614_evidence.py`: valid output with clean registry.
  - `python -m compileall src ops tests_py`: 0 errors.
  - `git diff --check`: 0 errors.
  - `graphify update .`: updated cleanly.

P16.14 final blocker remediation evidence, pending formal live acceptance (2026-09-27):
- Fixed the sole remaining `BLOCKING` review finding: `AIReviewerCoordinator._detect_scope_gaps` now treats a committed declaration with `kind="unavailable"` as transiently unevaluable and returns no scope gap, while absent, invalid, ambiguous, and under-scoped declarations retain their existing fail-closed findings.
- Preserved the production review-budget/owner-continue stall as an end-to-end regression. A formal `continue` may reach `resume_exact_remediation` only for the exact latest completed reviewer gate with an exhausted remediation budget, exactly one explicitly `BLOCKING:` finding, matching task/project/branch/clean fingerprint, a current HEAD equal to or descended from the reviewed HEAD, matching completed source-remediation evidence, and no active Worker. Dirty state, task mismatch, non-descendant HEAD, ambiguous findings, and superseded gates remain fenced.
- The remediation prompt contains only the single blocking finding and explicitly excludes non-blocking/P17 architectural follow-ups. No macro lifecycle state was added. Broader convergence-controller and owner-gate simplification remains in staged P17, including replay of this incident, and P17 remains prohibited from starting except by accepted P16.14 handoff.
- Verification:
  - `tests_py/test_p1614_invariant_workflow.py`: 43/43 passed.
  - Touched control/reviewer/executor suites: 78 passed with 14 subtests.
  - Full repository suite with isolated basetemp: 1,365 passed with 102 subtests in 562.67s.
  - `python ops/p1614_evidence.py`: fault registry valid (10 scenarios).
  - `python -m compileall -q src ops tests_py`: 0 errors.
  - `git diff --check`: 0 errors.


P16.13 final closure evidence (2026-09-26):
- Restarted the canonical daemon from stale PID 26264 onto the committed
  lifecycle fence. The real runtime smoke passed 35/35 twice across distinct
  60-second ticks. `linescanviewer` and `xray-hw-platform` each retained its
  historical count of 244 while converging to `state=gated`, `fenced=true`,
  with a stable non-null gate ID; no recovery was re-actuated.
- Live `devorchestrator` authority remained P16.13 with repository/authority
  agreement, no owner gate, no predecessor owner, and all six invariants held.
- The restart exposed a legacy-attempt migration omission. Commit `0140cbd`
  fenced matching historical failed attempts without changing their counts or
  retrying blocked actuation.
- Normal independent review
  `ai_review:p1613-closure-delta-0140cbd-r2` returned `REMEDIATE` for one
  malformed-history fail-closed gap. Commit `57c7f19` preserves malformed
  `null`/list/scalar evidence, records a deterministic diagnostic, invokes no
  recovery, and still records the generic non-recoverable owner gate.
- Independent provider delta re-review
  `ai_review:p1613-closure-delta-57c7f19-r3` returned `NEXT` with no findings.
  Reviewer resource: `agy/agy-1/claude-opus-4-6-thinking`; the prior Codex
  reviewer and failed `claude/default/opus` resource were excluded.
- Exact closure review `ai_review:p1613-final-exact-head-c0293ab-r4` returned
  `NEXT` and its decision settled terminally. That live settlement exposed a
  sticky Watchdog projection: all six invariants held, but the prior resolved
  lifecycle gate remained active. Commit `2e4c162` now archives resolved gate
  evidence and clears only gates whose shared-evaluator code currently holds.
- Independent delta review
  `ai_review:p1613-resolved-gate-delta-2e4c162-r5` returned `NEXT` with no
  findings. Two live ticks then held P16.13 at COMPLETE with no authority or
  Watchdog gate, one archived gate, `paused=false`, and no Worker.
- Final focused verification: 112 tests and 15 subtests passed. Final full
  regression: 1,311 tests and 102 subtests passed in 550.19s. `compileall`,
  `git diff --check`, and Graphify update passed.
- At closure P16.13 had no roadmap successor. Commit `6eb8e57` later restored
  the P16.13 -> P16.14 roadmap edge and staged P16.14. The two external-project
  owner gates remain deliberately fail closed and require per-project owner
  disposition; they do not block this task's accepted closure.

P16.13 -> P16.14 successor recovery fix (2026-09-26):
- Live symptom: the authority was `P16.13/COMPLETE` with no owners, no owner
  gate and no P16.14 handoff or transition, and the roadmap validly named
  P16.14. The daemon stayed IDLE because `NEXT_TASK_WITHOUT_HANDOFF` evidence
  showed `next_decisions=[]` and `holds=true`.
- Root cause: `evaluate_lifecycle_invariants` derived the invariant only from
  surviving `apply/next/next_task` decisions. All three P16.13 closure reviews
  settled `task_complete` ("no next executable task is advertised") at
  06:07-06:30Z, before the roadmap edge existed. So no decision owed a handoff
  and the roadmap successor was never considered.
- Fix: the shared evaluator also raises the invariant for a quiescent terminal
  authority whose roadmap names a successor with no durable handoff or in-flight
  transition. It is recoverable only for an unambiguous valid `successor`.
  Ambiguous, inconsistent or invalid evidence, and refused transitions, gate.
  Watchdog takes the recovery source from `roadmap_successor.source_task_id` and
  reuses `reconcile_successor_handoff`, the transition journal and the normal
  Planner handoff consumer. No new authority file was added.
- Regression: `TerminalRoadmapSuccessorInvariantTests` and
  `TerminalRoadmapSuccessorEndToEndTests` in
  `tests_py/test_p1613_successor_consistency.py`.
- Verification status: PENDING. Test execution, live daemon reload and live
  P16.13 -> P16.14 lineage evidence were not yet produced.

P16.13 review and remediation evidence (2026-09-26):
- Projection boundary (`f51a01b`): the lifecycle authority overlay is re-applied
  after the managed-run and orchestration-role projections, and the Activation
  Supervisor advances on the authoritative summary instead of the raw monitor
  summary. Both projections rewrite `lifecycle_state`/`telemetry.task_id` from
  evidence that can name a stale predecessor, so without this the dispatch view
  and published projection reported the predecessor task and the Supervisor saw
  no authority at all. Confirmed live before the fix: the authority read
  `READY_TO_RUN` while `summary.json` published `RECOVERY_REQUIRED`.
- Independent Technical Review returned `REMEDIATE` with four blocking findings;
  the bounded remediation re-review of the delta returned `ACCEPT` with all four
  closed and four `NON_BLOCKING` follow-ups.
- Remediation (`b5d42f2`): recovery actuation is fenced by its own owner gate
  (the gate was advisory, so two projects reached 91 consecutive failed attempts
  on a non-transient reason); reconciliation passes the decisions ledger so the
  authority no longer attests that `NEXT_TASK_WITHOUT_HANDOFF` holds while the
  Watchdog reports it violated for the same project and tick; lifecycle barriers
  must name the same task or be a consumed handoff, so an unconsumed cross-task
  barrier can no longer drain a pending review obligation; and a durably blocked
  review actuation fails closed to a durable gate instead of pinning the
  authority forever.
- Follow-ups (`981a342`): undrained source ownership is reported as a wait rather
  than a refusal, so the fence cannot permanently stop the only code path that
  rebuilds a durable handoff; the Watchdog normalizes a non-mapping decisions
  ledger exactly as reconciliation does; and the durability/isolation assertions
  were strengthened. Review finding N-C (a legacy ledger with pending
  obligations on two different tasks could newly gate on
  `SINGLE_ACTIVE_LIFECYCLE_OWNER`) is recorded as accepted fail-closed risk and
  is not reachable in any live project.
- Runtime smoke (`ops/p1613_lifecycle_smoke.py`, `e8eece7`): 33 of 35 checks
  pass. The two failures are `linescanviewer` and `xray-hw-platform`, whose
  recovery is still looping under the daemon process that predates the fence.
- Validation: focused lifecycle regression 95 tests and 25 subtests; full Python
  regression 1,301 tests and 99 subtests; `compileall` and `git diff --check`
  clean.
- Acceptance 4/6/8 evidence (`60ef19b`): `ZeroTouchSuccessorHandoffEndToEndTests`
  drives the real Watchdog, TransitionExecutor, ControlCommandCoordinator and
  AIPlannerCoordinator in tick order from the matrix-B fault. The Watchdog
  rebuilds one handoff, the control plane starts the successor Planner
  automatically, `agent/next.md` advances to `P2 READY_TO_RUN` with the frozen
  plan, the authority advances to `P2` keeping `P1` as source, and no owner
  command reaches the control plane. Both legs verified load-bearing by removal.
- Deferred findings N1 and N2 closed (`9629c44`): the Watchdog no longer answers
  the invariant block from an empty ledger, an unambiguous staged predecessor
  claim now wins over an absent roadmap, and a roadmap created by a failed
  reconcile is removed instead of dirtying the tree.
- Full Python regression after the above: 1,306 tests and 99 subtests.
- Remaining closure gate: restart the canonical daemon so it loads the fence,
  then re-run `python ops/p1613_lifecycle_smoke.py` and confirm 35 of 35. Each
  gated project performs at most one more recovery attempt and then fences with
  a stable `gate_id`. The restart was not performed in-session because the
  environment denied stopping the running daemon.

P16.13 implementation evidence (2026-09-26):
- Added one authoritative lifecycle record and source/target/generation transition journal inside the existing transition-executor ledger.
- Fenced successor authority until predecessor Worker/Reviewer/remediation ownership drains and the handoff is durably consumed.
- Added staged predecessor/roadmap consistency repair, centralized lifecycle invariants, Watchdog missing-handoff recovery, restart replay, and exact lineage propagation.
- Added the bounded exactly-one diff-localized remediation extension and the deterministic A-J lifecycle fault matrix.
- Root-cause and authority/projection boundaries are recorded in `docs/P16_13_SUCCESSOR_CONSISTENCY_CONTRACT.md`.
- Focused lifecycle regression: 122 tests and 10 subtests passed. Full Python regression: 1,281 tests and 92 subtests passed. Runtime smoke and independent Technical Review remain closure gates.

P16.12 Web Sol Persistent Pairing & Truthful Availability completion (2026-09-25):
- Implemented persistent pairing, truthful multi-signal availability, probe lifecycle, and failover:
  1. Capability Security Hardening (`src/dev_orchestrator/control/security.py`):
     - Added `CapabilityVerdict` enum (`VALID`, `REVOKED`, `UNKNOWN`, `UNAVAILABLE`) and `CapabilityStoreUnavailableError`.
     - Hardened store loading against I/O, malformed JSON, and schema errors; mutation methods fail closed without overwriting unreadable files.
     - Added `capability_state`, `capability_status`, `capability_identity`, and `renew_session_capability` methods on `ControlSecurity` and top-level functions.
  2. Control Store Session Pairing (`src/dev_orchestrator/control/store.py`):
     - Added `capability_id` and `capability_source` parameters to `heartbeat(...)`.
     - Persists `capability_pairing_id`, `capability_source`, `verified_at` per session/tab.
     - Extended `session_status(...)` and `list_sessions(...)` with `active_tab_count`, `stale_tab_count`, `verified` flag, and capability pairing metadata.
  3. Browser Bridge Durable Cancellation & Maximum Claim Lifetime (`src/dev_orchestrator/bridge/store.py`):
     - Added `_DEFAULT_MAX_CLAIM_LIFETIME_SECONDS = 900` (15 minutes) and `WithdrawResult` dataclass (`outcome`, `request_id`, `binding_id`, `adapter`, `cancel_deadline`, `reason`).
     - Clamped claims and renewals to `claim_deadline_at`.
     - Implemented `withdraw(...)` (tombstone for pending, atomic `cancel_requested_at` and `cancel_deadline` for active, finalized `withdrawn` after lease expiry).
     - Implemented `discard_probe(adapter, binding_id, request_id, nonce)` accepting only exact reserved `probe:` request IDs.
  4. Web Sol Truthful Health Evaluation & Store (`src/dev_orchestrator/core/websol_health.py`):
     - Implemented `WebSolAvailability` enum (`AVAILABLE`, `DEGRADED`, `OFFLINE`, `PAIRING_REQUIRED`, `PROBE_FAILED`).
     - Implemented `WebSolSignal`, `WebSolHealth`, `health_key`, `probe_bridge_listener(...)`, `collect_websol_signals(...)` covering all 6 independent signals (`bridge_listener`, `browser_claim_presence`, `control_heartbeat`, `binding_identity`, `capability`, `probe`).
     - Implemented deterministic fail-closed `evaluate_websol_availability(...)` and `WebSolHealthStore` under atomic writes and `InterProcessFileLock`.
  5. Deterministic End-to-End Inference Probe (`src/dev_orchestrator/core/websol_probe.py`):
     - Implemented `run_websol_probe(...)` with timeout, failure classification, marker verification, and automatic discard.
     - Isolated probes from workflow decisions: Response Consumer (`src/dev_orchestrator/core/response_consumer.py`) skips reserved probe requests from entering decisions ledger.
  6. Web Sol Occurrence Failover Engine (`src/dev_orchestrator/core/websol_failover.py`):
     - Implemented `FailoverState`, `FailoverDecision`, `FailoverRecord`, `evaluate_failover_decision(...)`, `WebSolFailoverStore`, and `FailoverEngine.reconcile(...)` with prepared work grace period and bounded attempt capping.
  7. Direct Reviewer Failover Integration (`src/dev_orchestrator/core/ai_reviewer.py`):
     - Implemented `submit_failover_review(project_id, run_id, failover_record_id, policy=None)` anchored to `ai_review:<run_id>` without AGY acquisition.
  8. Dispatcher Truthful Availability Gating (`src/dev_orchestrator/core/dispatcher.py`):
     - Added `availability_provider` parameter to `dispatch_worker_done_events` and `_dispatch_one`.
     - Gated delivery on `WebSolAvailability.AVAILABLE.value` via `_delivery_allowed` when `availability_provider` returns non-None health (preserves backward compatibility when absent/None).
     - Blocked bridge submission if `prepared.get("failover_state")` is set.
  9. Control Web Server Endpoints & Userscript (`src/dev_orchestrator/web/server.py`, `browser/chatgpt-web-adapter.user.js`):
     - Heartbeat returns `{session, websol_health}` and 401 with reason codes for revoked/unknown capability, 503 for store unavailable.
     - Exposed `POST /v1/capability/renew` and `GET /v1/websol/health` endpoints.
     - Userscript updated to store structured capability record, retain token on transport/503 errors, and compute badge status demoting from authoritative health snapshot.
  10. Daemon Web Sol Coordination (`src/dev_orchestrator/daemon.py`):
      - Wired Web Sol health evaluation, failover reconciliation, probe execution with prerequisites check, and truthful availability provider into orchestration tick.
  11. CLI Commands (`src/dev_orchestrator/cli.py`):
      - `websol-status`: Outputs JSON or formatted table of 6 signals and availability for project bindings.
      - `websol-probe`: Triggers on-demand inference probe and prints result.
- Round 1 Technical Review Remediation (2026-09-25):
  1. Availability Evaluation & Generation Alignment (`src/dev_orchestrator/core/websol_health.py`, `src/dev_orchestrator/daemon.py`):
     - Updated `evaluate_websol_availability` so probe parameters (`probe_passed`, `probe_generation`, `current_probe_generation`) fall back safely to details in `signals["probe"]` instead of defaulting to `False`/`1`.
     - In `daemon.py`, passed actual probe generation, store current generation, and probe outcome from `health_store.get_probe_info` into availability evaluation.
  2. Browser Adapter Pure State Machine (`browser/chatgpt-web-adapter.user.js`):
     - Added `DEGRADED: 2` state rank and badge color (`#b25e00`).
     - Fixed case-normalization in `computeAdapterState`. Missing, expired, mismatched, duplicate tabs, or stale heartbeat states demote to `DEGRADED`; `OFFLINE` or `PROBE_FAILED` demote to `OFFLINE`; `PAIRING_REQUIRED` demotes to `PAIRING_REQUIRED`.
     - Strictly required authoritative unexpired `AVAILABLE` snapshot to promote `LIVE`; prevented `claimPhase` (`waiting`/`claimed`) from masking non-AVAILABLE states.
     - Exported `applyComputedState` and `setAdapterStatus` on `adapterApi`, routing 204, claim errors, heartbeat responses, response acks, renew retries, and claim abandons through pure state transitions.
  3. Web Server Serialization & Security (`src/dev_orchestrator/core/websol_health.py`, `src/dev_orchestrator/web/server.py`):
     - Added `WebSolHealth.to_dict()` for clean serialization of health records and nested signal dataclasses, resolving `AttributeError` on `GET /api/v1/control/websol-health`.
     - Added `_owner_authorized()` authentication gate to `GET /api/v1/control/websol-health`.
     - Removed blanket `Access-Control-Allow-Origin: https://chatgpt.com` on GET routes and removed `/api/v1/control/websol-health` from ChatGPT OPTIONS preflight allowlist.
  4. Capability Signal Status Disambiguation (`src/dev_orchestrator/core/websol_health.py`):
     - Emitted `no_active_tabs` as `status="no_active_tabs"` (not `"unknown"`) so it does not falsely trigger `PAIRING_REQUIRED`.
     - Emitted legacy unverified capability as `status="unverified"` (not `"degraded"`) so `evaluate_websol_availability` correctly classifies it as `DEGRADED` and never `AVAILABLE`.
  5. Dispatcher Truthful Availability Gating (`src/dev_orchestrator/daemon.py`, `src/dev_orchestrator/config.py`, `docs/PORTABLE_PROJECT_INTEGRATION.md`):
     - In `_daemon_availability_provider`, defaulted truthful availability gating to active for `browser_bridge` transport when `health_store` is present, or when `require_truthful_availability` is set. Documented and validated `require_truthful_availability` on conversation binding schema.
  6. Durable Cancellation Nonce Verification (`src/dev_orchestrator/bridge/store.py`):
     - Enforced request nonce verification in `BrowserBridgeStore.withdraw()`, raising `BridgeConflictError` on nonce mismatch.
  7. CLI Probe Recording & Incident Evidence (`src/dev_orchestrator/cli.py`, `src/dev_orchestrator/daemon.py`):
     - `cmd_websol_probe` records probe execution results directly into `health_store`.
     - Incident evidence serialization in `daemon.py` iterates `health.signals.values()` instead of dict keys.
- Round 2 Technical Review Remediation (2026-09-26):
  1. WebSol Health & Probe Daemon Import Correction (`src/dev_orchestrator/daemon.py`):
     - Fixed `NameError` in `_run_orchestration_tick` where `utc_now()` and `timedelta` were called during `WebSolHealth` construction without being imported. Added missing imports (`logging`, `datetime`, `timedelta`, `timezone`, `parse_utc`, `utc_now`, `utc_now_iso`) and `logger = logging.getLogger(__name__)`.
     - Replaced broad `except Exception: pass` with explicit exception logging via `logger.exception(...)` so health evaluation and probe scheduling failures are visible.
  2. WebSol Generation Lifecycle & Invalidation (`src/dev_orchestrator/core/websol_health.py`, `src/dev_orchestrator/daemon.py`, `src/dev_orchestrator/control/store.py`, `src/dev_orchestrator/control/security.py`, `src/dev_orchestrator/web/server.py`):
     - Wired generation invalidation on:
       - Daemon startup (`daemon.py`).
       - Project binding synchronizer (`health_store.sync_bindings(current_bindings)` in `daemon.py` and `websol_health.py` bumps generation on route change or removal).
       - Static and dynamic conversation rebind/bind/unbind and heartbeat capability pairing changes (`control/store.py`).
       - Session capability pairing redemption and revocation (`control/security.py`).
       - Web server pairing redemption endpoint (`POST /api/v1/control/adapter-pairings/redeem`).
     - In `WebSolHealthStore.invalidate_generation`, cleared `probe_backoff` so fresh probes can run immediately after invalidation.
     - In `WebSolHealthStore.should_probe`, immediately trigger probe when current generation exceeds recorded probe generation.
  3. Hard Attempt Cap & Missing Capability Availability Classification (`src/dev_orchestrator/core/websol_health.py`):
     - Enforced `probe_consecutive_failures < DEFAULT_PROBE_MAX_ATTEMPTS` in `should_probe` so background probes cease after maximum consecutive failures.
     - Updated `evaluate_websol_availability` so missing/unknown capability signals (`capability_missing`, `capability_unknown`) demote to `DEGRADED` rather than falsely escalating to `PAIRING_REQUIRED`.
  4. Truthful Availability Gating Bypass & Option Preservation (`src/dev_orchestrator/daemon.py`, `src/dev_orchestrator/control/binding_resolver.py`, `tests_py/test_worker_done_daemon_integration.py`, `tests_py/test_response_consumer_daemon_integration.py`):
     - In `binding_resolver.py`, preserved `conversation_binding` extra options (such as `require_truthful_availability` and `require_availability`) across static and runtime project resolution.
     - In `daemon.py`, skipped Web Sol health evaluation, probe scheduling, and binding synchronization when `require_truthful_availability` or `require_availability` is explicitly `False`.
     - Added `"require_truthful_availability": False` to P13 legacy integration test configs testing basic BrowserBridge dispatch without control plane/Tampermonkey userscripts.
- Round 3 Technical Review Remediation (2026-09-26):
  1. Authoritative-Unknown Capability vs Missing Capability Classification (`src/dev_orchestrator/core/websol_health.py`):
     - Corrected `evaluate_websol_availability` so authoritative unknown capability (`CapabilityVerdict.UNKNOWN`, status `"unknown"`, reason `"capability_unknown"`) properly classifies as `PAIRING_REQUIRED`, restoring incident emission and contract compliance.
     - Preserved missing capability signal (`status="missing"`, reason `"capability_missing"`) demotion to `DEGRADED`.
  2. Realistic Default & Configurable Probe Timeout (`src/dev_orchestrator/core/websol_probe.py`, `src/dev_orchestrator/daemon.py`, `src/dev_orchestrator/config.py`, `src/dev_orchestrator/control/binding_resolver.py`):
     - Added `DEFAULT_PROBE_TIMEOUT_SECONDS = 900.0` aligned to the 15-minute browser wait deadline.
     - Made probe timeout configurable via `conversation_binding` (`probe_timeout_seconds` or `probe_timeout`), preserved across runtime binding resolution, and validated in project config normalization.
     - Wired daemon tick background probe execution to use the configured timeout or the 900.0s deadline default instead of hardcoded 10.0s.
  3. Bounded Time-Based Attempt Cap Reset (`src/dev_orchestrator/core/websol_health.py`, `src/dev_orchestrator/daemon.py`):
     - Added `DEFAULT_PROBE_FAILURE_RESET_SECONDS = 900.0` (15 minutes).
     - In `WebSolHealthStore.probe_consecutive_failures` and `should_probe`, added bounded time-based reset: if `last_failure_at` is older than `reset_seconds`, consecutive failures reset to 0 and probe attempts resume.
     - Allowed `probe_reset_seconds` / `probe_failure_reset_seconds` override on `conversation_binding`.
  4. In-Flight Probe Guard & Non-Blocking Hardening (`src/dev_orchestrator/core/websol_health.py`, `src/dev_orchestrator/daemon.py`, `src/dev_orchestrator/bridge/store.py`):
     - Added `is_probe_in_flight`, `mark_probe_in_flight`, `clear_probe_in_flight` to `WebSolHealthStore`, preventing concurrent/overlapping daemon ticks from launching duplicate probe requests.
     - Fixed `BrowserBridgeStore.list_bindings` so adapters requiring percent-encoding (e.g. `custom/adapter`) are discovered in the no-adapter discovery path.
- Verification:
  - 32 dedicated unit and integration tests in `tests_py/test_p1612_websol_pairing_and_availability.py` passing 100% (added 5 focused remediation tests covering authoritative unknown capability, time-based attempt cap reset, in-flight guard, percent-encoded adapter discovery, and daemon configurable probe timeout).
  - 64 tests in adjacent suites (`test_chatgpt_web_adapter.py`, `test_daemon_transition_integration.py`, `test_worker_done_daemon_integration.py`, `test_response_consumer_daemon_integration.py`, `test_p12_control_foundation.py`, `test_p13_web_bridge_adapter.py`, `test_p13_control_logs.py`, `test_p15_acceptance.py`, `test_p15_mobile_gateway.py`) passing 100%.
  - Full repository regression: 1270 passed in pytest (100% pass, 0 failures).
  - Python compilation (`compileall src ops tests_py`) and `git diff --check` clean.
  - Knowledge graph updated via `graphify update .` (6382 nodes, 17865 edges, 279 communities).
- Final Independent Technical Review Acceptance and Successor Handoff (2026-09-26):
  - Independent Technical Review (`ai_review:ai_review:rereview:p1612-final-rereview2-7a4addb`) on clean HEAD `894f5ee` accepted all P16.12 deliverables with `decision: "next"`, `next_action: "next_task"`, and `disposition: "apply"`. Confirmed both blocking findings closed with zero remaining blockers.
  - Closed residual non-blocking observation in `src/dev_orchestrator/core/websol_health.py` by ensuring `InterProcessFileLock` is held during consecutive failure reset in `WebSolHealthStore.probe_consecutive_failures`.
  - Normalized `agent/staged/P16.13.md` to LF-only UTF-8 without BOM and set `Status: **PENDING DESIGN**`, resolving `RoadmapResult.kind == "successor"` for `read_successor`.
  - Advanced `agent/next.md` to P16.13 (`Status: **PENDING DESIGN**`) and updated `agent/execution-state.json` to `pending_design` bound to task `P16.13`.
  - Verification: 32 dedicated P16.12 tests, 73 focused tests (`test_p1612_websol_pairing_and_availability.py`, `test_staged_roadmap.py`, `test_staged_handoff.py`), and full repository regression (1270 passed, 92 subtests passed in 492.85s) passing 100%.
  - Python compilation (`compileall src ops tests_py`) and `git diff --check` clean.
  - Knowledge graph updated via `graphify update .` (6376 nodes, 17860 edges, 256 communities).
- Round 4 Technical Review Remediation (2026-09-26):
  1. Lock-Order Inversion Resolution (`src/dev_orchestrator/core/websol_health.py`):
     - Corrected lock acquisition hierarchy in `WebSolHealthStore.probe_consecutive_failures` from `self._lock -> InterProcessFileLock` to `InterProcessFileLock -> self._lock`, matching all other mutating operations (`invalidate_generation`, `sync_bindings`, `put`, `record_probe`) and eliminating the deadlock between the HTTP server `/redeem` thread and daemon tick probe scheduling.
  2. Write Amplification & Stale Backoff Prevention (`src/dev_orchestrator/core/websol_health.py`):
     - In `WebSolHealthStore.probe_consecutive_failures`, cleared `last_failure_at` and `next_allowed_at` to `None` upon consecutive failure reset, and guarded reset on `consecutive > 0`. Prevents repeated disk writes on every tick for expired failures and unblocks `can_probe`.
  3. Verification & Regressions (`tests_py/test_p1612_websol_pairing_and_availability.py`):
     - Added `test_concurrent_invalidate_generation_and_should_probe_no_deadlock` verifying concurrent `invalidate_generation` and `should_probe` loops on a shared store instance terminate cleanly without deadlocks.
     - Added `test_probe_consecutive_failures_reset_clears_timestamps_single_write` verifying failure timestamps are cleared and subsequent calls to `probe_consecutive_failures` and `should_probe` do not perform duplicate disk writes.
     - 34 dedicated P16.12 tests passing 100%.
     - 64 adjacent suite tests passing 100%.
     - Full repository regression: 1272 passed in pytest (100% pass, 0 failures in 460.46s).
     - Python compilation (`compileall src ops tests_py`) and `git diff --check` clean.
     - Knowledge graph updated via `graphify update .` (6382 nodes, 17871 edges, 277 communities).



P16.11 AGY-First AI Resource Pool Benchmark & Routing completion (2026-09-25):
- Delivered the AGY-first AI resource pool, real-time availability/quota telemetry, reviewer independence enforcement, same-failure heterogeneous escalation, and representative replay benchmark:
  1. AGY Resource Pool (`src/dev_orchestrator/pool/agy_pool.py`, `src/dev_orchestrator/pool/models.py`):
     - Represents all 3 AGY accounts (`agy-1`, `agy-2`, `agy-3`) as a shared quota- and time-window-constrained compute pool.
     - Tracks real-time availability, sliding-window utilization (60 requests / 900s), active concurrency (max 1 in-flight per account), dynamic quota degradation (`HEALTHY` -> `CONSERVE` -> `LOW` -> `EXHAUSTED`), and cooldown triggers/expiry.
     - Implements load-balanced parallel dispatch preferring least-loaded and least-recently-used accounts across independent work.
  2. AGY-First Deterministic Routing Policy (`src/dev_orchestrator/pool/routing_policy.py`):
     - Prioritizes near-zero marginal-cost AGY accounts for eligible roles (`planner`, `worker`, `debugger`, `evidence_packaging`).
     - Reviewer Independence Invariant: Enforces strict provider-level separation when `independence="provider"` (such as technical review or candidate regression promotion when worker was `agy`). Bypasses AGY entirely and routes to independent models (`claude/default/opus` or `codex/default/gpt-5.6-sol`). Allows cross-account review within AGY when `independence="account"`.
     - Same-Failure Retry Barrier: Normalized semantic error hashing (`FailureSignature`). When an attempt on AGY fails with signature S, the policy strictly blocks blind rotation to `agy-2` or `agy-3` without a strategy change, immediately triggering heterogeneous escalation to paid baseline models (`codex/default/gpt-5.6-sol`). When strategy is changed (`strategy_changed=True`), allows an adapted second attempt on an alternate AGY account before escalating.
     - Pool exhaustion and cooldown fallback: Automatically spills over to heterogeneous baseline when all AGY capacity is saturated.
  3. Canonical Representative Replay Benchmark Suite (`src/dev_orchestrator/pool/replay_benchmark.py`):
     - 5 canonical historical replay tasks covering all primary DevO role classes (`replay_planner_arch`, `replay_worker_cache`, `replay_debugger_root_cause`, `replay_evidence_packaging`, `replay_reviewer_compat`).
     - Independent review evaluation gate (`evaluate_independent_review`) verifying required semantic findings and strict provider independence.
     - Computes AGY coverage, first-pass acceptance, heterogeneous escalation rate, repeated-failure rate, median/p95 wall time, and matched Non-AGY baseline comparative cost metrics.
  4. CLI Commands (`src/dev_orchestrator/cli.py`):
     - `pool-status`: Outputs JSON snapshot of total/available accounts, cooldown states, and rolling window counters.
     - `agy-benchmark`: Executes representative benchmark replay, supporting `--simulate-failure <task_id>`.
     - `agy-route`: Evaluates machine-readable routing decisions with `--role`, `--independence`, `--worker-provider`, `--failure-signature`, `--strategy-changed`.
  5. Authoritative Policy Document (`docs/AGY_ROUTING_POLICY.md`):
     - Documents architectural principles, pool telemetry semantics, capability boundary matrix (`SUPPORTED`, `UNSUPPORTED`, `UNCERTAIN`), same-failure retry rules, reviewer independence guarantees, and empirical benchmark findings.
- Verification:
  - 21 focused unit and integration tests across 4 dedicated suites passing 100%:
    - `tests_py/test_p1611_agy_pool.py` (5 tests)
    - `tests_py/test_p1611_routing_policy.py` (6 tests)
    - `tests_py/test_p1611_replay_benchmark.py` (4 tests)
    - `tests_py/test_p1611_cli.py` (6 tests)
  - Full repository regression: 1233 passed, 92 subtests passed in 468.83s (0 failures).
  - Python compilation (`compileall`) and `git diff --check` clean.
  - Knowledge graph updated via `graphify update .` (6181 nodes, 17201 edges, 255 communities).

P16.10 Automatic Failure Harvesting & Regression Promotion completion (2026-09-25):
- Implemented restart-safe incident packet store under `runtime/incident-packets/`:
  - Atomic commit protocol via `TransactionIntent` with single-point-of-commit index mutation and automatic reconciliation on open/tick (`committed_confirmed`, `reapplied`, `superseded_txn`, `payload_unverified`).
  - Strict append-only family recurrence: duplicate fingerprints increment recurrence count and append bounded evidence references without creating duplicate packets.
  - Fail-closed corruption quarantine with degraded memory state.
  - Store durability & locking: `_load_index_failclosed` isolates IO/OS read errors without deleting or quarantining valid `index.json`. `reconcile_journal` checks index references before moving superseded transaction side-files to `orphans/`, preserving live packets and candidates. Process-local reentrancy added to `InterProcessFileLock`.
- Semantic incident fingerprinting (`src/dev_orchestrator/incidents/fingerprint.py`):
  - Normalized semantic failure hashing strictly over allowlisted semantic fields; volatile keys (timestamps, ages, UUIDs, PIDs, paths) rejected fail-closed.
- Automated failure harvesting detectors in `harvest_tick` (`src/dev_orchestrator/incidents/harvesting.py`):
  - 8 named detectors: `watchdog_recovery`, `actionable_execution_loss`, `unconsumed_plan_or_review`, `launch_gap`, `restart_reconcile_outcome_change`, `recovery_cycle_exhaustion_or_livelock`, `control_only_intervention`, and `orchestrator_alive_task_stalled`.
  - Guarded invocation in daemon tick (`src/dev_orchestrator/daemon.py`).
  - CONTROL_ONLY owner attribution strictly requires proven forward progress in `after` snapshot; falls back to `UNCONFIRMED_PROGRESS` otherwise.
- Fail-closed owner-gate authority resolution (`src/dev_orchestrator/incidents/owner_gate.py`):
  - Surfaces `latest_owner_gate` from watchdog or planner ledgers, evaluates `OwnerControlStore` pause state, and resolves pending gate authority.
- 3-Role liveness resolution and diagnosis precedence (`src/dev_orchestrator/incidents/liveness.py`, `src/dev_orchestrator/core/diagnostics.py`):
  - Resolves Worker, Planner, and Reviewer liveness independently.
  - Coordinator returns `unknown` when coordinator object or heartbeat is unreadable/missing even if ledger is idle.
  - Diagnosis precedence guarantees pre-existing P16.7–P16.9 failure modes (`worker_vanished`, `execution_record_disappeared`, `running_without_provider_output`, `inconsistent_active_state`, `agent_stalled`) take precedence over `orchestrator_alive_task_stalled`.
- Progress obligation resolution (`src/dev_orchestrator/incidents/obligations.py`):
  - Differentiates legal wait states (pauses, declared external waits, startup grace, pending owner gates) from silent stalls. Terminal states (`DONE`, `COMPLETED`, `TERMINAL`) marked as legal wait with no pending progress obligation.
- Truthful task status and activity distinction (`src/dev_orchestrator/core/project_status.py`):
  - Exposes `system_alive`, `task_active`, `task_progressing`, and `incident_metrics` (`control_only_interventions`, `target_control_only_interventions: 0`, `incidents_captured`, `recurrence_count`, `candidates_generated`, `promotions_settled`).
  - Normalizes `last_task_activity_at` separately from `last_meaningful_progress_at`; daemon heartbeats and watchdog self-writes do not count as meaningful task progress.
- Operator-invoked, destination-free staged candidate regression promotion pipeline:
  - Candidates synthesized in runtime store (`generate_candidate`), materialized strictly into git-excluded `tests_candidate/` of verified DevOrchestrator owner (`materialize_candidate`).
  - Real executable candidate gates (`run_executable_candidate_gates`): executes synthetic test with `DEVORCH_FIXTURE_MODE=failing` asserting `AssertionError`, executes with `DEVORCH_FIXTURE_MODE=corrected` asserting exit code 0, 3-run stability loop, AST isolation gate forbidding unauthorized imports, and store + `tests_py/` deduplication. Real deterministic unittest test code generated.
  - Immutable `pre_review_digest` computed solely over candidate content SHA256 and the 5 executable gate results.
  - Independent review gate binds to `pre_review_digest` without modifying executable snapshots.
  - Promotion (`promote_candidate`) executes under `store.lock`, checks `promotion_blocked` flag, verifies initial status hash, and safely rolls back and unlinks targets on failure. If disk residue or status mismatch persists, sets `promotion_blocked: True` and records residue in owner-gate (`failed_dirty`).
- New CLI commands in `src/dev_orchestrator/cli.py`:
  - `incident-list`, `incident-show`, `candidate-list`, `candidate-evaluate`, `candidate-review`, `candidate-materialize`, `candidate-promote`.
- Repreoduced 2026-09-25 false-running incident regression:
  - Fresh heartbeats with `REVIEW_FAILED` and dead roles correctly diagnosed as `orchestrator_alive_task_stalled`, status reflects `system_alive=True`, `task_active=False`, `task_progressing=False`, and enters bounded recovery.
- Technical Review Remediation Round 2 (2026-09-25):
  - Closed candidate synthesis gap: Replaced inline generator in `src/dev_orchestrator/incidents/capture.py` with `generate_candidate(...)`, generating deterministic unittest test cases with real failing/corrected fixture oracles and accurate content SHA-256 matching. Fixed Windows CRLF newline conversions by explicitly passing `newline="\n"` on all candidate and side-file writes.
  - Closed fail-closed role liveness gap: Updated `_check_worker_liveness` in `src/dev_orchestrator/incidents/liveness.py` to accept executor as dict or `.state()` callable, inspecting active executions. Made `_check_worker_liveness` fail closed to `alive: None, state: 'unknown'` when no sources are readable, rather than falsely declaring worker idle/dead.
  - Closed store mutation on degraded state: Added fail-closed checks in `execute_txn` and `reconcile_journal` in `src/dev_orchestrator/incidents/store.py` to raise `RuntimeError` before mutating when `is_degraded` is true, preventing degraded memory state from overwriting index files and wiping families or `promotion_blocked`.
  - Closed isolation gate under-enforcement: Expanded `_FORBIDDEN_MODULES` in `src/dev_orchestrator/incidents/evaluation.py` to include `subprocess`, `urllib`, `serial`, `http`, etc. Added AST call analysis for `_FORBIDDEN_OS_CALLS` (`os.system`, `os.popen`, etc.) and `_FORBIDDEN_BUILTIN_CALLS` (`eval`, `exec`, `__import__`).
  - Strengthened false-running regression: Replaced mock object with real `evaluate_stall`, real `WatchdogCoordinator.advance`, and `build_project_status` status remap to `STALLED`. Populated `"stall"` in `_watchdog_view` and separated `task_active` from `task_progressing`.
  - Hardened Windows file locking: Added `_normalize_lock_key` in `src/dev_orchestrator/accounting/events.py` to strip Windows `\\?\` and `\\?\UNC\` extended path prefixes from resolved lock paths, preventing process-local `RLock` aliasing between pre-existing and newly-created files.
  - Deduplicated detector registration in `src/dev_orchestrator/incidents/harvesting.py` and replaced `except TypeError` in `src/dev_orchestrator/daemon.py` with `inspect.signature` inspection.
- Technical Review Remediation Round 3 (2026-09-25):
  - Closed Finding 1 (Stall detector fail-closed telemetry staleness & false-running regression failure):
    - In `src/dev_orchestrator/incidents/harvesting.py`, updated `_detect_orchestrator_alive_task_stalled` to derive activity age from multiple telemetry metrics (`telemetry.watchdog_safe_activity_age_seconds`, `last_activity_age_seconds`, `activity.watchdog_safe_activity_age_seconds`, `activity.age_seconds`), or fallback timestamp age difference against `now` / `utc_now()`. Inspected `task_active` from both top-level and nested `activity`.
    - In `tests_py/test_p1610_false_running_regression.py`, aligned snapshot fixture with `task_active: False` and `telemetry` age metrics, passed `now=eval_now` to `harvest_tick`, and added `test_stalled_detector_accepts_absent_age_as_stale_by_timestamp` verifying fallback timestamp age derivation without numeric telemetry.
  - Closed Finding 2 (Sticky Reviewer Owner Gate & Lifecycle Override):
    - In `src/dev_orchestrator/control/surface.py`:
      - Updated `latest_owner_gate` to import `_latest` from `lifecycle_projection`, compute consumed reviews from `rereview_of` references, check the latest review via `_latest(reviews, project_id)`, and require unconsumed `completed` status with `decision == "owner_gate"`.
      - Scoped `OWNER_GATE` lifecycle override in both `project_identity` and `project_control_view` to exclude active running lifecycles (`EXECUTING`, `REVIEWING`, `PLANNING`, etc.) and terminal states (`DONE`, `COMPLETED`, `TERMINAL`).
    - In `tests_py/test_p1610_owner_gate_and_attribution.py`:
      - Added 4 dedicated regression tests:
        - `test_reviewer_owner_gate_recognized`
        - `test_reviewer_owner_gate_not_sticky_after_later_next_review`
        - `test_reviewer_owner_gate_not_sticky_when_consumed_by_rereview`
        - `test_reviewer_owner_gate_scoped_out_for_executing_and_terminal`
- Verification:
  - 68 focused tests across 8 P16.10 test modules passing 100%.
  - Adjacent suites: 43 passed (`test_lifecycle_projection.py`, `test_p169_*.py`, `test_control_commands.py`).
  - Watchdog suites: 119 passed, 5 subtests passed (`test_watchdog*.py`).
  - Coordinator and CLI suites: 117 passed, 18 subtests passed.
  - Full repository regression: 1209 passed, 92 subtests passed in 476.57s (0 failures).
  - Candidate test collection isolation verified (0 tests collected without `DEVORCH_CANDIDATE_TESTS=1`).
  - `compileall` and `git diff --check` clean.
  - Knowledge graph updated via `graphify update .` (6000 nodes, 16811 edges, 260 communities).

P16.9 Watchdog Execution-Loss Detection & Recovery completion (2026-09-24):
- Solved the xray-hw-platform incident blind spot: an accepted execution reached WORKER_RUNNING, emitted no provider output, and vanished with the project returning to READY_TO_RUN without a terminal state, while watchdog previously reported state=ok with zero recovery.
- Enforced core invariant: Every accepted execution that reaches launch/running must have a durable terminal outcome: `accepted -> launched/running -> {completed | failed | cancelled | explicitly_reconciled}`.
- Durable Execution Lineage (`src/dev_orchestrator/core/execution_lifecycle.py`):
  - Created `runtime/execution-lineage.json` (schema 1) protected under `InterProcessFileLock` on `execution-lineage.lock`.
  - Implemented `open_execution_obligation` as a fail-closed pre-actuation barrier: atomically records launch obligation with git anchor, status hash, engine handle, and read-back integrity verification before worker thread/broker dispatch.
  - Fail-closed quarantine on corruption/unsupported version (`execution-lineage.json.corrupt-<stamp>-<hash>`) forcing non-ok health (`LINEAGE_STORE_DEGRADED`).
  - Implemented `record_execution_observation` and `close_lineage_record` maintaining audit history and terminal outcomes.
- Two-Phase Compare-and-Set Reconciliation (`src/dev_orchestrator/core/transition_executor.py`):
  - Added additive terminal state `explicitly_reconciled` to `_TERMINAL_STATES` (distinguished from successful completion).
  - Implemented `TransitionExecutor.reconcile_execution_loss` under `self._lock`: validates fresh conclusive death evidence, exact launch anchor, absence of conflicting active executions, and compare-and-sets active/disappeared rows to `explicitly_reconciled` tombstone with full audit evidence.
  - Handled completion races: preserves genuine completed/failed/cancelled outcomes without overwriting and suppresses retries.
- Watchdog Execution Loss Reconciler & Recovery (`src/dev_orchestrator/core/watchdog.py`):
  - Hooked `observe_executions` before no-progress/activity short-circuits. Adopts active rows and tracks invariants: `WORKER_VANISHED_WITHOUT_TERMINAL_STATE`, `EXECUTION_RECORD_DISAPPEARED`, `RUNNING_WITHOUT_PROVIDER_OUTPUT`, `INCONSISTENT_ACTIVE_STATE`.
  - Multi-probe liveness resolution in `resolve_execution_liveness` (ledger claim, broker request status, PID + started_at identity, fresh output, active claims). Requires conclusive death proof with no live proof before recovery.
  - Two-phase safe recovery: reserves slot `wd-xl-<invariant_key>`, calls `reconcile_execution_loss`, reloads executor state, re-verifies strict `_has_active_execution` (deferring enqueue as `reconciled_pending_retry` if snapshot is stale), and enqueues idempotent continue command.
  - Crash-boundary resumption: preserves `execution_loss_slots` across restarts and resumes phase with the same command ID.
  - Health enforcement: watchdog health remains non-ok (`status: "execution_loss_detected"`) while any unresolved invariant exists.
  - Resolved findings upon replacement execution reaching terminal outcome or original genuine completion.
- Diagnostics, Blockers, & Status:
  - Added `EXECUTION_LOSS_UNRESOLVED` blocker in `src/dev_orchestrator/core/blockers.py`.
  - Registered milestones: `EXECUTION_LOSS_DETECTED`, `EXECUTION_LOSS_RECOVERY_STARTED`, `EXECUTION_LOSS_ESCALATED`, `EXECUTION_LOSS_RESOLVED`.
  - Exposed `execution_loss`, `execution_loss_slots`, `unresolved_invariants`, and `reconciliation_phase` in `src/dev_orchestrator/core/project_status.py`.
- Full Verification & Technical Review Remediation (2026-09-24):
  - Technical Review remediation closed all 5 blockers (B1: durable finding write-back via `update_finding_state(...)`; B2: replacement execution lineage linkage via `start_control` and `observe_executions` correlation; B3: `RUNNING_WITHOUT_PROVIDER_OUTPUT` strictly diagnostic-only; B4: broker status `unknown`/unavailable reclassified as `LIVENESS_UNKNOWN` with broker queries restricted to `aibroker`; B5: handled `blocked`/`recovery_required` rows in `reconcile_execution_loss`) plus non-blocking items (a-d: milestone emission, quarantine deduplication, unmutated write avoidance, and watchdog error reset).
  - Added 5 new regression tests in `tests_py/test_p169_watchdog_execution_loss.py`.
  - 23 focused tests passing 100% across `test_p169_*.py`.
  - 130 watchdog regression tests passing (`test_watchdog*.py`).
  - 147 transition executor and P16.7–P16.9 regression tests passing.
  - Full test suite: 1141 passed, 92 subtests passed, 0 failures.
  - Clean `compileall` and `git diff --check`.
  - Graphify knowledge graph updated (`5722 nodes, 16076 edges, 263 communities`).

P16.8 DevO Golden-Path Lifecycle Hardening completion (2026-09-24):
- Delivered the Golden-Path lifecycle guarantee: proved that a fresh, owner-authorized task moves from PENDING_DESIGN to DONE under a single owner continue command without manual lifecycle repair.
- Authoritative Task State Precedence (3-rule contract in src/dev_orchestrator/core/readiness.py): valid structured readiness is authoritative, absent falls back to Markdown, invalid/stale/mismatched fails closed; Markdown disagreement is diagnostic-only.
- Centralized canonical task status parsing, predicates, and editing in src/dev_orchestrator/core/task_status.py, eliminating raw substring/regex status checks across production modules (enforced via static scan regression).
- Durable ExecutionContext schema version 1 in runtime/execution-context.json protected by InterProcessFileLock under execution-context.lock, with git-anchor revalidation, stale detection (EXECUTION_CONTEXT_STALE), and idle continuation faulting (CONTINUATION_FAULT) with deterministic redispatch command IDs.
- Structured actionable artifact error payloads on schema/item violations (field, expected, actual, correction, actionable_message).
- Terminal DONE action closure (continue, retry, rereview, reconcile disabled).
- 9-field additive activity telemetry exposed in project_status.py and control/surface.py.
- Registered progress milestones: CONTINUATION_DISPATCHED, CONTINUATION_FAULT, CONTINUATION_HOLD, EXECUTION_CONTEXT_STALE.
- Contract document authored in docs/P16_8_GOLDEN_PATH_LIFECYCLE_CONTRACT.md and backlog.md updated.
- Full verification: 25 focused tests passed across test_p168_task_status.py, test_p168_golden_path.py, and test_p168_fault_injection.py (including 12 fault-injection tests covering anchor invalidation, failover, bounded review remediation to DONE, empty-blockers indexing guard, and restart); 1118 full repository regression tests passed (0 failures).
- Technical Review Remediation Round 1 (2026-09-24):
  - Hardened golden_path_harness.py with real AIReviewerCoordinator wiring, fail-closed plan apply, complete_worker_run ledger updates, and apply_review_acceptance.
  - Added terminal DONE closure assertions (clean worktree, no active role or running execution, no owner gate, terminal actions disabled).
  - Resolved unbound variables and coordinator background thread joins to eliminate Windows WinError 32 cleanup collisions.
- Technical Review Remediation Round 2 (2026-09-24, ai_review:ai_review:auto-1eac8a332382d1d9e986420f:execute):
  - Guarded top_blocker = blockers[0] in ActivationSupervisor: when disposition is remediate with empty blockers, advances forward transition if next_action is actionable, else holds, eliminating unhandled IndexError crash.
  - Removed unreachable lifecycle_hold branch in activation_supervisor.py.
  - Added regression test test_remediation_disposition_with_empty_blockers_advances_forward_transition in test_p168_fault_injection.py.
  - Full final-tree regression verified: 1118 passed, 92 subtests passed in 402.56s (0 failures); compileall and git diff --check clean.
- Technical Review Closure Remediation Round 3 (2026-09-24):
  - Moved recovery budget evaluation after non-consuming hold exits so legitimate Planner/Reviewer/Worker and lifecycle holds cannot age into false `RECOVERY_BUDGET_EXHAUSTED`.
  - Added deterministic +31 minute lifecycle-hold regression; intent remains active with zero actions used and no exhaustion/livelock blocker.
  - Verification: 179 focused passed + 15 subtests; full repository regression 1118 passed + 92 subtests in 408.30s; compileall and git diff --check clean.

P16.7 Self-Healing Project Activation & Readiness completion (2026-09-23):
- Implemented diagnosed and self-healing project-activation and readiness failure handling without repeated owner interventions, replaying the 2026-09-23 xray-hw-platform incident as an automated end-to-end regression.
- Machine-Readable Readiness Authority (`src/dev_orchestrator/core/readiness.py`):
  - Created `agent/execution-state.json` (schema_version 1) as the authoritative readiness contract, bound to the current task ID. Mismatched, stale, or schema-invalid files fail closed (`READINESS_TASK_ID_MISMATCH`, `READINESS_SCHEMA_INVALID`).
  - Closed legacy component grammar for `agent/next.md` status tokens (e.g. `READY / OWNER_GOAL_DEFINED / NOT_STARTED`), mapping safely to `ready_to_run` migration candidates without altering live regex projection.
  - Implemented `migrate_legacy_readiness` requiring full predicate satisfaction, committing only `agent/execution-state.json`, and appending to append-only audit log `runtime/readiness-migrations.jsonl`.
- Orphan Detection & Bootstrap Registration (`src/dev_orchestrator/core/activation.py`):
  - Implemented `detect_orphan_state` (`ORPHANED_PROJECT_STATE`) identifying local `.devorch/status.json` or runtime project mirrors absent from the active registry.
  - Implemented `record_activation_request` and `load_activation_requests` (`runtime/activation-requests.json`, schema_version 1) as the single bootstrap origin of candidate identity and repo paths.
  - Implemented `reconcile_project_registration` with atomic project appending, profile-based worker configuration (`REGISTRATION_TEMPLATE_MISSING`), uniqueness validation, and `.devorch/` git exclude safeguarding.
- Canonical Structured Blockers (`src/dev_orchestrator/core/blockers.py`):
  - Created canonical `Blocker` dataclass and `explain_block` evaluating registered project IDs or unregistered repo paths across 12+ failure codes.
  - Surfaced canonical blockers across CLI (`project-explain-block`), Web Control API (`GET /api/v1/control/projects/{id}/blockers`, `project_control_view`), Mobile projection, and enriched `project-status` not_found.
- Durable Execution Intent & Supervisor Loop (`src/dev_orchestrator/core/execution_intent.py`, `src/dev_orchestrator/core/activation_supervisor.py`):
  - Created durable `runtime/execution-intent.json` ledger tracking active target execution intents across daemon restarts.
  - Enforced independent recovery budgets (20 actions, 3 identical fingerprints, 30 minutes elapsed) and livelock cycle detection (A -> B -> A -> B), failing closed as `RECOVERY_BUDGET_EXHAUSTED` / `RECOVERY_LIVELOCK_DETECTED` with zero fabricated owner gates.
  - Implemented per-tick `ActivationSupervisor` executing strictly after `watchdog.advance`, consuming watchdog handoffs, evaluating blockers, applying bounded remediations, and re-submitting forward transitions.
- Watchdog & Control Hardening:
  - Added intent-gated watchdog handoff (`recovery_handoff` milestone, status `max_attempts_handed_off`) suppressing non-genuine owner gates when active intent matches.
  - Added idempotent `NOOP_ALREADY_EXECUTING` for duplicate `continue`/`start` commands on active runs.
  - Atomic staged successor activation in `ai_planner.py` writing `pending_design` alongside `agent/next.md` and safe rollback restoring both files.
- Technical Review Remediation Round 1 (`ai_review:bc404b1b-5fd9-4ed8-b11f-729b6fdff6bc:execute`):
  - Fixed unbound `submit_control_command` in `src/dev_orchestrator/core/activation_supervisor.py`: imported and wired `control_commands.submit_control_command`, built safe expected identity fallback when snapshot is None, and used `-rem-` command ID marker to prevent command ID collision when advancing from IDLE to READY_TO_RUN during recovery.
  - Fixed porcelain parsing in `src/dev_orchestrator/core/repository.py`: preserved leading whitespace before index 3 slice (`rstrip("\r\n")`) and added unquoting and rename parsing in `classify_porcelain_entries` so valid workspace changes (e.g. `' M agent/next.md'`) are not truncated into `'gent/next.md'` and misclassified.
  - Added transient infrastructure classification in `src/dev_orchestrator/core/blockers.py`: wired `classify_failure_class` to inspect snapshot errors, broker status, and git timeouts, returning `TRANSIENT_INSPECTION_FAILURE` and `TRANSIENT_GIT_TIMEOUT` with backoff suggestions; added `Sequence` import in `control_commands.py`; allowed optional `recovery_epoch_id` in `command_store.py`.
  - Added dedicated regression coverage: added 9 new tests across porcelain classification, transient backoff, and monitor auto-start racing explicit continue in `tests_py/test_p167_self_healing_activation.py` (25/25 passed).
- Technical Review Remediation Round 2 (`ai_review:ai_review:bc404b1b-5fd9-4ed8-b11f-729b6fdff6bc:execute`):
  - Repaired stale-readiness self-heal path in `src/dev_orchestrator/core/readiness.py`: `resolve_readiness()` now resolves legacy markdown tokens (`_resolve_legacy_markdown`) upon encountering `READINESS_TASK_ID_MISMATCH`, populating `migration_candidate`, `migration_required=bool(...)`, and `raw_token`. In `migrate_legacy_readiness`, predicate 3 successfully validates the candidate for superseded tasks, writes the updated state for the current task, commits to git, and records `superseded_task_id` in `runtime/readiness-migrations.jsonl`. Commit rollback checks out `before_head` for `agent/execution-state.json` when a superseded task ID was present.
  - Applied recovery budgets and elapsed launch timeout on forward path in `src/dev_orchestrator/core/activation_supervisor.py`: no-blocker forward transitions now execute after `check_intent_budgets` and record actions via `record_intent_action`, enforcing the 20 actions budget and the 30-minute elapsed launch timeout.
  - Terminated active execution intents upon worker launch: when worker execution is launching or running (detected via transition executor `_active_execution` or snapshot worker state in `{"starting", "running"}`), supervisor terminates the intent as `state="satisfied"`, `reason="target execution launched"`, eliminating unbounded re-submission of `continue` commands after worker completion.
  - Hardened genuine owner gate and owner pause precedence in `src/dev_orchestrator/core/blockers.py` and `activation_supervisor.py`: updated `_SEVERITY_ORDER` to rank `OWNER_GATE_PRESENT` and `OWNER_PAUSED` at priority 0 (highest severity), ensuring genuine owner gates sort ahead of `TRANSIENT_*` (1), `INSPECTION_FAILED` (2), `ORPHANED_PROJECT_STATE` (3), and `READINESS_*` (7-10). Supervisor evaluates genuine owner gates (`b.code in {"OWNER_GATE_PRESENT", "OWNER_PAUSED"} or b.failure_class == "owner_gate"`) across all blockers, terminating intent as `owner_gate` without burning recovery budgets or attempting spurious readiness migration.
  - Removed unused import `submit_control_command` from line 17 of `activation_supervisor.py`.
  - Added dedicated regression test class `TestP167ReviewRemediation` with 7 test cases in `tests_py/test_p167_self_healing_activation.py` covering stale task ID migration, rollback on commit failure, supervisor migration forward dispatch, elapsed time and action budget exhaustion, worker launch intent satisfaction, and owner pause precedence over readiness mismatches.
- Technical Review Remediation Round 3 (`ai_review:ai_review:ai_review:bc404b1b-5fd9-4ed8-b11f-729b6fdff6bc:execute`):
  - Fixed TransitionExecutor decision actuation during post-worker handoff in `src/dev_orchestrator/core/transition_executor.py`:
    - Extended `_readiness_allows_launch`, `_next_task_ready`, and `_fresh_guard` with `anchor_task_id` and `predecessor_task_id` keyword arguments.
    - Exact remediation (`decision == "remediate"`) anchors to `anchor_task_id=task_id`; when `agent/execution-state.json` matches the reviewed task identity (`file_task == anchor_task_id`), `READINESS_TASK_ID_MISMATCH` is recognized as a valid anchor and does not block remediation execution.
    - Accepted successor actuation (`decision == "next"`) passes `predecessor_task_id=task_id`; when `agent/execution-state.json` was bound to the accepted predecessor task and the successor in `next.md` is `ready_to_run`, launch proceeds without false readiness blocks.
    - Non-permanent decision row consumption: if `launch_task is None` due to a readiness block (`"readiness" in (guard_error or "").lower()`), `_record_blocked` is bypassed so the decision row remains unconsumed in `ledger["executions"]`, enabling subsequent supervisor migration to actuate it.
  - Hardened lifecycle vs terminal blocker handling in `src/dev_orchestrator/core/blockers.py` and `activation_supervisor.py`:
    - Updated `READINESS_NOT_READY_TO_RUN` `failure_class` from `"terminal"` to `"lifecycle"`.
    - In `ActivationSupervisor._advance_intent`, handled lifecycle blockers (`top_blocker.failure_class == "lifecycle"` or `top_blocker.code == "READINESS_NOT_READY_TO_RUN"`) before terminal blocker evaluation. Terminates active intent as `state="stopped"` (emitting `status="lifecycle_hold"`), preserving recovery budgets and preventing phantom `RECOVERY_BUDGET_EXHAUSTED` blockers in `explain_block`.
  - Normalized legacy token parsing in `src/dev_orchestrator/core/readiness.py`: stripped optional leading `"Status:"` prefix before component splitting in `_resolve_legacy_markdown`.
  - Added 4 dedicated regression test cases in `tests_py/test_p167_self_healing_activation.py` under `TestP167ReviewRemediation`:
    - `test_remediate_after_handoff_with_committed_execution_state_and_readiness_projection`: verifies remediate actuation succeeds when `next.md` points to successor while `execution-state.json` is anchored to predecessor.
    - `test_accepted_next_task_with_committed_predecessor_execution_state_and_readiness_projection`: verifies next task actuation succeeds when `execution-state.json` is anchored to accepted predecessor and `next.md` is ready to run.
    - `test_readiness_caused_block_does_not_permanently_consume_decision_row`: verifies unlaunchable readiness failures preserve the decision row for subsequent supervisor migration.
    - `test_readiness_not_ready_to_run_lifecycle_hold_terminates_intent_without_false_exhaustion`: verifies `READINESS_NOT_READY_TO_RUN` stops the intent as `lifecycle_hold` without burning attempts or generating false `RECOVERY_BUDGET_EXHAUSTED`.
- Technical Review Remediation Round 4 (`ai_review:ai_review:ai_review:ai_review:bc404b1b-5fd9-4ed8-b11f-729b6fdff6bc:execute` on commit `7adf2cb`):
  - Watchdog recovery handoff persistence & thread-safe consumption (`src/dev_orchestrator/core/watchdog.py`):
    - Added `consume_recovery_handoff(project_id, *, epoch_id=None)` under `self._lock` in `WatchdogCoordinator` to atomically pop `recovery_handoff` and persist via `_save_state(self._cached_state)`.
    - In `_emit_owner_gate_once`, called `self._save_state(self._cached_state)` after writing `recovery_handoff`.
    - Evaluated `self_healing.enabled` configuration in both `max_attempts_per_run` handoff check and `_emit_owner_gate_once`.
  - Inter-process file locking on execution intent (`src/dev_orchestrator/core/execution_intent.py`):
    - Wrapped `record_or_refresh_intent`, `record_intent_action`, `set_intent_backoff`, `clear_intent_backoff`, and `terminate_intent` in `InterProcessFileLock(runtime / "execution-intent.lock")`.
    - Extended `check_intent_budgets` to check `self_healing.enabled is False`, returning `(True, "self-healing disabled by project configuration", "SELF_HEALING_DISABLED")`.
  - Strict project ID validation across ingress surfaces (`src/dev_orchestrator/core/activation.py`, `src/dev_orchestrator/cli.py`, `src/dev_orchestrator/web/server.py`):
    - Added `VALID_PROJECT_ID_REGEX` (`^[A-Za-z0-9_-]+$`) and `validate_project_id(value)`.
    - Validated project ID in `record_activation_request`, `cmd_project_activate`, and `POST /api/v1/control/projects/activate` (returning HTTP 400 on invalid input).
    - Fixed project ID defaulting in `record_activation_request` to validate explicit strings (even empty ones) before defaulting to `resolved_repo.name`.
  - Context propagation in blocker derivation (`src/dev_orchestrator/core/control_commands.py`):
    - Passed `project_id=project_id` to `explain_block` at line 349 so unregistered projects correctly emit `PROJECT_NOT_REGISTERED`.
  - Closed legacy component grammar and token normalization (`src/dev_orchestrator/core/readiness.py`):
    - Reused `parse_legacy_status_components` uniformly in `_resolve_legacy_markdown`, ensuring consistent parsing, conflict handling, and candidate derivation.
    - Set `code = "READINESS_TOKEN_UNSTRUCTURED" if candidate else "OK"` and `migration_required = bool(candidate)` so unmigrated legacy tokens are properly migrated by the supervisor.
  - Supervisor watchdog integration & transient backoff (`src/dev_orchestrator/daemon.py`, `src/dev_orchestrator/core/activation_supervisor.py`):
    - Passed `watchdog` to `ActivationSupervisor` in `daemon.py`.
    - Handled transient infrastructure errors via scheduled backoffs and non-action-consuming waiting before executing the retry under budget.
    - Evaluated `self_healing.enabled: false` in `ActivationSupervisor`, stopping intents cleanly as `lifecycle` without spurious remediation loops.
  - Added dedicated regression test cases in `tests_py/test_p167_self_healing_activation.py`:
    - `test_two_tick_watchdog_handoff_consumption`: verifies watchdog handoff consumption clears `_cached_state` and does not restore or double-consume on tick 2.
    - `test_supervisor_terminates_intent_when_self_healing_disabled`: verifies supervisor terminates intent as `stopped` with `lifecycle` when self-healing is disabled.
    - `test_project_id_validation_and_rejection`: verifies valid/invalid project ID validation across `validate_project_id` and `record_activation_request`.
    - Updated `test_supervisor_transient_backoff_and_forward_retry` to verify scheduled backoff, waiting, and subsequent retry phases across timestamps.
- Acceptance & Verification:
  - Canonical contract authored in `docs/P16_7_SELF_HEALING_ACTIVATION_CONTRACT.md`.
  - Focused activation suite: 39 passed, 5 subtests passed in `tests_py/test_p167_self_healing_activation.py`.
  - Adjacent transition & control suites: 72 passed, 10 subtests passed across `test_transition_executor.py`, `test_watchdog.py`, `test_control_commands.py`, `test_cli.py`, `test_p16_cli_and_integration.py`.
  - Full test suite regression: 1086 passed, 92 subtests passed in 391.06s (0 failures).
  - Python compilation (`python -m compileall -q src ops tests_py benchmark/src`): passed cleanly (exit 0).
  - `git diff --check`: passed cleanly (0 whitespace/formatting defects).
  - Knowledge graph updated via `graphify update .`: 5415 nodes, 15160 edges, 253 communities.
- Preserved handoff: P16.8 DevO Golden-Path Lifecycle Hardening (`agent/staged/P16.8.md`, Status: **PENDING DESIGN**).

P16 AI Capability Benchmark Project remediation & state (2026-09-21):
- Remediated all findings from Technical Review (`ai_review:auto-58add3451902f376035e2fe7:execute`):
  1. Execution source provenance: Added `execution_source` ("live" vs "pipeline_self_test") and `has_real_broker_evidence` across `TrialRecord`, `TrialPlan`, `RunSummary`, and `PromotionDecision`. Runner, summary generator, and CLI explicitly track and propagate execution provenance.
  2. Fail-closed evidence gate: `evidence_gate` in `decision.py` now verifies authenticity and fails closed (`ev_pass=False`, reasons include `evidence_gate_failed`, failed_gates include `evidence_gate`) whenever evidence originates from mock/pipeline self-test execution or lacks real broker dispatch/execution IDs.
  3. Non-destructive ACL denial audit: `check_directory_write_denied` in `containment.py` returns `False` if `not dir_path.exists()`, preventing non-existent paths from falsely appearing denied.
  4. Network denial regex matching: `verify_network_denial` in `zvec.py` uses `re.search(r"action:\s*(?:block|deny)\b", ...)` and `re.search(r"direction:\s*out\b", ...)`, preventing false positives when `"out"` is present elsewhere in the rule output.
  5. Committed baseline evidence alignment: Updated `benchmark/evidence/p16_baseline_20260921/` (`trial_plan.json`, `results.jsonl`, `summary.json`, `promotion_decision.json`, `report.md`) to record `execution_source: "pipeline_self_test"`, `has_real_broker_evidence: false`, `decision: NO_PROMOTE`, and `evidence_gate: FAIL`.
  6. Integrity test modernization: Rewrote `test_committed_evidence_consistency` in `tests_py/test_p16_cli_and_integration.py` to assert schema, monotonic timestamps, and `pipeline_self_test` provenance (`evidence_gate: FAIL`, `fallback_gate: PASS`) rather than hard-asserting artificial 100% scores.
  7. Account-provisioning requirement removed (owner policy update 2026-09-22): current-user live benchmark is authorized. Dedicated Windows account/SID, ACL deny provisioning, Task Scheduler identity, and host elevation are optional hardening only and must not raise OWNER_GATE. Evidence authenticity remains fail-closed for mock/self-test runs.
- Final evaluated decision: **`NO_PROMOTE`** (`reasons: ["capability_unsupported", "evidence_gate_failed"]`).
- Canonical acceptance record updated in `docs/P16_BENCHMARK_ACCEPTANCE.md`.
- Acceptance suite: 9 dedicated P16 test suites passed (63 passed after current-user mode update).
- Full repository regression: 1044 passed, 87 subtests passed in 365.77s (0 failures).
- Python compilation (`python -m compileall -q src ops tests_py benchmark/src`): passed cleanly (exit code 0).
- `git diff --check`: clean (0 whitespace/formatting defects).
- Graphify update (`graphify update .`): updated cleanly.
- Staged roadmap handoff: terminal milestone in `agent/staged/roadmap.json` (`successor: null`).

P15 Mobile Observability & Guarded Control remediation & closure (2026-09-21):
- Remediated bounded P15 defects from independent review:
  1. Resolved hard deadlock in `src/dev_orchestrator/mobile/gateway.py`: eliminated nested acquisition of `_events_lock` in `evaluate_and_broadcast_alerts`, performing deduplication state tracking under `_state_lock` and invoking `broadcast_event()` without holding any locks while maintaining thread safety of event buffer/cursor/condition.
  2. Implemented alert deduplication return contract: defined explicit contract where `evaluate_and_broadcast_alerts()` returns newly emitted/broadcast alert items and `GET /api/v1/mobile/v1/alerts` returns the full currently active evaluated notification set via `get_active_alerts()`. Updated `docs/P15_MOBILE_CONTRACT.md` and added regression test.
  3. Resolved progress listener locking in `src/dev_orchestrator/core/progress.py`: snapshotted registered in-process listeners under `ProgressChannel._lock` and moved listener invocation outside the state/dedupe persistence lock, preventing slow or blocked listeners from wedging progress publication. Added concurrency test oracle.
- Implemented and verified Android-native observation and bounded-control surface backed by daemon-owned MobileGateway without creating a second lifecycle authority.
- Canonical contract frozen in `docs/P15_MOBILE_CONTRACT.md`.
- Implemented single chokepoint authentication via `MobileDeviceAuthorizer` protocol backed by `ControlSecurity`, persisting canonical pairing and device state in `runtime/control/adapter-capabilities.json` with monotonic `mobile_revocation_generation`. Bearer tokens are never propagated or persisted beyond gateway ingress.
- Hardened `ControlCommandStore.submit` with durable source-locking: replay of command ID with mismatched source raises `ControlCommandConflictError`.
- Extended `ControlAdapterClient.submit_control` with internal `source` argument (`X-DevO-Control-Source: mobile_gateway:<device_id>`) validated against master bearer and tokenless live-device lookup.
- Implemented `mobile_owner_gate_eligibility` and updated `AIPlannerCoordinator.approve_owner_gate` to support `approval_channel='mobile_device'` with `approving_device_id`, bypassing conversation-binding requirement while strictly preserving all repository-truth, pending-gate, and clean-worktree invariants without launching workers.
- Implemented `MobileProjectionService` exposing `MOBILE_CONTROL_ACTIONS` (`continue`, `pause`, `resume`, `stop`, `retry`, `reconcile`, `approve_owner_gate`), copying watchdog and recovery-epoch classifications verbatim, and reporting explicit `progress_observation_state`.
- Implemented Tailscale bind address verification (`100.64.0.0/10` and `fd7a:115c:a1e0::/48`), strictly rejecting wildcard, loopback, and RFC1918 addresses.
- Implemented reconnectable SSE and long-poll streams with bounded cursors and 15s mid-stream revocation checks.
- Implemented disjoint progress/transport alert evaluator in `src/dev_orchestrator/mobile/alerts.py` where stall alerts derive strictly from authoritative watchdog state.
- Implemented headless Python `MobileContractClient` and Kotlin/Jetpack Compose Android client skeleton in `android/`.
- Acceptance suite: 13 comprehensive P15 test suites passed (66 passed in 39.27s).
- Verification: focused P15 + progress suites passed (84 passed in 40.61s); full repository regression passed (978 passed, 87 subtests in 381.52s); `compileall`, `git diff --check`, and `graphify update .` all passed cleanly.
- Preserved handoff: P16 AI Capability Benchmark Project (Status: **PENDING DESIGN**).

P14.6 Technical Review Remediation Round 2 & Gate closure (2026-09-20):
- Remediated all Technical Review findings from ai_review:ai_review:p146-owner-continue-20260920:
  1. Blocker closed (durability/crash recovery in worker failover loop): Aligned worker failover request ID naming to `ai-worker:{source_request_id}:failover-{attempt - 1}` and persisted `broker_request_id` via `_update_record(source_request_id, broker_request_id=current_request.request_id)` immediately before `self._ai_execution_port.execute(current_request)` on every attempt. During crash recovery (`_recover_interrupted_runs`), in-flight failover executions reconcile strictly against the active failover ID, matching `fact['request_id']`, setting `state='recovery_required'` with `recovery_safe_retry=False` rather than querying the failed attempt 1 ID and mistakenly setting terminal `failed` state (which would have permitted concurrent actuation). Added comprehensive reproduction oracle and regression in `test_worker_failover_crash_recovery_reconciliation`.
  2. Secondary review bypass closed: In `_advance_completed_predecessor_handoffs`, when `reviewer_enabled` is True on a project, auto-handoff is strictly skipped; predecessor promotion is exclusively driven by accepted technical review decisions (`next` / `next_task`) via `_advance_decisions`. Verified that previously failed worker tasks never permit auto-handoff when reviewer is enabled.
- Acceptance suite: `tests_py/test_p14_6_unattended_gate.py` (9 passed in 11.64s).
- Verification: focused/adjacent suites (205 passed in ~84s), full regression 898 passed + 87 subtests in 310.90s; compileall, git diff --check, and graphify update all passed cleanly.
- Preserved handoff: P15 Mobile Observability & Guarded Control (Status: **PENDING DESIGN**).


Watchdog recovery-epoch cleanup closure (2026-09-20):
- Existing epoch-scoping implementation from 6b1e7f3 was acceptance-reviewed and extended with explicit active Reviewer identity (review_id).
- Exact P14.5-era stale-state regression proves a healthy newer EXECUTING Worker clears historical watchdog owner_gate/attempts=20 and projects ok / attempts_this_run=0.
- Genuine current OWNER_GATE remains preserved; restart/replay coverage remains green.
- Verification: 62 watchdog-recovery tests + 5 subtests, 40 adjacent watchdog tests, full 882 tests + 87 subtests, compileall and diff-check all passed.
- Next task is P14.6; no P14.5 re-review is required.
P14.5 closure (2026-09-20, owner-approved at OWNER_GATE):
- Closure anchor: `e2fce11 fix(review): restore actionable remediate dispositions`. Local only; not pushed.
- All three frozen acceptance criteria were demonstrated live, not only unit-tested:
  - AC-1: a real diff review of `e2fce11` ran as durable P14 job `job-ed3f5145819dd8ac` with `exit_code=0` over transport `local` (no RDC). Coverage selected and reviewed exactly the three files changed in that commit; `coverage_rate=1.0`, `completeness=complete`, `skipped=0`, `failed=0`.
  - AC-2: delegation via three AIBroker dispatches `ocr_review:p145-smoke-a:0..2`, all `succeeded`, `role=reviewer`; `findings.json`, `coverage.json`, `review.sarif` and `session.json` persisted; daemon-restart recovery settled the review with exactly one durable decision and exactly one `REVIEW_ACCEPTED`, and a second restart emitted nothing and left the decision byte-identical.
  - AC-3: discriminating test — with one `blocking` finding injected into the job's authoritative `session.json` artifact while the harness still reported `disposition='next'`, DevO independently returned `decision='remediate'`, `next_action='continue_current_stage'` and emitted `REMEDIATE`. The harness verdict was not copied.
- INV-2 demonstrated live: worker resource `agy/agy-1/gemini-3.8-flash-high`, reviewer allocated `claude/default/opus` with `independence='resource'`.
- Seven blocking findings were confirmed and closed across six remediation anchors (B1, B2, F1, F2, G1, G2, H1, H2, H3, J1). Each has an executable reproduction oracle under `.devorch/forensics/`; all report closed at `e2fce11`.
- Verification at closure: full regression 878 passed with 87 subtests; focused P14.5 plus transition 100 passed with 43 subtests; adjacent P14/reviewer 49 passed; live decision ledger validates 31/31; `compileall`, both `node --check` runs and `git diff --check` pass.
- Remediation budget was exhausted (6 anchors against a limit of 3; 5 rounds on the original frozen blocking set against a limit of 2), so closure was taken as an explicit owner decision at OWNER_GATE rather than an automatic advance.
- Closure evidence: `.devorch/forensics/p145-closure-decision-packet-e2fce11-v2.md`, with review verdicts, reproduction oracles and smoke outputs alongside it.
- Residual non-blocking items NB-1..NB-12 are carried forward in `agent/next.md`; none blocks closure.
- Not performed at closure: DevOrchestrator remains owner-paused, no successor task was started, and nothing was pushed.
- Immediate continuation rule: preserve clean handoff for next task; commit locally clean without push.

Implementation summary:
- Added the opt-in `dev_orchestrator.accounting` package with a cross-thread/process serialized, fsynced JSONL event ledger; closed event/phase/role taxonomy; deterministic replay IDs; bounded corruption evidence; and explicit torn-tail quarantine/recovery.
- Added deterministic exclusive interval construction with clipping, right-censoring, overlap precedence, idle filling, explicit owner-gate correlation and no wall-clock double counting.
- Added accounting summaries for phase time, plan-review churn, retry wall time, owner wait, longest no-progress interval, rejected attempt time and EDR.
- EDR counts only AI execution and managed validation linked to an explicit technically accepted Worker/remediation attempt; rejected and unresolved attempts do not enter the numerator.
- Added structured, predicate-matched failure memory with canonical fingerprints, verification/provenance, capped prompt rendering, recurrence count/cost and fail-closed state loading.
- Seeded and injected the verified Windows PowerShell 5.1 `&&`/`||` lesson into matching Planner, Worker and Reviewer prompts when accounting is enabled.
- Instrumented Planner, plan review/remediation/retry, Broker/legacy Worker, legacy fallback retry, direct/browser technical review, browser queue and explicit owner-gate boundaries with observed correlation/resource identity.
- Added top-level `execution_accounting` opt-in and optional `project-continue --gate-id`; missing/disabled accounting preserves existing orchestration calls and prompts.
- Added the contract document `docs/EXECUTION_ACCOUNTING_CONTRACT.md` and dedicated concurrency, accounting, failure-memory, runtime and instrumentation test suites.

Verification status:
- Focused P11b and adjacent lifecycle regression suites pass.
- Full `python -m pytest tests_py -q`: 453 tests and 16 subtests passed.
- `python -m compileall -q src tests_py`, userscript syntax and `git diff --check` passed.
- Graphify AST update completed successfully: 2327 nodes, 6163 edges and 128 communities. Because the worktree had no tracked Graphify baseline, its newly generated cache/output was kept out of the P11b commit.

P11c result:
- AIBroker results now emit durable, idempotent provider evidence with exact
  request/dispatch/execution/resource/session correlation and optional explicit
  timing, quota, rate-limit, and first-output facts.
- Provider summaries report resource/provider/account/model/session continuity,
  unknown fields, resource switches, and evidence-qualified failover latency.
- Normalized RDC JSON/JSONL evidence can be imported into the accounting ledger;
  deterministic classifiers cover isolated concurrency, HOL blocking,
  starvation, session coupling, reconnect contamination, and no-output deadlock.
- Recovery target calculation is project-scoped and read-only.
- Focused P11c tests and the full Python regression suite pass.

P11d result:
- Added one deterministic P11 report across accounting, provider/context and RDC evidence, with explicit source provenance, unknown-data warnings and no inferred facts.
- Added project/task/role time breakdown, original-hypothesis comparison, ranked evidence-backed bottlenecks and seven machine-testable default acceptance gates.
- Added read-only `execution-report`, `/api/accounting`, and 8770 dashboard views for EDR, phase loss, provider/failover, RDC findings, hypotheses, gates and evidence IDs.
- Custom event-ledger paths are published by the enabled runtime; explicit CLI config reads remain side-effect free.
- Representative DevOrchestrator and deterministic DOM fixtures cover the report and rendered dashboard. Full regression passed with 487 tests and 16 subtests.
- Python compilation, both JavaScript syntax checks, `git diff --check`, and Graphify AST refresh (2512 nodes, 6675 edges, 131 communities) passed.

P12 design result:
- Froze one 8770 Control API architecture with the daemon and existing `ControlCommandCoordinator` as the sole lifecycle mutation authority.
- Split delivery into unified reads, authenticated/idempotent command transport, guarded actions/conversation consolidation, and operator UI/representative acceptance.
- Defined loopback-plus-secret/session security, CSRF/origin guards, exact state revisions, atomic replay/conflict behavior, crash recovery and always-on redacted audit evidence.
- Defined selective forward-porting from `feature/conversation-control-plane`; no wholesale branch merge and no second 8766 lifecycle-control authority.
- Design verification passed: staged-roadmap suite 22/22; full Python suite 487 tests plus 16 subtests; Python/JavaScript syntax, whitespace, and Graphify AST refresh passed.
- Owner authorized implementation on 2026-09-15. `xray-hw-platform` remains paused and unchanged.

P12 implementation result:
- Port 8770 now exposes stable `/api/v1/control/*` read envelopes and a one-call operator overview while preserving every legacy GET route; standalone web remains read-only.
- Unified-daemon POST uses loopback plus bearer or same-origin browser-session authorization, exact Host/Origin/CSRF checks, strict bounded JSON schemas, narrow ChatGPT CORS preflight, short-lived single-use pairing and revocable hash-only heartbeat capabilities.
- CLI lifecycle commands now use the same authenticated 8770 ingress, and the ChatGPT userscript redeems dashboard pairing codes into a heartbeat-only private capability; neither client bypasses daemon authority.
- Command submission is cross-process atomic and replay-safe, with canonical request hashes, conflict detection, complete expected identity on every mutation path, persist-before-ack semantics, terminal-audit-before-inbox-removal recovery and redacted append-only audit evidence. Corrupt/torn queue or audit records are preserved in quarantine and surfaced as degraded control health.
- Daemon-owned control implements safe continue, pause/resume, exact supported AIBroker stop, guarded runtime conversation bind/unbind/rebind, and exact Planner owner-gate approval. Approval requires a live bound conversation, inactive browser claim, clean unchanged repository and exact current gate/task identity; it never launches a Worker, and later progress still requires explicit `continue`. Retry and reconcile remain unavailable without exact safe adapters.
- Selective branch convergence retained no 8766 service and no second lifecycle authority. The old adjudicator was intentionally not restored: current plan remediation is strictly bounded and exhaustion durably enters `OWNER_GATE` fail closed.
- Durable owner pause is enforced at final Worker launch gates and suppresses later legacy static starts. Runtime conversation bindings override static migration fallback without disabling direct AIBroker projects.
- The dashboard renders server-advertised capabilities, active roles, resources/executions, bindings, P11 evidence and pending/settled command results, including confirmation for stop/owner-gate actions.
- Verification: 509 tests and 16 subtests passed; Python compilation, dashboard and browser-adapter JavaScript syntax, `git diff --check`, and Graphify refresh passed (2744 nodes, 7357 edges, 141 communities).

P12.5 result:
- Added `ops/self_host_acceptance.py` standard-library operational acceptance utility enforcing loopback-only HTTP endpoints and issuing GET requests only against `/api/v1/control/overview`, `/api/resources`, and `/api/executions`.
- Verifies daemon health/freshness, enabled/non-degraded control authority, exact project identity (`devorchestrator` on branch `main`), P11 execution accounting availability, and AIBroker resources/executions visibility through both the unified overview and direct 8875 endpoints.
- Preserves overview warnings as non-fatal diagnostics and returns deterministic JSON with categorized diagnostics excluding response bodies and secrets.
- Verified live deployment reports overall PASS against running daemon (PID 5712) and AIBroker on 8875.
- Documented canonical self-host deployment commands, expected exit behavior, and endpoint overrides in README.md.
- Added 16 focused tests in `tests_py/test_self_host_acceptance.py`.
- Full regression passed: 550 tests and 22 subtests. Python compilation, JavaScript syntax, and git diff check passed. Knowledge graph updated to 2865 nodes, 7785 edges, 140 communities.

P12.5 review remediation result:
- Closed stale broker evidence gap: unified broker resources and executions now fail closed on `availability="stale"` or `stale=True` (and corresponding sources availability), as well as direct broker endpoints.
- Closed HTTP redirect gap: requests now use `NoRedirectHandler` preventing redirection to non-allowlisted or remote targets, returning explicit HTTP redirect errors without following.
- Closed credentials/userinfo gap: `validate_loopback_url` explicitly rejects userinfo/credentials, URLs are sanitized for safe diagnostics, and credentials are never leaked in error messages or subprocess outputs.
- Added 5 new regression tests in `tests_py/test_self_host_acceptance.py` (21 focused tests total passing).
- Full regression passed: 555 passed, 22 subtests passed. Python compilation, JavaScript syntax, and `git diff --check` passed cleanly. Knowledge graph updated to 2879 nodes, 7820 edges, 145 communities.
- Live deployment check against running daemon (PID 5712) and AIBroker (8875) verified PASS.

P12.5 self-host recovery hotfix result:
- Owner `continue` and `resume` preserve exact review-driven remediation after a
  `WorktreeUnsafeError` broker failure with explicit `broker_status=failed`,
  allocation IDs/resource context, null provider session and no usable output:
  a new remediation identity uses the configured remediation prompt plus durable
  original reviewer evidence; the failed execution remains immutable history.
- Recovery is fail-closed unless the applied REMEDIATE decision, task,
  branch/HEAD, clean current worktree, no-active-run state and absence of all
  usable provider session/output/work evidence match exactly. Broker allocation
  IDs and resource context alone do not imply useful provider work. Generic
  failed Workers and ambiguous attempts remain non-retryable.
- Documented unattended continuation authority without adding a second
  lifecycle authority. P12.5 now hands off to the bounded P12.6 persistent
  harness acceptance/closure spec, which verifies existing capability only.
- Verification: focused remediation/control/staged suites passed (55 tests);
  full `python -m pytest tests_py -q` passed (560 tests and 22 subtests), as
  did `python -m compileall -q src ops tests_py` and `git diff --check`.

P12.5 recovery review remediation result:
- Recovery selection is now anchored to the failed remediation and its durable
  applied reviewer decision, so a reviewed P1 remediation remains P1 even
  after `agent/next.md` advertises P2.
- A differing clean fingerprint is accepted only for a closed generated-only
  proof (`?? graphify-out/`), including the independently verified legacy
  fingerprint; tracked-source cleanup/revert and ambiguous historical rows
  fail closed.
- Recovery lineage, reviewed fingerprint evidence and explicit no-output
  fields are persisted in the new launch ledger row before the Broker thread
  starts. Verification: 58 focused tests and 563 tests plus 22 subtests in the
  full suite passed; compileall, both JavaScript syntax checks and diff check
  passed.

P12.5 recovery consumption remediation result:
- A failed remediation is durably consumed by a retry row's `recovery_of`
  lineage without mutating historical rows. Continue/resume cannot replay it
  while that retry is active or awaiting its normal technical-review
  transition; after the reviewed transition, ordinary P2 control is unblocked.
- Contradictory positive provider evidence (raw output, provider-work flag,
  output flag, first-output timestamp or session) overrides stale negative
  evidence and fails recovery closed.
- Verification: focused transition/control/remediation suites passed (39
  tests); full suite passed (566 tests and 22 subtests in 151.23s).

P12.5 post-reanchor closure remediation result (2026-09-17):
- Acceptance parsing now fails closed on malformed/null overview collections and incorrectly typed nested project identity objects; IPv6 loopback normalization preserves brackets.
- Reconcile replay is restart-idempotent after a durably persisted reviewer launch, avoiding false blocked settlement after a crash boundary.
- Exact current-HEAD re-anchored REMEDIATE work blocked only by transient lifecycle/active-worker state can be recovered by a new explicit owner `continue` without mutating historical evidence.
- Recovery precedence preserves the established descendant-recovery barrier when no exact re-anchor candidate exists.
- Focused regression: 54 tests and 9 subtests passed.
- Full regression: 583 tests and 31 subtests passed; compileall and `git diff --check` passed.
- Live self-host acceptance passed against daemon PID 29212 and AIBroker 8875.
- Current implementation is ready for independent technical re-review before P12.5 closure.

P12.5 independent closure review (2026-09-17):
- Independent reviewer: `copilot/default/claude-sonnet-4.6`; exact-run execution `4c8f55f2-bfd6-470e-bf81-bdeb537a2c68`; Copilot session `6f61368b-f089-43e3-8986-6fc2167da180`. The reviewer execution completed with exit code 0 and no file modifications.
- Verdict: `NEXT`; blocking findings: none. The reviewer accepted all five post-reanchor closure criteria, recovery lineage/consumption, fail-closed guards, command idempotency, and barrier precedence at HEAD `05129fc8c5ae90d19e3b3e20c2ffe1b757a83fce`.
- Non-blocking notes only: cosmetic blocked-reason precedence when two barriers coincide; harmless `None` member in `consumed_sources`; theoretical reconcile replay equality if both task IDs are absent, constrained away by valid reconcile target requirements.
- P12.5 is CLOSED. Active handoff is P12.6, which remains `PENDING DESIGN` and `OWNER START REQUIRED`.

P12.6 persistent harness acceptance result (2026-09-17):
- Implemented loopback-only HTTP URL validation in `AIBrokerExecutionPort._service_call`, rejecting non-HTTP schemes, non-loopback hosts, query/fragment parameters, and credentials.
- Blocked HTTP redirects via `_NoRedirectHandler` and redacted service tokens from diagnostics and exception logs.
- Quoted exact request identifiers safely in status and interrupt routes; 404 responses return `None`.
- Hardened `ControlCommandCoordinator._stop` to require exact correlated Broker evidence (`status` in `interrupted`, `failed`, `cancelled` and `interrupt_supported is not False`); unsupported, unconfirmed, missing, or mismatched evidence fails closed with retained durable pause (`effect="pause_future_launches"`, `state="failed"`).
- Added dedicated acceptance suite in `tests_py/test_p12_6_persistent_harness_acceptance.py` using an ephemeral loopback HTTP server fixture covering authenticated dispatch, correlation, lifecycle neutrality, pre-launch pause barrier, active-run pause, exact stop interrupt, fail-closed stop, cross-project isolation, restart reconciliation projections, and CLI fallback.
- Updated `docs/AIBROKER_INTEGRATION_CONTRACT.md` and authored `docs/P12_6_PERSISTENT_HARNESS_ACCEPTANCE.md`.
- Focused persistent harness regression: 64 passed, 8 subtests passed.
- Full regression: 601 passed, 33 subtests passed in 198.18s. Python compilation, JavaScript syntax checks, and `git diff --check` passed cleanly.
- Knowledge graph refreshed via `graphify update .`: 3046 nodes, 8390 edges, 152 communities.

P12.6 review remediation result (2026-09-17):
- Hardened `ControlCommandCoordinator._stop` to require positive capability-qualified interrupt evidence (`interrupt_supported is True` along with exact request correlation and status in `{"interrupted", "failed", "cancelled"}`); missing or False `interrupt_supported` fails closed, retaining durable pause (`effect="pause_future_launches"`, `state="failed"`).
- Sealed the CLI fallback stop gap: AIBroker CLI `interrupt-dispatch` reports `status="failed"` without persistent harness proof; it now fails closed with retained pause instead of incorrectly reporting `pause_and_interrupt`.
- Corrected trailing whitespace in `docs/P12_6_PERSISTENT_HARNESS_ACCEPTANCE.md` lines 3-6 so `git diff --check` passes with zero whitespace defects.
- Updated `FakeInterruptPort` in `tests_py/test_p12_control_actions.py` to include `interrupt_supported: True`, and added regression test cases covering missing `interrupt_supported`, CLI fallback result shape, and `interrupt_supported: True` with unconfirmed status.
- Added Case 4 to `test_stop_fails_closed_when_interrupt_evidence_is_unsupported_or_unconfirmed` and added dedicated `test_stop_cli_fallback_retains_pause_and_fails_closed_without_persistent_capability` in `tests_py/test_p12_6_persistent_harness_acceptance.py`.
- Focused persistent harness regression: 65 passed, 8 subtests passed.
- Full regression: 602 passed, 33 subtests passed in 196.61s.
- `python -m compileall -q src ops tests_py`, node syntax checks on `web/app.js` and `browser/chatgpt-web-adapter.user.js`, and `git diff --check` passed cleanly.
- Updated `docs/P12_6_PERSISTENT_HARNESS_ACCEPTANCE.md` focused test results (65 passed, 8 subtests passed) and added full regression evidence (602 passed, 33 subtests passed).
- Verified canonical worktree is clean without untracked `graphify-out/` to ensure clean lifecycle transition.

P12.6 closure review remediation result (2026-09-17):
- Remediated Technical Review finding from `ai_review:closure:p126:final2:36eb632d1b7f`:
  - Successor status contract: `agent/staged/P12.7.md` status corrected to `Status: **PENDING DESIGN**` (from `READY_TO_RUN`), satisfying `read_successor` contract and preventing terminal handoff blocks.
  - Successor roadmap link regression: `tests_py/test_staged_roadmap.py` now asserts `read_successor(checkout_root, "P12.6")` returns successor `P12.7`, spec path `agent/staged/P12.7.md`, and `Status: **PENDING DESIGN**`; `test_real_repo_p126_to_p127_staged_contract` verifies `READY_TO_RUN` and approved design markers are absent.
  - End-to-end handoff lifecycle regression: `tests_py/test_staged_handoff.py` added `test_p126_to_p127_staged_handoff_contract_and_lifecycle` reproducing both the defect (`READY_TO_RUN` causing `state="blocked"` with missing pending design reason) and the fix (`PENDING DESIGN` advancing to `state="handoff"` with `next_task_id="P12.7"` and unblocking deferred planning).
- Verification:
  - Focused suites passed: 85 passed, 9 subtests passed (`test_p126_review_retry.py`, `test_transition_executor_aibroker.py`, `test_p12_6_persistent_harness_acceptance.py`, `test_staged_roadmap.py`, `test_staged_handoff.py`).
  - Full suite passed: 619 passed, 40 subtests passed in 214.13s (`python -m pytest tests_py -q`).
  - `python -m compileall -q src ops tests_py`, node syntax checks on `web/app.js` and `browser/chatgpt-web-adapter.user.js`, and `git diff --check` passed cleanly with 0 defects.
  - Canonical worktree is clean and ready for final independent technical re-review.

P12.7 web control surface visual refresh result (2026-09-17):
- Refactored `web/index.html`, `web/style.css`, and `web/app.js` into a dense single-page dashboard referencing OpenCode Data visual/information-architecture principles without copying branding, assets, or product metrics.
- Preserved all 26 legacy DOM IDs, GET API contracts, daemon mutation authority, CSRF/origin/Host headers, and CSP (`default-src 'self'`).
- Implemented pure exported helpers (`buildControlTarget`, `describeGuardedAction`, `computeFreshnessState`, `computeIncidentCount`, `computeKPIs`, `severityRank`, `compareSeverityThenIdThenTime`).
- Wired control buttons through `buildControlTarget` with target validation and disabled state handling.
- Added native confirmation dialogs for the five lifecycle-changing guarded actions (`stop`, `retry`, `rereview`, `reconcile`, `approve_owner_gate`).
- Replaced `Promise.all` with `Promise.allSettled` in `refresh()` to prevent partial fetch failures from masking errors as healthy.
- Added dedicated test suite `tests_py/test_web_ui_refresh.py` (8 tests) covering target building, confirmation prompts, disabled states, helpers, fake-DOM rendering fixtures, required IDs, security checks, and read-only GET behavior.
- Focused web & control regression passed: 55 passed, 6 subtests passed; `tests/web-selftest.ps1` passed; full regression passed: 629 passed, 40 subtests passed in 206.09s.
- `python -m compileall -q src ops tests_py`, node syntax checks on `web/app.js` and `browser/chatgpt-web-adapter.user.js`, and `git diff --check` passed cleanly.
- Knowledge graph updated via `graphify update .`: 3110 nodes, 8624 edges, 148 communities.
- Canonical worktree clean and ready for independent technical review.

P12.7 review remediation result (2026-09-17):
- Remediated all 6 Technical Review findings from `ai_review:auto-cf45dc90052f1a5988604625:execute`:
  - Added first-viewport `#daemonBadge` and `#watchdogBadge` in header status strip (`web/index.html`, `web/style.css`, `web/app.js`), ensuring daemon and watchdog health are immediately visible alongside monitor and freshness badges.
  - Hardened `computeFreshnessState` to fail closed to `'Disconnected'` whenever monitor fetch fails (`fetchFailed=true`), `!monitor`, `monitor.available===false`, or `monitor.process_alive===false`, preventing false `'Live'` display when overview has `observed_at`.
  - Hardened `computeIncidentCount` to detect top-level `watchdog.degraded === true` and increment incident count by 1, correctly reporting incidents when watchdog is degraded with 0 projects.
  - Hardened `renderWatchdogDiagnostics` to omit fabricated/unknown placeholders (`—`), display `State` only when returned by API, render `Degraded: yes/no`, render `Auto recovery: unavailable` when undefined instead of fabricating `disabled`, and omit placeholder `Observed: —`.
  - Recorded explicit manual 1366x768 viewport verification:
    - Topbar (daemon, monitor, watchdog, freshness badges, last refresh, refresh button) and 5 KPI cells fit in the first viewport (155px height vs 768px).
    - Contrast ratios: bright text `#f0f6fc` on `#0d1117` (15.8:1), body `#c9d1d9` on `#161b22` (10.4:1), muted `#8b949e` (5.1:1), status colors (OK `#3fb950` 6.7:1, Warn `#d29922` 6.9:1, Bad `#f85149` 5.4:1, Info `#58a6ff` 6.8:1) exceeding WCAG AA/AAA standards.
    - Visible keyboard focus via `*:focus-visible` (2px solid `#58a6ff` with 2px offset).
    - Guarded action confirm dialogs with labeled identity lines and consequence descriptions; 0 fetches on cancel.
  - Added comprehensive regression test `test_partial_failure_states_in_kpis_header_and_diagnostics` in `tests_py/test_web_ui_refresh.py` (9 tests total now) covering monitor failure, degraded watchdog with no projects, broker/accounting unavailable, and daemon/watchdog health badges. Also updated `test_freshness_kpi_and_incident_helpers` and `test_dom_ids_security_and_opencode_exclusion`.
- Verification:
  - Focused web/control suites passed: 56 passed, 6 subtests passed (`test_web.py`, `test_accounting_dashboard.py`, `test_unbound_web_ui.py`, `test_web_ui_refresh.py`, `test_p12_control_actions.py`, `test_p12_control_foundation.py`, `test_control_commands.py`).
  - `tests/web-selftest.ps1`: PASS.
  - Full test suite passed: 630 passed, 40 subtests passed in 205.12s (`python -m pytest tests_py -q`).
  - Python compilation (`compileall`), node syntax checks (`web/app.js`, `browser/chatgpt-web-adapter.user.js`), and `git diff --check` passed cleanly with 0 defects.
  - Knowledge graph updated with `graphify update .` (3114 nodes, 8631 edges, 157 communities).
  - Canonical worktree is clean without untracked `graphify-out/` to ensure clean lifecycle transition.

P16.10 final owner-gate closure and review acceptance (2026-09-25):
- HEAD lineage includes 2958304 fixing the final OWNER_GATE rereview disposition mismatch.
- Added dedicated regression coverage in test_p127_closure_rereview.py for completed reviewer OWNER_GATE -> clean descendant -> exactly one rereview candidate/control -> rereview_of gate consumption.
- Independent review `ai_review:rereview:2cfa3024-fa26-49a6-9770-6125ef1074b7` on HEAD `ca0807e` returned `decision: "next"`, `next_action: "next_task"` ("No concrete fixable blocking finding remains in the reviewed task, so P16.10 may close and the successor chain may advance").
- Resolved secondary non-blocking improvements:
  - In `src/dev_orchestrator/control/reconcile.py`, allowed `OWNER_GATE` lifecycle in `_rereview_task_matches_current_or_pending_successor` for cross-task re-review when successor is pending design.
  - In `src/dev_orchestrator/core/transition_executor.py`, added `"review accepted current READY_TO_RUN task and no next executable task is advertised"` to `_legacy_no_next_settle`, covered by `test_legacy_no_next_settle_ready_to_run_reconciles_to_staged_handoff`.
  - In `src/dev_orchestrator/incidents/evaluation.py`, added early-return on isolation gate failure to avoid subprocess-executing un-isolated test code, covered by `test_isolation_failure_skips_subprocess_execution`.
  - In `src/dev_orchestrator/incidents/store.py`, simplified line 258 condition (`if live_last_txn == txn_id:`).
  - In `src/dev_orchestrator/control/surface.py`, separated `active_lifecycles` from `terminal_lifecycles`.
- Full tests_py regression on the final closure tree passed: 1212 passed, 92 subtests passed in 475.40s (0 failures).
- P16.10 is marked COMPLETE and closed. Successor chain advances to P16.11.

P12.7 second review remediation result (2026-09-18):
- Remediated findings from `ai_review:ai_review:auto-cf45dc90052f1a5988604625:execute`:
  - Hardened `computeIncidentCount` and `computeKPIs` in `web/app.js` to treat watchdog payloads with `available === false` as missing rather than present-and-empty. When all sources (summary, control overview, watchdog) are unavailable or missing, `computeIncidentCount` and `computeKPIs` return `'unavailable'` rather than displaying `0`.
  - When summary succeeds but watchdog fails (`{available: false}` as built by `refresh()`), `computeIncidentCount` correctly evaluates project incidents without treating missing watchdog data as present-and-empty.
  - Hardened `renderWatchdogBadge` in `web/app.js` to display `'No watchdog projects'` when watchdog reports empty projects (`projects: {}`), distinguishing an empty/unrun watchdog from `'Watchdog healthy'`.
  - Expanded `test_partial_failure_states_in_kpis_header_and_diagnostics` in `tests_py/test_web_ui_refresh.py` with cases 6 and 7 covering all three sources failed and summary OK with watchdog unavailable using the `{available: false}` objects that `refresh()` actually builds. Updated `test_freshness_kpi_and_incident_helpers` to verify both `'Watchdog healthy'` and `'No watchdog projects'`.
- Verification:
  - Focused web/control suites passed: 56 passed, 6 subtests passed (`test_web.py`, `test_accounting_dashboard.py`, `test_unbound_web_ui.py`, `test_web_ui_refresh.py`, `test_p12_control_actions.py`, `test_p12_control_foundation.py`, `test_control_commands.py`).
  - `tests/web-selftest.ps1`: PASS.
  - Full test suite passed: 630 passed, 40 subtests passed in 205.73s (`python -m pytest tests_py -q`).
  - Python compilation (`compileall`), node syntax checks (`web/app.js`, `browser/chatgpt-web-adapter.user.js`), and `git diff --check` passed cleanly with 0 defects.
  - Knowledge graph updated with `graphify update .` (3115 nodes, 8632 edges, 161 communities).
  - Canonical worktree is clean without untracked `graphify-out/` to ensure clean lifecycle transition.

P12.7 third review remediation result (2026-09-18):
- Remediated findings from `ai_review:rereview:closure3`:
  - Acceptance 2 (guarded-action CAS projection): Hardened `cmd_project_control` in `src/dev_orchestrator/cli.py` to mirror the daemon coordinator's per-action validation projection. For `retry` and `rereview`, expected CAS identity is derived using the orchestration lifecycle overlay (`overlay_orchestration_lifecycle` with `ai-reviewer.json` and `summary.json`), matching what `ControlCommandCoordinator._advance_command` observes (e.g. `REVIEW_FAILED`). For all other controls (e.g. `pause`), raw per-project snapshots are retained so commands are not rejected as stale project identity.
  - Daemon reviewer fallback: Hardened `ControlCommandCoordinator._advance_command` in `src/dev_orchestrator/core/control_commands.py` to fall back to durable on-disk reviewer state (`ai-reviewer.json`) when `self.reviewer` is not injected.
  - CLI CAS regression tests: Added `test_project_control_retry_uses_orchestration_lifecycle_overlay` and `test_project_control_rereview_uses_orchestration_lifecycle_overlay` in `tests_py/test_cli.py` verifying that both actions derive `expected.lifecycle_state = "REVIEW_FAILED"`.
- Verification:
  - Focused web/control/cli suites passed: 101 passed, 16 subtests passed (`test_cli.py`, `test_web.py`, `test_accounting_dashboard.py`, `test_unbound_web_ui.py`, `test_web_ui_refresh.py`, `test_p12_control_actions.py`, `test_p12_control_foundation.py`, `test_control_commands.py`, `test_p126_review_retry.py`, `test_p127_closure_rereview.py`).
  - `tests/web-selftest.ps1`: PASS.
  - Full test suite passed: 658 passed, 45 subtests passed in 228.65s (`python -m pytest tests_py -q`).
  - Python compilation (`compileall`), node syntax checks (`web/app.js`, `browser/chatgpt-web-adapter.user.js`), and `git diff --check` passed cleanly with 0 defects.
  - Knowledge graph updated with `graphify update .` (3146 nodes, 8781 edges, 159 communities). Canonical worktree is clean without untracked `graphify-out/` to ensure clean lifecycle transition.
P14 fourth review remediation result (2026-09-19):
- Cleared stale `transport_unreachable` failure metadata on successful recovery from `unknown_recovery` to `running`/`completed`.
- Restored legal retry of genuinely recovered completed predecessors while unresolved `unknown_recovery` remains fail-closed and non-retryable.
- Added regression coverage for recovered metadata clearing, retry eligibility, unresolved ambiguity refusal, and explicit failure metadata preservation.
- Verification: 43 focused P14 passed; 71 passed + 6 subtests adjacent; 790 passed + 45 subtests full; compileall/node/diff-check clean.
- Ready for independent P14 technical re-review; do not advance to P14.5 before reviewer NEXT.
