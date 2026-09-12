# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator` (D1/P8 owner-authorized promotion completed).
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Branch: `feature/self-hosted-dev`.
- D1 Progress Channel + broker-native status implementation, P8 UTF-8 remediation, and final review fixes are accepted; code promotion point is `c6a46aa` with 213 unit tests passing.
- Final independent Opus review accepted commit `6b296d8`; the Windows GBK transport blocker and all three final D1 findings are closed.

Current task: **P8 D1 Final Review UTF-8 Remediation** (COMPLETED AND PROMOTED).
Child Python environment for AIBroker dispatch and reconciliation is forced to UTF-8. Unicode reviewer output regression tested; 213 unit tests pass, browser adapter validation passes, and promotion regression is clean.

Owner-authorized promotion completed on 2026-09-12. Stable branch was fast-forwarded from `f6f643d` to code promotion point `c6a46aa`; stable daemon restarted successfully and Browser Bridge health is OK. No promotion owner gate remains.
