# DevOrchestrator Self-Hosted Development State

- Stable controller source: `C:\work\github\DevOrchestrator` at accepted baseline `f6f643d`.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Development branch: `feature/self-hosted-dev`.
- Stable controller daemon must continue running from the stable worktree.
- This development worktree is managed by the stable DevOrchestrator as project `devorchestrator-dev`.
- Ordinary development execution engine: AIResourceBroker.
- No current Worker is allowed to restart/promote the stable daemon.
- Promotion/restart remains an owner/control-plane action after tests/review.

Current state: D1 complete. Broker-native execution status projected into project-status, transport-only Progress Channel with quiet/normal/verbose levels and deduplication implemented, wired into daemon and adapter, regression tests passing. Ready for owner promotion review.
