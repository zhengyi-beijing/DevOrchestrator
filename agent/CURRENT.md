# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator` is running the accepted D1/P8 release; Workers must not modify or restart it.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Branch: `feature/self-hosted-dev`.
- D1/P8 is complete and promoted. Current development target is Gate B daily-use readiness.

Current task: **P10 Remediation Round 7 (Live Identity Binding + Record Integrity)** (COMPLETED, pending review).

Purpose: fix two remaining GPT-5.6 Sol promotion blockers — BLOCKER A (live identity binding for agent_stalled/process_dead) and BLOCKER B (complete actuation-record integrity hash).

Status:
- **BLOCKER A FIXED** (Round 7): `_check_and_trigger_recovery` now requires a non-empty active current worker state for `agent_stalled` (not conditionally accepting missing state). Worker state check moved before PID check so an absent worker dict fails at `agent_stalled_current_worker_not_active` (not `agent_stalled_current_pid_absent`). Both `agent_stalled` and `process_dead` now verify `started_at` binding: if evidence has `started_at`, the current worker must carry the same value — a reused PID owned by a new process has a different `started_at` and fails closed. New gate reasons: `agent_stalled_started_at_mismatch`, `process_dead_started_at_mismatch`.
- **BLOCKER B FIXED** (Round 7): `compute_record_integrity_hash` function added — stable SHA-256[:16] over `attempt_key`, `run_scope_key`, `diagnosis`, `completed_at`, `evidence_hash`. Persisted as `record_integrity_hash` in `_run_diagnostic_worker` when completing an attempt. In `_check_and_trigger_recovery`, checks before RESERVE: (1) internal `attempt_key` must equal the map key parameter (gate: `attempt_key_mismatch`, quarantine); (2) `record_integrity_hash` must be present (gate: `malformed_attempt_no_record_hash`); (3) recomputed hash must match stored (gate: `record_integrity_hash_mismatch`, quarantine). Legacy/missing hash fails closed (never silently trusted).
- 352 unit tests passing (11 new regressions added: 4 BLOCKER A, 7 BLOCKER B — Round 7 adds 11 tests to 341 → 352).
- Browser adapter userscript syntax validated (`node --check`).
- Git diff check clean.
