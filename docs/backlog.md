# DevOrchestrator Backlog

Longer-horizon work intentionally outside the currently accepted portable
integration V1 slice.

## Machine-independent development environment

Status: **PORTABLE V1 BASELINE DELIVERED 2026-09-04 / FOLLOW-UPS REMAIN**

Delivered:
- clean clone on ZXZ-PC runs the normal automated suite without XLabServer;
- external repositories are registered through project configuration only;
- relative `repo_path` is resolved from the selected config directory;
- `validate-config` checks canonical config, adapter availability and Git truth;
- generic tracked example/configuration documentation no longer requires a
  LabDemo/XLabServer path;
- temporary/fake repositories cover normal Core integration tests.

Remaining candidates:
- verify macOS/Linux portability in CI or equivalent clean hosts;
- decide whether a non-editable packaged distribution is a supported product
  surface; current V1 deployment model is a standalone DevOrchestrator clone;
- isolate any remaining machine-specific browser/runtime deployment overrides;
- add a dynamic third-party ProjectAdapter registration/discovery surface only if a
  real target cannot adopt the existing `agent_files` contract;
- keep a separate opt-in real LabDemo/XLabServer + deployed-browser profile.

## Deployed ChatGPT browser POC

Adapter 0.1.2 real-browser deployment on TS-ZY_PC with XLabServer remains a
valuable integration gate, but it is not a prerequisite for normal Core
feature development or third-party project onboarding.

## Transition Executor / Worker actuation

Still owner-gated. Before implementation, freeze a new contract covering
execution authority, idempotency/replay, project isolation, stop conditions,
and stage/owner boundaries. Do not infer authorization from a valid Web Sol
disposition alone.
