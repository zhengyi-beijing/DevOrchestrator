# DevOrchestrator Self-Hosted Development State

- Stable controller source: `C:\work\github\DevOrchestrator` at accepted baseline `f6f643d`.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Development branch: `feature/self-hosted-dev`.
- Stable controller daemon must continue running from the stable worktree.
- This development worktree is managed by the stable DevOrchestrator as project `devorchestrator-dev`.
- Ordinary development execution engine: AIResourceBroker.
- No current Worker is allowed to restart/promote the stable daemon.
- Promotion/restart remains an owner/control-plane action after tests/review.

Current state: D1 executable. AIResourceBroker P6 ChatGPT Web/Sol backend and P7 production E2E canary are accepted; proceed with broker-native status projection plus transport-only Progress Channel.
