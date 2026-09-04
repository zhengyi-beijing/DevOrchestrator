# NEXT — Portable bootstrap hardening + real external-project smoke test

Status: **READY CANDIDATE / NO WORKER-ACTUATION AUTHORIZATION**

Recommended next bounded slice:
- exercise the documented onboarding flow against one real non-LabDemo project
  using only local project configuration (monitor-only first, then determine whether
  its task/Worker contract is `agent_files` compatible);
- verify `validate-config`, one monitor tick, dashboard projection, and daemon
  lifecycle without changing DevOrchestrator Core for that project;
- identify any remaining assumptions that force project-specific files or host paths;
- if needed, harden bootstrap/launch scripts so a clean clone can be started
  from an arbitrary working directory with explicit config/runtime locations.

Acceptance target:
- real external project is registered with config only;
- no source-code branch or product-name special case is added;
- observed project remains unchanged by monitor/Bridge/Decision Guard;
- full automated regression remains green;
- any machine-specific values remain local/untracked configuration.

Deferred independent integration gate:
- deploy ChatGPT adapter 0.1.2 on TS-ZY_PC and rerun the real LabDemo browser POC
  when TS-ZY_PC/XLabServer are available.

Hard stop:
Transition Executor / Worker actuation still requires a separately frozen
contract and explicit owner authorization.
