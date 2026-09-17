# P12.7 Web Control Surface Visual Refresh

Status: **READY_TO_RUN**

Design authorization: **FROZEN 2026-09-17**

Implementation authorization: **OWNER AUTHORIZED AFTER P12.6 CLOSURE**

Goal: refresh the existing port 8770 Operations Console into a denser,
calmer operational dashboard using OpenCode Data as a visual/information-
architecture reference without copying branding, assets, or product metrics.

Scope:
- Preserve the daemon and `ControlCommandCoordinator` as the only mutation authority.
- Keep the existing P12 Control/Event API contracts and guarded action semantics.
- Reorganize the UI around Overview, Projects, Runs/Activity, AI Resources,
  Watchdog/Diagnostics, and Accounting.
- Make freshness, active roles, failures/stalls, owner gates, and unavailable
  evidence first-class operational states.
- Prefer neutral surfaces, thin dividers, compact tables, restrained KPI cells,
  and low visual noise over elevated card stacks and decorative charts.

Delivery:
- Start with fixture-backed hierarchy/prototype validation before production styling.
- Refactor `web/index.html` and `web/style.css` first; modify `web/app.js` only
  where deterministic rendering/filtering requires it.
- Keep read-only navigation/filter/chart interactions mutation-free.

Acceptance:
1. Existing 8770 GET/control API contracts remain compatible.
2. Guarded actions retain exact identity, idempotency, CSRF/origin/Host, and audit semantics.
3. The first viewport exposes daemon/monitor/watchdog health, active AI roles,
   incident count, AI resource availability, freshness, and last refresh time.
4. `REVIEW_FAILED`, stalls, owner gates, disconnected/unknown, and unavailable
   evidence remain explicit and cannot be presented as healthy/zero.
5. Project/resource/run tables support compact deterministic scanning.
6. Keyboard/focus/contrast behavior remains acceptable and status never relies on color alone.
7. Representative web/control fixtures, JavaScript syntax, Python regression,
   and `git diff --check` pass.

Non-goals:
- No lifecycle/control-plane redesign or second source of truth.
- No OpenCode branding, wording, proprietary assets, or pixel-copying.
- No P13 remote-job implementation or P14 Android-client implementation in this task.

Frozen design: `docs/P12_7_WEB_UI_DESIGN.md`.

## Approved executable design

Refresh the port 8770 Operations Console (web/index.html, web/style.css, and only the necessary parts of web/app.js) into a dense single-page dashboard. It has compact section navigation, a header strip for health and freshness, flat KPI cells and deterministic compact tables. Existing P12 GET/control API contracts, the static allowlist, CSP and guarded-action semantics stay unchanged. The daemon and ControlCommandCoordinator remain the only mutation authority. Every lifecycle-changing guarded action (stop, retry, rereview, reconcile, approve_owner_gate) gets a tested confirmation before submission, showing the exact identity and the state consequence. A pure buildControlTarget helper produces, for every action, exactly the target fields allowed by command_store.py; approve_owner_gate sends {gate_id: control_identity.gate_id}.

