# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator` is running the accepted D1/P8 release; Workers must not modify or restart it.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Branch: `feature/self-hosted-dev`.
- D1/P8 is complete and promoted. Current development target is Gate B daily-use readiness.

Current task: **P10 Remediation Round 4 (FR-1, FR-2, FR-3)** (COMPLETED, pending review).

Purpose: remediate Sol promotion review findings FR-1, FR-2, FR-3 (P10 round 4) on HEAD 6dcd2e1.

Status:
- **FR-1 FIXED**: Async diagnostic completion no longer actuates recovery using the stale snapshot captured at diagnostic start. The direct `_check_and_trigger_recovery` call was removed from `_run_diagnostic_worker`. Recovery actuation is now deferred to the next watchdog advance tick (dedup path), which always supplies the latest projected snapshot. Regression: `test_fr1_stale_snapshot_not_used_at_diagnostic_completion` verifies EXECUTING→PLANNING/REVIEWING lifecycle change blocks recovery.
- **FR-2 FIXED**: `_check_and_trigger_recovery` now validates persisted attempt schema before any recovery: (1) evidence dict present; (2) evidence_hash non-null; (3) evidence_hash recomputed and exact match required — mismatch quarantines the attempt; (4) non-null PID required in process_liveness; (5) process_alive key required; (6) diagnosis-consistent liveness: agent_stalled requires process_alive=True. All existing test fixtures updated with correct SHA-256 evidence_hash values. New tests: altered hash, missing hash, missing PID, missing process_alive, malformed evidence dict, agent_stalled+dead PID.
- **FR-3 FIXED**: `clear_degraded()` now verifies the quarantine artifact corresponding to the active corruption identity exists and is readable before clearing. `_quarantine_corrupt_state` persists `corrupt_identity` (16-char hash) in `_cached_state`. `clear_degraded()` gates with OWNER_GATE on: no matching file, wrong-hash file, empty file, unreadable file. New tests: no quarantine file, wrong hash, empty file, unreadable file, corrupt_identity stored correctly.
- 322 unit tests passing (12 new regressions added, 310 → 322).
- Browser adapter userscript syntax validated (`node --check`).
- Git diff check clean.
