# P10 Remediation Round 7 (Live Identity Binding + Record Integrity)

Status: **COMPLETED (PENDING REVIEW)**

Goal: Fix two remaining GPT-5.6 Sol promotion blockers on HEAD 935e916.

## Fixes delivered

### BLOCKER A HIGH — live identity binding

- **Root cause 1**: `agent_stalled` worker state check was conditional (`if current_worker_state and ...`) — an empty or absent worker state silently passed the check without verifying it was active.
- **Root cause 2**: Recovery bound only to PID equality + liveness; a reused PID from an unrelated/new process could match. No stable execution identity (e.g. `started_at`) was checked.
- **Fix 1**: Changed `if current_worker_state and current_worker_state not in ACTIVE_WORKER_STATES:` to `if not current_worker_state or current_worker_state not in ACTIVE_WORKER_STATES:`. Moved check BEFORE PID check so absent worker dict fails at `agent_stalled_current_worker_not_active`.
- **Fix 2**: Added `started_at` identity binding for both `agent_stalled` and `process_dead`. If evidence has `started_at`, the current worker must carry the same value. A reused PID owned by a new process has a different `started_at` — fails closed. New gate reasons: `agent_stalled_started_at_mismatch`, `process_dead_started_at_mismatch`.
- **Regressions** (4 new tests): empty/absent worker state blocked, PID reuse detected via `started_at` mismatch for both diagnosis types, happy path with matching `started_at` proceeds, updated existing test `test_fr2b_agent_stalled_no_worker_in_snapshot_blocks_recovery` expected reason.

### BLOCKER B MAJOR — complete actuation-record integrity

- **Root cause**: Only `evidence_hash` was persisted and verified before RESERVE. Tampered `diagnosis`, `completed_at`, `attempt_key`, or `run_scope_key` were undetectable; a substituted record could consume a wrong recovery slot.
- **Fix**: Added `compute_record_integrity_hash(attempt_record)` — stable SHA-256[:16] over `attempt_key`, `run_scope_key`, `diagnosis`, `completed_at`, `evidence_hash`. Persisted as `record_integrity_hash` in `_run_diagnostic_worker`. Added three pre-RESERVE checks in `_check_and_trigger_recovery`: (1) `attempt_record["attempt_key"] == attempt_key` parameter (gate: `attempt_key_mismatch`, quarantine); (2) `record_integrity_hash` present (gate: `malformed_attempt_no_record_hash`); (3) recomputed hash matches stored (gate: `record_integrity_hash_mismatch`, quarantine). Legacy records without the field fail closed (never silently trusted). Updated 5 existing happy-path tests to include `record_integrity_hash`.
- **Regressions** (7 new tests): hash covers all fields (unit test), missing hash blocked, tampered `diagnosis` blocked, tampered `completed_at` quarantined, tampered `attempt_key` in record quarantined, tampered `run_scope_key` quarantined, happy-path preserved.

## Test results
- **352 unit tests passing** (11 new regressions added, prior: 341).
- `node --check browser/chatgpt-web-adapter.user.js` clean.
- `git diff --check` clean.

## Scope constraints
- No changes to stable controller `C:\work\github\DevOrchestrator`.
- No push, no destructive git, no credential or hardware actions.
- All changes are surgical within existing watchdog abstractions.
