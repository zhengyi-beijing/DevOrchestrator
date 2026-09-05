# Transition Executor / Worker Actuation V1

Status: OWNER AUTHORIZED / CONTRACT FROZEN 2026-09-05

## Goal

Close one bounded unattended loop without weakening the existing Decision Guard:

```text
managed Worker completes
  -> WORKER_DONE
  -> Web Sol reviewer
  -> Decision Guard
  -> APPLY + (NEXT_TASK | reviewed REMEDIATE)
  -> Transition Executor
  -> one bounded Worker
```

V1 automates only `NEXT + NEXT_TASK` and `REMEDIATE + CONTINUE_CURRENT_STAGE`. It does not automate `next_stage`, `retry`, owner-gated actions, or hardware actions.

## Execution authority

A Web Sol disposition is necessary but never sufficient execution authority.
A project is executable only when its local project config contains all of:

- `execution.enabled == true`;
- `execution.owner_authorized == true`;
- `execution.allowed_next_actions` is a non-empty duplicate-free subset of `continue_current_stage` / `next_task`;
- `execution.preferred_backends` is an ordered non-empty subset of `agy` / `dsh`;
- backend-specific options live under `execution.backends.agy` and `execution.backends.dsh`.

The executor accepts only a newly consumed `APPLY` response in the `WORKER_DONE` reviewer flow, and only for `NEXT + NEXT_TASK` or `REMEDIATE + CONTINUE_CURRENT_STAGE`. The selected action must also be explicitly owner-authorized by the project execution policy.

## Fresh-truth and task gates

Immediately before launch the executor re-reads repository truth and requires:

- repository truth is valid;
- branch and HEAD still equal the reviewed request identity;
- the project monitor state is `READY_TO_RUN`;
- no external task Worker is currently alive;
- the current `agent/next.md` resolves to a task id;
- for automatic `NEXT_TASK`, the repository is clean and the current task id differs from the task just reviewed;
- for `REMEDIATE`, the current task id must equal the reviewed task id and the current Git status hash must exactly equal the dirty fingerprint frozen when `WORKER_DONE` was dispatched.

The dispatcher freezes `review_status_hash` with the review occurrence. Response Consumer rechecks that exact hash in Decision Guard and persists it with the decision; Transition Executor checks it again immediately before launching remediation. Any intervening worktree change therefore fails closed.

The launched Worker request uses role `worker`, working directory `repo_path`, and requires `code` + `repository` capabilities. Routing remains provider-neutral through `AgentRouter`. V1 supports `agy` and `dsh`; `preferred_backends` defines deterministic project-local priority and normal router fallback. AGY may configure an explicit executable path so daemon behavior never depends on an ambient PATH refresh.

If the selected backend starts but then fails with an explicit provider quota-exhaustion signal, the executor may perform one runtime fallback to the next already-eligible backend. Runtime fallback is allowed only when fresh repository branch, HEAD, and status hash still exactly equal the values captured immediately before the failed backend launch. Any repository change disables fallback. Ordinary task/test failures never trigger provider fallback.

## Idempotency and replay

Before starting a model, the executor persists an entry keyed by the source request id in `runtime/transition-executor.json`.

- the same request id can launch at most once;
- at most one managed Worker may be active per project;
- duplicate daemon ticks or duplicate response consumption do not launch again;
- a daemon restart that finds `launching` or `running` state marks it `recovery_required` and never replays it automatically.

This fail-closed restart rule is required because the current `AgentBackend` contract tracks active runs only within one process.

## Project isolation and completion truth

Authoritative managed-run truth stays under the DevOrchestrator runtime (`agent-runs/` plus the transition ledger). For operator visibility only, the daemon writes a derived project-local mirror at `.devorch/status.json`. That mirror is never consumed as execution authority, contains no prompt/nonce/claim token, is written atomically, and `.devorch/` is added only to the repository-local `.git/info/exclude` so tracked project files and Git cleanliness are unchanged.

When a managed run completes successfully, the daemon overlays that DevOrchestrator-owned terminal run onto the in-memory project summary solely for the existing `WORKER_DONE` dispatcher. The synthetic Worker projection carries the task id captured at launch, so a Worker that updates `agent/next.md` cannot relabel the just-completed run as the next task.

A successfully dispatched review request is recorded in the transition ledger. If the browser binding is unavailable, the completed run remains pending and is retried through the existing dispatcher idempotency rules.

## Stop conditions

No Worker is launched when any of these is true:

- execution config is absent, disabled, malformed, or not owner-authorized;
- disposition is not `APPLY`;
- decision/action is outside `NEXT + NEXT_TASK` or `REMEDIATE + CONTINUE_CURRENT_STAGE`;
- selected next action is not authorized by project execution policy;
- event/role is outside `WORKER_DONE` reviewer flow;
- repository truth is unavailable, stale, or branch/HEAD changed after review;
- clean `NEXT_TASK` sees a dirty repository or an unadvanced task;
- remediation lacks the reviewed fingerprint, the fingerprint changed, or the current task differs from the reviewed task;
- another managed/external Worker is active;
- routing has no eligible backend.

A managed run that exits nonzero is recorded `failed` and stops the automatic chain in V1. There is no implicit retry.

## One-time owner bootstrap

An unattended chain needs an initial Worker before any `WORKER_DONE` decision exists. V1 therefore permits one explicitly configured bootstrap token:

```json
"bootstrap": { "request_id": "owner-unique-id", "task_id": "P1" }
```

The bootstrap uses the same execution policy, fresh-truth checks, one-active-run rule, router, and ledger. The exact bootstrap request id is persisted before launch and can never auto-run twice. The configured `task_id` must equal the current `agent/next.md` task id.

## Acceptance

1. Unit tests prove project authority for `NEXT + NEXT_TASK` and `REMEDIATE + CONTINUE_CURRENT_STAGE`.
2. Known-dirty remediation applies only when the current Git status hash exactly matches the fingerprint frozen at review dispatch.
3. Any post-review worktree change blocks remediation; duplicate decisions/ticks start one Worker only.
4. Restart of an active ledger entry becomes `recovery_required` without replay.
5. Completion overlay preserves the task id captured at launch.
6. Failed Worker stops without implicit retry.
7. Full `tests_py`, `node --check`, and `git diff --check` remain green.
8. Real AGY and DSH probes remain non-prompt in automated tests.
9. Real LineScanViewer smoke proves bootstrap -> WORKER_DONE -> Web Sol remediation -> same-task Worker -> review, before allowing `NEXT_TASK`.
