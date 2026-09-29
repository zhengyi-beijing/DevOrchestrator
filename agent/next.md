# P19.2 — Remote MCP Interface

Status: **READY_TO_RUN**

Predecessor: P19 (P19.1 Remote HTTP/HTTPS Gateway + Web UI)
Successor: TBD

## Objective

Provide a standards-compliant remote Model Context Protocol endpoint through
which authenticated ChatGPT and other MCP-capable AI clients can use a small,
bounded set of DevOrchestrator capabilities. P19.2 is a protocol adapter over
the same DevO control/service layer used by the P19.1 REST/Web path; it is not a
new lifecycle authority, execution layer, or parallel job state machine.

P19.2 is documentation-only until activated through the normal P19 successor
handoff. Its implementation must use an official or well-maintained MCP SDK
unless a strong reason for an exception is documented and reviewed.

## Architecture and ownership boundary

```text
ChatGPT / MCP client                    Browser / Web client
        |                                       |
        v                                       v
remote /mcp endpoint                  P19.1 HTTP/HTTPS Gateway
        |                                       |
        v                                       v
MCP Adapter ----------------> shared DevO Control Service
                                      |
                                      v
                              ExecutionTransport
                                      |
                         LocalTransport / SSHTransport
                                      |
                              target host / ZXZ-PC
```

- The MCP Adapter owns protocol translation, MCP schema validation, request
  correlation, authentication at its ingress, and MCP error/result mapping.
- The shared DevO control/service layer owns orchestration operations, policy,
  authoritative state access, durable jobs, idempotency, audit, and delegation
  to ExecutionTransport. REST/Web and MCP adapters must not duplicate that
  business logic.
- P18 remains the native execution layer: ExecutionTransport, LocalTransport,
  SSHTransport, durable execution primitives, bounded file/stat/write
  primitives, capability discovery, and transport safety/observability.
- Existing lifecycle authority, owner gates, retry and provider/model policy,
  successor rules, invariant evaluation, and hardware authorization remain
  authoritative. P19.2 adds no lifecycle ledger mutation surface.
- Normal MCP operations select LocalTransport or SSHTransport. RDC is neither
  an implicit fallback nor a hidden reachability mechanism.

## Initial MCP tool surface

All tool schemas are closed, bounded, and reject unknown or over-limit input.
The initial surface is deliberately limited to:

1. `devo_status`
   - Return consolidated authoritative daemon, project/task, lifecycle,
     `owner_gate`, active execution, active AI role, latest meaningful activity,
     Git HEAD/dirty state, unresolved invariant, and transport-health data where
     available.
   - Read authoritative DevO state; do not reconstruct truth from Markdown
     status files. A terminal task must not be projected as a stale owner gate.
2. `devo_start_task`
   - Start or resume work only through the existing DevO orchestration/control
     interfaces and their lifecycle, owner-gate, retry, provider/model, and
     hardware-safety policy.
   - Require a caller-visible idempotency key and return a stable DevO job/work
     identifier. Never launch by directly editing lifecycle state.
3. `devo_job_status`
   - Return durable queued/running/completed/failed/cancelled state plus the
     associated goal/task, worker/provider/model when available, timestamps,
     exit/result state, verification summary, and relevant commit/hash/result
     identifiers.
4. `devo_cancel_job`
   - Perform bounded, idempotent cancellation of only the referenced job.
     Repeated cancellation is safe and unrelated lifecycle state is unchanged.
5. `devo_read_log`
   - Provide bounded access by explicit source with line/output limits and a
     source/path allowlist. It provides no arbitrary traversal.
6. `devo_read_file`
   - Initially read-only, with allowed roots, canonicalized paths, maximum size,
     traversal rejection, and explicit text/binary behavior.

Possible later tools include `devo_git_status`, `devo_run_tests`, `devo_stat`,
and controlled CAS write. A generic unrestricted `shell(command)` tool is not
part of the primary design or required for normal operation.

## MCP and long-running behavior

- Support normal MCP tool discovery and invocation, structured input schemas
  and results, clear protocol-level errors, and request/correlation IDs.
