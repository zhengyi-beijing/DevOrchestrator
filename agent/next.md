# P16.9 Watchdog Execution-Loss Detection & Recovery

Status: **READY_TO_RUN**

## Goal

Fix the watchdog blind spot exposed by the xray-hw-platform P1 incident: an accepted execution reached WORKER_RUNNING, produced no provider output, then disappeared and the project returned to READY_TO_RUN without a terminal execution outcome, while watchdog still reported state=ok and performed no diagnosis or recovery.

## Incident regression

Observed sequence:
1. P1 continue was accepted.
2. Project entered WORKER_RUNNING / EXECUTING.
3. active_execution existed and process_alive=true.
4. provider_output_observed remained false.
5. The Worker/execution later disappeared: active_execution=null, active_roles=[], worker=not_started.
6. Project projected READY_TO_RUN again with no completed/failed/cancelled terminal outcome for the vanished execution.
7. A later continue/recovery epoch refreshed activity/recovery context.
8. watchdog reported state=ok, last_diagnosis=null, attempts=0, last_recovery=null.

The defect is not merely a long no-progress threshold. A new control/recovery epoch or agent-file activity can mask an unterminated prior execution.

## Required invariant

Every accepted execution that reaches launch/running must have a durable lifecycle outcome:

accepted -> launched/running -> {completed | failed | cancelled | explicitly_reconciled}

The following transition is invalid unless accompanied by durable failure/reconciliation evidence:

WORKER_RUNNING / EXECUTING
-> active_execution absent
-> READY_TO_RUN

If this occurs, classify it as an execution-loss condition such as:
WORKER_VANISHED_WITHOUT_TERMINAL_STATE.

## Required outcomes

- Persist enough execution lineage/history that a new recovery epoch cannot erase or mask an unterminated prior execution.
- Watchdog must compare current state against prior accepted/launched execution records, not only current no-progress/activity timestamps.
- Agent-file touches, control requests, new recovery epochs, or READY_TO_RUN projection must not count as proof that a prior execution terminated correctly.
- Detect at least: vanished worker, missing terminal outcome, running-without-provider-output beyond bounded startup grace, broker/execution record disappearance, and inconsistent active role/execution state.
- Emit structured diagnosis containing project_id, task_id, prior execution/control IDs, launch/head anchor, last known worker state, provider_output_observed, expected terminal states, observed state, and evidence sources.
- For safe recoverable cases, automatically reconcile/retry with bounded attempts/backoff and preserve the failed execution audit trail.
- Never launch duplicate Workers: recovery must verify that no supported backend execution is still alive before retry.
- Escalate only when recovery is unsafe, identity/HEAD changed ambiguously, retry budget is exhausted, or a genuine Owner Gate is required.
- Watchdog health must not remain `ok` while an unresolved execution-loss invariant exists.

## Acceptance tests

Automate the exact P1 failure:
- accept a continue at a clean immutable HEAD;
- mark Worker launched/running;
- keep provider_output_observed=false;
- remove/drop the active Worker/execution without writing completed/failed/cancelled;
- inject a new continue/control epoch and touch agent/next.md;
- prove watchdog still detects the prior execution loss rather than resetting to ok;
- prove diagnosis is durable and references the vanished execution;
- prove one bounded safe recovery occurs;
- prove no duplicate Worker launches;
- prove successful recovered execution closes the diagnosis with audit evidence;
- prove exhausted/unsafe recovery becomes degraded/blocked with a precise reason rather than silently returning READY_TO_RUN.

Also test the legitimate cases: normal completion, explicit cancellation, broker-declared failure, daemon restart with durable running execution, slow provider startup within grace, and a live backend execution whose local projection temporarily disappears.

## Relationship to P16.7

P16.7 covers broader self-healing project activation/readiness, structured blockers, transient recovery, orphan registration and idempotent controls.