### Implementation steps
- Record a baseline: web, accounting-dashboard, unbound-UI and P12 control pytest suites, node --check web/app.js, and tests/web-selftest.ps1.
- Freeze the compatibility surface: existing DOM ids, module.exports, and the literal strings asserted by test_unbound_web_ui.py.
- Add tests_py/test_web_ui_refresh.py using the Node fake-DOM harness, with fixtures for healthy, REVIEW_FAILED, stall, owner gate, paused, stale monitor, failed fetch, broker unavailable and accounting unavailable.
- Restructure index.html into one SPA: a header with health, freshness and last-refresh plus a Refresh button, a KPI strip, and Overview, Projects, Runs/Activity, AI Resources, Watchdog/Diagnostics and Accounting sections.
- Put guarded controls (control rows, pairing bar, command outcomes) in a separate labelled Controls region; add no inline scripts or styles because of the CSP.
- Rewrite style.css with a neutral solid surface, 1px dividers, minimal shadow and radius, compact tables, monospace IDs, visible :focus-visible, reduced-motion support and a narrow-screen collapse.
- In app.js, replace Promise.all in refresh() with per-source allSettled handling so a failed source shows as unavailable or disconnected, never healthy.
- Add pure exported helpers for KPIs, the Live/Stale/Disconnected/Unknown freshness state and the incident count; missing inputs yield unavailable, never zero or healthy.
- Render the Projects, AI Resources, Runs/Activity and Watchdog tables with a deterministic severity-then-id-then-time sort, using only createElement, textContent and className.
- Render Watchdog/Diagnostics from control overview data.watchdog or GET /api/watchdog, with state, diagnostic code and degraded reason as text.
- Add a pure exported buildControlTarget(project, capability, selectedSession) that returns the exact target for each action under the interfaces target contract, or null if a required value is missing.
- Wire the control buttons through buildControlTarget; disable a button when data.control_enabled is false, capability.available is false, or buildControlTarget returns null.
- Add a pure exported describeGuardedAction(project, capability, target) that returns labelled identity lines and a fixed consequence sentence for stop, retry, rereview, reconcile and approve_owner_gate.
- The identity lines are project_id, task_id, lifecycle_state, branch@head, dirty and revision, plus target.target_id for retry/rereview/reconcile and target.gate_id for approve_owner_gate; missing values show as unavailable.
- sendControl shows window.confirm with the describeGuardedAction text for those five actions before any fetch; if cancelled, it returns null and sends nothing.
- Keep the command body {schema_version, command_id, project_id, action, expected: control_identity, target}, the CSRF/Sec-Fetch-Site headers, polling, session handling and pairing flows unchanged.
- Add Node tests for buildControlTarget covering all 11 CONTROL_ACTIONS: exact target objects, no fields outside the command_store allowlist, and null when target_id, gate_id or the session is missing.
- Add Node tests for the five confirmed actions: the confirm text includes identity, target_id or gate_id, and the consequence; cancel issues zero fetches; confirm posts expected equal to control_identity and the exact target.
- Extend tests: helper outputs, deterministic ordering, GET-only refresh and navigation, required ids, no inline scripts or OpenCode strings, and the focus and reduced-motion CSS rules.
- Run the full validation, then manually check the first viewport, keyboard focus and one confirm dialog against the fixture server, and record the evidence.