- Prefer Streamable HTTP and the currently supported standard remote-MCP
  transport semantics; do not implement a custom pseudo-MCP HTTP API.
- `devo_start_task` returns promptly with a stable `job_id`. Clients use
  `devo_job_status(job_id)` and, when needed, `devo_cancel_job(job_id)` rather
  than holding one MCP request open for a complete Worker execution.
- Reuse DevO's existing durable execution/job abstraction. Restart, replay, and
  ambiguous timeout handling must reconcile the same job identity rather than
  creating a second MCP-specific state machine or duplicate work.

## Remote reachability and deployment

A service bound only to `127.0.0.1` is not directly reachable by an external
ChatGPT service. Preserve loopback binding as the safe default and publish the
MCP endpoint only through a separately configured secure access layer, such as
an authenticated HTTPS reverse proxy, secure tunnel, or supported MCP tunnel.
Network exposure does not grant or replace DevO lifecycle authority. Raw,
unauthenticated router port forwarding is unsupported.

Documentation must explain endpoint startup, connection, authentication,
credential storage, tunnel/exposure setup, supported tools, limits, diagnostics,
and rollback without committing secrets or weakening the loopback-first
deployment.

## Mandatory security and safety requirements

- Fail closed and require authenticated remote access; anonymous MCP access is
  forbidden.
- Keep credentials and tokens outside tracked source files, never return or log
  secrets, and retain request IDs plus a sanitized audit trail.
- Enforce bounded tool schemas, command/task concurrency limits, timeouts,
  output limits, allowed roots, path canonicalization, and idempotency for every
  mutating action.
- Provide no arbitrary filesystem access, unrestricted shell tool, lifecycle
  ledger mutation tool, owner-gate bypass, fabricated successor, or transport
  capability bypass.
- Never silently route through RDC.
- Preserve all current hardware-safety fences. P19.2 must not automatically
  enable real X-ray emission, conveyor/VFD motion, detector-board/serial-device
  actuation, or any other real hardware operation without the existing explicit
  human authorization.

## Verification plan

### Discovery and protocol

- Connect through the supported remote MCP transport, list tools, validate all
  schemas, invoke representative tools, and verify structured results/errors and
  correlation IDs.

### Authentication and secrecy

- Reject missing and invalid credentials; accept valid credentials; prove the
  secret never appears in application, proxy, audit, or tool-result logs.

### Authoritative status

- Confirm status comes from authoritative lifecycle/runtime sources, includes
  Git and transport state, and does not project a terminal task as a stale owner
  gate.

### Durable task execution and replay

- Start a harmless task, receive a job ID, poll it to a terminal state, restart
  the server and recover the same durable job, and prove duplicate/replayed
  starts do not unintentionally create duplicate work.

### Cancellation

- Cancel an active harmless job and prove repeated cancel is safe, idempotent,
  and scoped to that job.

### Files and logs

- Prove allowed reads succeed while traversal, outside-root, unknown-source, and
  oversized reads fail closed or are bounded according to the published schema.

### Policy, safety, and regression

- Prove the MCP path cannot bypass an owner gate, edit lifecycle ledgers,
  fabricate a successor, bypass transport capability checks, implicitly invoke
  RDC, or weaken hardware authorization.
- Run security, restart, replay, idempotency, P19.1 REST/Web, P18 transport,
  durable-job, lifecycle/successor, watchdog, project-status, and control-plane
  invariant regressions.
- Record representative zero-RDC MCP operations with request IDs, job IDs,
  selected transports, results, and an observed RDC call count of zero.

## Acceptance

P19.2 is complete only when:

1. A standards-compliant remote MCP endpoint exists.
2. MCP tool discovery succeeds.
3. `devo_status`, `devo_start_task`, `devo_job_status`, `devo_cancel_job`,
   `devo_read_log`, and `devo_read_file` work with their documented schemas.
