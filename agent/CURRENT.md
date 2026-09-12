# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator` is running the accepted D1/P8 release; Workers must not modify or restart it.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Branch: `feature/self-hosted-dev`.
- D1/P8 is complete and promoted. Current development target is Gate B daily-use readiness.

Current task: **P10 Remediation Round 5 (FR-2A, FR-2B, FR-3 cryptographic binding)** (COMPLETED, pending review).

Purpose: remediate three remaining GPT-5.6 Sol promotion review findings on HEAD 44e597e.

Status:
- **FR-2A FIXED**: `_check_and_trigger_recovery` now fails closed unless `completed_at` is (1) present, (2) parseable by `parse_utc`, (3) not materially future-dated (tolerance: `RECOVERY_COMPLETED_AT_CLOCK_SKEW_S = 60` s), and (4) within the cooldown window. Previously the check was optional (skipped entirely if `completed_at` absent). New OWNER_GATE reasons: `evidence_missing_completed_at`, `evidence_unparseable_completed_at`, `evidence_completed_at_future`. New tests: missing, malformed, too-future, stale `completed_at`.
- **FR-2B FIXED**: `_check_and_trigger_recovery` now fails closed when the current snapshot has no worker PID. For `agent_stalled`: absent `current_pid` → `agent_stalled_current_pid_absent`; mismatch → `agent_stalled_pid_mismatch`. For `process_dead`: absent `current_pid` → `process_dead_current_pid_absent`; mismatch → `process_dead_pid_mismatch`. Previously absent PID was treated as "no mismatch" (allowed). New tests: missing current PID for both diagnosis types.
- **FR-3 FIXED (cryptographic binding)**: `clear_degraded` now (1) requires `corrupt_identity` to be present (absent → `corrupt_identity_absent` gate, fail closed); (2) uses exact trailing-segment match (`f.name.endswith(f"-{corrupt_identity}")`) not substring; (3) recomputes SHA-256[:16] of artifact bytes and compares to stored identity — content mismatch → `quarantine_artifact_content_mismatch`. `_quarantine_corrupt_state`: when raw_bytes unavailable, copies file, reads back copied bytes, derives identity from copy (not reason-string); if copy/read fails, `corrupt_identity = None` (fail closed). New tests: absent identity, content mismatch, correct content, copy-semantics identity derivation.
- 334 unit tests passing (12 new regressions added for round 5, 322 → 334).
- Browser adapter userscript syntax validated (`node --check`).
- Git diff check clean.
