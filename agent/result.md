# DevOrchestrator Self-Hosting Result Log

P12 Unified AI Control Surface (complete 2026-09-15):
- Added the stable `/api/v1/control/*` read model and one-call 8770 overview across project/lifecycle identity, active work, watchdog, P11 accounting, broker evidence, sessions, bindings and command history while preserving legacy GET compatibility.
- Added daemon-only authenticated mutations with loopback enforcement, master bearer and same-origin browser sessions, strict Host/Origin/Fetch-Metadata/CSRF/body/schema checks, narrow ChatGPT preflight, and single-use pairing into revocable hash-only heartbeat capabilities.
- Replaced the loose inbox with a cross-thread/process atomic command store, canonical exact replay/conflict behavior, persist-before-ack/result-before-remove crash recovery and an fsynced redacted audit trail.
- Added daemon-owned pause/resume, exact supported AIBroker stop and conversation bind/unbind/rebind adapters. Owner pause now gates every final Worker launch and suppresses stale static starts; unsafe retry/reconcile/universal approval paths remain explicitly unavailable.
- Added capability-driven dashboard actions, confirmations, pending-result polling and command/binding views, plus CLI project control and overview commands.
- Added focused concurrency, security, pairing, stale-identity, action isolation, binding/liveness/claim guard, DOM and representative 8770 acceptance tests.
- Verification: 501 tests and 16 subtests passed; Python compilation, dashboard/userscript syntax, whitespace validation and Graphify AST refresh passed (2702 nodes, 7223 edges, 140 communities).

Self-hosting baseline established:
- stable controller remains in the original worktree;
- isolated development worktree created on `feature/self-hosted-dev`;
- no Worker may mutate or restart the stable controller directly.

D1 Self-Hosted DevOrchestrator Integration:
- Broker-native running state projection:
  - Added `project_runtime_status` in `src/dev_orchestrator/core/project_status.py` overlaying active broker worker runs (from `transition-executor.json`), reviews (from `ai-reviewer.json`), and plans (from `ai-planner.json`) onto project snapshots.
  - Updated `write_execution_status` and `write_review_status` to persist `broker_execution` and active `EXECUTING` / `REVIEWING` statuses in `.devorch/status.json`.
  - Updated `overlay_managed_runs` in `TransitionExecutor` to project `broker_execution` and `engine="aibroker"` on snapshots.
  - Updated CLI `project-status` to display the projected runtime status instead of stale `READY_TO_RUN`.
  - Updated `src/dev_orchestrator/config.py` to recognize `ai_roles.planner.enabled` as orchestration-ready.
- Transport-only Progress Channel:
  - Implemented `ProgressChannel` in `src/dev_orchestrator/core/progress.py` emitting milestone notifications: `PLAN_STARTED`, `PLAN_ACCEPTED`, `WORKER_STARTED`, `WORKER_DONE`, `TEST_FAILED`, `REVIEW_STARTED`, `REMEDIATE`, `REVIEW_ACCEPTED`, `OWNER_GATE`, `BLOCKED`, `TASK_COMPLETE`, `NEXT_TASK`.
  - Configurable notification levels: `quiet`, `normal` (default), `verbose`.
  - Added deduplication, rate-limiting, and restart-safe idempotency via `runtime/progress-channel.json`.
  - Isolated progress transport: `BrowserBridgeStore.submit_progress` and `claim_progress` routing through `runtime/bridge/progress/<adapter>/<binding_id>.jsonl` completely distinct from decision queues (`runtime/bridge/queues/`). Never triggers model inference and consumes zero AIBroker quota.
  - Bridge HTTP `/v1/progress` GET and POST endpoints added in `BridgeHTTPServer`.
  - Wired `ProgressChannel` into the lifecycle emitters that own milestones: `TransitionExecutor`, `AIReviewerCoordinator`, `AIPlannerCoordinator`, and `daemon.py`; `ControlCommandCoordinator` delegates lifecycle events to those owners and does not carry a redundant ProgressChannel dependency.
  - Browser adapter `browser/chatgpt-web-adapter.user.js` polls `/v1/progress` and displays progress toasts without submitting turns to ChatGPT composer.
