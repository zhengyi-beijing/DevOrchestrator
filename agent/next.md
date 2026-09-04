# NEXT — Deploy adapter 0.1.2 → final deployed-browser POC

Status: **NOT YET EXECUTED**

Immediate next candidate:
- upgrade the DevOrchestrator Tampermonkey adapter on TS-ZY_PC from 0.1.1 to repository version 0.1.2;
- preserve the real topology: XLabServer runs LabDemo/DevOrchestrator, TS-ZY_PC runs Chrome/ChatGPT;
- use an explicit, documented browser-to-XLabServer Bridge path instead of relying on an accidental localhost assumption;
- rerun one final real LabDemo review-only POC with the deployed 0.1.2 adapter.

Final deployment gate:
- initial UNBOUND remains prepared and does not submit;
- rebind reuses the same request_id/nonce/prompt;
- one browser prompt only;
- response is POSTed only after the full assistant turn is stable;
- Response Consumer produces exactly one Decision Guard disposition;
- exact duplicate response replay remains idempotent;
- no duplicate WORKER_DONE after daemon restart or runtime-state pruning covered by the durable tombstone;
- no Worker start/restart, no PHASE_AUTO, no hardware action.

After that deployment gate passes, the next architectural slice may be Transition Executor / Worker actuation, but it requires a separate owner authorization and a new frozen contract.