P16.9 is narrower and deeper: watchdog must enforce durable execution lifecycle invariants across recovery epochs so an execution cannot vanish without diagnosis. P16.9 must not be considered solved merely by adding readiness aliases, reducing the stale threshold, or issuing another continue.

## Safety / non-goals

- Do not weaken Owner Gates or Git/head identity checks.
- Do not treat provider silence alone as proof that a process is dead; verify backend/process state before retry.
- Do not auto-start real X-ray, conveyor/VFD, or other physical hardware.
- Do not discard historical execution evidence when creating a new recovery epoch.

## Approved executable design

Close the watchdog execution-loss blind spot with durable launch obligations, epoch-proof loss findings, evidence-aggregated liveness, and one bounded safe recovery. A launching/running ledger row remains claim-only, while retry requires conclusive backend/process death with no live proof. To remove the stale-row guard contradiction, recovery first reserves a durable command identity, then uses a TransitionExecutor-owned compare-and-set operation to mark the exact lost row explicitly_reconciled, preserving its audit evidence. Only after reloading that terminal row does the unchanged strict _has_active_execution guard run; any other active row or still-active snapshot blocks enqueue. Crashes between reservation, reconciliation, and enqueue resume idempotently with the same invariant and command IDs.

### Implementation steps
- Add src/dev_orchestrator/core/execution_lifecycle.py owning runtime/execution-lineage.json with schema_version 1. Mutate it only under accounting.events.InterProcessFileLock on execution-lineage.lock using storage.json_store atomic writes. Fail closed on malformed or future-version payloads: quarantine them and expose a degraded_reason that prevents ok watchdog health.
- Define never-pruned lineage records keyed by a deterministic hash of project_id and source_request_id. Preserve project/task/control/execution IDs, engine and backend handles, branch/HEAD/status-hash launch anchors, worker identity, provider-output facts, lifecycle phase, liveness evidence, recovery attempts, findings, terminal outcome, integrity hash, and append-only audit entries.
- Implement open_execution_obligation as a fail-closed pre-actuation barrier: atomically upsert, write, re-read, and verify the record integrity hash. Raise ObligationPersistError on lock, write, corruption, or verification failure; never report success without a verified durable record.
- In TransitionExecutor._launch and _launch_aibroker, persist the launching row, release self._lock, verify the obligation, and only then start the worker thread or submit to AIBroker. If obligation persistence fails, start nothing and replace the exact row under self._lock with blocked/execution_obligation_unpersisted plus visible progress evidence.
- Keep the executor row and lineage obligation as independent durable launch witnesses. observe_executions reconstructs a missing obligation from any row that reached launching/running, while a surviving obligation with no row produces EXECUTION_RECORD_DISAPPEARED. Store corruption remains degraded rather than silently reconstructing an ok state.
- Hook record_execution_observation into TransitionExecutor._update_record after its ledger write. Refresh lifecycle handles, worker identity, output facts, and state; close the obligation only after a completed, failed, cancelled, or explicitly_reconciled ledger outcome is durably visible.
- Preserve the ordering contract: launching ledger row, verified obligation, actuation. For termination, write the executor terminal row before closing the obligation. A crash after the terminal row but before obligation closure is repaired idempotently by observation. A never-actuated obligated record past acceptance grace becomes explicitly_reconciled with launch_never_actuated evidence.
- Update _recover_interrupted_runs to record restart observations and close obligations only for conclusive broker terminal facts. Unknown or still-running broker facts remain liveness-resolvable and cannot be converted into an invented successful termination.
- Implement observe_executions as the watchdog tick reconciler before activity/no-progress short-circuits. It adopts launching/running rows, tracks recovery epochs without clearing findings, closes obligations from durable terminal rows, and never weakens stronger terminal evidence.
- Implement resolve_execution_liveness to aggregate every available exact-lineage probe without early exit: ledger claim, exact broker request status, PID plus started_at identity, fresh provider-output advance, and project-level active claims. Persist every probe result and timestamp in the finding evidence.
- Define alive only from an exact live broker fact, a live PID with matching started_at, or fresh provider-output advance. Define dead only when no live proof exists and an exact broker terminal/unknown-execution fact, matching dead PID, or expired no-handle acceptance grace supplies conclusive death. Provider silence, READY_TO_RUN, missing roles, and an active ledger claim alone are never death proof.
- Persist findings through candidate, open, suppressed_live, actionable_dead, recovery_reserved, reconciled_pending_retry, recovering, unresolved_unknown, escalated, and resolved. Revalidate live suppression every tick; keep unknown and suppressed findings non-ok; permit retry only from actionable_dead after configured confirmation count.
- Add TransitionExecutor.reconcile_execution_loss(source_request_id, *, project_id, invariant_key, command_id, expected_anchor, evidence) as the sole writer for stale executor reconciliation. Under self._lock, re-read the ledger, validate fresh conclusive-death evidence, exact project/source/task/branch/HEAD/status-hash identity, and absence of every other launching/running row for the project. Compare-and-set the exact active row to state=explicitly_reconciled with prior_state, terminal/completed timestamps, invariant_key, command_id, evidence hash, and reason watchdog_execution_loss. If the row disappeared, create an equivalent terminal tombstone from verified lineage anchors; if it genuinely completed/failed/cancelled meanwhile, preserve that outcome and suppress retry; if it already has the same reconciliation proof, return idempotent success.
- Treat explicitly_reconciled as an additive terminal executor state everywhere terminality is interpreted, without treating it as successful Worker completion or advancing to technical review. Preserve all original row identifiers and audit fields so project status and diagnostics distinguish reconciliation from completion, failure, and cancellation.
- Correct _trigger_execution_loss_recovery sequencing: perform policy, Owner Gate, pause, repository, launch-anchor, retry-budget, backoff, and fresh liveness checks; reject any active row other than the finding's exact stale row; durably reserve execution_loss_slots[invariant_key] and command_id wd-xl-<invariant_key>; call executor.reconcile_execution_loss; then reload executor.state and require the reconciled row to be terminal and the existing strict _has_active_execution(snapshot, refreshed_state) to be false before submitting continue. Do not weaken or special-case _has_active_execution.
- If the post-reconciliation active guard remains true because the current snapshot still reports a worker/backend or another execution appeared, leave the finding in reconciled_pending_retry and enqueue nothing. A later tick may proceed only after a refreshed snapshot and executor state both show no active execution; every other active lineage remains a hard duplicate-launch stop.
- Make the recovery transaction restartable across file boundaries: reservation before reconciliation; reconciliation row before obligation closure; obligation closure before or during enqueue; idempotent enqueue with the reserved command ID. On restart, resume an uncompleted phase from durable watchdog, executor-row, lineage, and control inbox/history evidence rather than allocating another slot or command ID.
- Keep the original execution obligation terminal after explicit reconciliation, but keep its execution-loss finding non-ok in reconciled_pending_retry/recovering until the replacement command is resolved. Link the replacement execution obligation to recovery_of_lineage_key and resolve the finding only after the recovered execution reaches a durable terminal outcome or an authoritative original terminal outcome supersedes reconciliation.
- Make RUNNING_WITHOUT_PROVIDER_OUTPUT diagnostic-only while liveness remains alive or unknown. After bounded grace it degrades health and may escalate after its configured window, but it never directly authorizes retry.
- Extend diagnostics, progress milestones, blockers, config validation, and watchdog policy additively for execution-loss detection, confirmation count, startup/output grace, live-proof freshness, unknown escalation, retry count, and backoff. Existing configurations retain defaults and current Owner Gate, Git identity, pause, and execution-intent precedence.
- Run lifecycle detection before the ordinary activity-evidence and no-progress returns in WatchdogCoordinator._advance_under_lock. Preserve execution_loss and execution_loss_slots across epoch resets, record exceptions as non-ok tick errors without dropping findings, and keep all finding evidence independent of file touches and progress fingerprints.
- Expose execution-loss state, lifecycle counts, unresolved invariants, reconciliation phase, and precise degraded reasons through project_status, CLI, web, and mobile payloads. Add focused P16.9 lifecycle, watchdog, crash-boundary, and legitimate-case suites; document the contract and update agent status/result evidence.
- After implementation and tests, run graphify update . so graphify-out reflects the new lifecycle module, executor reconciliation interface, watchdog sequencing, and cross-file status surfaces.

