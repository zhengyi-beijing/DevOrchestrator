# P18 Handoff - External Control Entrypoint

Last updated: 2026-09-29 09:25 +08:00.
Branch: `main`.
Final implementation commit: `e948bec2dde1f43ba37dbb0b6360cd68f23c4ccc`.
Push status: not pushed.

## Outcome

P18 is **COMPLETE**. Its authoritative lifecycle outcome is `task_complete`; there is no active execution, owner gate, Worker, Reviewer, or unresolved active finding. P18 has no declared successor, and no P19 was fabricated.

The repository now has a bounded external control entrypoint that reuses the unified daemon, its control/service layer, native transport selector, durable job store, and existing authority model. It does not expose direct lifecycle-ledger mutation or bypass owner gates.

## Live service

- URL: `http://127.0.0.1:8770`
- Unified daemon: PID `55436`, healthy, `last_error = null`
- Bind: loopback only; non-loopback control is disabled
- Authentication: bearer token in untracked `runtime/control/api-token`; never copy it into source or MCP configuration
- AIBroker diagnostics: PID `54324`, loopback port `8875`

Start, inspect, or stop with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ops/start_devo_control_api.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ops/status_devo_control_api.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ops/stop_devo_control_api.ps1
```

Stopping this API deliberately stops the unified daemon because the endpoint is daemon-owned.

## Exact next ChatGPT connection action

Register this local stdio MCP server in ChatGPT, then restart/reconnect that MCP integration:

```text
command: C:\work\github\DevOrchestrator-dev\ops\devorch.cmd
args: mcp-adapter --runtime-root C:\work\github\DevOrchestrator-dev\runtime --enable-transport-tools
cwd: C:\work\github\DevOrchestrator-dev
```

Do not add the bearer token to the configuration. The local adapter reads it from the runtime directory. After connecting, call `devorch_status` with `project_id = devorchestrator` before issuing operations.

## Evidence and verification

- Acceptance summary: `docs/evidence/P18_EXTERNAL_CONTROL_ZERO_RDC_ACCEPTANCE.json`
- Verbatim operation rows: `docs/evidence/P18_EXTERNAL_CONTROL_OPERATIONS.ndjson`
- Operator/API guide: `docs/P18_EXTERNAL_CONTROL_API.md`
- Implementation decision note: `docs/P18_EXTERNAL_CONTROL_IMPLEMENTATION_NOTE.md`
- Result: PASS; 19 native machine operations selected `local`; RDC call count 0
- Completed job: `job-be03619939a7088d`, exit 0
- Cancelled job: `job-0e76a41e4fd95663`, durably terminal
- Binary create SHA-256: `0e18b2456ca0105f08884d5090345c340c5a3036304135a54f1c00411834f3cd`
- Binary final SHA-256: `d0388d4b8a672e40329f564bd8300d7e1d27dde3e471a7d66d3a8a4146874fc7`
- Regression: 441 tests passed in 187.597 seconds
- Static verification: `python -m compileall -q src tests_py ops`, `git diff --check`, and `graphify update .` passed
- MCP smoke: 15 tools advertised; authenticated `devorch_status` returned valid JSON, a request ID, P18 `COMPLETE`, and null owner gate/active execution

## Security and operational limits

- Commands remain allowlisted by `runtime/execution-jobs.json`; arbitrary shell commands are not accepted.
- Writes require staged content plus create-only or expected-digest CAS semantics. Paths remain scoped by transport policy.
- Requests, output, reads, timeouts, and job concurrency are bounded. Audits exclude bearer tokens and file contents.
- Remote SSH behavior is covered by regression tests but was not exercised against a second live host in this acceptance.
- `external_control_sleep` exists only in the local ignored runtime command configuration to exercise durable cancellation; it is not a tracked production policy.
- Real X-ray source or conveyor actions remain explicitly human-authorized. This entrypoint does not weaken hardware interlocks or safety policy.
- RDC remains available only as fallback/bootstrap/GUI/emergency infrastructure and was not invoked during acceptance.
