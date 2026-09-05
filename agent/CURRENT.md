# CURRENT — DevOrchestrator

Branch: `feature/browser-bridge-multiproject`

Phase: **DevOrchestrator V1**
Status: **IMPLEMENTATION COMPLETE / REAL E2E ACCEPTED**

V1 is a standalone, multi-project orchestration service. Projects are registered
by local `projects.json`; DevOrchestrator does not require source embedding in
the managed repository.

Delivered:
- config-only multi-project onboarding, `validate-config`, monitor/dashboard and daemon lifecycle;
- Browser Bridge with live binding presence, lease/renewal, idempotent dispatch and response completion gate;
- Web Sol identity validation, Decision Guard and durable decision/disposition ledgers;
- owner-authorized Transition Executor for `NEXT + NEXT_TASK` and reviewed
  `REMEDIATE + CONTINUE_CURRENT_STAGE`, with fresh branch/HEAD/status-hash guards;
- AGY and DSH backends with project-local preference and safe runtime quota fallback;
- project-local `.devorch/status.json` mirror while DevOrchestrator runtime remains authoritative;
- ChatGPT Web adapter 0.1.4 with compact status badge and cross-client conversation reload sync.
Acceptance evidence on ZXZ-PC:
- full `tests_py`: **127/127 PASS**;
- `node --check browser/chatgpt-web-adapter.user.js`: PASS;
- `git diff --check`: PASS;
- real LineScanViewer E2E: bootstrap -> AGY Worker -> WORKER_DONE -> Web Sol
  remediation -> AGY quota failure -> unchanged-repo guard -> automatic DSH
  fallback -> completed handoff commit `42f7d9d`;
- adapter 0.1.4 confirmed installed in Tampermonkey local extension storage and active in Chrome;
- cross-client acceptance: a second Chrome window produced the matching assistant
  response while another window held the Bridge claim; the claimed queue became
  `responded` automatically and Decision Guard consumed `STOP` without RDC response injection.

Operational state after acceptance:
- daemon running on `127.0.0.1:8770`, Browser Bridge on `127.0.0.1:8765`;
- daemon `last_error=null`;
- no managed Worker running;
- LineScanViewer P2 is `DESIGN READY / EXECUTABLE` but intentionally not started.
