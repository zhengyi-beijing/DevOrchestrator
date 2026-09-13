# P11-A Bounded Plan-Review Remediation Loop

Status: **READY_TO_RUN**

Owner authorization: **START P11-A / 2026-09-13**

Goal: stop actionable plan-review rejection from terminating the task and dropping the project to IDLE. A rejected implementation plan must be revised and re-reviewed automatically, with a hard bound, before owner escalation.

Scope:
- Change only the plan lifecycle in `src/dev_orchestrator/core/ai_planner.py` plus the minimum lifecycle/status/watchdog mappings and tests needed to expose the new state.
- Add planner policy `max_plan_remediation_rounds`, default 3, valid integer range 1..5.
- Add active planner state `remediating`; project lifecycle must expose it as `REMEDIATING_PLAN`.
- On reviewer decision `reject`, do not call terminal `failed`. Enter `remediating`, preserve the rejection, and automatically request a revised plan without a new `project-continue`.
- The remediation planner prompt must include the exact reviewer rejection, the prior plan JSON, task id, planning repository HEAD, and the prior planner resource context when available.
- Re-review every revised plan using the configured independent reviewer policy. Keep unique request ids per remediation round.
- Preserve a durable `rejection_chain` with round, reason, prior plan, planner/reviewer dispatch identity, resource identity, and timestamps where available.
- Approval follows the existing path unchanged: `REVIEWING_PLAN -> READY_TO_RUN -> Worker auto-launch`.
- Reviewer `owner_gate` remains immediately terminal as OWNER_GATE.
- Planner/reviewer transport, provider, cancellation, schema, or repository-truth failures remain fail-closed; do not reinterpret infrastructure failure as design rejection.
- After the configured remediation bound is exhausted, transition to `OWNER_GATE` rather than `failed` or IDLE. The final reason must state bounded exhaustion and retain the complete rejection chain.
- Emit the existing progress milestone `REMEDIATE` for each automatic plan-remediation round and `OWNER_GATE` on bounded exhaustion.
- Add `REMEDIATING_PLAN` to watchdog-active planning-family states so periodic progress checks continue while a remediation planner is running.

Acceptance:
- Deterministic test: initial plan review rejects once, remediation revises, second review approves; prove READY_TO_RUN is reached without any new control command.
- Deterministic test: reviewer always rejects; with bound 3, prove exactly 3 remediation rounds occur and then OWNER_GATE is reached with the full rejection chain.
- Verify the remediation prompt contains the exact rejection and prior plan and carries previous planner resource evidence when available.
- Verify request ids are unique across initial planner/reviewer and every remediation round.
- Verify daemon restart while `remediating` becomes `recovery_required` under the existing restart-safety rule.
- Verify project status projects `remediating` as `REMEDIATING_PLAN` and watchdog treats it as an active planning-family lifecycle.
- Preserve current planner schema-retry behavior inside each planning/remediation attempt; no unbounded nested retry.
- Run focused planner/control/lifecycle/watchdog tests, then the full Python regression suite and `git diff --check`.
- Commit locally on `feature/self-hosted-dev`; do not push and do not modify/restart the stable controller during Worker implementation.

Out of scope for P11-A:
- Execution accounting, EDR, context-hit metrics, quota/failover metrics, RDC isolation probes, lessons/failure memory, managed validation timing, dashboard/reporting, and representative-day acceptance. These move to P11-B/C/D after this bootstrap loop is working.
- Changes to AIBroker resource-selection policy or provider implementations.
- Resuming the paused `xray-hw-platform` project.

## Approved executable design

P11-A: when the reviewer rejects a plan, the task no longer fails and drops to IDLE. The planner enters a new active state, remediating (shown as REMEDIATING_PLAN). It asks for a revised plan built from the exact rejection, the prior plan, the task id, the planning HEAD and the prior planner resource. It re-reviews each revision with the configured independent reviewer. It keeps a durable rejection_chain and stops at max_plan_remediation_rounds (default 3, allowed 1..5), then moves to OWNER_GATE. Approval, owner_gate, infrastructure fail-closed behavior and schema retries stay as they are.

