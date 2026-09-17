# P12.7 Web Control Surface Visual Refresh

Status: **FUTURE / DESIGN CONSTRAINT FROZEN**  
Reference: `https://opencode.ai/data` (observed 2026-09-17)  
Scope: DevOrchestrator port 8770 operator UI only; no lifecycle-authority redesign.

## 1. Objective

P12.7 refreshes the existing DevOrchestrator Operations Console into a denser,
calmer data/control console. OpenCode Data is a visual and information-architecture
reference, not a brand or component-copy target.

The useful reference pattern is: a compact header, prominent but restrained KPI
summary, time-series data, ranked/tabular detail, explicit freshness, and low visual
noise. DevO adapts those patterns to orchestration operations rather than reproducing
OpenCode's product metrics.

P12.7 MUST preserve the P12 control contract: the daemon and
`ControlCommandCoordinator` remain the only mutation authority. No UI refactor may
introduce implicit state changes, weaker identity guards, or a second source of truth.

## 2. Current-state delta

The current UI is functionally broad but visually card-heavy: dark gradient page
background, elevated rounded panels, strong shadows, and many equally weighted
sections. That makes operational priority harder to scan as data volume grows.
P12.7 should move toward neutral surfaces, thin dividers, less elevation, tighter
spacing, stronger typography hierarchy, and status emphasis through structure rather
than decorative effects. Status must never rely on color alone.

## 3. Information architecture

Initial implementation SHOULD remain one SPA to minimize risk. Use a compact section
navigation rather than introducing client-side routing until the information model is
validated.

Recommended top-level views/sections:
- **Overview** — project/runtime health, active roles, incidents, freshness and KPIs.
- **Projects** — lifecycle/task state, next action, progress age, worker/reviewer state.
- **Runs / Activity** — execution/review/remediation history and throughput trends.
- **AI Resources** — provider/model/account availability, current load and failures.
- **Watchdog / Diagnostics** — stalls, `REVIEW_FAILED`, recovery attempts and gates.
- **Accounting** — phase time, retries, EDR, bottlenecks and acceptance evidence.

OpenCode-to-DevO adaptation:
- Sessions trend -> DevO executions/reviews per time window.
- Projects metric -> configured/active/stalled project counts.
- Models ranking -> provider/model utilization, success/failure and latency ranking.
- Languages ranking -> DevO bottleneck/recovery ranking; language data is irrelevant.

## 4. Overview composition
The first viewport should answer, without scrolling: Is DevO healthy? What is running?
What is blocked? Which AI resource is in use? How fresh is the data?

Header strip:
- product/project context and compact section navigation;
- monitor/daemon/watchdog health;
- `Live`, `Stale`, `Disconnected`, or `Unknown` freshness state plus last refresh time;
- manual refresh as a secondary action.

KPI strip (flat cells, not large floating cards):
- Active projects / total projects;
- Active AI roles;
- Stalled or failed projects requiring attention;
- Recent successful / failed executions;
- Available / degraded / unavailable AI resources.

Primary content should combine one or two useful trends with dense ranked tables.
Avoid charts whose only purpose is decoration. The operator should be able to move
from summary to exact project/run evidence in one interaction.

## 5. Control and safety presentation

Read-only analytics and mutation controls MUST be visually separated. Controls stay
project-scoped and continue to expose only capabilities projected as available.

Dangerous or lifecycle-changing actions (`stop`, retry/reconcile, owner-gate approval)
must show target identity and state consequence before submission. P12 expected-identity,
idempotency, CSRF/origin/Host protections and audit semantics remain unchanged.

No chart click, row selection, filter, tab change, or refresh may issue a mutation.
## 6. Visual rules

- Prefer a neutral solid page surface over the current radial-gradient background.
- Prefer 1 px separators and subtle grouping over shadows/elevated card stacks.
- Reduce border radius and vertical whitespace; optimize for desktop operational density.
- Use large numerals only for top-level KPIs; detail rows stay compact and aligned.
- Use monospace selectively for IDs, HEADs, durations and machine evidence.
- Preserve semantic status text/icons; color is supplemental, never the sole signal.
- Keep motion minimal and respect reduced-motion preferences.
- Provide responsive collapse for narrow screens without hiding health/failure state.

## 7. Delivery strategy

P12.7 should start with a low-cost static/data-bound prototype against captured 8770
status fixtures. Validate information hierarchy before changing production controls.
Then refactor `web/index.html` and `web/style.css` while retaining existing API calls
and control handlers in `web/app.js`; change JS only where the new information model
requires deterministic rendering or filtering.

Do not make P12.7 a prerequisite for P12.6 closure. P12.6 remains the active task until
its persistent-harness/reviewer-recovery acceptance is complete.

## 8. Acceptance

- Existing 8770 GET/control API contracts remain compatible.
- All current guarded actions remain explicit and identity-checked.
- First viewport exposes daemon/monitor/watchdog health, active role(s), incident count,
  AI resource availability, freshness state and last refresh time.
- `REVIEW_FAILED`, stalls, owner gates and disconnected/unknown data are first-class states.
- No read-only interaction can mutate orchestration state.
- Project/resource/run tables support compact scanning and deterministic status ordering.
- UI exposes unavailable evidence as unavailable rather than inventing zero/healthy values.
- Keyboard operation, focus visibility and acceptable contrast are retained.
- No OpenCode branding, wording, logos, proprietary assets or pixel-copying are used.
- Web tests and representative P12 dashboard fixtures pass after the refresh.