- P6 Review Remediation (conversation_binding propagation gap):
  - Fixed `ProgressChannel.emit()` to resolve `conversation_binding` from an in-memory and persistent cache when not explicitly passed by the caller, so progress events emitted by `AIPlannerCoordinator` and `AIReviewerCoordinator` are delivered to the bound ChatGPT conversation even when only a `project_id` is supplied.
  - Added `ProgressChannel.register_project` and `register_projects` to cache project bindings and config at coordinator startup; cache is persisted to `runtime/progress-channel.json` and survives daemon restarts.
  - Added `ProgressChannel.resolve_binding` with layered fallback: in-memory cache → `project_resolver` callback → `summary.json` → `transition-executor.json` → `ai-planner.json` → `ai-reviewer.json`.
  - Added `ProgressChannel.set_project_resolver` for runtime injection of a project lookup callback.
  - `emit()` now accepts a bare string `project_id` in addition to a dict.
  - `AIPlannerCoordinator` and `AIReviewerCoordinator` now propagate `conversation_binding` in all `emit()` calls and populate `_project_bindings` cache on startup and review launch.
  - `progress-channel.json` schema extended with `project_bindings` and `project_configs` sections.
  - 7 new regression tests added in `ProgressChannelBindingPropagationTests` covering register, resolve, string-project emit, cross-restart persistence, resolver callback, and explicit-binding cache update.
- P8 Review Remediation (Windows GBK Unicode Transport Blocker):
  - In `src/dev_orchestrator/ai/aibroker_subprocess.py`, forced child Python environment to UTF-8 (`PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`) across all AIBroker dispatch (`execute`) and reconciliation (`_reconcile_call`: `status`, `interrupt`) calls via `_build_env()`, preserving existing provider selection and PYTHONPATH configuration.
  - Added regression test `test_execute_handles_unicode_reviewer_output_with_checkmark` in `tests_py/test_aibroker_execution_port.py` verifying reviewer output with checkmarks (`✅`, `✓`, `✔`) is properly received and preserved in `AIRoleResult.output`.
  - Added real child process regression test `test_child_environment_forces_utf8_for_unicode_reviewer_output` in `tests_py/test_aibroker_execution_port.py` confirming that child Python processes run with UTF-8 stdout encoding and emit Unicode reviewer output without GBK `UnicodeEncodeError`.
  - Added assertions in `test_success_maps_broker_result_and_preserves_semantics` and `test_status_and_interrupt_use_broker_reconciliation_commands` confirming child environment flags.
- Final D1 review remediation:
  - Replaced manual `/v1/progress` GET query parsing with `urllib.parse.parse_qs`, so percent-encoded `binding_id` values resolve to the original binding before filesystem-safe encoding.
  - Removed the unused `ProgressChannel` dependency from `ControlCommandCoordinator`; lifecycle milestone emission remains owned by `TransitionExecutor`, `AIPlannerCoordinator`, and `AIReviewerCoordinator`, avoiding duplicate notifications.
  - Hardened `ProgressChannel.emit()` against non-mapping/`None` telemetry before resolving the fallback `task_id`.
  - Added regressions for URL-decoded progress bindings and `telemetry=None`.
- Verification:
  - 213 unit tests passing cleanly (`python -m unittest discover -s tests_py`).
  - `node --check browser/chatgpt-web-adapter.user.js` passing.
  - `git diff --check` clean.

- Final independent Opus review: **ACCEPT** for code commit `6b296d8`; all three final D1 findings closed with no outstanding defect in the reviewed diff/blast radius.

- Stable promotion (owner-authorized, 2026-09-12):
  - Fast-forwarded `feature/browser-bridge-multiproject` from `f6f643d` to accepted code/docs point `c6a46aa`; created rollback branch `backup/pre-d1-promotion-20260912`.
  - Preserved the pre-existing local `docs/backlog.md` modification and Graphify/agent untracked files; none were included in the promotion.
  - Post-promotion verification: 213 unit tests OK, browser userscript syntax OK, promotion diff check OK.
  - Restarted the stable daemon with its original configuration; new PID `24456`, ports 8765/8770 healthy, `last_error=null`, Browser Bridge `/v1/health` reports `ok`.
  - AIBroker remained healthy on port 8875; no active dispatch was interrupted.