### Interfaces / contracts
- src/dev_orchestrator/core/execution_lifecycle.py exports schema and state constants, ObligationPersistError, lineage_key_for, invariant_key_for, open_execution_obligation, record_execution_observation, observe_executions, resolve_execution_liveness, detect_execution_loss, close_lineage_record, and load_execution_lineage.
- TransitionExecutor.reconcile_execution_loss(source_request_id, *, project_id, invariant_key, command_id, expected_anchor, evidence) -> dict returns reconciled, already_reconciled, superseded_by_terminal, conflict, or unavailable with the durable row and reason. It owns all writes to transition-executor.json for this reconciliation.
- The reconciliation compare-and-set accepts only the exact finding lineage in launching/running state or an absent row reconstructible from verified lineage anchors. It rejects identity drift, malformed/degraded evidence, non-dead verdicts, live proofs, and every other active project execution.
- The additive executor terminal state explicitly_reconciled satisfies the accepted-to-terminal invariant but is not completion and must not trigger technical review, handoff, or success projection.
- WatchdogCoordinator._trigger_execution_loss_recovery receives the live executor instance already passed to advance. If the executor or reconciliation interface is unavailable, it records execution_reconciliation_unavailable and enqueues nothing; watchdog never writes the executor ledger directly.
- The existing _has_active_execution(snapshot, executor_state) remains strict and unchanged. It is evaluated for execution-loss retry only after exact-row reconciliation and a fresh executor.state read, so the stale launching/running row no longer contradicts the guard.
- The pre-reconciliation active check is narrowly scoped to ownership, not launch permission: the only permissible active row is the finding's exact source_request_id with matching launch anchors. Any second active row blocks reconciliation and retry.
- Durable recovery phase data is execution_loss_slots[invariant_key] = {command_id, lineage_key, phase, reserved_at, reconciled_at, requested_at, resolved_at, reason}. The same command_id and invariant_key are reused after every crash or restart.
- runtime/execution-lineage.json uses schema_version 1 and records keyed by lineage_key; runtime/execution-lineage.lock serializes mutation. Corrupt payloads are quarantined and force non-ok health.
- Liveness verdicts are alive, dead, or unknown. Active ledger state is claim-only; confirmed live proof always forbids retry; dead requires conclusive death and no contradictory live proof; unknown stays durable and non-actuating.
- Watchdog project rows gain additive execution_loss and execution_loss_slots fields preserved by epoch resets. WATCHDOG_SCHEMA_VERSION remains 1 because the additions are backward-compatible.
- Recovery uses action continue and command_id wd-xl-<invariant_key> through submit_control_command with current project_identity, preserving existing two-phase control-store idempotency and expected-identity checks.
- close_lineage_record closes the original obligation after the executor terminal write. For watchdog explicit reconciliation it records the terminal lifecycle result but leaves the associated finding in reconciled_pending_retry/recovering until replacement resolution.
- Config adds optional validated watchdog keys execution_loss_detection, provider_output_grace_seconds, execution_loss_confirmations, execution_loss_max_recoveries, execution_loss_backoff_seconds, execution_loss_unknown_escalation_minutes, execution_acceptance_grace_seconds, and live_proof_freshness_seconds.
- Progress adds EXECUTION_LOSS_DETECTED, EXECUTION_LOSS_RECOVERY_STARTED, EXECUTION_LOSS_ESCALATED, and EXECUTION_LOSS_RESOLVED, all excluded from progress fingerprints. Status payloads add execution_loss, execution_lifecycle, unresolved_invariants, and reconciliation phase.