4. MCP and P19.1 REST/Web paths use the same DevO control/service layer.
5. No duplicate lifecycle authority exists.
6. No generic unrestricted shell MCP tool is required for normal operation.
7. Authentication is mandatory.
8. Secrets are neither committed nor logged.
9. File access is constrained to allowed roots.
10. Long-running work uses stable durable job IDs.
11. Lifecycle and owner-gate policy cannot be bypassed.
12. Real hardware safety authorization remains intact.
13. Normal MCP operations use LocalTransport or SSHTransport and do not silently
    route through RDC.
14. Zero-RDC acceptance is demonstrated for representative MCP operations.
15. Security, restart, replay, and idempotency regressions pass.
16. Documentation explains connection, authentication, startup,
    tunnel/exposure, supported tools, limitations, and rollback.

## Non-goals and sequencing

- Do not replace or redesign P19.1, reopen P18, or change hardware policy.
- Do not implement a second orchestration service, lifecycle authority, durable
  job state machine, transport selector, or business-logic copy in the adapter.
- Do not make P19.2 implementation a condition for P19.1 completion. P19.1 must
  preserve the shared service seam, then close and hand off normally to P19.2.
- Do not implement P19.2 before its normal successor activation and executable
  plan review.

## Approved executable design

Add a standards-compliant, authenticated remote MCP endpoint at POST /mcp inside the existing P19.1 loopback daemon (src/dev_orchestrator/web/server.py), exposing exactly six bounded tools (devo_status, devo_start_task, devo_job_status, devo_cancel_job, devo_read_log, devo_read_file) as a protocol adapter over the same DevO control/service layer the REST/Web path uses. The adapter adds no lifecycle authority, no second durable job state machine, no shell or write tool, and no RDC path. A shared service module (control/operations.py) is extracted from the four inline transport route bodies in web/server.py so REST and MCP call one implementation; status and logs reuse the existing pure helpers external_status_payload() and read_control_logs(). Remote reach is achieved only through a separately configured same-host authenticated HTTPS proxy or tunnel forwarding /mcp; loopback-first binding and all P18 hardware/transport fences are preserved.

