# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator` is running the accepted D1/P8 release; Workers must not modify or restart it.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Branch: `feature/self-hosted-dev`.
- D1/P8 is complete and promoted. Current development target is Gate B daily-use readiness.

Current task: **P9 D2 Durable Project Context Foundation** (COMPLETED, pending review).

Purpose: establish durable, project-scoped context that survives conversations and is injected into semantic Planner/Worker/Reviewer execution. Graphify can supplement repository structure discovery but must remain optional. Preserve project isolation, secret boundaries, fail-closed validation, and the stable-controller safety boundary.

Status:
- Foundation implementation complete across core, config, planner, executor, reviewer, monitor, status, and CLI.
- 242 unit tests passing cleanly in full test suite discovery.
- Browser adapter userscript syntax validated (`node --check`).
- Git diff formatting verified clean.
- Reference context authored in `agent/project-context.json`.
- Comprehensive design documented in `docs/DURABLE_PROJECT_CONTEXT_DESIGN.md`.
- P9 D2 Durable Project Context accepted by independent native Claude Opus 5 at `f48e2c9`; next task is P10 active-project progress watchdog and automatic diagnostics.