D2 Durable Project Context Foundation (P9):
- Schema and Document Model:
  - Created `src/dev_orchestrator/core/project_context.py` with `PROJECT_CONTEXT_SCHEMA_VERSION = 1` and seven canonical domains (`goals`, `architecture`, `protected_scope`, `safety_constraints`, `validation_commands`, `runtime_assumptions`, `key_decisions`).
  - Implemented immutable `ProjectContextDocument` and `ProjectContextResolution` dataclasses.
  - Implemented strict fail-closed document validator `validate_context_document` enforcing schema keys, types, entry limits (<=40 entries, <=600 chars), secret marker filtering (`password`, `api_key`, `secret`, `token`, `bearer `, `client_secret`, `private_key`, `-----begin`), and `project_id` repository matching.
  - Implemented path traversal guard `resolve_document_path` prohibiting absolute, drive-qualified, or repo-escaping relative paths.
  - Implemented precedence resolver `resolve_project_context`: declared document is authoritative; optional supplemental document (e.g. Graphify output) fills empty declared domains only; computes deterministic 16-hex sha256 digest over canonical JSON of merged domains.
  - Implemented bounded rendering `render_context_block` with standard delimiters `[PROJECT_CONTEXT_BEGIN]` and `[PROJECT_CONTEXT_END]`, provenance indicators (`(supplemental, unverified)`), and truncation boundary marker `[PROJECT_CONTEXT_TRUNCATED]` within configured `max_chars`.
- Configuration and Role Injection:
  - Extended `src/dev_orchestrator/config.py` with optional `project_context` declaration (`enabled`, `document_path`, `supplement_path`, `require_valid`, `max_chars`, `inject_roles`), full type/range validation, and default normalization.
  - Injected context into `AIPlannerCoordinator.start()`: fails closed with explicit reason on invalid context when `require_valid` is true; records context metadata (`context_block`, `context_state`, `context_digest`, `context_sources`) on plan record; appends context block to planner and plan-reviewer prompts.
  - Injected context into `TransitionExecutor._launch()`: appends context block to worker prompts across both legacy agent and AIBroker dispatch; blocks execution via `_record_blocked()` when context is invalid and `require_valid` is true; records `context_state` and `context_digest` in execution ledger.
  - Injected context into `AIReviewerCoordinator.advance()`: validates context prior to review launch; fails closed via `_record_terminal()` on invalid context; appends context block to reviewer prompt; records `context_state` and `context_digest` in review record.
- Observability and CLI:
  - Surfaced `project_context` status metadata in project monitor ticks (`run_monitor_once`), status file mirror (`.devorch/status.json`), and runtime projections (`project_runtime_status`). Raw prompt text and secret content are never exposed.
  - Added CLI subcommand `dev-orchestrator project-context` for read-only inspection and validation.
  - Extended `dev-orchestrator validate-config` with project context status reporting.
- Documentation and Reference Instance:
  - Created comprehensive architectural design in `docs/DURABLE_PROJECT_CONTEXT_DESIGN.md`.
  - Updated `docs/PORTABLE_PROJECT_INTEGRATION.md` and `config/projects.example.json`.
  - Authored self-hosting reference context in `agent/project-context.json`.
- Verification:
  - 16 new unit tests in `tests_py/test_project_context.py`.
  - Full test suite: 242 tests passing cleanly (`python -m unittest discover -s tests_py`).
  - Userscript syntax check: `node --check browser/chatgpt-web-adapter.user.js` passed.
  - Git diff check: `git diff --check` clean (0 formatting defects).

P9 independent final review (2026-09-12):
- Native Claude Opus 5 reviewed clean commit `f48e2c9` read-only and returned `next / next_task`.
- Review accepted the schema, project isolation/secret rejection, declared-authoritative supplement behavior, bounded Planner/Worker/Remediator/Reviewer injection, fail-closed invalid-context behavior, status/CLI surfaces, Graphify runtime independence, backward compatibility, and clean local commit.
- Reviewer could not rerun the Python suite because its safe-mode permission layer denied test execution, but repository evidence records 242 passing tests; reviewer independently confirmed `git diff --check` clean and found no blocking defect.
- Non-blocking follow-up: the design-doc example contains the word `tokens`, which the current broad secret-marker heuristic would reject; narrow/document that heuristic in a later bounded cleanup, not in P10.