### Implementation steps
- Create src/dev_orchestrator/control/operations.py exposing spawn_job(), poll_job(), cancel_job(), read_file(). Move the operation cores currently inline in web/server.py (/api/v1/control/transport/spawn ~1870, /poll ~1988, /cancel ~2038, /transport/read ~1257) verbatim in behavior: host/policy resolution via load_transport_hosts_config and resolve_execution_policy, transport acquisition via _get_transport_for_host, the transport call, and log_transport_operation with request_id and capability_id. Each returns a plain result dict plus a status hint or raises ControlOperationError(status, reason, message).
- Make each operations.py function fail closed when the resolved selection is rdc_fallback_required or rejected, and assert the reported selected_transport is exactly 'local' or 'ssh'. Never add an RDC fallback path and never weaken transport/selector.py hardware rejection.
- Rewrite the four REST route bodies in web/server.py to delegate to control/operations.py. Keep authentication (_validate_transport_auth, _owner_authorized), _local_job_belongs_to_project ownership checks, _control_envelope shaping, and the exact HTTP status codes (200/202/400/403/404) in the route so tests_py/test_p18_external_control.py and tests_py/test_p19_web_console*.py keep passing unchanged.
- Create src/dev_orchestrator/control/mcp_http.py implementing JSON-RPC 2.0 over Streamable HTTP: initialize, ping, tools/list, tools/call; notifications return 202 with no body. Negotiate protocolVersion from a supported set (2025-06-18, 2025-03-26, 2024-11-05), echoing the client value when supported and otherwise returning the newest supported version.
- Implement content negotiation on Accept in mcp_http.py: respond application/json by default, and emit a single-event text/event-stream frame when the client accepts only SSE. GET /mcp returns 405 with an Allow header (permitted for servers offering no server-initiated stream); DELETE /mcp terminates a session.
- Implement bounded in-memory MCP session handling: issue Mcp-Session-Id on initialize, require it on subsequent calls, enforce a TTL and a maximum concurrent session count, and return 404 for unknown or expired sessions so the client re-initializes. Sessions are intentionally not durable; durable jobs are.
- Define the six closed tool schemas in mcp_http.py with additionalProperties:false and explicit required lists, reusing the existing argument-checking and 64KB result-bounding style of control/mcp_adapter.py (_checked_args, _format_tool_result) by factoring the shared helpers rather than duplicating them.
- Wire devo_status{project_id?} to web.server.external_status_payload() (authoritative lifecycle, owner_gate, git, daemon projection). Do not reconstruct status from Markdown, and preserve the existing rule that a terminal task is not projected as a stale owner gate.
- Wire devo_start_task{project_id, command_ref, idempotency_key, parameters?, host_id?, expected_working_directory?} to operations.spawn_job(), returning promptly with the stable durable job id. Require the caller-supplied idempotency_key and never generate one server-side, so replays reconcile to the same ExecutionJobStore job.
- Wire devo_job_status{project_id, job_id} to operations.poll_job() and devo_cancel_job{project_id, job_id, reason?} to operations.cancel_job(), preserving the existing exact-job ownership check so cancellation is idempotent and scoped to the referenced job only.
- Wire devo_read_log{source, project_id?, job_id?, limit?, cursor?} to control.logs.read_control_logs() with source restricted by a closed enum allowlist and limit clamped to 1-100; rely on the existing redact_secrets() path. Wire devo_read_file{project_id, path, max_bytes?, offset_bytes?} to operations.read_file() with allowed-root canonicalization, traversal rejection, an MCP default of 1MB and the existing 10MB hard cap, and explicit text/binary reporting.
- Add the /mcp route to web/server.py: accept POST (and DELETE for session teardown), enforce mandatory bearer authentication before any JSON-RPC parsing, reject anonymous access with 401 plus WWW-Authenticate: Bearer, and keep the existing _client_is_loopback() gate on the ingress.
- Extend authentication in control/security.py with a revocable MCP capability token minted through the existing capability-store pattern (create_transport_capability / validate_transport_capability): hashed at rest, constant-time comparison, scoped and revocable, accepted alongside the master owner token. Never log, echo, or return the token value.
- Add a dev-orchestrator mcp-token {mint,list,revoke} CLI subcommand in src/dev_orchestrator/cli.py so operators configure a tunnel without pasting the master token, following the existing subparser registration pattern near the mcp-adapter command.
- Validate the Origin header against a configured allowlist when present (DNS-rebinding defense) and allow an absent Origin for non-browser MCP clients, which remain gated by mandatory bearer plus loopback. Never trust X-Forwarded-For or X-Real-IP for authorization decisions.
- Register '/mcp' in _EXTERNAL_OPERATION_PATHS so it inherits X-DevOrch-Request-ID handling and the external_operation audit row, and emit a per-call sanitized mcp_tool_call audit entry carrying request id, session id, tool name, job id, and outcome, with no argument values that could carry secrets.
- Enforce bounds in the MCP path: 64KB maximum request body, a global and per-session in-flight request cap, per-tool timeouts, and 64KB bounded tool results with explicit truncation markers.
- Write tests_py/test_p19_2_remote_mcp.py covering discovery and schema validity, protocol-version negotiation, session lifecycle and expiry, missing/invalid credential rejection, secret absence across audit and transport logs, authoritative status including git and transport state, spawn to terminal poll, restart recovery of the same job id, replayed idempotency_key producing one job, repeated cancel being safe and scoped, traversal/outside-root/unknown-source/oversized reads failing closed, absence of any shell/write/lifecycle tool, and that rdc_fallback_required is never silently satisfied.
- Write ops/p19_2_remote_mcp_acceptance.py following the shape of ops/p19_web_console_acceptance.py: drive a real separate-process MCP session against the running daemon and emit docs/evidence/P19_2_REMOTE_MCP_ACCEPTANCE.json plus docs/evidence/P19_2_REMOTE_MCP_OPERATIONS.ndjson containing request IDs, job IDs, selected transports, results, and an observed RDC call count of zero. Fail the run if any selected transport is not local or ssh.
- Write docs/P19_2_REMOTE_MCP_DESIGN.md covering architecture and ownership boundary, the six tool schemas, startup, connection, authentication and credential storage, tunnel/exposure setup restricted to forwarding only /mcp, limits, diagnostics, and rollback (source revert plus daemon restart, no store migration). Commit no secrets.
- Document the MCP SDK exception in the same design doc: the official mcp Python SDK requires anyio/starlette/uvicorn/pydantic and an ASGI host, which conflicts with the deliberately zero-dependency stdlib ThreadingHTTPServer daemon (pyproject.toml dependencies = []), so P19.2 extends the already-reviewed in-repo JSON-RPC core and conforms to published Streamable HTTP semantics instead.
- Cross-link the new design doc from README.md and docs/P19_WEB_CONSOLE_DESIGN.md, and record the P19.2 acceptance evidence references alongside the existing P18/P19 evidence entries.
- Run the focused tests, then the full tests_py regression, then the live acceptance script, and record the results before proceeding through the normal lifecycle closure and successor handoff for P19.2.

