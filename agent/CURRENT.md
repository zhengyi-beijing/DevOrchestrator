# CURRENT — DevOrchestrator

Branch: `feature/browser-bridge-multiproject`

Phase: **Browser reliability + response-side idempotency remediation**
Status: **ACCEPTED — real LabDemo POC + fresh independent review complete**

Accepted baseline HEAD before this commit: `c894a00`.

Delivered in this remediation:
- unified daemon now enables `require_live_binding=True`;
- configured binding is not treated as live presence;
- UNBOUND occurrences stay frozen as `prepared` and resume the same request identity;
- exact duplicate `/v1/response` replay is idempotent; conflicting replay remains fail-closed;
- `websol-decisions.json` acts as a durable consumed-response tombstone so a consumed deterministic WORKER_DONE cannot be re-emitted after dispatcher/queue loss;
- existing response completion/stability gate remains the browser-side final-response requirement;
- no Worker actuation, PHASE_AUTO, project-name special case, or hardware action was added.

Acceptance evidence:
- full `tests_py`: **83/83 PASS**;
- Node syntax checks: PASS;
- `git diff --check`: PASS;
- real LabDemo POC: UNBOUND → same request_id/nonce → claim → complete response → Decision Guard → one disposition;
- real duplicate-response replay: HTTP 200, unchanged `responded_at`, one decision, one occurrence;
- fresh independent dsh Reviewer: **ACCEPTED**, no blocking correctness/safety/isolation/scope finding.

Deployment debt: TS-ZY_PC Tampermonkey is still running DevOrchestrator adapter **0.1.1**; repository adapter is **0.1.2**. Upgrade and final deployed-browser POC remain pending.
