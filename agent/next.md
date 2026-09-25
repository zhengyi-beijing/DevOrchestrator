# P16.10 - Automatic Failure Harvesting & Regression Promotion

Status: **PENDING DESIGN**

## Goal

Convert unattended-execution anomalies and control-only owner interventions into durable incident families and validated regression candidates automatically.

## Problems to close

1. Unattended-execution anomalies (such as watchdog execution loss, transient worker failures, launch gaps, or recovery handoffs) often require manual inspection or ad-hoc debugging to understand failure patterns.
2. Manual `continue`, `retry`, `reconcile`, or restart interventions that supply no new owner information but restore forward progress are symptoms of unhandled or incomplete recovery. These control-only interventions should be automatically recorded as first-class quality metrics and incident signals.
3. Incidents must be fingerprinted and deduplicated so recurring anomalies strengthen an existing incident family rather than generating noise or duplicate records.
4. Candidate regression fixtures and tests should be synthesized from incident evidence automatically, but must remain staged outside the permanent regression suite until reproduction, discrimination, stability, and independent review gates pass.

## Requirements & Scope

- Treat any `continue`, `retry`, `reconcile`, or restart command that supplies no new owner information (such as identical parameters/HEAD) but restores progress as an incident signal (`CONTROL_ONLY_INTERVENTION`).
- Capture authoritative before/after lifecycle, execution, review, broker, watchdog, Git/HEAD, and recovery evidence in restart-safe incident packets (`runtime/incident-packets/`).
- Compute stable semantic failure fingerprints over normalized failure causes, lifecycle transitions, and diagnostic evidence to deduplicate recurring failures.
- Generate candidate deterministic fixtures/tests automatically, keeping candidate tests isolated in a staging area (e.g. `tests_candidate/`) until automated reproduction, discrimination, and independent review gates approve promotion to `tests_py/`.
- Use ProgressObligation and execution lineage contracts to distinguish legal waiting/startup grace from silent stalls or execution loss.
- Make task-level activity evidence a watchdog input: daemon/monitor/summary heartbeat writes prove only `SYSTEM_ALIVE` and MUST NOT refresh task progress. Planner/Reviewer/Worker output growth, broker/request state transitions, execution-lineage changes, lifecycle transitions, progress milestones, and Git/HEAD changes are task evidence.
- Track `last_task_activity_at` separately from `last_meaningful_progress_at`; repeated `STALL_DETECTED`, identical diagnostics/owner gates, watchdog self-writes, and heartbeat-only file churn MUST NOT count as meaningful progress.
- If the daemon continues ticking but no Planner/Reviewer/Worker is alive and no task-level activity occurs within the bounded ProgressObligation window, classify `ORCHESTRATOR_ALIVE_TASK_STALLED`, capture it as a P16.10 incident, and enter bounded recovery rather than reporting the task as running.
- Add a regression reproducing the 2026-09-25 P16.10 false-running case: daemon heartbeat remains healthy while lifecycle is `REVIEW_FAILED`/`OWNER_GATE`, worker is not alive, and task evidence is stale; watchdog must report stalled and attempt/route safe recovery.
- Track manual `CONTROL_ONLY` interventions as an explicit metric; target zero control-only interventions for standard lifecycle paths.
- Safety boundaries: Never auto-merge production fixes, weaken owner/safety/Git gates, or actuate real physical hardware as part of candidate regression generation.

## Sequence

P16.7 self-healing activation -> P16.8 golden-path liveness -> P16.9 execution-loss watchdog -> P16.10 automatic failure harvesting -> P16.11 AGY-first resource routing -> P16.12 Web Sol persistent pairing and truthful availability.