P10 Active-Project Progress Watchdog and Automatic Read-Only Diagnostics:
- Architecture and Policy Design:
  - Authored and frozen complete V5 design specification in `docs/PROGRESS_WATCHDOG_DIAGNOSTICS_DESIGN.md`.
  - Defined pure policy in `src/dev_orchestrator/core/watchdog.py`: `ACTIVE_LIFECYCLE_STATES` (`PLANNING`, `REVIEWING_PLAN`, `APPLYING_PLAN`, `EXECUTING`, `REVIEWING`, `REMEDIATING`), `LIFECYCLE_OVERRIDE_FAMILY` mapping, `resolve_watchdog_policy`, and `resolve_threshold_seconds`.
- Canonical Path Identity and Dual Provenance:
  - Implemented `canonical_path(p) = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(p))))`.
  - Implemented `path_contains(root, candidate)` using `os.path.commonpath`, catching `ValueError` across drives safely.
  - Re-implemented `is_watchdog_owned_path(repo_root, candidate, *, runtime_root=None)` with exact and glob filtering without string prefix matching.
  - In `src/dev_orchestrator/adapters/agent_files.py`, refactored activity collection to preserve raw `ActivityEntry` tuples and compute dual aggregation: unfiltered legacy aggregate (`last_activity_at`, `last_activity_age_seconds`) + `watchdog_safe` projection carrying dual cryptographic provenance fingerprints (`repo_root_fingerprint`, `runtime_root_fingerprint`, `repo_scope`, `runtime_scope`).
- Clock-Free Fingerprint Purity:
  - Frozen `FINGERPRINT_FIELDS` allowlist and strict `FINGERPRINT_FORBIDDEN` / timing token validation in `build_progress_fingerprint`.
  - Derived stable `run_key` (`norun:<hash>` fallback), `run_scope_key`, and `attempt_key`.
- Read-Only Diagnostics and Classification:
  - Implemented `src/dev_orchestrator/core/diagnostics.py` under monotonic deadline budget without model inference or destructive actions.
  - Implemented deterministic rule-based `classify_evidence` for the 7 frozen codes: `healthy_slow`, `agent_stalled`, `process_dead`, `provider_or_quota_blocked`, `state_desync`, `external_wait`, `unknown`.
  - Implemented stable `evidence_hash` and bounded 500-char secret-redacted log tailing.
- Fail-Closed Persistence and Generation Fencing:
  - Implemented durable `watchdog.json` persistence with corrupt-state quarantine (`watchdog.json.corrupt-<stamp>`), `degraded` flag, single OWNER_GATE emission, and per-project row quarantine.
  - Generation token fencing (`fence_token = f"{att_key}:{generation}"`) with hard deadline reaping (`_reap_overdue_attempts`) and late result discard.
  - Interrupted in-flight attempt marking upon coordinator restart.
- Safe Two-Phase Recovery:
  - Crash-atomic two-phase protocol: `RESERVE` (recording `command_id = wd-<attempt_key>` in durable state) -> `ENQUEUE` (writing control command into `runtime/control/inbox` with milestone `RECOVERY_STARTED`) -> `RECONCILE` (`_reconcile_recoveries` against inbox/history).
  - Explicit opt-in (`auto_recovery=true`), restricted exclusively to non-destructive `continue`, with single recovery per run scope.
- Observability, CLI, and Daemon Integration:
  - Added read-only `GET /api/watchdog` route in Web dashboard server.
  - Added CLI subcommand `watchdog-status [--config PATH] [--runtime-root PATH] [--project-id ID]`.
  - Integrated `WatchdogCoordinator` into `_run_orchestration_tick` and `run_daemon` in `src/dev_orchestrator/daemon.py`.
