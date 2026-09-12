# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator` is running the accepted D1/P8 release; Workers must not modify or restart it.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Branch: `feature/self-hosted-dev`.
- D1/P8 is complete and promoted. Current development target is Gate B daily-use readiness.

Current task: **P10 Remediation Round 3 (R3-F1..F6)** (COMPLETED, pending review).

Purpose: remediate all primary (R3-F1, R3-F2) and low-risk (R3-F3..F6) blockers from Opus review 3 of the P10 progress watchdog and automatic diagnostics implementation.

Status:
- **R3-F1 FIXED**: `agent_stalled` classification and recovery now restricted to EXECUTING/REMEDIATING lifecycles. Alive stale/reused PIDs in PLANNING, REVIEWING, REVIEWING_PLAN, APPLYING_PLAN return `unknown` + owner_gate. Recovery guard in `_check_and_trigger_recovery` checks current snapshot lifecycle against WORKER_EXPECTED_LIFECYCLE_STATES and evidence `worker_state` against ACTIVE_WORKER_STATES before enqueuing any `wd-` command.
- **R3-F2 FIXED**: Before RESERVE, re-probes current active-run PID against evidence PID (for both agent_stalled and process_dead). Evidence age bounded by cooldown window (`completed_at + cooldown_minutes`). Auto_recovery toggled on within cooldown proceeds (fresh evidence); stale evidence beyond cooldown blocks with owner_gate.
- **R3-F3 FIXED**: `watchdog_payload` validates schema version before trusting file's degraded flag — catches future-version files where `_preserve_existing_state_file` prevented the coordinator from writing its degraded state back.
- **R3-F4 FIXED**: `_quarantine_corrupt_state` uses reason-based hash (not constant empty-bytes hash) when `raw_bytes` is empty (file unreadable), so distinct unreadable errors get distinct quarantine identities.
- **R3-F5 FIXED**: `watchdog-clear-degraded` CLI now fails closed if daemon is alive (checks `daemon.pid`) and requires at least one verified quarantine file to exist before overwriting state.
- **R3-F6 FIXED**: `_prune_state` retains recovery_slots for all run scopes with consumed (non-null) slots, even when the corresponding attempt falls off the MAX_TERMINAL_ATTEMPTS_PER_PROJECT window.
- End-to-end regression `test_f1_planning_pending_design_stale_pid_cannot_enqueue_wd` demonstrates PLANNING + PENDING DESIGN + stale alive PID cannot enqueue wd- or trigger second planner.
- 310 unit tests passing (added 11 new regression tests).
- Browser adapter userscript syntax validated (`node --check`).
- Git diff check clean.
