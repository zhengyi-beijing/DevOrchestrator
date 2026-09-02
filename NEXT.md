# P2 READY — Read-only Web Dashboard

Status: **ONE EXECUTABLE DEVORCHESTRATOR TASK**.
Baseline: `d54f429` (P1.1 telemetry accepted).

## Goal

Implement a standalone read-only Web dashboard over the normalized P1 runtime state. The dashboard must visualize monitor/project health without invoking AI, Workers, builds/tests in observed projects, Git mutation, or hardware actions.

## Server boundary

- Add a dependency-free local HTTP server under `src/` using Windows/.NET facilities available on XLabServer; do not require Node/npm or a framework.
- Default bind: `127.0.0.1:8770`; support explicit `-ListenAddress` and `-Port` for LAN access.
- Serve only this repository's static web assets and normalized files under `runtime/`; prevent path traversal.
- GET/HEAD only. Reject POST/PUT/PATCH/DELETE. No shell/prompt/Worker-control endpoint.

## Dashboard

- `/` overview: monitor heartbeat plus one card per project.
- Project card/detail must show normalized state, phase/next title, Git branch/HEAD/dirty, Worker state/PID, elapsed, ETA range/source/confidence, health, last activity age, and latest transition.
- Add read-only JSON endpoints for summary, one project snapshot, and bounded recent events/runs.
- Browser refreshes data automatically without full-page reload; stale/dead monitor state must be visually explicit.
- Render unknown/missing fields safely; never infer hardware acceptance from software state.

## Operations and tests

- Add `ops/start-web.ps1`, `ops/status-web.ps1`, `ops/stop-web.ps1`; Web lifecycle is independent from the 60-second monitor.
- Add hardware-free tests using fixture runtime data and loopback HTTP requests: GET success, API shape, 404, method rejection, traversal rejection, stale monitor rendering, and start/status/stop lifecycle.
- Update README with local URL and explicit LAN-access command example.
- `git diff --check` and existing telemetry self-test must remain GREEN.

STOP after P2 implementation and evidence; do not start PHASE_AUTO/P3.
