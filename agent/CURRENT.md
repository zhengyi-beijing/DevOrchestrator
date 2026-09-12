# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator` is running the accepted D1/P8 release; Workers must not modify or restart it.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Branch: `feature/self-hosted-dev`.
- D1/P8 is complete and promoted. Current development target is Gate B daily-use readiness.

Current task: **P10 Active-Project Progress Watchdog and Automatic Diagnostics** (COMPLETED, pending review).

Purpose: make DevOrchestrator detect active projects exceeding a configurable no-progress threshold, gather deterministic read-only diagnostics without model inference, and safely trigger recovery or escalate to OWNER_GATE without false progress self-refresh loops or clock leakage.

Status:
- Comprehensive design documented and frozen in `docs/PROGRESS_WATCHDOG_DIAGNOSTICS_DESIGN.md` (V5 Accepted Design).
- Complete watchdog coordinator and bounded read-only diagnostics implemented in `src/dev_orchestrator/core/watchdog.py` and `src/dev_orchestrator/core/diagnostics.py`.
- Source-level activity filtering and dual aggregation implemented in `src/dev_orchestrator/adapters/agent_files.py`.
- Canonical path resolution and commonpath containment with dual provenance verification.
- Clock-free progress fingerprint purity strictly enforced with frozen allowlist and forbidden timing/age denylist.
- Fail-closed state persistence with corruption quarantine (`watchdog.json.corrupt-<stamp>`) and degraded mode.
- Two-phase safe recovery (`RESERVE` -> `ENQUEUE` -> `RECONCILE`) with deterministic `wd-<attempt_key>` command IDs and single recovery per run scope.
- Read-only web projection `GET /api/watchdog` and CLI subcommand `watchdog-status`.
- Full daemon integration in `src/dev_orchestrator/daemon.py`.
- 281 unit tests passing cleanly in full test suite discovery (`tests_py/`).
- Browser adapter userscript syntax validated (`node --check browser/chatgpt-web-adapter.user.js`).
- Git diff formatting verified clean (`git diff --check`).
- Config validation smoke-tested (`python -m dev_orchestrator validate-config`).
- Static analysis guards verified: zero destructive process killing or Git write commands in watchdog or diagnostics.