- Comprehensive Verification:
  - 10 new dedicated test suites with 39 new unit tests:
    - `tests_py/test_activity_evidence.py`
    - `tests_py/test_watchdog_owned_paths.py`
    - `tests_py/test_watchdog_fingerprint.py`
    - `tests_py/test_watchdog_self_exclusion.py`
    - `tests_py/test_watchdog.py`
    - `tests_py/test_watchdog_state_failclosed.py`
    - `tests_py/test_watchdog_diagnostics.py`
    - `tests_py/test_watchdog_timeout_fencing.py`
    - `tests_py/test_watchdog_recovery.py`
    - `tests_py/test_watchdog_concurrency.py`
  - Full test suite: 281 tests passing cleanly (`python -m unittest discover -s tests_py`).
  - Userscript syntax check: `node --check browser/chatgpt-web-adapter.user.js` passed.
  - Git diff check: `git diff --check` clean.
  - Config validation smoke test passed.
  - Static guard verified: zero forbidden APIs in watchdog/diagnostics modules.

P10 R8 Bounded Remediation (two high-severity blockers closed):
- Blocker 1 — live worker identity binding (agent_stalled + process_dead):
  - Changed both STABLE-IDENTITY checks in `_check_and_trigger_recovery` from conditional mismatch to fail-closed: recovery is now blocked unless BOTH `ev_started_at` (from evidence `process_liveness`) AND `current_started_at` (from snapshot `worker`) are present, non-empty, and equal.
  - New gate codes: `agent_stalled_evidence_started_at_absent`, `agent_stalled_current_started_at_absent`, `process_dead_evidence_started_at_absent`, `process_dead_current_started_at_absent`.
  - 20 existing tests updated to include matching `started_at` in evidence and snapshot fixtures; 5 new regression tests added covering all absent/mismatch combinations for both classes.
- Blocker 2 — diagnostic timeout / single-flight permanence:
  - In `collect_evidence` (`diagnostics.py`): wrapped `ai_execution_port.status(request_id)` in a daemon thread with `join(timeout=remaining_budget)` so the broker call cannot block indefinitely.  At most one orphaned broker thread per diagnostic (bounded by project count); late threads discard via existing late-result fence.
  - In `_reap_overdue_attempts` (`watchdog.py`): after marking an attempt `timed_out`, call `self._threads.pop(pid, None)` to release the single-flight slot, allowing the next `advance()` tick to start a fresh diagnostic even if the old thread is still alive.
  - Also fixed pre-existing `RepositoryTruth.uncommitted_files` AttributeError in `collect_evidence` (correct attribute is `dirty_entries`).
  - 2 new regression tests added: `test_r8b2_single_flight_released_after_timeout_allows_new_diagnostic` (proves slot release + late-result fence), `test_r8b2_broker_status_call_is_bounded_by_deadline` (proves bounded broker call).
- Verification:
  - 359 tests passing cleanly (`python -m pytest tests_py/`).
  - `node --check browser/chatgpt-web-adapter.user.js` passed.
  - `git diff --check` clean (CRLF warnings on Windows only, no whitespace errors).
  - Config validation: no regression.
  - Static guard: zero forbidden APIs in watchdog/diagnostics modules.

## P10 Final Acceptance and Stable Promotion (2026-09-13)

- Accepted P10 code HEAD: `8550c3ca2476f2b11a1bb5b317dd4c5e0a68fcca`.
- Independent GPT-5.6 Sol final promotion review returned `approve` with zero blockers and exact matching `reviewed_head`.
- Development exact-HEAD regression: 359 tests PASS.
- Stable controller fast-forward promoted from `fbe153698337d4f38c46074fe1901db807fd173b` to `8550c3ca2476f2b11a1bb5b317dd4c5e0a68fcca`.
- Rollback ref created: `backup/pre-p10-promotion-20260913`.
- Post-promotion stable regression: 359 tests PASS.
- Browser userscript syntax, `git diff --check`, and five-project `validate-config` all PASS.
- Stable daemon restarted successfully; runtime PID `12980`, `last_error=null`.
- Port 8770 watchdog API reports `degraded=false`; all five configured projects report `state=ok`.
- Port 8765 Browser Bridge health reports `ok`.
- Existing stable `docs/backlog.md` modification and untracked agent/Graphify files were preserved and excluded from promotion.
- No push was performed.
- Owner boundary: P10 is complete. Stop execution here; do not start P11 automatically.

## P11-A Bounded Plan-Review Remediation Loop (2026-09-13)

