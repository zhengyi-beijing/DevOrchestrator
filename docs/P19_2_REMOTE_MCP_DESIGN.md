# P19.2 Remote MCP Interface

P19.2 provides a standards-compliant, streamable remote Model Context Protocol
(MCP) endpoint over HTTP at `POST /mcp` and `DELETE /mcp` in DevOrchestrator's
existing loopback daemon.

It enables remote AI agents, IDE extensions (e.g. Claude Desktop, Cursor,
Antigravity, Windsurf), and autonomous orchestration workflows to inspect and
safely operate DevOrchestrator without requiring terminal access, shell execution,
or direct filesystem mutations.

## Architecture and Shared Service Layer

Rather than duplicating execution, host resolution, policy evaluation, and
observability logging, P19.2 extracts core transport operations into
`src/dev_orchestrator/control/operations.py`:

- `spawn_job`: validates arguments, resolves execution policies, selects transport, invokes `transport.spawn()`, and emits `log_transport_operation`.
- `poll_job`: asserts job ownership, invokes `transport.poll()`, and logs results.
- `cancel_job`: asserts job ownership, idempotently cancels the specific job without cross-job side effects, and logs results.
- `read_file`: verifies path containment within configured roots, invokes `transport.read_file()`, differentiates text vs binary content, and logs operations.

Both the REST routes (`/api/v1/control/transport/*`) and the remote MCP adapter
delegate to this single shared service layer.

## Closed Tool Surface

Remote MCP exposes strictly six closed, bounded tools. It explicitly omits raw
shell, arbitrary command execution, file deletion, and unrestricted file writes:

1. `devo_status`: Returns consolidated authoritative daemon state, project status, lifecycle state, owner gates, and git head.
2. `devo_start_task`: Spawns one registered allowlisted command (e.g. `transport_compileall`, `pytest`) with a stable caller-provided `idempotency_key`, returning a durable job ID.
3. `devo_job_status`: Polls status, exit code, duration, and output preview for a specific background job ID.
4. `devo_cancel_job`: Idempotently cancels only the referenced background job without affecting any other jobs.
5. `devo_read_log`: Queries bounded, redacted logs across allowlisted sources (`events`, `runs`, `control_audit`, `accounting`) with limit clamping (1..100).
6. `devo_read_file`: Reads bounded content from configured project roots, with explicit `is_text` boolean reporting (`content` for UTF-8 text, `content_base64` for binary data).

## Protocol Specification & Streamable HTTP

The endpoint conforms to the standard MCP Streamable HTTP transport:

- **JSON-RPC 2.0**: All calls use standard request, response, and notification envelopes.
- **Protocol Versions**: Negotiates `2025-06-18` (default/latest), `2025-03-26`, and `2024-11-05`.
- **Session Lifecycle**:
  - `POST /mcp` with method `initialize` establishes a new session and returns the `Mcp-Session-Id` header.
  - Client notifications (such as `notifications/initialized`) return `202 Accepted` with an empty body.
  - Subsequent requests require the `Mcp-Session-Id` header.
  - `DELETE /mcp` with `Mcp-Session-Id` cleanly terminates the session.
- **Content Negotiation**: Supports both `Accept: application/json` and `Accept: text/event-stream` (SSE format: `event: message\ndata: {json}\n\n`).
- **Bounded Resources**: In-flight concurrency limits, 64KB request size limit, and bounded session cache.

## Security Boundary & Capability Tokens

1. **Loopback Binding**: The daemon listens only on loopback (`127.0.0.1`). External access requires an authenticated SSH local port forward (e.g. `ssh -L 8770:127.0.0.1:8770 host`) or scoped reverse proxy.
2. **Bearer Authentication**: Every request requires a valid `Authorization: Bearer <token>` header.
3. **Capability Tokens**: In addition to the daemon master secret, operators can mint scoped, revocable capability tokens stored in `runtime/control/adapter-capabilities.json`:
   ```powershell
   python -m dev_orchestrator.cli mcp-token mint --label "claude-desktop" --ttl 86400
   python -m dev_orchestrator.cli mcp-token list
   python -m dev_orchestrator.cli mcp-token revoke <capability_id>
   ```
4. **Token Secrecy**: Tokens are compared using constant-time HMAC digest matching and are never written to audit logs, traces, or MCP JSON-RPC responses.
5. **Zero-RDC Constraint**: All job and transport operations are strictly verified to use native `local` or `ssh` transport. RDC fallback is prohibited.

## Zero-Dependency Rationale

To maintain DevOrchestrator's core invariant of zero external runtime
dependencies, the remote MCP endpoint is implemented entirely with Python
standard library primitives (`http.server`, `urllib`, `json`, `secrets`,
`hashlib`, `hmac`, `threading`). No third-party ASGI frameworks (Starlette,
FastAPI), async runtimes (AnyIO), or MCP Python SDKs are added.

## Evidence and Verification

Live acceptance against daemon port 8770:

```powershell
python ops/p19_2_remote_mcp_acceptance.py
```

The script runs a complete separate-process remote MCP session covering initialization,
discovery, authoritative status, task execution, poll completion, job cancellation, log
reading, file reading, SSE negotiation, session deletion, and zero-RDC transport audit.
Evidence is saved to:

- `docs/evidence/P19_2_REMOTE_MCP_ACCEPTANCE.json`
- `docs/evidence/P19_2_REMOTE_MCP_OPERATIONS.ndjson`

Unit and integration test suites:

```powershell
python -m pytest tests_py/test_p19_2_remote_mcp.py -v
```

## Lifecycle Boundary and Rollback

P19.2 is bounded to the remote MCP interface. It does not alter lifecycle
transition executors, reviewer/adjudicator policies, or owner gates.

Rollback consists of reverting the modified files and restarting the daemon.
All existing jobs, transport configs, and dashboard data remain fully compatible.
