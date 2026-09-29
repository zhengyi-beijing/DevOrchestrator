# P18 External Control Implementation Note

## Reusable foundation

- `src/dev_orchestrator/web/server.py` already hosts the daemon-owned Control API
  and authenticated P18 transport routes for native exec, durable spawn/poll/cancel,
  scoped read/stat, staged binary content, and CAS write.
- `src/dev_orchestrator/control/security.py` owns the untracked runtime bearer token,
  constant-time comparison, scoped transport capabilities, request nonces, expiry,
  revocation, command allowlists, and allowed-root restrictions.
- `src/dev_orchestrator/transport/` owns evidence-based LocalTransport/SSHTransport
  selection, capability discovery, fail-closed policy checks, and sanitized operation
  evidence. RDC is not a normal selectable transport.
- `src/dev_orchestrator/jobs/` owns host-local command resolution, parameter and
  environment policy, bounded output, timeout, concurrency, durable job identity,
  idempotency, polling, and cancellation.
- `src/dev_orchestrator/control/adapter.py` and `mcp_adapter.py` already provide the
  authenticated client boundary and a local stdio MCP server.

## Missing external-control layer

- A dedicated authenticated status read for external clients.
- MCP/client methods for the existing exec, spawn, poll, cancel, read, stat, staged
  write, and CAS write routes.
- Explicit opt-in for privileged MCP transport tools (the existing class option is
  not wired through the CLI).
- Per-request identity and durable audit evidence on the external operation surface.
- Windows start/stop/status wrappers, an end-to-end zero-RDC acceptance client, and
  operator/ChatGPT connection documentation.

## Planned files

- `src/dev_orchestrator/web/server.py`
- `src/dev_orchestrator/control/adapter.py`
- `src/dev_orchestrator/control/mcp_adapter.py`
- `src/dev_orchestrator/cli.py`
- `tests_py/test_p18_external_control.py`
- `ops/start_devo_control_api.ps1`
- `ops/stop_devo_control_api.ps1`
- `ops/status_devo_control_api.ps1`
- `ops/p18_external_control_acceptance.py`
- P18 external-control documentation, acceptance evidence, and agent handoff files.

## Control-plane impact classification

This work does **not** modify lifecycle authority, transition execution, successor
resolution, owner gates, launch fences, or watchdog recovery. The external surface
reads their existing authoritative projections and delegates machine operations to
the existing transport/job services. It introduces no lifecycle ledger mutation
route and no independent lifecycle controller, so the protected control-plane
declaration contract is not triggered.
