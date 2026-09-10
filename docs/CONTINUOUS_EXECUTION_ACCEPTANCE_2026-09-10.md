# Continuous Execution Acceptance — 2026-09-10

## Scope

This acceptance covers the DevOrchestrator ↔ AIResourceBroker execution path and lifecycle control plane. Product-repository feature work is not part of the acceptance scope.

The accepted authority boundary is:

- DevOrchestrator owns project/task/stage/role lifecycle, planning gates, review disposition, remediation, owner gates, restart recovery, and continuous progression.
- AIResourceBroker owns provider/account/model/resource selection, exact provider invocation, provider session/cancel facts, health/quota/usage observations, and dispatch telemetry.
- ChatGPT, RDC, CLI, and the dashboard are stateless control/observation surfaces. They do not own authoritative project lifecycle state.

## Accepted lifecycle

A single `project-continue <project_id>` can progress across task boundaries without another user command:

`Worker P1 → Review P1 → P2 PENDING DESIGN → Planner P2 → Plan Review P2 → Worker P2 → Review P2 → settled/task_complete`.

`PENDING DESIGN` means plan before execution; it is not a rejection of the continue intent. Planner output is structured and repository-read-only until an independent plan review approves deterministic plan freeze.## Verification evidence

Accepted evidence on 2026-09-10:

- Full DevOrchestrator regression: `190/190 PASS` after the historical-overlay fix.
- Synthetic multi-task E2E: `SYNTHETIC_MULTITASK_CONTINUOUS_E2E_PASS`.
- The synthetic call order was Worker P1, Reviewer P1, Planner P2, Plan Reviewer P2, Worker P2, Reviewer P2.
- Final synthetic transition was `settled / task_complete`; no phantom Worker was launched.
- Restart reconciliation converted the exact legacy terminal-block pattern to `settled` only when review identity, HEAD, branch, clean worktree, and COMPLETE truth matched.
- A newer settled lifecycle record now forms a barrier that prevents older completed Workers from being resurrected into `WAITING_REVIEW` overlays.

## Browser decoupling

Direct AIBroker reviewer/planner projects do not require `conversation_binding`. Browser Bridge remains a compatibility transport for projects not yet migrated.

LineScanViewer was used only as a canary for no-browser orchestration acceptance. Its feature scope is not part of this integration acceptance. After reconciliation it remains `IDLE`, clean, and unbound.

## Current migration boundary

`execution.engine = "aibroker"` is provider-neutral. DevOrchestrator sends only semantic role/quality/independence plus execution correlation and repository context.

Legacy `AgyBackend` / `DshBackend` routing remains only for projects still configured with `execution.engine = "legacy"`. It must be removed only after those projects are migrated; it is not part of the AIBroker execution path.
## Code lineage

The accepted implementation is represented by the pushed commits `0e5d210` (cross-lifecycle continuation), `a4d7e2b` (legacy terminal-block reconciliation), and `f3f0ecc` (historical Worker resurrection barrier).