# P19 Web Native Operations Console

P19 adds an **Operations** destination to the existing loopback dashboard at
`http://127.0.0.1:8770/#operations-section`. The console is a browser adapter
over the P18 native Control API. It does not add an executor, scheduler, file
store, lifecycle route, or lifecycle authority.

## Authority and security boundary

- The daemon remains bound to loopback. The page never reads, stores, or sends
  the master bearer token.
- The browser establishes an in-memory session with
  `POST /api/v1/control/browser-sessions`. Subsequent requests carry the
  HttpOnly `devorch_control` cookie, `X-DevOrch-CSRF`, and
  `Sec-Fetch-Site: same-origin`.
- Mutating requests still require a valid same-origin `Origin`. An Origin-less
  POST is rejected even when the cookie and CSRF value are valid.
- Browsers normally omit `Origin` on same-origin GET/HEAD requests. Those reads
  are accepted only when `Sec-Fetch-Site` is exactly `same-origin`; a missing,
  `same-site`, or `cross-site` value fails closed.
- `GET /api/v1/control/transport/commands` is owner-only. It returns a bounded
  catalog of `command_ref`, effect class, runtime limit, and parameter types.
  It never exposes argv, cwd, environment values, or executable paths, and a
  scoped transport capability cannot use it to enumerate commands.
- Browser sessions expire after 15 minutes and are lost on daemon restart. The
  console retries once after a 401/403 by creating a fresh session. It never
  retries a timeout or 5xx response, so an ambiguous mutation is not replayed.

## Supported operations

The console exposes the existing P18 operations: authoritative status, command
catalog, bounded `exec`, durable `spawn`, exact-job `poll` and `cancel`, bounded
binary `read`, `stat`, staged binary upload, and create-only or expected-digest
CAS `write`. Every request receives a request ID and transport evidence.

The UI accepts only an allowlisted `command_ref`; it has no arbitrary command,
argv, shell, environment, or executable input. Hardware-effect commands remain
unselectable and are rejected by the server. Native transport selection must
resolve to `local` or `ssh`; there is no RDC fallback.

Spawn, cancel, and write commit require explicit confirmation. Exec, read,
stat, catalog discovery, status, staging, and evidence refresh are read-only or
preparatory and do not prompt. Idempotency keys are generated in memory when
the operator does not provide one.

## Staged and CAS writes

The browser hashes the selected bytes with SHA-256, Base64-encodes them, and
stages them through the P18 write store. Commit requires exactly one
precondition:

- `if_absent=true` for create-only writes; or
- `expected_sha256` for compare-and-swap updates.

The staged content reference is held only in JavaScript memory and is cleared
after a commit attempt. File contents, form data, CSRF values, results, and
operation history are not written to localStorage, sessionStorage, IndexedDB,
or cookies by application code.

## Evidence and verification

Run the live browser-shaped acceptance without a bearer token:

```powershell
python ops/p19_web_console_acceptance.py
```

It exercises status, discovery, exec, spawn/poll, cancel, read/stat, staged
binary create, CAS update, conflict rejection, readback, and audit evidence. It
writes:

- `docs/evidence/P19_WEB_CONSOLE_ACCEPTANCE.json`
- `docs/evidence/P19_WEB_CONSOLE_OPERATIONS.ndjson`

Acceptance fails if a reported native transport is not `local` or `ssh`, if
the binary digest does not round-trip, or if the stale CAS precondition is not
rejected. Request IDs can be correlated with
`runtime/control/audit.jsonl` and
`runtime/logs/transport-operations.ndjson`.

## Lifecycle boundary and rollback

P19 does not write `runtime/transition-executor.json`, owner gates, invariant
state, `agent/next.md`, or staged successor files. Normal reviewed lifecycle
machinery remains solely responsible for advancing to P19.2 (see
`docs/P19_2_REMOTE_MCP_DESIGN.md` for remote MCP interface details).

Rollback is a source revert followed by a unified-daemon restart. P18 job,
transport, write, audit, and lifecycle stores require no migration.
