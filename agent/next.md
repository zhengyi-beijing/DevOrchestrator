# P19.2 — Remote MCP Interface

Status: **PENDING DESIGN**

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