- Implemented bounded plan-review remediation loop preventing rejection from dropping to IDLE:
  - Validated `max_plan_remediation_rounds` (default 3, range 1..5) in `_planner_policy` (`src/dev_orchestrator/core/ai_planner.py`).
  - Added `'remediating'` to `_ACTIVE_STATES` in `ai_planner.py` ensuring restart safety (`recovery_required`).
  - Refactored `_run_cycle` into modular helpers `_run_planner_attempts` and `_run_plan_review` with unique request IDs across rounds (`:planner:remediate-R`, `:reviewer:remediate-R`).
  - Extended `_planner_prompt` with `[PLAN_REMEDIATION]` block carrying round, exact reviewer rejection reason, prior plan JSON, task ID, planning HEAD, and prior planner resource context.
  - Propagated `previous_resource_context` to remediation planner calls.
  - Implemented durable `rejection_chain` in plan records capturing round, reason, prior plan, planner/reviewer dispatch & execution IDs, resource payloads, and timestamps.
  - Emitted progress milestones: `REMEDIATE` on each automatic revision round; `OWNER_GATE` with bounded exhaustion reason after N rounds.
  - Projected `'remediating'` as `REMEDIATING_PLAN` in `lifecycle_projection.py` and `project_status.py` (with `remediation_round` and `rejection_chain_length`).
  - Integrated `REMEDIATING_PLAN` into watchdog `ACTIVE_LIFECYCLE_STATES` (`PLANNING` family fallback) and config `_ALLOWED_WATCHDOG_LIFECYCLES`.
- Verification:
  - 13 unit tests passing in `tests_py/test_ai_planner.py` (including 6 new tests covering single rejection remediation, bounded exhaustion, prompt/resource propagation, request ID uniqueness, restart recovery, policy validation, and reviewer transport failure).
  - 372 tests passing in full test suite (`python -m unittest discover -s tests_py`).
  - Userscript syntax check passed (`node --check browser/chatgpt-web-adapter.user.js`).
  - Git diff check clean (`git diff --check`).

## P11x Deferred Staged Handoff (Planner-Owned Materialization) (2026-09-14)

- Implemented planner-owned staged handoff allowing automatic progression to staged successors without pre-approval repo mutation:
  - Pure roadmap reader `src/dev_orchestrator/core/staged_roadmap.py` (`read_successor()`, `read_raw()`, `sha256_bytes()`, `RoadmapResult`) importing `extract_task_id` from `telemetry.py`.
  - Fail-closed validation for `agent/staged/roadmap.json` against schema v1, POSIX paths under `agent/staged/`, non-null parity, duplicate/self task ID guards, and strict UTF-8 LF-only spec contracts.
  - Extended `TransitionExecutor._record_handoff()` to capture `staged_successor`, `staged_spec_path`, `staged_spec_sha256`, `reviewed_branch`, and `reviewed_head` without repo writes or storing raw spec text.
  - Integrated reviewer-'next' COMPLETE path in `TransitionExecutor.advance()` with `read_successor()`: blocks fail-closed on invalid roadmap, settles on absent or null successor, and records staged handoff on valid successor after repository-truth guard passes.
  - Extended `ControlCommandCoordinator._resume_decision_handoffs()` to recognize staged handoffs and delegate to `AIPlannerCoordinator.start_deferred()`.
  - Refactored `AIPlannerCoordinator.start()` through shared `_begin_lifecycle()`, and added `start_deferred()` enforcing strict repository truth, clean worktree, branch/head match, predecessor `agent/next.md` digest, and staged spec digest validation.
  - Implemented `AIPlannerCoordinator._task_source()` returning staged successor spec and audit header for deferred records while preserving exact byte-identical prompt output for non-deferred runs across initial planner, retry, remediation, and review prompts.
  - Implemented `_apply_deferred_plan()` in `AIPlannerCoordinator` with strict pre-write digest re-checks, TOCTOU repository truth re-read, atomic write of rendered spec to `agent/next.md`, single commit via `_commit_plan()`, and reset-free predecessor byte restoration recovery (`git add -- agent/next.md`) if write/commit fails.
  - Real staged roadmap verified: `P11x -> P11b -> P11c -> P11d -> null` with machine-distinct IDs and staged specs.