### Interfaces / contracts
- src/dev_orchestrator/control/operations.py: spawn_job(runtime_root, *, project_id, command_ref, idempotency_key, host_id='local', parameters=None, expected_working_directory=None, input_digest=None, request_id=None, capability_id=None) -> dict; poll_job(...); cancel_job(..., reason='cancelled'); read_file(runtime_root, *, project_id, path, host_id='local', max_bytes, offset_bytes, request_id=None, capability_id=None) -> dict. Each raises ControlOperationError(status_code, reason, message) and emits log_transport_operation.
- src/dev_orchestrator/control/mcp_http.py: MCPHttpEndpoint/handle_jsonrpc(raw_body, *, session_id, request_id, auth_context) -> (status_code, headers, body_bytes) plus a TOOLS registry describing the six devo_* tools.
- HTTP surface: POST /mcp (JSON-RPC 2.0, Streamable HTTP, application/json or single-event text/event-stream), DELETE /mcp (session termination), GET /mcp -> 405 with Allow. Headers: Authorization: Bearer, Mcp-Session-Id, MCP-Protocol-Version, X-DevOrch-Request-ID.
- MCP tool schemas (all additionalProperties:false): devo_status{project_id?}; devo_start_task{project_id, command_ref, idempotency_key, parameters?, host_id?, expected_working_directory?}; devo_job_status{project_id, job_id}; devo_cancel_job{project_id, job_id, reason?}; devo_read_log{source, project_id?, job_id?, limit?, cursor?}; devo_read_file{project_id, path, max_bytes?, offset_bytes?}.
- Reused unchanged: dev_orchestrator.web.server.external_status_payload(), dev_orchestrator.control.logs.read_control_logs() and redact_secrets(), dev_orchestrator.transport.hosts.get_transport_for_host(), dev_orchestrator.transport.selector.select_transport(), dev_orchestrator.jobs.store.ExecutionJobStore, dev_orchestrator.transport.observability.log_transport_operation().
- dev_orchestrator.control.security.ControlSecurity: add a revocable MCP capability alongside the existing transport-capability mint/validate/revoke API; bearer validation remains constant-time and hashed at rest.
- CLI: dev-orchestrator mcp-token mint|list|revoke, registered next to the existing mcp-adapter subparser in src/dev_orchestrator/cli.py.
- Evidence artifacts: docs/evidence/P19_2_REMOTE_MCP_ACCEPTANCE.json and docs/evidence/P19_2_REMOTE_MCP_OPERATIONS.ndjson, correlatable by request id with runtime/control/audit.jsonl and runtime/logs/transport-operations.ndjson.

