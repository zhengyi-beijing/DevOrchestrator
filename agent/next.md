# Agent Backend + Router remediation — COMPLETE (handoff to fresh Reviewer)

STATUS: REMEDIATION COMPLETE — verify with a fresh Reviewer cycle
BASELINE: `feature/agent-router-foundation` @ `81120db`
AUTHORITY: frozen design + Reviewer regression `tests_py/test_agent_reviewer_regressions.py` (unweakened)

## What the Worker changed

1. **Defect 1 — Router probe containment** (`src/dev_orchestrator/agents/router.py`): `route()` now wraps each registered backend's `probe()` in a try/except. A raised probe records that backend as ineligible with explicit `probe error: <exc>` evidence and evaluation continues over the remaining backends, so a healthy eligible fallback remains selectable. Routing remains side-effect free.
2. **Defect 2 — Dsh terminal settlement** (`src/dev_orchestrator/agents/backends/dsh.py`): per-run records carry a `settled` flag; `status()`/`collect()` finalize state/exit_code/log handles exactly once when a naturally ended process is observed (idempotent, re-entrant `_settle()`); `cancel()` preserves an already-terminal run's true terminal state and only kills/marks CANCELLED a genuinely running process. The start-failure path also settles the record.

No test file was modified. No code outside the two modules above was changed.

## Local evidence (Worker run)

- `tests_py/test_agent_reviewer_regressions.py`: 2/2 PASS.
- Full `tests_py`: **Ran 19 tests — OK**.
- PowerShell telemetry self-test: 10/10 PASS; PowerShell Web self-test: PASS.
- Real `DshBackend.probe()` on this host: available; probe argv captured as `['--version']` only (never a prompt/profile flag).
- Real LabDemo `monitor --once`: exit 0; HEAD unchanged, `git status --porcelain` unchanged, file timestamps unchanged (`LAB_FILE_TIMES_CHANGED=0`).
- `git diff --check`: exit 0 (tracked diff and untracked new files both clean).
- Detailed record: gitignored `runtime/review2-evidence/README.md` (+ transcripts in that tree).

## Reviewer verification checklist

Re-run the local evidence above (sandbox note: the DSH workspace-write sandbox makes `tempfile` scratch dirs unwritable unless a `sitecustomize` shim widens `os.mkdir` modes — the shim used is `runtime/review2-evidence/pyfix/sitecustomize.py` on PYTHONPATH; it is session-local and never ships in `src/`).

## STOP / non-goals (unchanged)

No PHASE_AUTO, no automatic AI workflow invocation, no generic prompt HTTP/CLI surface, no other provider adapters, no quota scraping, no cross-process run recovery, no remote nodes, no LabDemo P4.3.4, no hardware actions, and no unrelated cleanup. Stop for the fresh Reviewer cycle verdict.
