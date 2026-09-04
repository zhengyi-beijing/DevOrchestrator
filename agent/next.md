# Event Dispatcher → WebSolRequest → Bridge

Status: AUTHORIZED — bounded first event slice only

Goal: when an orchestration-ready project transitions to a completed Worker result, DevOrchestrator emits exactly one `WORKER_DONE` reasoning event, builds a `WebSolRequest` with role `REVIEWER`, renders the accepted prompt, and submits it to the project’s exact `conversation_binding` Bridge queue.

Required behavior:
1. Derive repository truth immediately before request construction; branch/head in the request come from fresh repository truth, not stale monitor text.
2. Event identity is deterministic/idempotent for the same completed Worker occurrence so repeated monitor ticks do not enqueue duplicates.
3. Route only through the project’s validated `conversation_binding` (`transport`, `adapter`, `binding_id`). Projects without a ready binding remain monitorable and emit no Bridge request.
4. Preserve project isolation: project A can never enqueue into project B’s binding.
5. Use existing Web Sol event/role contract: event=`WORKER_DONE`, role=`REVIEWER`; request contains project_id, request_id, task_id/stage_id, branch, head, nonce.
6. Bridge remains transport-only. Dispatcher may submit a rendered request but must not claim/respond or interpret a Web Sol response.
7. Persist enough dispatcher state to survive daemon restart without replaying the same Worker completion.

Acceptance tests must cover: single enqueue on Worker completion; duplicate ticks/restart do not duplicate; two projects route to distinct bindings; missing/malformed binding does not enqueue; fresh branch/head captured; non-WORKER_DONE routine ticks emit nothing; daemon still uses one PID and existing tests remain green.

Non-goals: Web Sol response consumption/application; REMEDIATE/NEXT/OWNER_GATE transitions; Worker start/restart; PHASE_AUTO; generic event fan-out; project-name special cases; hardware actions; LabDemo P4.3.4; restoring Codex Reviewer WIP.

Stop after implementation, complete tests/evidence, and a fresh independent Reviewer verdict for this slice.