### Validation plan
- Run python -m pytest tests_py/test_p169_execution_lineage.py tests_py/test_p169_watchdog_execution_loss.py tests_py/test_p169_legitimate_cases.py -q.
- Prove fail-closed actuation: obligation write or read-back failure starts no thread and performs no broker submission, and the executor row becomes blocked/execution_obligation_unpersisted. Successful launch has a verified obligation before actuation.
- Prove the rejected stale-row case end to end: create the exact launching/running row, return conclusive exact-backend or matching-PID death, assert it is the only permitted pre-reconciliation active row, assert reconcile_execution_loss writes explicitly_reconciled, reload executor state, assert _has_active_execution is false, and only then assert one wd-xl command is submitted.
- Instrument operation order and assert reserve precedes executor reconciliation, the terminal ledger write precedes obligation closure, the refreshed active guard precedes submit_control_command, and submit never occurs when any preceding durable step fails.
- Test a second launching/running row for the same project: reconciliation returns conflict, neither row is overwritten, _has_active_execution remains true, no command is enqueued, and health stays non-ok with duplicate_execution_present.
- Test a stale snapshot after successful row reconciliation: snapshot worker/broker activity keeps the strict post-reconciliation guard true, the finding remains reconciled_pending_retry, and no command is submitted. On a later tick with a refreshed inactive snapshot, the same reserved command ID is submitted once.
- Test a completion race: after dead evidence but before compare-and-set, change the row to completed, failed, or cancelled. Reconciliation preserves the genuine terminal outcome, resolves or reclassifies the finding, and never retries.
- Test missing-row recovery: a verified obligation with a disappeared executor row and conclusive death creates one explicitly_reconciled tombstone before retry; identity drift, another active row, or degraded lineage storage prevents tombstone creation and enqueue.
- Inject crashes after reservation, after explicit_reconciled ledger persistence, after obligation closure, and after control-store submission but before watchdog requested-state persistence. Reconstruct coordinators each time and prove idempotent phase resumption, one terminal reconciliation audit entry, one command ID across inbox/history, and no duplicate Worker launch.
- Exercise the liveness table: active claim alone is unknown; exact broker live, matching live PID, or fresh output is alive; exact broker terminal/unknown-execution, matching dead PID, or expired no-handle grace with no live proof is dead; unavailable or contradictory probes are unknown. Assert all probes are recorded without early exit.
- Automate the complete P1 sequence including a new recovery epoch and agent/next.md touch. Assert the finding survives, watchdog never reports ok while unresolved, diagnosis references the vanished execution and launch anchor, one recovery occurs, and the replacement terminal outcome resolves the finding with linked audit evidence.
- Verify zero retries and zero escalations for normal completion, explicit cancellation, broker-declared failure, daemon restart with durable live broker execution, provider startup within grace, and temporarily missing local projection with confirmed live backend proof.
- Run existing watchdog regression suites: python -m pytest tests_py/test_watchdog.py tests_py/test_watchdog_recovery.py tests_py/test_watchdog_diagnostics.py tests_py/test_watchdog_state_failclosed.py tests_py/test_watchdog_timeout_fencing.py tests_py/test_watchdog_self_exclusion.py tests_py/test_watchdog_fingerprint.py tests_py/test_watchdog_owned_paths.py tests_py/test_watchdog_concurrency.py -q.
- Run executor and lifecycle regressions: python -m pytest tests_py/test_transition_executor.py tests_py/test_transition_executor_aibroker.py tests_py/test_daemon_transition_integration.py tests_py/test_p167_self_healing_activation.py tests_py/test_p168_golden_path.py tests_py/test_p168_fault_injection.py tests_py/test_p168_task_status.py tests_py/test_control_commands.py tests_py/test_lifecycle_projection.py tests_py/test_p12_control_actions.py tests_py/test_p145_job_recovery.py -q.
- Run python -m pytest tests_py -q and record totals in agent/result.md. Then run python -m compileall -q src and git diff --check as separate PowerShell-safe commands with explicit exit-code checks; do not use PowerShell 5.1-incompatible && or ||.
- Run CLI/config smoke checks, verify existing projects config loads unchanged, verify execution-loss reconciliation is visible, verify progress fingerprints reject timing/lifecycle evidence fields, and run graphify update . followed by a scoped graphify query confirming the executor-to-watchdog reconciliation path.

