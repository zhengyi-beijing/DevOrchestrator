# P10 Remediation Round 4 (FR-1, FR-2, FR-3)

Status: **COMPLETED (PENDING REVIEW)**

Goal: Fix all three Sol promotion review blockers for P10 on HEAD 6dcd2e1.

## Fixes delivered

### FR-1 HIGH — async diagnostic completion deferred to next advance tick
- **Root cause**: `_run_diagnostic_worker` called `_check_and_trigger_recovery` directly with the `snapshot` captured at diagnostic start.  If the project transitioned EXECUTING → PLANNING/REVIEWING during the diagnostic window (up to 120 s), recovery would act on a stale lifecycle, run_scope_key, and worker identity.
- **Fix**: Removed the direct call from `_run_diagnostic_worker`.  The diagnostic worker now only writes the completed attempt and saves state.  The next `advance()` tick finds the completed attempt via the dedup path (`att_key in attempts`) and calls `_check_and_trigger_recovery` with the current snapshot, which may be PLANNING, REVIEWING, or any other fresh lifecycle.
- **Regression**: `test_fr1_stale_snapshot_not_used_at_diagnostic_completion` — diagnostic starts in EXECUTING, subsequent call to `_check_and_trigger_recovery` with PLANNING and REVIEWING snapshots fires `OWNER_GATE` and enqueues no `wd-` command.

### FR-2 HIGH — attempt schema validation and evidence_hash recomputation before RESERVE
- **Root cause**: `_check_and_trigger_recovery` relied on stored `evidence` and `evidence_hash` without revalidation, could not detect corrupted/tampered persisted attempts, and allowed None PIDs to skip identity checks.
- **Fix**: Added fail-closed validation immediately after the diag-code guard:
  1. `evidence` must be a non-null dict (gate: `malformed_attempt_no_evidence`).
  2. `evidence_hash` must be non-null (gate: `malformed_attempt_no_evidence_hash`).
  3. `evidence_hash(evidence)` recomputed — must exactly match stored value.  Mismatch marks attempt `state="quarantined"` (gate: `evidence_hash_mismatch`).
  4. `proc_liveness.pid` must be non-null (gate: `malformed_attempt_missing_pid`).
  5. `proc_liveness.process_alive` key must be present (gate: `malformed_attempt_missing_process_alive`).
  6. `agent_stalled` requires `process_alive=True` (gate: `evidence_inconsistent_agent_stalled_dead`).
- All eleven existing test fixtures updated to carry correct 16-char SHA-256 `evidence_hash` values.
- **New tests**: `test_fr2_altered_evidence_hash_quarantines_attempt`, `test_fr2_missing_evidence_hash_blocks_recovery`, `test_fr2_missing_pid_blocks_recovery`, `test_fr2_missing_process_alive_blocks_recovery`, `test_fr2_malformed_completed_attempt_no_evidence_dict`, `test_fr2_agent_stalled_with_dead_pid_inconsistent_with_diagnosis`.

### FR-3 MEDIUM — clear_degraded verifies matching quarantine artifact
- **Root cause**: `clear_degraded()` reset the degraded flag unconditionally without verifying that the quarantine artifact corresponding to the active corruption event exists and is readable.
- **Fix**:
  - `_quarantine_corrupt_state` now stores `corrupt_identity` (the 16-char file hash used in the quarantine filename) in the returned state dict.
  - `clear_degraded()` locates all `watchdog.json.corrupt-*{corrupt_identity}*` files and requires at least one to be readable (non-empty bytes, no IOError).  On failure, emits `OWNER_GATE` with gate reason `no_matching_quarantine_artifact` or `quarantine_artifact_unreadable` and returns without clearing.
  - Added module-level helper `_is_quarantine_file_readable(path)`.
- **New tests**: `test_fr3_clear_degraded_blocked_no_quarantine_file`, `test_fr3_clear_degraded_blocked_wrong_hash_quarantine_file`, `test_fr3_clear_degraded_blocked_empty_quarantine_file`, `test_fr3_clear_degraded_blocked_unreadable_quarantine_file`, `test_fr3_corrupt_identity_stored_in_degraded_state`.

## Test results
- **322 unit tests passing** (12 new regressions added, prior: 310).
- `node --check browser/chatgpt-web-adapter.user.js` clean.
- `git diff --check` clean.

## Scope constraints
- No changes to stable controller `C:\work\github\DevOrchestrator`.
- No push, no destructive git, no credential or hardware actions.
- All changes are surgical within existing abstractions.
