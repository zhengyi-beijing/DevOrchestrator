# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator`.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Development branch: `feature/self-hosted-dev`.
- Stable branch: `feature/browser-bridge-multiproject`.

Current task: **P11x Deferred Staged Handoff (Planner-Owned Materialization)** — **REMEDIATED / READY FOR REVIEW**.

Implementation summary:
- Added pure staged-roadmap reader `src/dev_orchestrator/core/staged_roadmap.py` with `RoadmapResult`, `read_successor()`, `read_raw()`, and `sha256_bytes()` using existing `extract_task_id` telemetry helper.
- Validated `agent/staged/roadmap.json` against strict schema v1, relative POSIX spec paths under `agent/staged/`, non-null parity, duplicate/self task ID guards, and strict UTF-8 LF-only spec contracts.
- Extended `TransitionExecutor._record_handoff()` to capture `staged_successor`, `staged_spec_path`, `staged_spec_sha256`, `reviewed_branch`, and `reviewed_head` without mutating repository state or storing raw spec text.
- Wired reviewer-'next' COMPLETE path in `TransitionExecutor.advance()` to inspect `read_successor()`: fail-closed on invalid roadmap, settle on absent or `successor: null`, and record staged handoff on successor.
- Extended `ControlCommandCoordinator._resume_decision_handoffs()` to recognize staged handoff rows and invoke `AIPlannerCoordinator.start_deferred()`.
- Refactored `AIPlannerCoordinator.start()` via shared `_begin_lifecycle()`, and added `start_deferred()` enforcing strict repository truth, clean worktree, branch/head match, predecessor `agent/next.md` digest, and staged spec digest validation.
- Implemented `AIPlannerCoordinator._task_source()` returning staged successor spec and audit header for deferred records while preserving exact byte-identical prompt output for non-deferred runs across initial planner, retry, remediation, and review prompts.
- Implemented `_apply_deferred_plan()` in `AIPlannerCoordinator` with strict pre-write digest re-checks, TOCTOU repository truth re-read, atomic write of rendered spec to `agent/next.md`, single commit via `_commit_plan()`, and reset-free predecessor byte restoration recovery (`git add -- agent/next.md`) if write/commit fails.
- P11x Review Remediation:
  - Tightened staged-row eligibility check in `ControlCommandCoordinator._resume_decision_handoffs` using `re.search(r"\bCOMPLETED?\b", ...)` regex word boundaries to reject non-matching statuses such as `INCOMPLETE`.
  - Added regression test in `tests_py/test_control_commands.py` proving `INCOMPLETE` is rejected and not consumed.
  - Added deferred apply guard test `test_deferred_apply_new_commit_fails` in `tests_py/test_staged_handoff.py` proving a concurrent commit fails with `'repository changed during planning'` with HEAD and `agent/next.md` bytes unchanged.
  - Added recovery test `test_deferred_recovery_when_head_moved_leaves_worktree_untouched` in `tests_py/test_staged_handoff.py` proving that if commit succeeds and post-commit failure raises, the worktree is left untouched at the new commit without prohibited git commands.
  - Added restart/idempotency test `test_restart_idempotency_after_deferred_start_before_apply` in `tests_py/test_staged_handoff.py` proving restart between deferred start and apply transitions in-flight plan to `recovery_required`, re-running ticks does not duplicate planner, makes no new port calls, produces no commit, and leaves `agent/next.md` predecessor bytes untouched.
- Unit and integration tests:
  - `tests_py/test_staged_roadmap.py` (22 tests): absent file, malformed JSON, schema version, duplicate/blank tasks, path traversal, non-POSIX, CRLF, non-UTF-8, heading mismatch, non-PENDING status, approved marker, end-of-roadmap, and real-repo roadmap validation (`P11x -> P11b -> P11c -> P11d -> null`).
  - `tests_py/test_transition_executor.py`: staged handoff row emission, invalid roadmap fail-closed blocking, and end-of-roadmap terminal settlement.
  - `tests_py/test_control_commands.py`: staged handoff resumption, idempotency across ticks/instances, snapshot task/status mismatch filtering, and `INCOMPLETE` rejection.
  - `tests_py/test_staged_handoff.py` (14 tests): deferred prompt generation, start_deferred guards (head moved, dirty worktree, branch mismatch, digest mismatch, missing metadata), apply pre-write guards, pre-write truth race condition, concurrent commit failure, reset-free recovery without prohibited git commands, post-commit failure with moved HEAD leaving worktree untouched, end-to-end handoff execution, and restart/idempotency both before and after apply.
  - `tests_py/conftest.py`: ensures local repository `src/` is prioritized on `sys.path`.
- Full test suite: 418 passed, 16 subtests passed (`python -m pytest tests_py -q`).
- Userscript syntax check: `node --check browser/chatgpt-web-adapter.user.js` passed.
- Repository hygiene: `git diff --check` clean.
