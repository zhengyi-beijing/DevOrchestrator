# AIBroker Integration Contract

DevOrchestrator owns project/task/stage/role lifecycle, transitions, review disposition, remediation, owner gates, and stop semantics. AIResourceBroker owns provider/account/model resources, strategy selection, exact invocation, provider sessions, cancellation, and resource telemetry.

## Execution boundary

DevOrchestrator sends an `AIRoleRequest` containing orchestration correlation IDs, role, semantic quality/independence requirements, prompt, working directory, and timeout. It never selects a provider, account, model, or exact resource.

AIBroker returns an `AIRoleResult` containing dispatch/decision/execution/session IDs and exact resource facts. Those facts are audit evidence and input to future semantic independence constraints, not routing policy owned by DevOrchestrator.

A single Broker dispatch decides once and executes at most one resource. It never interprets NEXT, REMEDIATE, OWNER_GATE, STOP, task advancement, or project stages, and it performs no automatic provider fallback or task remediation.

## Opt-in migration

`execution.engine = "aibroker"` routes a DevOrchestrator Worker through the configured `AIExecutionPort`. The legacy AGY/DSH execution engine remains the compatibility default until a project explicitly opts in.

A Broker Worker persists `dispatch_id`, `decision_id`, `execution_id`, `session_id`, and `resource_context` into DevOrchestrator's execution ledger. Business task IDs and Broker execution IDs remain separate namespaces.

`worker_timeout_seconds` is an orchestration task timeout. The subprocess transport timeout must cover that task timeout plus a bounded margin so transport cannot terminate a valid long-running task first.

## Direct reviewer

A project may opt in with `ai_roles.reviewer.enabled = true`. The daemon then reviews the latest completed Broker Worker through AIBroker, using semantic `quality` and `independence` plus the Worker's prior `ResourceContext`.

Direct-review projects are excluded from Browser Bridge reviewer dispatch, preventing duplicate reviewers. There is no silent browser fallback when the direct reviewer is enabled but its Broker execution port is unavailable.

Direct reviewer output is strict JSON and is persisted in transport-neutral `review-decisions.json`. TransitionExecutor reads both this ledger and the legacy `websol-decisions.json` during migration.

A project with a direct reviewer does not require `conversation_binding` to be orchestration-ready. Legacy browser-bound projects remain supported unchanged until explicitly migrated.

## Restart and fail-closed behavior

If the daemon restarts while a managed Broker Worker or direct Reviewer is active, the persisted run is marked `recovery_required`; automatic replay is forbidden. A reviewer only accepts its result if branch, HEAD, and reviewed worktree fingerprint are unchanged during review.

Only the latest completed Broker Worker per project is eligible for a new direct review. Historical completed Workers are not bulk-replayed when a project enables the new reviewer path.

Machine-specific Broker paths and Python environment live under ignored runtime configuration (`runtime/aibroker-execution.json`), not project source. Browser transport remains a legacy compatibility adapter, not a required state store for the direct Broker path.

## Planner and stateless continue

`project-continue <project_id>` expresses lifecycle intent; it is not a direct Worker-start command. The client is stateless and does not supply a conversation id, provider, account, model, or exact resource.

For a `PENDING DESIGN` task, DevOrchestrator runs an AIBroker Planner that returns a strict structured plan without modifying the repository. An independent plan Reviewer must use the configured semantic independence constraint against the Planner resource.

Only after plan approval does DevOrchestrator deterministically update `agent/next.md` to `READY_TO_RUN`, append the approved executable design, and create a local plan-freeze commit. The repository branch, HEAD, worktree fingerprint, and original `agent/next.md` must remain unchanged throughout planning or the apply step fails closed.

The same accepted control command then resumes on a later daemon tick and launches the Worker through the existing `TransitionExecutor` guard. A plan failure, owner gate, or restart recovery is synchronized back to control history instead of leaving the command permanently reported as planning.

Control commands use atomic per-command inbox files and bounded safe command IDs. Replay of the same command id is idempotent. A control command cannot bypass an unresolved Broker Worker review or unsafe recovery state.

## Restart reconciliation

For Broker Workers, daemon restart reconciliation queries persisted Broker dispatch facts when available. A Broker-succeeded execution is projected as completed; a provider failure remains failed. Running or ambiguous work becomes `recovery_required` and is never automatically replayed.

A daemon-managed interruption may be marked safe for an explicit owner continue only when the Broker interruption reason is the managed-stop reason and repository branch, HEAD, and launch fingerprint are unchanged.

Stopping the daemon terminates the recorded daemon process tree before marking active Broker dispatches interrupted. Broker interruption is not recorded when process-tree termination cannot be verified.

## 2026-09-10 continuous-execution acceptance

The provider-neutral control/execution path has passed a synthetic multi-task lifecycle acceptance covering Worker → Review → next-task Planner → Plan Review → Worker → Review → terminal settle from a single continue intent. Restart/self-heal and historical-worker overlay barriers are included in the accepted behavior.

See `CONTINUOUS_EXECUTION_ACCEPTANCE_2026-09-10.md` for the frozen evidence and current legacy-migration boundary.