### Risks / failure modes
- A false dead verdict could reconcile and retry a live execution. Mitigation: aggregate all probes, require fresh conclusive exact-lineage death with no live proof, compare-and-set the exact row, reject other active rows, and retain the strict post-reconciliation active guard.
- The worker may complete between probing and reconciliation. Mitigation: TransitionExecutor re-reads under its own lock and never overwrites a genuine completed, failed, or cancelled outcome.
- Direct watchdog writes could race executor updates or violate ledger ownership. Mitigation: watchdog receives the existing executor instance and calls its reconciliation API; it never edits transition-executor.json itself.
- A crash after terminal reconciliation but before enqueue could leave the project idle. Mitigation: reservation and command identity are persisted first, reconciliation embeds both IDs in the executor row, and restart resumes the same phase and command.
- A crash after enqueue could cause duplicate commands. Mitigation: submit_control_command uses the reserved deterministic command ID, and recovery reconciles both inbox and history before resubmission.
- A stale projection may continue to claim activity after the row is terminal. Mitigation: the unchanged strict active guard defers enqueue until a later refreshed snapshot; no exact-row bypass is introduced.
- Introducing explicitly_reconciled as a terminal row could accidentally appear as success. Mitigation: explicitly map it as terminal-but-not-completed in executor, projection, review, handoff, and status tests.
- Lineage storage failure can block legitimate launches or reconciliation. Mitigation: fail closed with explicit blocked/degraded evidence instead of permitting an undetectable or duplicate execution.
- Lock ordering between watchdog and executor could deadlock. Mitigation: keep lineage writes outside executor self._lock, keep executor reconciliation bounded to one ledger read/compare/write, avoid callbacks while holding the executor lock, and cover concurrent completion/reconciliation in tests.
- Unknown liveness in deployments without usable broker or PID probes can keep health degraded. Mitigation: allow fresh output as live proof, use a bounded unknown window, and escalate precisely without launching.
- Preserving findings across recovery epochs may strand stale diagnoses. Mitigation: allow closure only from durable terminal outcomes, audited explicit reconciliation plus replacement resolution, or a precise escalation; cap resolved history without pruning open state.
- The expanded detection path runs before ordinary activity checks and could fail a tick. Mitigation: persist prior findings, report tick errors as non-ok, and never downgrade health on observation or reconciliation exceptions.

