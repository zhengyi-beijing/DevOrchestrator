# CURRENT — DevOrchestrator

Branch: `feature/multiproject-websol-contract`
Baseline: `64950cb` (accepted AgentBackend + Router foundation)

Phase: **Multi-project daemon + Web Sol Core contract**
Status: **ACCEPTED — fresh Reviewer verified**

Accepted architecture:
- one preferred DevOrchestrator daemon manages N configured projects;
- each project has canonical `project_id`, nonblank `repo_path`, worker/task projection, adapter, and optional conversation binding;
- missing/malformed conversation binding is monitor-only (`orchestration_ready=false`);
- ProjectAdapter is config-selected; Core has no LabDemo/xray special cases;
- Web Sol protocol/decision guard is fail-closed and does not execute Workers;
- unified daemon owns monitor + Web heartbeat under one PID.