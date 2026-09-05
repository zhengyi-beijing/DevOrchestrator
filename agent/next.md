# NEXT — V1 maintenance / future expansion

Status: **MAINTENANCE / NO BOUNDED CORE TASK PENDING**

DevOrchestrator V1 is accepted. New managed projects should be onboarded through
local project configuration and their own `agent/*` contract; no DevOrchestrator
Core change is required for ordinary onboarding.

Future work is owner-selected, not part of unfinished V1 acceptance:
- multi-machine central coordinator / fleet view;
- additional provider backends and richer quota telemetry;
- explicitly authorized `next_stage` / retry policies;
- hardware-action authority boundaries;
- richer dashboard/history/notification UX;
- broader browser adapters if ChatGPT DOM contracts change.

Operational default:
- keep daemon + Browser Bridge running on the host;
- keep machine-specific project config untracked;
- use `.devorch/status.json` for quick project status queries;
- preserve fail-closed branch/HEAD/status-hash and one-active-Worker guards.

Start a new DevOrchestrator development slice only after an explicit owner request.
