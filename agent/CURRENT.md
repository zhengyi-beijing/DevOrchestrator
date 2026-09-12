# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator` is running the accepted D1/P8 release; Workers must not modify or restart it.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Branch: `feature/self-hosted-dev`.
- D1/P8 is complete and promoted. Current development target is Gate B daily-use readiness.

Current task: **P9 D2 Durable Project Context Foundation** (PENDING DESIGN).

Purpose: establish durable, project-scoped context that survives conversations and is injected into semantic Planner/Worker/Reviewer execution. Graphify can supplement repository structure discovery but must remain optional. Preserve project isolation, secret boundaries, fail-closed validation, and the stable-controller safety boundary.
