# DevOrchestrator Self-Hosted Development State

- Canonical worktree: C:\work\github\DevOrchestrator-dev.
- Canonical branch: main.
- Runtime mode: self-hosted canonical daemon on 8770 with AIBroker diagnostics on 8875.
- Historical detached worktree C:\work\github\DevOrchestrator is not an active controller.

Current task: **P16.8 DevO Golden-Path Lifecycle Hardening** (Status: **PENDING DESIGN**).

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
