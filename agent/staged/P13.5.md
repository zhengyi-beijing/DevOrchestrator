# P13.5 Canonical Dashboard Sidebar Migration

Status: **PENDING DESIGN**

Goal: migrate the approved left-sidebar dashboard information architecture from the dashboard-redesign worktree into the canonical DevOrchestrator web control surface without regressing P12.7 functionality.

Scope:
- Use `C:\work\github\DevOrchestrator-dashboard-redesign` as read-only visual/interaction reference; do not merge that branch wholesale.
- Replace the current top-tab navigation with a persistent left sidebar in the canonical `DevOrchestrator-dev` web UI.
- Preserve all current canonical functions and authoritative data projections, including project state, runs, resources, watchdog, accounting and controls.
- Map the sidebar into coherent sections such as Overview, Projects, AI Resources, Usage/Accounting, Logs and System; keep safety-critical controls discoverable.
- Preserve P12/P12.7 authentication, CSRF/origin/Host protections, stale/unknown rendering and fail-closed behavior.
- Keep this phase UI/information-architecture only; do not redesign lifecycle, AIBroker routing or P13 transport contracts.

Acceptance:
- The canonical dashboard uses the left-sidebar layout and no longer depends on the old top-tab navigation.
- All capabilities available before the migration remain reachable and functional.
- Existing P12/P12.7 control and dashboard regressions remain green, with new navigation/layout tests added.
- The old dashboard-redesign worktree is treated only as source reference; canonical `main` remains the sole production implementation.
