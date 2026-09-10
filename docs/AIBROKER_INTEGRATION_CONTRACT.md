# AIBroker Integration Contract

DevOrchestrator owns project, task, stage, role, transition, review, remediation, owner-gate, and stop semantics.
AIResourceBroker owns provider/account/model resources, selection, exact invocation, provider sessions, cancellation, and resource telemetry.

## Boundary

DevOrchestrator sends an `AIRoleRequest` containing orchestration correlation IDs, role, semantic quality/independence requirements, prompt, working directory, and timeout.
It does not select a provider, account, model, or exact resource.

AIBroker returns an `AIRoleResult` containing dispatch/decision/execution/session IDs and the exact resource facts that were used.
Those resource facts are evidence for audit and future independence constraints; they are not routing policy owned by DevOrchestrator.

AIBroker must not interpret project-stage semantics such as NEXT, REMEDIATE, OWNER_GATE, or STOP.
A single Broker dispatch decides once and executes at most one resource; it does not perform provider fallback or task remediation.
