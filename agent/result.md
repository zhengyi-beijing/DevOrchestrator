# DevOrchestrator Self-Hosting Result Log

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