### Validation plan
- python -m pytest tests_py/test_p19_2_remote_mcp.py -q passes, covering tool discovery, schema validity, protocol negotiation, and structured results/errors with correlation IDs.
- Authentication tests prove missing and invalid credentials are rejected, valid credentials are accepted, and a scan of application, audit, and tool-result output shows the secret value never appears.
- Status tests prove devo_status comes from the authoritative projection, includes git and transport state, and does not project a terminal task as a stale owner gate.
- Durability tests prove devo_start_task returns a stable job id, devo_job_status polls to a terminal state, the same durable job is recovered after a daemon restart, and a replayed idempotency_key does not create duplicate work.
- Cancellation tests prove devo_cancel_job is bounded, idempotent on repeat, and cannot affect an unrelated job.
- File/log tests prove allowed reads succeed while traversal, outside-root, unknown-source, and oversized reads fail closed or are bounded per the published schema.
- Policy tests prove the MCP path exposes no lifecycle-mutating, owner-gate-approving, successor-fabricating, shell, or write tool, cannot bypass transport capability checks, and never silently satisfies rdc_fallback_required.
- python -m pytest tests_py -q passes as a full regression across P18 transport, P19.1 REST/Web, durable jobs, lifecycle/successor, watchdog, project status, and control-plane invariant suites.
- Live run: start the daemon, then python ops/p19_2_remote_mcp_acceptance.py produces docs/evidence/P19_2_REMOTE_MCP_ACCEPTANCE.json and docs/evidence/P19_2_REMOTE_MCP_OPERATIONS.ndjson with representative operations, request IDs, job IDs, selected transports of local or ssh only, and an observed RDC call count of zero.
- Correlate acceptance request IDs against runtime/control/audit.jsonl and runtime/logs/transport-operations.ndjson to confirm the sanitized audit trail exists for every MCP tool call.
- Record a real MCP client transcript (tool list plus at least one call per tool) in the acceptance evidence to demonstrate interoperability rather than only self-testing.
- All ops scripts and documented Windows command sequences avoid && and || because Windows PowerShell 5.1 does not implement pipeline-chain operators; use separate statements with explicit $LASTEXITCODE checks (provenance: seed:p11b:rdc-powershell-5.1).

### Risks / failure modes
- Extracting four inline route bodies from web/server.py could perturb P18/P19.1 response shapes or status codes. Mitigation: keep envelope shaping, auth, and ownership checks in the route, move only the operation core, and gate on the full tests_py regression.
- Hand-rolled Streamable HTTP could drift from real client expectations. Mitigation: conform to the published transport semantics (Accept negotiation, Mcp-Session-Id, protocol-version negotiation, 405 on GET) and capture a real client transcript in the acceptance evidence.
- Publishing through a tunnel that forwards more than /mcp would expose the whole loopback-gated control surface. Mitigation: mandatory bearer on /mcp, documented proxy configuration restricted to /mcp only, and never trusting forwarded-for headers for authorization.
- The MCP specification favors OAuth 2.1 for remote authorization while P19.2 ships a revocable static bearer behind an authenticated tunnel. This is a documented limitation and may need revisiting if a client refuses static bearer; it is not an OAuth implementation.
- MCP sessions are in-memory and lost on daemon restart while durable jobs survive. Clients must re-initialize and resume by job_id. Mitigation: document explicitly and cover with a restart test.
- Declining the official MCP SDK is an exception to the stated preference in agent/next.md. Mitigation: document the zero-dependency stdlib daemon rationale in the design doc for review rather than silently deviating.
- Concurrent MCP calls run on ThreadingHTTPServer threads and could exhaust resources or race on job stores. Mitigation: global and per-session in-flight caps, per-tool timeouts, and reliance on the existing durable store concurrency guarantees.
- Bounded result truncation could hide a needed field from an AI client. Mitigation: explicit truncation markers and paginated log/file access rather than silent loss.