- Comprehensive Verification:
  - 22 unit tests in `tests_py/test_staged_roadmap.py`.
  - 3 new unit tests in `tests_py/test_transition_executor.py`.
  - 2 new unit tests in `tests_py/test_control_commands.py` plus regression test for `INCOMPLETE` rejection.
  - 14 comprehensive unit and integration tests in `tests_py/test_staged_handoff.py`.
  - Full test suite: 418 tests and 16 subtests passing cleanly (`python -m pytest tests_py -q`).
  - Userscript syntax check: `node --check browser/chatgpt-web-adapter.user.js` passed.
  - Git diff check: `git diff --check` clean.

- P11x Review Remediation:
  - Tightened staged-row eligibility check in `ControlCommandCoordinator._resume_decision_handoffs` with `re.search(r"\bCOMPLETED?\b", ...)` regex word boundaries to reject non-matching statuses such as `INCOMPLETE`.
  - Added deferred apply guard test `test_deferred_apply_new_commit_fails` (`tests_py/test_staged_handoff.py`) proving a concurrent commit fails with `'repository changed during planning'` with HEAD and `agent/next.md` bytes unchanged.
  - Added recovery test `test_deferred_recovery_when_head_moved_leaves_worktree_untouched` (`tests_py/test_staged_handoff.py`) proving that if commit succeeds and post-commit failure raises, worktree is left untouched at the new commit without prohibited git commands.
  - Added restart/idempotency test `test_restart_idempotency_after_deferred_start_before_apply` (`tests_py/test_staged_handoff.py`) proving restart between deferred start and apply transitions in-flight plan to `recovery_required`, re-running ticks does not duplicate planner, makes no new port calls, produces no commit, and leaves `agent/next.md` predecessor bytes untouched.
  - Added `INCOMPLETE` status rejection regression test in `tests_py/test_control_commands.py`.
  - Added `sys.path` and module cache purging to `tests_py/test_control_commands.py` for direct unittest execution.

## P11b Execution Accounting Foundation / EDR / Failure Memory (2026-09-14)

- Added the opt-in `dev_orchestrator.accounting` foundation:
  - Cross-thread/process serialized and fsynced append-only JSONL ledger with contiguous sequences, deterministic replay IDs, strict stored-row validation, bounded corruption evidence, and explicit torn-tail quarantine/recovery.
  - Closed event, phase, role and outcome taxonomies with observed project/task/request/source/dispatch/decision/execution/session/resource correlations.
  - Deterministic interval pairing, clipping, right-censoring, precedence resolution and idle filling, producing an exclusive wall-clock breakdown without double counting.
- Added accounting metrics for phase time, plan-review churn, retry wall time, owner wait, longest no-progress span, rejected attempt time and EDR.
  - EDR counts only Worker/remediation AI execution and managed validation associated with an explicit accepted technical-review outcome.
  - Direct and Browser Reviewer paths retain the real Worker source/attempt identity; missing identities remain absent rather than inferred.
- Added structured failure memory with canonical SHA-256 fingerprints, deterministic environment predicates, verification/provenance, capped prompt injection, recurrence count/cost and fail-closed state loading.
  - Seeded the verified Windows PowerShell 5.1 `&&`/`||` lesson and injected matching lessons into Planner, plan Reviewer, Worker/remediation Worker, direct Reviewer and Browser Reviewer prompts.
- Instrumented Planner, plan review/remediation/retry, Broker and legacy Worker execution, legacy fallback retry, direct/browser technical review, browser queue and explicit owner-gate boundaries.
- Added top-level `execution_accounting` configuration and optional `project-continue --gate-id`; missing or disabled accounting preserves existing calls, prompts and runtime file behavior.
- Added `docs/EXECUTION_ACCOUNTING_CONTRACT.md` plus focused concurrency, corruption, interval/EDR, failure-memory, runtime compatibility and instrumentation tests.
- Verification:
  - Focused P11b and adjacent lifecycle suites passed.
  - Full suite: 453 tests and 16 subtests passed (`python -m pytest tests_py -q`).
  - Python compilation, userscript syntax and `git diff --check` passed.
  - Graphify AST update completed successfully with 2327 nodes, 6163 edges and 128 communities; newly generated untracked Graphify output was excluded because this worktree has no tracked graph baseline.

