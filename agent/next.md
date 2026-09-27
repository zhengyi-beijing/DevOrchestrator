# P18 — Native Execution Transport & RDC Dependency Reduction

Status: STAGED / NOT STARTED
Predecessor: P17
Successor: TBD

## Objective

Build a first-class DevOrchestrator execution transport layer so routine machine operations no longer depend on Remote Desktop Commander (RDC). RDC becomes an optional bootstrap / GUI / emergency fallback rather than the normal control path.

## Target abstraction

Define an ExecutionTransport-style contract for at least:
- exec
- spawn
- poll/status
- cancel
- read_file
- write_file
- stat / capability discovery

Provide implementations or adapters for:
- LocalTransport
- SSHTransport
- RDCTransport (fallback only)

Prefer DevO MCP / Control API -> ExecutionTransport -> Local/SSH for routine automation.

## Scope

1. Inventory current RDC-dependent operations and classify which genuinely require GUI/desktop interaction.
2. Recover and reconcile prior ExecutionTransport/SSH design work rather than re-inventing it.
3. Implement capability discovery and host profiles so transport selection is evidence-based.
4. Route routine git, process, log, file, test, build, Codex/Claude invocation, and DevO status operations through Local/SSH transports where possible.
5. Preserve RDC for bootstrap, GUI-only workflows, emergency recovery, and unsupported hosts.
6. Add transport failover with explicit safety and identity fences; do not silently move unsafe hardware actions between transports.
7. Add observability: selected transport, fallback reason, command identity, host, duration, exit status, and failure fingerprint.
8. Add regression tests for transport selection, SSH loss/recovery, duplicate/replay behavior, timeout/cancel, and fallback.
9. Validate Windows and Linux paths relevant to the current DevO environment.
10. Document deployment and credentials/host-key handling without embedding secrets.

## Acceptance

- Routine DevO self-development/status/log/test/git operations on ZXZ-PC can execute without RDC.
- Supported remote hosts can use SSHTransport for non-GUI operations.
- RDC usage is measurably reduced and is not the default for routine command/file operations.
- Transport failures do not corrupt lifecycle ownership or cause duplicate execution.
- Existing safety policy remains intact, including explicit human authorization for real X-ray source/conveyor actions.
- Stable/runtime and development-workspace separation established in P17 is preserved.
- P18 starts only through the normal P17 successor handoff; do not manually launch it.