### Out of scope
- Any MCP write, CAS write, or stage-write tool; the remote surface is read plus bounded job start/poll/cancel only.
- Any remote lifecycle-control tool: devorch_control and lifecycle ledger mutation remain stdio/local only, with no owner-gate approval or successor fabrication over MCP.
- A generic or unrestricted shell tool.
- Any RDC usage, RDC fallback, or change to the RDC escalation descriptor.
- Enabling real X-ray emission, conveyor/VFD motion, detector-board or serial-device actuation, or any other real hardware operation; existing explicit human authorization fences are untouched.
- Redesigning or replacing P19.1 REST/Web, reopening P18 transport work, or changing hardware policy.
- Replacing or removing the existing P13 stdio MCP adapter and its dev-orchestrator mcp-adapter CLI entry point.
- Implementing OAuth 2.1 / dynamic client registration for MCP authorization.
- Provisioning, operating, or committing tunnel or reverse-proxy credentials, certificates, or vendor-specific tunnel configuration beyond documentation.
- Creating a second orchestration service, lifecycle authority, durable job state machine, transport selector, or business-logic copy inside the adapter.
- Adding third-party runtime dependencies to the zero-dependency package.
- Later candidate tools such as devo_git_status, devo_run_tests, and devo_stat.

### Independent plan review
- Approved: The plan is execution-ready for a Worker without design guessing. All findings are NON_BLOCKING. (1) LIFECYCLE SEQUENCING: execution-state.json shows task_id P19.2 in pending_design, which is the correct predecessor-activated state for plan review; P19.1 is IMPLEMENTATION_COMPLETE and has handed off. (2) INTERFACE ACCURACY: Every referenced function, constant, class, route, and line number was verified against the repository — _EXTERNAL_OPERATION_PATHS (line 99), spawn (1872), poll (1988), cancel (2038), read (1257), external_status_payload (line 835, signature confirmed with runtime_root/config_path/bridge_store/project_id parameters), _get_transport_for_host (line 118), _validate_transport_auth (line 1390), _owner_authorized (line 1378), _client_is_loopback (line 1358), _local_job_belongs_to_project (line 142), _control_envelope (line 393), ControlSecurity.create/validate/revoke_transport_capability with confirmed signatures, mcp_adapter._checked_args/_format_tool_result (lines 547/602), read_control_logs with limit 1-100 clamping and redact_secrets, ExecutionJobStore with claim_or_get/get/update/list, log_transport_operation with full audit field set, select_transport returning rdc_fallback_required, and dependencies=[] in pyproject.toml all exist as described. (3) CORE CONTRACT: The plan specifies closed tool schemas, operations.py function signatures with raises/return semantics, MCPHttpEndpoint.handle_jsonrpc interface, JSON-RPC 2.0 method dispatch, session lifecycle, content negotiation, and the HTTP surface (POST/DELETE/GET→405) — sufficient for implementation without guessing. (4) EXTRACTION SAFETY: Moving operation cores from inline route bodies (spawn 1872-1986, poll 1988-2036, cancel 2038-2086, read 1257-1312) while keeping auth/envelope/ownership/status-codes in the routes, gated by existing test_p18_external_control.py and test_p19_web_console*.py regression, is a sound refactoring strategy with a clear verification path. (5) SECURITY: Mandatory bearer, loopback-first, Origin allowlist, no RDC path, no lifecycle mutation, no shell/write tool, hashed-at-rest revocable tokens via confirmed ControlSecurity capability-store pattern, bounded schemas/bodies/results with 64KB truncation matching existing mcp_adapter.py pattern, secret redaction via confirmed redact_secrets. (6) MCP SDK EXCEPTION: Justified by dependencies=[] constraint and precedent of existing mcp_adapter.py (639 lines of in-repo JSON-RPC 2.0 over stdio); plan routes documentation to design doc for review per task spec. (7) VERIFICATION PATH: Focused unit tests, full regression (8 existing P18/P19 test files confirmed), live acceptance following confirmed p19_web_console_acceptance.py pattern (stdlib-only, sequential requests, transport verification, evidence JSON/NDJSON output), real client transcript, audit correlation, and zero-RDC demonstration cover all 16 acceptance criteria. (8) NON_BLOCKING observations for Worker: (a) _checked_args actual signature is (args, allowed, required) not (args, required, optional) as plan states — Worker resolves when factoring; (b) MCP protocol version negotiation details are implementation choices; (c) PowerShell 5.1 constraint (provenance seed:p11b) correctly applied.
