# P19 — Web Native Operations Console

Status: **PENDING DESIGN**

Predecessor: P18
Successor: TBD

## Objective

Add a production-quality, loopback web console for the bounded native machine
operations delivered by P18. The browser UI must reuse the existing Control API,
transport selector, durable job service, staged-write/CAS path, audit trail, and
lifecycle authority. It must not create a second controller or expose an
unrestricted shell or filesystem.

## Architecture boundary

```text
same-origin browser UI
  -> existing short-lived browser session + CSRF authorization
  -> existing /api/v1/control/transport/* routes
  -> existing transport selector
  -> LocalTransport or SSHTransport
```

- Never disclose or send `runtime/control/api-token` to browser JavaScript.
- Reuse the P18 route implementations; do not duplicate execution, job, file,
  CAS, policy, capability, lifecycle, or audit logic in the UI.
- This task does not change lifecycle authority, transition execution, successor
  rules, owner gates, invariant evaluation, or watchdog recovery.
- RDC remains outside the normal request path and must never be selected
  implicitly.
- Real X-ray, conveyor, detector-board, serial-device, or other hardware actions
  remain unavailable without the existing explicit human safety authorization.

## Scope

1. Add an Operations destination to the existing dashboard navigation and an
   accessible, responsive native-operations workspace.
2. Establish/reuse the existing same-origin browser session and attach its CSRF
   token to every privileged transport request. Fail closed on expired or
   missing sessions and allow the operator to reconnect without reloading.
3. Provide bounded forms for:
   - `exec` with project, host, allowlisted command reference, JSON parameters,
     working directory, timeout, and idempotency key;
   - `spawn`, returning a stable durable job ID;
   - `poll` and `cancel`, scoped to the selected project and exact job ID;
   - `read_file` with allowed-root path, offset, and byte limit;
   - `stat`;
   - staged binary upload followed by create-only or expected-digest CAS write.
4. Present request ID, selected transport, status, exit code, job ID, hashes,
   bounded stdout/stderr/output, timestamps, and actionable errors without
   rendering untrusted output as HTML.
5. Require explicit confirmation for effectful spawn, cancel, and write commit.
   Do not store operation history, file contents, secrets, or tokens in browser
   persistent storage.
6. Show recent sanitized transport-operation evidence from the existing P18
   observability endpoint and provide an explicit refresh control.
7. Keep command entry at the `command_ref` level. Do not add arbitrary argv,
   shell, environment-variable, executable-path, or raw filesystem authority.
8. Keep all existing dashboard observation and guarded lifecycle controls
   functional and visually consistent.
9. Document browser usage, security boundaries, session recovery, supported
   operations, RDC verification, and rollback.

## Security and failure behavior

- The console is available only from the loopback-bound unified daemon.
- Same-origin, fetch-metadata, HttpOnly cookie, SameSite=Strict, and CSRF checks
  remain mandatory for browser authority.
- All request bodies, timeouts, output, reads, writes, paths, concurrency, and
  job identity remain bounded by the existing P18 server and transport policy.
- Mutating requests use caller-visible idempotency keys. Writes require staged
  content plus exactly one precondition: `if_absent=true` or
  `expected_sha256`.
- A browser refresh must not replay a mutation. The UI may retain only
  in-memory form/result state for the active page.
- Authentication, policy, stale capability, path, CAS, job ownership, timeout,
  and transport failures must be shown as failures; never fall back to RDC.
- Output and file previews use text nodes/textContent only. Binary readback must
  be represented safely and remain bounded.

## Verification

- Focused server tests prove browser-session/CSRF acceptance and rejection for
  every operation family while bearer and scoped-capability behavior remains
  unchanged.
- UI contract tests cover required controls, no inline script/style regression,
  no master-token exposure, safe text rendering, confirmation gates, request ID
  display, and staged/CAS write sequencing.
- Browser-level acceptance exercises status/exec/spawn/poll/cancel/read/stat and
  staged/CAS write against the live loopback daemon from the rendered page.
- Acceptance evidence records request IDs, selected transports, job IDs, exit
  codes, hashes, and RDC call count. All machine operations must select Local or
  SSH; RDC call count must be zero.
- Run P19 focused tests plus P18 external-control/transport, P13/P14 job,
  dashboard, successor/lifecycle, transition-executor, watchdog, project-status,
  and control-plane invariant regressions; run `python -m compileall -q src
  tests_py ops`, JavaScript syntax checking, `git diff --check`, and
  `graphify update .`.

## Acceptance

- An authorized local operator can perform every requested native operation
  from the web page without learning the master bearer token.
- An unauthenticated, cross-origin, missing-CSRF, expired-session, out-of-scope,
  or replayed request fails closed.
- Exec/spawn accept only configured command references; read/stat/write remain
  inside configured roots; existing output/read/write/concurrency limits hold.
- Poll/cancel cannot cross project or job identity boundaries.
- Existing staged-write and CAS conflict semantics are preserved, including an
  arbitrary-binary round trip.
- Every operation exposes a request ID and durable sanitized audit evidence.
- No normal browser operation selects RDC, mutates a lifecycle ledger, bypasses
  an owner gate, fabricates a successor, or authorizes real hardware.
- The existing dashboard and P18 MCP/API clients remain compatible.
- P19 begins only through the normal P18 successor handoff and completes with a
  clean, committed, unpushed worktree.
