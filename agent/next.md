# P10 Remediation Round 3 (R3-F1..F6)

Status: **COMPLETED (PENDING REVIEW)**

Goal: Fix all primary (R3-F1, R3-F2) and low-risk (R3-F3..F6) blockers from Opus review 3 of the P10 progress watchdog implementation.

## Fixes delivered

### R3-F1 — agent_stalled lifecycle and worker guard
- `diagnostics.py` `classify_evidence`: alive PID in non-worker lifecycle (not EXECUTING/REMEDIATING) now returns `unknown` + `owner_gate_required=True` instead of `agent_stalled`. This prevents stale/reused PIDs from the prior execution from being misclassified.
- `watchdog.py` `_check_and_trigger_recovery`: before RESERVE, verifies (a) current snapshot `lifecycle_state` ∈ `WORKER_EXPECTED_LIFECYCLE_STATES`, (b) evidence `worker_state` ∈ `ACTIVE_WORKER_STATES`. Non-worker lifecycle → `agent_stalled_non_worker_lifecycle` owner_gate; inactive worker state → `agent_stalled_worker_not_active` owner_gate.
- End-to-end test: PLANNING + PENDING DESIGN + stale alive PID never enqueues `wd-continue`.

### R3-F2 — stale evidence bounding before RESERVE
- `watchdog.py` `_check_and_trigger_recovery`: re-probes current active-run PID against evidence PID. Mismatch → `agent_stalled_pid_mismatch` / `process_dead_pid_mismatch` owner_gate. Evidence older than `cooldown_minutes` → `evidence_stale_beyond_cooldown` owner_gate. Auto_recovery toggled on within cooldown proceeds normally (evidence is fresh).

### R3-F3 — watchdog_payload schema validation
- `web/server.py` `watchdog_payload`: validates schema version before using the file's `degraded` flag. Future-version or invalid version files return `degraded=True` even when the coordinator could not write its in-memory state back due to `_preserve_existing_state_file`.

### R3-F4 — quarantine identity for unreadable files
- `watchdog.py` `_quarantine_corrupt_state`: uses reason-based SHA-256 hash (not constant empty-bytes hash `e3b0c44298fc1c14`) when `raw_bytes` is empty, so distinct unreadable error types get distinct quarantine identities.

### R3-F5 — clear-degraded CLI fail-closed
- `cli.py` `cmd_watchdog_clear_degraded`: refuses to run if daemon PID is alive; also requires at least one `watchdog.json.corrupt-*` quarantine file to exist before overwriting state.

### R3-F6 — prune retains consumed recovery budgets
- `watchdog.py` `_prune_state`: adds consumed (non-null) `recovery_slots` keys to `keep_run_scopes` before pruning, so long-lived projects that exceed `MAX_TERMINAL_ATTEMPTS_PER_PROJECT` cannot silently lose their per-run-scope single-recovery budget.

## Test results
- 310 unit tests passing (11 new regressions added).
- `node --check browser/chatgpt-web-adapter.user.js` clean.
- `git diff --check` clean.

## Scope constraints
- No changes to stable controller `C:\work\github\DevOrchestrator`.
- No push, no destructive git, no credential or hardware actions.
- Architecture unchanged; all changes are surgical within existing abstractions.
