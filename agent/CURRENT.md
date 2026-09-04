# CURRENT — DevOrchestrator

Branch: `feature/browser-bridge-multiproject`

Phase: **ChatGPT Web duplicate/reclaim remediation**
Status: **ACCEPTED**

Accepted predecessor: WORKER_DONE Event Dispatcher (`de59353`).
Frozen regression commit: `b07adb4` (`test: freeze chatgpt reclaim duplicate regression`).

Implemented acceptance scope:
- same `(binding_id, request_id)` is not re-inserted after lease expiry/reclaim;
- submitted evidence is scoped by binding + request in browser localStorage;
- existing user-message DOM marker is a fallback and promotes to storage evidence;
- submitted mark is written only after `insertAndSubmit()` succeeds;
- reclaimed request resumes waiting under the new claim token instead of re-submitting;
- existing renew/lease fail-closed behavior remains unchanged;
- Userscript remains transport-only with no workflow-decision logic.

Acceptance evidence:
- focused duplicate/Bridge suite: 11/11 PASS;
- full Python suite on XLabServer: 63/63 PASS;
- `node --check`: PASS; `git diff --check`: PASS;
- live dedicated-browser reclaim POC: two distinct claim tokens, final state `responded`;
- live UI evidence: `REQUEST_MESSAGE_COUNT=1` for the reclaimed request;
- fresh independent Reviewer: ACCEPT, no blocking defect.

This conversation is DevOrchestrator-only. Do not read/test/modify LabDemo unless explicitly requested.