## P11c Provider Context and RDC Evidence (2026-09-14)

- Added durable `provider_result_observed` events at the central AIBroker subprocess boundary. Exact request/dispatch/decision/execution and resource/provider/account/model/session facts are preserved, together with Broker-supplied timing, first-output, quota, and rate-limit observations when available.
- Added deterministic provider summaries for resource/provider/account/model/session continuity, unknown fields, resource switches, explicit quota and rate-limit counts, and evidence-qualified failover latency.
- Added strict normalized RDC evidence ingestion through both Python and the `import-rdc-evidence` JSON/JSONL CLI. Deterministic event IDs make replay idempotent and conflicting evidence fail closed.
- Added explicit-threshold classifiers for isolated concurrency, head-of-line blocking, starvation, session coupling, reconnect contamination, and no-output deadlock, plus read-only project-isolated recovery targeting.
- Added `docs/PROVIDER_RDC_EVIDENCE_CONTRACT.md` and focused synthetic fixtures covering provider/context/failover calculations, every RDC classification, import durability, and cross-project isolation.
- Verification: focused suites passed; final full suite passed with 470 tests and 16 subtests. Python compilation, userscript syntax, `git diff --check`, and Graphify AST refresh also passed.

## P11d Reporting and Quantitative Acceptance (2026-09-15)

- Added `build_p11_report()` as the deterministic P11 reporting boundary over an explicit UTC window and optional project/task/role scope.
- The report combines EDR and exclusive phase time, accepted/rejected work, plan-review churn, retry/owner-wait/idle loss, provider/context continuity, quota/rate-limit and failover evidence, and RDC classifications.
- Added project/task/role time rows, five explicit original-hypothesis results, unknown-data warnings, ranked bottlenecks with durable evidence IDs and bounded recommendations, and seven quantitative gates with pass/fail/unavailable states.
- Added the read-only `execution-report` CLI and `/api/accounting` endpoint. Custom accounting ledger paths are discovered through runtime metadata; explicit config reads do not initialize Failure Memory or write runtime state.
- Extended the 8770 dashboard with EDR, bottleneck, provider/context, RDC, hypothesis, scope-breakdown, gate and warning panels. Deterministic Node DOM fixtures verify measured, derived, inference-disabled and unavailable rendering.
- Documented reporting semantics and default thresholds in `docs/P11_REPORTING_ACCEPTANCE.md`; no scheduler, routing, provider, cancellation or project lifecycle policy was changed.
- Verification: 36 focused tests passed; full suite passed with 487 tests and 16 subtests. Python compilation, `web/app.js` and browser userscript syntax, `git diff --check`, and Graphify AST refresh (2512 nodes, 6675 edges, 131 communities) passed.
- No push was performed. P11d had no staged successor at completion; P12 was staged later by explicit design authorization. `xray-hw-platform` remains paused and unchanged.

## P12 Unified AI Control Surface design (2026-09-15)

- Materialized the historical P12 backlog into an executable, four-gate design and added `P12` as the staged successor of completed `P11d`.
- Froze `/api/v1/control/*` as the versioned 8770 contract while preserving existing GET routes and the 8875 broker-specialist surface.
- Kept all lifecycle effects behind the daemon-owned `ControlCommandCoordinator`; HTTP only projects state or durably enqueues authenticated commands.
- Defined cross-process idempotency, canonical replay/conflict, exact state-revision guards, crash recovery, redacted audit, broker failure isolation and explicit unknown provenance.
- Defined safe semantics for capability-advertised lifecycle and conversation-binding actions; unsupported actions remain unavailable rather than gaining a weaker fallback.
- Defined selective consolidation of prior conversation-control work under 8770 without a second 8766 authority or wholesale branch merge.
- Verification passed: staged-roadmap suite 22/22; full Python suite 487 tests plus 16 subtests; Python compilation, both JavaScript syntax checks, `git diff --check`, and Graphify AST refresh (2542 nodes, 6703 edges, 129 communities).
- Implementation was not started because the owner authorized P12 design only. `xray-hw-platform` remains paused and unchanged.
