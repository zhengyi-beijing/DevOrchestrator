# P9 D2 Durable Project Context Foundation

Status: **PENDING DESIGN**

Goal: make project-specific durable context a first-class DevOrchestrator input so any control conversation can continue a project without relying on chat memory.

Required context domains: project goals, architecture/module responsibilities, protected scope and safety constraints, build/test/validation commands, deployment/runtime assumptions, and durable key decisions.

Design constraints:
- explicit project/repository context remains authoritative; generated context is supplemental;
- inject bounded context into Planner, Worker/Remediator, and Reviewer requests without exposing credentials or unrelated user data;
- preserve project isolation and fail closed on malformed context;
- Graphify may be used as an optional structure/dependency source, but DevO must not require Graphify at runtime;
- provide a stable schema/versioning/update path suitable for later automatic onboarding;
- expose enough context/status metadata for future Control API/dashboard use.

Scope: modify only C:\work\github\DevOrchestrator-dev. No stable-controller modification/restart/promotion, no push, and no physical hardware actions.

Acceptance: focused regressions + full unittest suite + browser userscript syntax check + git diff check; update agent docs and commit locally clean; independent reviewer must accept before any later owner-gated promotion.
