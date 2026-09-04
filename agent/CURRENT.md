# CURRENT — DevOrchestrator

Branch: `feature/browser-bridge-multiproject`

Phase: **Portable Integration Contract V1**
Status: **IMPLEMENTED / AUTOMATED ACCEPTANCE GREEN**

Baseline HEAD before this slice: `8d8fe98`.

Delivered:
- external Git projects can be monitored through `projects.json` without Core changes;
- relative `repo_path` values resolve from the config file directory;
- new read-only `validate-config` CLI checks canonical config, registered
  adapter availability, and fresh Git repository truth;
- monitor-only projects remain valid with `orchestration_ready=false`; full current
  WORKER_DONE orchestration remains `agent_files`-contract dependent;
- external-project integration test proves monitoring does not mutate the
  observed repository;
- tracked project example and README are project-neutral rather than LabDemo-specific;
- portable integration V1 is documented in `docs/PORTABLE_PROJECT_INTEGRATION.md`.

Acceptance evidence:
- full `tests_py`: **87/87 PASS** (83 existing + 4 new portable-contract tests);
- `node --check browser/chatgpt-web-adapter.user.js`: PASS;
- `git diff --check`: PASS;
- clean-clone development verified on ZXZ-PC with Python 3.12.10 / Node 24.14.1.

The TS-ZY_PC/XLabServer adapter 0.1.2 browser POC remains useful integration
debt but is no longer a blocker for Core development or external-project onboarding.