### Out of scope
- P16.10 incident-packet generation, automatic failure harvesting, or regression-candidate promotion.
- Weakening Owner Gates, pause barriers, Git/HEAD/status-hash identity checks, dirty-worktree refusal, or P16.8 terminal closure rules.
- Changing _has_active_execution to ignore arbitrary active rows or treating the stale row as inactive before its durable explicit reconciliation.
- Using provider silence, READY_TO_RUN, absent roles, agent-file touches, or a new recovery epoch as proof of process death.
- Auto-starting real X-ray, conveyor/VFD, or other physical hardware; validation uses temporary repositories and injected backends/probes.
- Changes to AIBroker transports, provider routing, browser bridges, or backend execution semantics beyond bounded read-only status probes.
- A general distributed transaction or two-phase-commit framework; restartability is limited to the existing atomic JSON stores, executor-owned compare-and-set, and idempotent control command IDs.
- Web or mobile UI redesign beyond passing through additive execution-loss, lifecycle, and reconciliation fields.
- Retroactive repair of committed history or existing external runtime artifacts from the original xray-hw-platform incident.
- Adding a new daemon, scheduler, or external service; observation and recovery remain within the existing watchdog tick, TransitionExecutor, and control-command flow.

### Independent plan review
- Approved: Plan is execution-ready and matches the repository. Verified against code: watchdog._has_active_execution (watchdog.py:420) and _active_execution_id (watchdog.py:388) treat only 'launching'/'running' executor rows as active, so the plan's core contract — executor-owned compare-and-set of the exact stale row to an additive terminal 'explicitly_reconciled' state (TransitionExecutor._TERMINAL_STATES at transition_executor.py:46), then a fresh executor.state read before the unchanged strict guard runs — genuinely resolves the stale-row/duplicate-launch contradiction without weakening the guard, and correctly leaves the snapshot worker-state branch (ACTIVE_WORKER_STATES) as a hard stop via reconciled_pending_retry. Ownership boundaries are sound: watchdog never writes transition-executor.json, lineage writes stay outside self._lock, and the launch ordering (launching row -> verified obligation -> actuation) fits the existing _launch/_launch_aibroker structure where the thread starts after the lock is released (transition_executor.py:1007-1080). Named primitives all exist: accounting.events.InterProcessFileLock, storage.json_store atomic write_json/read_json, submit_control_command with caller-supplied validated command_id, diagnostics DIAGNOSIS_CODES/ACTIVE_WORKER_STATES extension points, WATCHDOG_SCHEMA_VERSION=1 additive rows. Interfaces are specified to signature level (open_execution_obligation, record_execution_observation, observe_executions, resolve_execution_liveness, reconcile_execution_loss with its reconciled/already_reconciled/superseded_by_terminal/conflict/unavailable return contract, execution_loss_slots phase record, wd-xl-<invariant_key> command identity). Safety requirements from the task are honored: liveness is alive/dead/unknown with active-ledger-claim treated as claim-only, provider silence/READY_TO_RUN/missing roles/agent-file touches explicitly non-proof, retry only from actionable_dead with fresh conclusive death and no live proof, Owner Gate/Git-HEAD/pause checks preserved, no hardware actuation. Verification path is concrete and unverifiable-acceptance risk is low: the full P1 sequence (including new recovery epoch plus agent/next.md touch), crash-boundary resumption, completion race, second-active-row conflict, and all six legitimate cases are enumerated as assertions, and every named regression suite exists in tests_py (test_watchdog*.py, test_transition_executor*.py, test_p167/p168, test_control_commands, test_lifecycle_projection, test_p145_job_recovery). Validation also respects the recorded PowerShell 5.1 failure memory (fingerprint 281bf68..., provenance seed:p11b:rdc-powershell-5.1) by requiring separate commands with explicit exit-code checks instead of && / ||, and graphify update . matches existing repo convention (AGENTS.md). Scope is proportionate to the task rather than inflated; out_of_scope correctly fences P16.10, gate weakening, guard relaxation, and UI redesign. NON_BLOCKING findings to carry into Worker work and Technical Review, none of which justify delay: (1) step 13's 'everywhere terminality is interpreted' is over-broad as written — jobs/, review/harness.py and monitor/telemetry.py carry independent state vocabularies, so the Worker should confine explicitly_reconciled to the executor ledger and its direct projection/status/review/handoff consumers and let tests establish the true blast radius; (2) the task's 'inconsistent active role/execution state' detection is implied by project-level active claims and finding states rather than given its own named invariant key — the Worker should name it explicitly; (3) concrete field names, grace/backoff/confirmation defaults, and the resolved-finding history cap are local schema and tuning details resolvable during implementation; (4) the quarantine layout for corrupt lineage payloads is unspecified beyond 'quarantine and force non-ok health', which is sufficient for safe execution. A Worker can begin without guessing the core contract.
