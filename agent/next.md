# NEXT — LabDemo review-only POC → Transition Executor

Status: **AUTHORIZED BY OWNER**

Step 1 — real LabDemo review-only integration POC:
- add one real LabDemo project entry with exact repo path/worker runtime;
- bind it to its intended ChatGPT conversation;
- no LabDemo code modification for the POC;
- observe an existing/synthetic completed Worker occurrence only;
- prove WORKER_DONE → ChatGPT Reviewer → raw response → validated disposition;
- verify exact binding isolation and no Worker start/restart.

POC gate:
- fresh LabDemo branch/HEAD/dirty truth must be used;
- one request, one response, one persisted disposition;
- no project-crossing and no duplicate/reclaim resend;
- cleanly stop/restore temporary POC runtime after evidence capture.

Step 2 — only after POC PASS: Transition Executor / Worker actuation.
Bounded goal: validated disposition → explicit transition intent → Agent Router
→ at most one Worker action, with owner gates and fresh repository revalidation.

Required safety: fail closed on dirty/stale/unknown state; OWNER_GATE/STOP never
start a Worker; no test weakening; no project-name special cases; no hardware;
no PHASE_AUTO in this slice. Freeze RED tests before implementation and require
fresh independent Reviewer before acceptance/push.