# P13 Transport-Independent Control Bridge Contract

## Purpose

This document defines the authoritative architecture, trust model, and verification contract for P13 Transport-Independent Control Bridge. P13 removes RDC as the normal ChatGPT-to-DevOrchestrator control dependency while preserving DevOrchestrator and its daemon-owned `ControlCommandCoordinator` as the sole lifecycle authority.

## 1. Authority and Trust Model

1. **Sole Lifecycle Authority**: DevOrchestrator daemon coordinators (`ControlCommandCoordinator`, `TransitionExecutor`, `AIPlannerCoordinator`, `AIReviewerCoordinator`, `WatchdogCoordinator`) remain the exclusive authorities for task transitions, stage completions, review decisions, owner gates, pause, resume, and retry.
2. **Adapters Are API Clients**: Control Adapters (such as `MCPAdapter` and `WebBridgeAdapter`) are untrusted edge translators. They interact with DevOrchestrator strictly via authenticated loopback HTTP calls against the P12 Control API (`/api/v1/control/*`). Adapters never mutate runtime stores directly and never invoke lifecycle coordinators or schedulers.
3. **ExecutionTransport Carries Execution Only**: The `ExecutionTransport` boundary beneath `AIBrokerExecutionPort` (implementing `AIExecutionPort`) is solely responsible for dispatching semantic role execution, polling status, and issuing managed interrupts. It does not parse or interpret model output into lifecycle commands, does not transition tasks, and does not alter project state.
4. **Authoritative Local Repository Truth**: Regardless of whether execution occurs locally or across a remote transport (such as SSH over Tailscale), execution effects must be visible to the authoritative local worktree. DevOrchestrator's local repository-truth guards reject mismatched branch, HEAD, dirty state, task, or revision evidence fail-closed.
5. **No Automatic RDC Fallback**: RDC is strictly an emergency operator/GUI fallback. Ambiguous transport failures (e.g., SSH disconnects, timeouts, host mismatches) are recorded as unavailable or unknown/recovery-required; they never automatically fall back to RDC and never trigger duplicate dispatch or replay.

## 2. Shared ControlAdapter Boundary

The shared `ControlAdapterClient` exposes a bounded semantic client interface above the P12 HTTP surface:
- `status(project_id=None)`: Retrieves authoritative project or overview state.
- `logs(project_id=None, cursor=None, limit=50)`: Retrieves bounded, paginated log records with secret redaction and source availability.
- `submit_control(adapter_request_id, project_id, action, expected_revision, target=None)`:
  - Fetches the current projected project identity from the daemon.
  - Verifies that `expected_revision` matches the current projected revision; fails closed on revision mismatch.
  - Derives the complete `EXPECTED_IDENTITY_FIELDS` from the projection.
  - Enqueues the command with a deterministic command ID derived from `adapter_request_id` via `POST /api/v1/control/commands`.
- `command_status(command_id)`: Retrieves the audited status and outcome of an enqueued or settled command.

Every operation preserves versioned envelopes, source availability, observation timestamps, and correlation IDs.

## 3. Bounded Control Logs (`/api/v1/control/logs`)

The daemon exposes a read-only paginated logs route backed by durable evidence files (`events.jsonl`, `runs.jsonl`, `audit.jsonl`, and optional accounting `events.jsonl`):
- **Filtering**: Optional `project_id` filter limits output to records associated with the specified project.
- **Pagination**: Uses an opaque cursor with bounded limits ($1 \le \text{limit} \le 100$, default 50).
- **Redaction**: Replaces credentials, tokens, bearer headers, passwords, and private keys with `"[REDACTED]"`.
- **Source Availability**: Reports the availability of each underlying log source (`available`, `unavailable`, `degraded`) without fabricating values.
- **Fail-Closed**: Torn or unreadable source entries are logged as warnings and do not crash the response envelope.

## 4. MCPAdapter MVP

`MCPAdapter` is a local stdio JSON-RPC 2.0 process adhering to the Model Context Protocol:
- **Transport**: Standard input/output line-delimited JSON-RPC 2.0.
- **Tools**: Exposes exactly four closed, semantic tools:
  - `devorch_status`: Inspect overview or project status.
  - `devorch_logs`: Query paginated logs with project filter and cursor.
  - `devorch_control`: Submit guarded lifecycle control (`continue`, `pause`, `resume`, `stop`, `retry`, `rereview`, `reconcile`, `approve_owner_gate`, `bind_conversation`, `unbind_conversation`, `rebind_conversation`).
  - `devorch_command_status`: Inspect lifecycle command status.
- **Closed Schemas**: Tool arguments are strictly typed and reject unknown properties. The adapter provides no facility for arbitrary shell execution, prompt execution, raw HTTP requests, filesystem access, or patch application.
- **Bounded Output**: Output responses are clamped to prevent unbounded memory or buffer exhaustion.

## 5. WebBridgeAdapter and Extended Control Security

`WebBridgeAdapter` provides a secondary transport for ChatGPT web sessions via the daemon's port 8770 surface:
- **Port Isolation**: Port 8765 (`BrowserBridgeServer`) remains strictly transport-only for notifications/toasts. Port 8765 exposes NO lifecycle or control endpoints. All control ingress resides on port 8770.
- **WebBridge Control Capability**:
  - Distinct from the heartbeat-only capability (`session_heartbeat`).
  - Owner-created, hash-only (`web_bridge_control`), revocable, and bound to an exact `project_id` and live `conversation_binding`.
  - Rejects requests if capability is expired, revoked, unbound, or if the conversation session is dead or stale.
- **Durable Request Store**:
  - Keyed by `adapter_request_id` and canonical request hash.
  - Identical requests return recorded results (exact replay idempotency).
  - Reused `adapter_request_id` with differing body raises `409 Conflict`.
  - Corrupt or incomplete records are quarantined to `control/quarantine/` and published as degraded control health.
- **Freshness Window**: Requests must include an `issued_at` UTC timestamp within a 5-minute freshness window; stale or future timestamps fail closed.

## 6. ExecutionTransport Boundary

`AIExecutionPort` delegates dispatch, status, and interrupt operations to an underlying `ExecutionTransport`:
- `LocalTransport` (default):
  - Uses local subprocess execution or loopback service calls, preserving existing AIBroker behavior.
  - Absent transport configuration defaults to `LocalTransport` for complete backward compatibility.
- `SSHTransport`:
  - Uses the system OpenSSH client (`ssh`) with noninteractive authentication (`BatchMode=yes`), strict host verification (`StrictHostKeyChecking=yes`), and explicit Tailscale peer configuration.
  - Invokes a fixed remote helper: `python -m dev_orchestrator.ai.remote_helper`.
  - Communicates solely via structured JSON over stdin and stdout.
  - Accepts only three operations: `dispatch`, `status`, `interrupt`. Arbitrary commands, shell fragments, environment injection, and unrestricted paths are prohibited.
  - Applies deterministic path mapping between local and remote repository roots.
  - Correlates returned `request_id` and validates remote host identity.
  - Failures (disconnect, timeout, exit code != 0) fail closed without replay and do not fall back to RDC.

## 7. RDC Fallback Boundaries

- RDC is excluded from automatic transport selection.
- Existing RDC normalization and evidence import (`import-rdc-evidence`) remain intact for retrospective analysis.
- Unattended failures in Local or SSH transport enter explicit `unavailable` or `unknown`/`recovery_required` states. Operators must take manual action to initiate any RDC session.