### Interfaces / contracts
- Unchanged HTTP contracts: GET /api/monitor, summary, events, runs, orchestration, watchdog, accounting, broker/*, v1/control/overview and v1/control/commands/{id}; POST browser-sessions, commands and adapter-pairings create/revoke. No server.py changes.
- Guarded command body {schema_version:1, command_id, project_id, action, expected: control_identity, target} with X-DevOrch-CSRF and Sec-Fetch-Site: same-origin is unchanged.
- Target contract, matching the command_store.py target_allowed map: pause, resume, stop, continue and unbind_conversation send {}.
- Target contract: retry, rereview and reconcile send {target_id: capability.target_id}; when target_id is missing the helper returns null and the button is disabled.
- Target contract: approve_owner_gate sends {gate_id: project.control_identity.gate_id}; when gate_id is missing or blank the helper returns null and the button is disabled. This mirrors the CLI --target-id handling and the daemon gate_id check.
- Target contract: bind_conversation and rebind_conversation send {adapter, binding_id} from the selected live session; with no selection the helper returns null.
- buildControlTarget(project, capability, selectedSession) and describeGuardedAction(project, capability, target) are pure exported helpers in app.js; the describeGuardedAction text is the only content of the confirm prompt.
- The confirmation gate covers stop, retry, rereview, reconcile and approve_owner_gate; cancelling returns null before any fetch, and the daemon remains the final identity authority.
- Static allowlist stays at /, /app.js and /style.css under CSP default-src 'self'; existing app.js exports are kept.
- Freshness vocabulary is Live, Stale, Disconnected and Unknown as text; unavailable evidence is never shown as 0 or healthy.

### Validation plan
- node --check web/app.js exits 0.
- pytest passes for tests_py/test_web.py, test_accounting_dashboard.py, test_unbound_web_ui.py, test_web_ui_refresh.py, test_p12_control_actions.py, test_p12_control_foundation.py and test_control_commands.py.
- The full pytest tests_py run shows no new failures compared with the baseline.
- tests/web-selftest.ps1 passes; PowerShell 5.1 sequencing uses ';' and $LASTEXITCODE checks, never && or ||.
- A Node test asserts buildControlTarget output for all 11 actions, including approve_owner_gate returning {gate_id: 'gate-1'} for control_identity.gate_id 'gate-1' and null when gate_id is null.
- A test asserts every buildControlTarget result's keys are a subset of the command_store target_allowed set for that action.
- A test posts approve_owner_gate through sendControl with confirm accepted and asserts the body target equals {gate_id: control_identity.gate_id} and expected equals control_identity.
- For each of stop, retry, rereview, reconcile and approve_owner_gate, a Node test asserts the confirm text includes the identity lines, target_id or gate_id, and the consequence, and that cancel issues zero fetch calls.
- A test asserts retry, rereview, reconcile and approve_owner_gate buttons are disabled when the capability is unavailable or the required target value is missing.
- Optionally, a Python test passes the UI-shaped approve and retry bodies through the command_store request validation to show they are accepted as well-formed.
- Fixture tests show failure, stall, gate, paused, stale, disconnected and unavailable states render explicitly, and table ordering is deterministic.
- A non-mutation test shows refresh and navigation issue only GET requests; the existing stop-confirm/poll and pairing CSRF tests pass.
- git diff --check is clean and the diff touches only web/, tests_py/ and optional evidence docs.
- A manual 1366x768 check shows health, active roles, incidents, AI resources, freshness and last refresh in the first viewport, visible focus, and a correct confirm dialog.

### Risks / failure modes
- The current UI sends target {} for retry, rereview, reconcile and approve_owner_gate, which the daemon blocks. Filling target_id and gate_id aligns the web UI with the existing CLI and daemon contract without changing server semantics.
- A client-side target copied from a stale projection is still rejected by the daemon's exact identity and gate checks; the UI must surface blocked results as text, not success.
- window.confirm is plain text only; the identity must be on labelled lines to stay readable.
- Renaming DOM ids or exports can break the existing Node tests; freeze them and rerun often.
- allSettled handling could present a missing source as zero or healthy; unavailable fixtures guard against this.
- The test fake DOM supports only a minimal API; rendering must avoid unsupported DOM methods.
- CSP blocks inline scripts, inline styles and external assets.
- Freshness thresholds could conflict with the server's stale flags; prefer monitor.stale and sources availability.
- Contrast and focus need recorded manual evidence.
- agent/next.md still shows P12.6; use the staged P12.7 spec.

### Out of scope
- Changes to server.py, control/surface.py, control/command_store.py, core/control_commands.py, routes, security headers or daemon lifecycle.
- New APIs, capability or target schema changes, or any client-side source of truth.
- Adding an optional gate_id to continue from the web UI.
- A custom modal framework; the native confirm is enough for this task.
- Routing frameworks, build tooling, third-party chart libraries or external fonts.
- Decorative charts.
- OpenCode branding, wording, assets or pixel-copying.
- P13 remote jobs and P14 Android client.
- Changes to idempotency, CSRF, origin/Host, audit or pairing semantics.
- Lifecycle bookkeeping beyond recording P12.7 evidence.

### Independent plan review
- Approved: No BLOCKING findings. The plan now matches the repository’s guarded-target contracts, preserves mutation authority and HTTP/security semantics, covers every required operational state, sequences fixture validation before styling, and provides executable automated and manual verification for the frozen design.
