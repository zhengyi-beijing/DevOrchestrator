# NEXT — Real ChatGPT Web POC

Status: **AUTHORIZED — live transport POC only**

Goal: prove the accepted one-way chain against a real ChatGPT Web conversation:

`fixture Worker DONE → Event Dispatcher → WebSolRequest(REVIEWER) → conversation_binding → Browser Bridge → ChatGPT Web adapter → raw response returned to Bridge`.

Required POC evidence:
1. Use a DevOrchestrator-owned temporary fixture repository only; do not use LabDemo.
2. Bind exactly one live ChatGPT conversation by its URL-derived binding_id.
3. Trigger one synthetic completed Worker occurrence through the real unified daemon path.
4. Verify exactly one request is claimed by the intended conversation and no other binding can claim it.
5. Verify the prompt identity carries project_id/request_id/task_id-or-stage_id/branch/head/role/event/nonce.
6. Verify the browser adapter returns one raw assistant response to the same Bridge request.
7. Verify duplicate monitor ticks/restart do not re-submit the same occurrence.
8. Record browser/Bridge/runtime evidence and clean up the temporary fixture after the POC.

Non-goals: parse/validate/apply WebSolResponse; decision transitions; Agent Router/Worker start; PHASE_AUTO; LabDemo access; hardware actions.

After a successful live POC, next bounded development phase is **Response Consumer + Decision Guard**, still without Worker execution.