### Implementation steps
- In ai_planner._planner_policy, read max_plan_remediation_rounds (default 3). Reject bool, non-int or values outside 1..5 with a clear error. Return it in the policy dict.
- Add 'remediating' to _ACTIVE_STATES in ai_planner.py so _recover_interrupted turns it into recovery_required on daemon restart.
- Refactor the planner attempt loop in _run_cycle into a helper, _run_planner_attempts(plan_id, record, policy, round_no, rejection, prior_plan, prior_resource). It returns (plan, planner_result) or None after calling _finish failed. Keep the max_attempts schema-retry and cancellation logic unchanged.
- Make planner request ids unique per round. Round 0 keeps plan_id+':planner'[+':retry-N']. Remediation round R uses plan_id+':planner:remediate-R'[+':retry-N']. Set metadata remediation_round=R.
- Refactor the review call into a helper, _run_plan_review(plan_id, record, policy, plan, previous, round_no). Round 0 keeps request_id plan_id+':reviewer'; round R uses plan_id+':reviewer:remediate-R'. Any transport, status or parse failure still finishes failed with 'plan review failed'.
- Extend _planner_prompt with optional remediation arguments. When set, add a PLAN_REMEDIATION block with the round, the exact reviewer rejection reason, json.dumps of the prior plan, the task id and the record head. Still require the same JSON schema.
- For remediation planner requests, pass previous_resource_context as the prior round's planner ResourceContext. Include the _resource_payload of that resource in the remediation prompt when it exists.
- Turn _run_cycle into a bounded loop over round in 0..max_plan_remediation_rounds. Run the planner, save state 'reviewing' with plan fields, then run the review. Handle owner_gate exactly as today, and send approve to the existing _apply_plan.
- On a reject decision, append an entry to record['rejection_chain']. Include round, reason, prior plan, planner and reviewer dispatch/execution ids, planner and reviewer resource payloads, planner_completed_at, review_completed_at and rejected_at.
- After a rejection, if round < max: save state 'remediating' with remediation_round=round+1 and the latest rejection. Emit the progress milestone REMEDIATE (details plan_id, round, reason). Then continue the loop with no control command.
- After a rejection, if round == max: call _finish(plan_id, 'owner_gate', ...) with a reason stating that plan remediation hit its bound after N rounds, plus the last rejection. Keep rejection_chain in the record. Emit OWNER_GATE with plan_id, reason and rejection_chain length.
- In lifecycle_projection.py, map plan state 'remediating' to lifecycle REMEDIATING_PLAN.
- In project_status.py, add 'remediating' to the active-plan state set and map it to REMEDIATING_PLAN in the lifecycle expression. Expose rejection_chain length and remediation_round in the planner projection.
- Add REMEDIATING_PLAN to watchdog.ACTIVE_LIFECYCLE_STATES. Map it to 'PLANNING' in LIFECYCLE_OVERRIDE_FAMILY. Add it to config._ALLOWED_WATCHDOG_LIFECYCLES. Check that the diagnostics.py worker-expected set leaves it out.
- In tests_py/test_ai_planner.py, add a test: reject once, then approve. Assert the state reaches ready (READY_TO_RUN) through a single start() call, one REMEDIATE event, and a rejection_chain of length 1.
- Add test_ai_planner test: the reviewer always rejects with bound 3. Assert exactly 3 remediation planner calls, 4 reviews, final state owner_gate with a bounded-exhaustion reason, a rejection_chain of length 4, 3 REMEDIATE events and 1 OWNER_GATE event.
- Add tests that the remediation prompt contains the exact rejection text and the prior plan JSON, and that previous_resource_context equals the prior planner resource. Also assert all request_ids across planner, reviewer and remediation rounds are unique.
- Add tests: a restart while remediating gives recovery_required; invalid max_plan_remediation_rounds values (0, 6, True, '3') are refused; a reviewer transport failure during remediation still ends failed.
- Add projection tests that 'remediating' shows as REMEDIATING_PLAN in project_status and lifecycle_projection. Extend tests_py/test_watchdog.py to assert REMEDIATING_PLAN is active and in the PLANNING family.
- Run the focused planner/lifecycle/watchdog/status tests, then the full Python suite and git diff --check. Commit locally on feature/self-hosted-dev without pushing.

### Interfaces / contracts
- ai_roles.planner.max_plan_remediation_rounds: int, default 3, valid 1..5; otherwise start() refuses with an error
- Planner record state 'remediating' (active), with fields remediation_round:int, rejection_chain:list[dict], latest review_reason
- Project lifecycle value REMEDIATING_PLAN (lifecycle_projection, project_status, watchdog ACTIVE_LIFECYCLE_STATES, LIFECYCLE_OVERRIDE_FAMILY -> PLANNING, config allowed watchdog lifecycles)
- Request ids: plan_id+':planner:remediate-R'[+':retry-N'] and plan_id+':reviewer:remediate-R' for R>=1; round 0 ids unchanged
- Progress milestones: REMEDIATE for each automatic remediation round; OWNER_GATE when the bound is exhausted (both existing milestone names)
- _planner_prompt(record, retry_reason=None, attempt=1, remediation=None) where remediation carries round, rejection, prior_plan, prior_resource

### Validation plan
- python -m pytest tests_py/test_ai_planner.py -q
- python -m pytest tests_py/test_watchdog.py tests_py/test_watchdog_diagnostics.py -q
- python -m pytest tests_py -k "project_status or lifecycle or control or config" -q
- python -m pytest tests_py -q (full Python regression suite)
- git diff --check
- git log -1 shows a local commit on feature/self-hosted-dev and nothing is pushed

### Risks / failure modes
- Refactoring _run_cycle could change the existing approve/owner_gate/schema-retry paths; existing test_ai_planner tests must pass unchanged
- Rejection chain entries with full prior plans could bloat ai-planner.json; limited in practice by the 1..5 bound
- A watchdog could misjudge a long remediation planner as stalled if REMEDIATING_PLAN is not added to every lifecycle set (config, watchdog, projection)
- _apply_plan compares HEAD against record['head'] captured at start; remediation must keep using that original head so repository-truth checks still fail closed
- Nested retries: schema retries (max_attempts) inside each remediation round multiply provider calls up to max_attempts*(rounds+1); this is bounded but costly

### Out of scope
- Execution accounting, EDR, context-hit metrics, quota/failover metrics, RDC isolation probes, lessons/failure memory, managed validation timing, dashboard/reporting, representative-day acceptance (P11-B/C/D)
- Changes to AIBroker resource-selection policy or provider implementations
- Changes to the Worker review/remediation loop in ai_reviewer.py
- Resuming the paused xray-hw-platform project
- Modifying or restarting the stable controller, and pushing to a remote

### Independent plan review
- Approved: The plan is bounded, repository-aligned, and executable without design guessing. It specifies policy validation, remediation-round semantics, durable rejection evidence, unique request identities, prompt/resource propagation, fail-closed error handling, restart recovery, lifecycle/status/watchdog mappings, bounded OWNER_GATE exhaustion, and deterministic acceptance tests. The interpretation of a bound of 3 as three remediation attempts after the initial rejection—four reviews and four rejection-chain entries when all reject—is consistent with the task.
