# D1 Self-Hosted DevOrchestrator Integration

Status: **COMPLETED** (Ready for owner review and promotion to stable controller)
Timebox: **Completed within timebox**

Goal: complete self-hosting after AIResourceBroker P6/P7 acceptance, while keeping the stable controller isolated.

Scope:
- make DevO use semantic planner/reviewer dispatch through AIResourceBroker where the broker ChatGPT Web/Sol backend supersedes provider-specific lifecycle routing;
- project broker-native execution state into `project-status` so an active AIBroker Worker/Reviewer is visible as running instead of stale `READY_TO_RUN`;
- add a Progress Channel that emits lifecycle milestone notifications to a bound ChatGPT conversation without model inference;
- default notification level `normal`, with `quiet|normal|verbose` configuration;
- normal milestones: PLAN_STARTED/ACCEPTED, WORKER_STARTED/DONE, TEST_FAILED, REVIEW_STARTED, REMEDIATE, REVIEW_ACCEPTED, OWNER_GATE/BLOCKED, TASK_COMPLETE, NEXT_TASK;
- add dedupe/rate-limit/idempotency so restart/reconciliation cannot spam duplicate progress messages;
- keep progress transport separate from planner/reviewer decision transport and never consume AIBroker model quota.

Safety boundary:
- modify only `C:\work\github\DevOrchestrator-dev`;
- never modify or restart `C:\work\github\DevOrchestrator` stable controller from a Worker;
- no physical hardware actions.

Acceptance:
- broker-native running state is regression-covered;
- progress messages are transport-only, correlated to project/task/event, deduplicated, and configurable;
- existing continuous execution, restart reconciliation, and one-active-worker guards remain passing;
- full DevOrchestrator tests and `git diff --check` pass;
- update agent files and commit locally; do not push.
