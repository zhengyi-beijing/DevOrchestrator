# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator`.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Development branch: `feature/self-hosted-dev`.
- Stable branch: `feature/browser-bridge-multiproject`.

Current task: **P11-A Bounded Plan-Review Remediation Loop** — **COMPLETED / READY FOR REVIEW**.

Implementation summary:
- Added `max_plan_remediation_rounds` (default 3, valid range 1..5) policy validation to `_planner_policy` in `src/dev_orchestrator/core/ai_planner.py`.
- Added `'remediating'` to `_ACTIVE_STATES` in `ai_planner.py` for crash-atomic `recovery_required` transition on restart.
- Refactored `_run_cycle` into `_run_planner_attempts`, `_run_plan_review`, and bounded remediation loop.
- Ensured unique request IDs per round (`:planner:remediate-R`, `:reviewer:remediate-R`) and prompt remediation context injection with exact reviewer rejection reason, prior plan JSON, task ID, planning HEAD, and prior planner resource.
- Recorded durable `rejection_chain` tracking round, reason, prior plan, dispatch/execution IDs, resource payloads, and timestamps.
- Emitted progress milestones `REMEDIATE` on automatic revision and `OWNER_GATE` on bounded exhaustion.
- Mapped plan state `'remediating'` to `REMEDIATING_PLAN` in `lifecycle_projection.py` and `project_status.py` (exposing `remediation_round` and `rejection_chain_length`).
- Added `REMEDIATING_PLAN` to watchdog `ACTIVE_LIFECYCLE_STATES` (in `PLANNING` family) and `_ALLOWED_WATCHDOG_LIFECYCLES` in `config.py`.
- 6 new dedicated unit tests in `tests_py/test_ai_planner.py` plus regression tests in `test_lifecycle_projection.py`, `test_project_status.py`, `test_watchdog.py`, and `test_project_config_contract.py`.
- Full test suite: 372 unit tests passing (`python -m unittest discover -s tests_py`).
- Userscript syntax check: `node --check browser/chatgpt-web-adapter.user.js` passed.
- Repository hygiene: `git diff --check` clean.
