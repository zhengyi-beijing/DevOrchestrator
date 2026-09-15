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

## Browser acceptance harness hardening

Follow-up from CCP7 live acceptance:
- add a preflight that verifies the expected process owns Bridge port 8765
  before a live browser case starts;
- fail fast if a stale acceptance Bridge or production daemon already owns the
  port, and report the owning PID/command line;
- keep this as test-harness hardening only; CCP7 transport acceptance itself is
  complete.

## P11 staged roadmap

- **P11-A — Bounded plan-review remediation loop (complete):** bootstrap lifecycle fix. Reviewer rejection auto-revises/re-reviews for at most 3 rounds, then OWNER_GATE.
- **P11-B — Execution accounting foundation (complete):** durable lifecycle/test/retry/accepted-work timing, Effective Development Ratio, plan-review churn, and failure-memory foundation.
- **P11-C — Provider/context/RDC evidence (complete):** context continuity, quota/failover latency, RDC invocation metrics and multi-project isolation probes with deterministic classification.
- **P11-D — Reporting and acceptance (complete):** 8770 dashboard/API, project/task/role timing, evidence-backed bottleneck diagnoses, original-hypothesis comparison, representative DevOrchestrator fixtures, and quantitative acceptance gates. xray-hw-platform remains paused unless separately authorized.

## P12 staged roadmap

- **P12 — Unified AI Control Surface (complete 2026-09-15):** port 8770 is the normal operator surface for project lifecycle, active AI roles, AIBroker resources/executions, conversation bindings, P11 evidence and guarded owner actions.
- Delivery is bounded into P12.1 unified reads, P12.2 authenticated/idempotent command transport and audit, P12.3 guarded actions plus conversation-control consolidation, and P12.4 dashboard/client acceptance.
- The daemon and existing `ControlCommandCoordinator` remain the only mutation authority. Existing GET routes remain compatible, standalone web remains read-only, and 8875 remains the broker-specialist configuration/diagnostic surface.
- Detailed frozen design: `docs/UNIFIED_AI_CONTROL_SURFACE_DESIGN.md`.
- Acceptance passed with atomic/replay, security, action, conversation, UI and representative 8770 fixtures plus the full 501-test/16-subtest regression. Actions without an exact safe existing authority adapter remain capability-advertised as unavailable rather than gaining a weaker fallback.
