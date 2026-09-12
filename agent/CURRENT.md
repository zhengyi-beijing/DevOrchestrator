# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator` is running the accepted D1/P8 release; Workers must not modify or restart it.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Branch: `feature/self-hosted-dev`.
- D1/P8 is complete and promoted. Current development target is Gate B daily-use readiness.

Current task: **P10 Remediation Round 6 (FR2B-LIVE-IDENTITY)** (COMPLETED, pending review).

Purpose: fix the single remaining GPT-5.6 Sol promotion blocker P10-FR2B-LIVE-IDENTITY (PID equality against snapshot insufficient for safe actuation).

Status:
- **FR-2A FIXED** (Round 5): `_check_and_trigger_recovery` now fails closed unless `completed_at` is (1) present, (2) parseable by `parse_utc`, (3) not materially future-dated (tolerance: `RECOVERY_COMPLETED_AT_CLOCK_SKEW_S = 60` s), and (4) within the cooldown window. New OWNER_GATE reasons: `evidence_missing_completed_at`, `evidence_unparseable_completed_at`, `evidence_completed_at_future`.
- **FR-2B FIXED** (Round 5): `_check_and_trigger_recovery` fails closed when current snapshot has no worker PID. New OWNER_GATE reasons: `agent_stalled_current_pid_absent`, `agent_stalled_pid_mismatch`, `process_dead_current_pid_absent`, `process_dead_pid_mismatch`.
- **FR-3 FIXED** (Round 5, cryptographic binding): `clear_degraded` requires `corrupt_identity`, uses exact trailing-segment match, recomputes SHA-256[:16] for content verification.
- **FR2B-LIVE-IDENTITY FIXED** (Round 6): `_check_and_trigger_recovery` now re-probes live process liveness at recovery time using `is_pid_alive` from the platform process abstraction — PID equality against a static snapshot is no longer sufficient. For `agent_stalled`: after PID match, checks current snapshot worker state is active (new gate: `agent_stalled_current_worker_not_active`) then calls `self._liveness_probe(current_pid)`; if not True (dead or probe exception) → `agent_stalled_pid_not_alive_at_recovery`. For `process_dead`: calls `self._liveness_probe(ev_pid)`; if alive → `process_dead_pid_alive_at_recovery`; if probe exception → `process_dead_liveness_probe_unavailable`. `WatchdogCoordinator.__init__` now accepts `liveness_probe` kwarg (injectable for testing; defaults to `is_pid_alive`). 7 new regression tests: PID dead at recovery, current worker inactive/terminal, probe raises exception (agent_stalled), PID alive at recovery (process_dead), probe raises exception (process_dead), plus two happy-path preserve tests (agent_stalled and process_dead).
- 341 unit tests passing (7 new regressions added for round 6, 334 → 341).
- Browser adapter userscript syntax validated (`node --check`).
- Git diff check clean.
