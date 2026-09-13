# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator`.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Development branch: `feature/self-hosted-dev`.
- Stable branch: `feature/browser-bridge-multiproject`.

Current task: **P11x Deferred Staged Handoff (Planner-Owned Materialization)** — **COMPLETED / READY FOR REVIEW**.

Implementation summary:
- Added pure staged-roadmap reader `src/dev_orchestrator/core/staged_roadmap.py` with `RoadmapResult`, `read_successor()`, `read_raw()`, and `sha256_bytes()` using existing `extract_task_id` telemetry helper.
- Validated `agent/staged/roadmap.json` against strict schema v1, relative POSIX spec paths under `agent/staged/`, non-null parity, duplicate/self task ID guards, and strict UTF-8 LF-only spec contracts.
- Extended `TransitionExecutor._record_handoff()` to capture `staged_successor`, `staged_spec_path`, `staged_spec_sha256`, `reviewed_branch`, and `reviewed_head` without mutating repository state or storing raw spec text.
- Wired reviewer-'next' COMPLETE path in `TransitionExecutor.advance()` to inspect `read_successor()`: fail-closed on invalid roadmap, settle on absent or `successor: null`, and record staged handoff on successor.
- Extended `ControlCommandCoordinator._resume_decision_handoffs()` to recognize staged handoff rows and invoke `AIPlannerCoordinator.start_deferred()`.
- Refactored `AIPlannerCoordinator.start()` via shared `_begin_lifecycle()`, and added `start_deferred()` enforcing strict repository truth, clean worktree, branch/head match, predecessor `agent/next.md` digest, and staged spec digest validation.
- Implemented `AIPlannerCoordinator._task_source()` returning staged successor spec and audit header for deferred records while preserving exact byte-identical prompt output for non-deferred runs across initial planner, retry, remediation, and review prompts.
- Implemented `_apply_deferred_plan()` in `AIPlannerCoordinator` with strict pre-write digest re-checks, TOCTOU repository truth re-read, atomic write of rendered spec to `agent/next.md`, single commit via `_commit_plan()`, and reset-free predecessor byte restoration recovery (`git add -- agent/next.md`) if write/commit fails.
- Added comprehensive unit and integration tests:
  - `tests_py/test_staged_roadmap.py` (22 tests): absent file, malformed JSON, schema version, duplicate/blank tasks, path traversal, non-POSIX, CRLF, non-UTF-8, heading mismatch, non-PENDING status, approved marker, end-of-roadmap, and real-repo roadmap validation (`P11x -> P11b -> P11c -> P11d -> null`).
  - `tests_py/test_transition_executor.py`: staged handoff row emission, invalid roadmap fail-closed blocking, and end-of-roadmap terminal settlement.
  - `tests_py/test_control_commands.py`: staged handoff resumption, idempotency across ticks/instances, and snapshot task/status mismatch filtering.
  - `tests_py/test_staged_handoff.py` (11 tests): deferred prompt generation, start_deferred guards (head moved, dirty worktree, branch mismatch, digest mismatch, missing metadata), apply pre-write guards, pre-write truth race condition, reset-free recovery without prohibited git commands, end-to-end handoff execution, and restart/idempotency.
  - `tests_py/conftest.py`: ensures local repository `src/` is prioritized on `sys.path`.
- Full test suite: 415 passed, 16 subtests passed (`python -m pytest tests_py -q`).
- Userscript syntax check: `node --check browser/chatgpt-web-adapter.user.js` passed.
- Repository hygiene: `git diff --check` clean.
