# D1 Self-Hosted DevOrchestrator Integration

Status: **AWAITING AIBROKER P6 CONTRACT**
Timebox: **90 minutes maximum once executable**

Goal: make DevOrchestrator consume ChatGPT Web/Sol through AIResourceBroker instead of a DevOrchestrator-specific provider path, while preserving continuous-execution and restart-reconciliation guarantees.

Safety boundary:
- modify only `C:\work\github\DevOrchestrator-dev`;
- never modify `C:\work\github\DevOrchestrator` from a Worker;
- never restart or replace the stable controller daemon from a Worker;
- no physical hardware actions.

Prerequisite: AIResourceBroker P6 must define and verify the broker-side ChatGPT Web/Sol backend contract.

Acceptance once executable:
- DevO emits semantic planner/reviewer requests only;
- provider-specific Web/Sol routing is removed from the lifecycle decision path where superseded;
- restart reconciliation and one-active-worker guards remain regression-covered;
- full DevOrchestrator tests pass;
- update agent files and commit locally; do not push.
