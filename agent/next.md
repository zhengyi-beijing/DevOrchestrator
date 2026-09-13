# P11-A Bounded Plan-Review Remediation Loop

Status: **PENDING DESIGN**

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
