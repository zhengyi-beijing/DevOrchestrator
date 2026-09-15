# P12.5 Self-Host Operational Acceptance

Status: **PENDING DESIGN**

Owner authorization: **START P12.5 / 2026-09-15**

Goal: prove the canonical `main` deployment can safely operate DevOrchestrator as its own managed project, with P11 accounting and the P12 unified control surface observable from the running daemon.

Scope:
- Add a small read-only self-host smoke/acceptance utility under `ops/` that checks the local 8770 Control API and 8875 AIBroker surface without mutating lifecycle state.
- Verify the smoke result distinguishes daemon/control availability, AIBroker resource visibility, execution visibility, accounting availability, project identity, and warnings.
- Keep the utility machine-local friendly: loopback endpoints configurable, bounded timeouts, deterministic non-zero exit on failed required checks.
- Add focused automated tests for healthy and unavailable/malformed endpoint cases without requiring real providers.
- Document the minimal canonical self-host startup/verification commands and the fact that `C:\work\github\DevOrchestrator-dev` is currently the canonical `main` worktree.
- Preserve P11 accounting, P12 single-daemon authority, stale-state guards, audit recovery, owner-gate semantics, and existing 8770/8875 contracts.

Acceptance:
- The utility performs only GET/read-only health checks and cannot issue owner/lifecycle mutations.
- Healthy deterministic fixtures cover 8770 monitor/project/accounting plus 8875 resources/executions.
- Failure fixtures cover unreachable endpoint and malformed/partial response with clear diagnostics and non-zero status.
- Running the utility against the locally deployed daemon reports PASS for required self-host checks.
- Focused tests pass, then the relevant full regression passes, along with Python compilation and `git diff --check`.
