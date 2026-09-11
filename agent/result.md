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
  - Wired `ProgressChannel` into `TransitionExecutor`, `AIReviewerCoordinator`, `AIPlannerCoordinator`, `ControlCommandCoordinator`, and `daemon.py`.
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
- Verification:
  - 209 unit tests passing cleanly (`python -m unittest discover -s tests_py`).
  - `node --check browser/chatgpt-web-adapter.user.js` passing.
  - `git diff --check` clean.
