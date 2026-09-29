# P18 External Control API and MCP

DevOrchestrator exposes a bounded native control entrypoint through the existing
daemon on `http://127.0.0.1:8770`. It is an adapter over the existing control,
job, and `ExecutionTransport` services; it is not a lifecycle authority.

```text
ChatGPT / authorized client
  -> stdio MCP adapter or authenticated Control API
  -> daemon control/service layer
  -> evidence-based transport selector
  -> LocalTransport or SSHTransport
  -> durable audit + transport-operation evidence
```

## Security model

- The daemon binds to `127.0.0.1` by default. A non-loopback listener disables
  the Control API. Use a local MCP process or an authenticated SSH tunnel for a
  remote client; do not publish port 8770 directly.
- The master bearer token is generated at `runtime/control/api-token`, outside
  tracked source files. Comparison is constant-time. Tokens and content bytes
  are excluded from audit logs.
- Every external operation returns `X-DevOrch-Request-ID`; the Python client and
  MCP response copy it into the response envelope as `request_id`. A caller may
  supply a unique ID with `X-DevOrch-Request-ID`.
- Every external request is recorded as `external_operation` in
  `runtime/control/audit.jsonl`. Machine operations additionally write sanitized
  evidence to `runtime/logs/transport-operations.ndjson`.
- Command execution is by host-local `command_ref` only. Clients cannot supply
  `argv`, shell text, environment variables, or an executable path. Parameter
  values use the closed host-local parameter schema.
- Paths are restricted to the configured project repository and `file_roots`.
  Reads are capped at 10 MiB. Writes are staged, digest-verified, bounded by the
  host policy, and require create-only or expected-hash CAS semantics.
- Synchronous command timeouts cannot exceed the host-local command limit.
  Output is bounded by the durable-job log policy. Durable submission enforces
  a host-local `max_concurrent_jobs` cap (default 4).
- Poll and cancel require the exact stable job ID and project identity. Local
  jobs are checked against their owning project before either operation.
- Missing or stale capability evidence fails closed. Normal requests never
  fall back to RDC; selection must return `local` or `ssh`.
- `hardware` effect-class commands remain unconditionally rejected. This
  interface cannot authorize X-ray emission or conveyor/VFD activation.

The MCP adapter still includes the existing guarded `devorch_control` tool for
daemon-coordinated lifecycle commands. It never writes lifecycle ledgers
directly, cannot fabricate a successor, and cannot bypass owner gates. There is
no external lifecycle-ledger mutation route.

## Start, stop, and status on Windows

The API is owned by the unified daemon, so these PowerShell 5.1-compatible
wrappers manage that process and use its hidden/no-window launcher:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ops\start_devo_control_api.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ops\status_devo_control_api.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ops\stop_devo_control_api.ps1
```

Stopping the Control API stops the unified daemon. The default endpoint is
`http://127.0.0.1:8770`.

## MCP connection for ChatGPT or another MCP client

Register a local stdio server with this command and working directory:

```text
command: C:\work\github\DevOrchestrator-dev\ops\devorch.cmd
args: mcp-adapter --runtime-root C:\work\github\DevOrchestrator-dev\runtime --enable-transport-tools
cwd: C:\work\github\DevOrchestrator-dev
```

`--enable-transport-tools` is mandatory and explicit. Without it, the MCP server
continues to expose only the original four control tools. With it, these native
tools are added:

| Tool | Purpose |
| --- | --- |
| `devorch_status` | Authenticated daemon/project/lifecycle/owner/role/git/activity status |
| `devorch_exec` | Bounded synchronous allowlisted command |
| `devorch_spawn` | Durable allowlisted background job |
| `devorch_poll` | Exact job state, result, and bounded evidence |
| `devorch_cancel` | Idempotent exact-job cancellation |
| `devorch_read_file` | Bounded binary read under allowed roots |
| `devorch_stat` | Scoped file metadata and hash |
| `devorch_stage_write` | Stage digest-verified Base64 content |
| `devorch_write` | Create-only or expected-hash CAS write |
| `devorch_transport_*` | Host, capability, and operation evidence |

The adapter reads the bearer token from the runtime directory. Do not put the
token in an MCP config file or command line.

## Direct API

Send `Authorization: Bearer <token>` and JSON bodies to the following routes:

- `GET /api/v1/control/external/status?project_id=devorchestrator`
- `GET /api/v1/control/transport/commands?project_id=devorchestrator&host_id=local`
- `POST /api/v1/control/transport/exec`
- `POST /api/v1/control/transport/spawn`
- `POST /api/v1/control/transport/poll`
- `POST /api/v1/control/transport/cancel`
- `GET /api/v1/control/transport/read`
- `GET /api/v1/control/transport/stat`
- `POST /api/v1/control/transport/stage-write`
- `POST /api/v1/control/transport/write`

Exec/spawn/write require a caller-chosen `idempotency_key`. MCP writes also
require `if_absent=true` or `expected_sha256`. API responses report
`selected_transport` for every native machine operation.

## Browser session authorization

P19 also exposes these routes through the loopback dashboard without revealing
the master token. Create a browser session with a same-origin
`POST /api/v1/control/browser-sessions`, then send the returned HttpOnly cookie
and `X-DevOrch-CSRF` value on Control API calls. Mutations require a valid
same-origin `Origin`; GET/HEAD reads may omit `Origin` only when
`Sec-Fetch-Site` is exactly `same-origin`.

The owner-only `transport/commands` route returns safe command metadata for the
console and deliberately excludes argv, cwd, environment, and executable data.
Scoped transport capabilities cannot enumerate it. See
[`P19_WEB_CONSOLE_DESIGN.md`](P19_WEB_CONSOLE_DESIGN.md) for the complete browser
boundary and staged/CAS workflow.

## Token rotation

1. Stop the unified daemon.
2. Move `runtime/control/api-token` to a private backup location.
3. Start the daemon; it creates a new random 256-bit token file.
4. Restart local MCP clients and securely remove the old backup after validation.

Never print or commit either token. Existing scoped transport capabilities have
their own expiry/revocation records and should be reviewed or revoked separately.

## Verification and troubleshooting

- `status_devo_control_api.ps1` reports daemon liveness, loopback binding, port,
  token presence, last tick, and last error without printing the token.
- HTTP 401 means the bearer is missing or invalid. HTTP 403 means a scoped
  capability, nonce, root, command, or job/project fence rejected the operation.
- `unknown_command_ref` means the command is not in the executing host's
  `execution-jobs.json`. Do not add an arbitrary-shell command to work around it.
- `rdc_fallback_required`, stale capabilities, or no safe native candidate is a
  closed failure. Fix Local/SSH evidence instead of silently invoking RDC.
- Confirm zero RDC by correlating response request IDs with
  `runtime/logs/transport-operations.ndjson`; every row must select `local` or
  `ssh`. The real acceptance artifact is in `docs/evidence/`.

## Rollback

Revert the external-control commit and restart the unified daemon. The existing
P18 transport/job stores and lifecycle authority remain compatible. Runtime
tokens, jobs, and audit evidence may be retained; no lifecycle ledger migration
is introduced by this entrypoint.
