# P12.6 Persistent Harness Acceptance Record

**Date**: 2026-09-17
**Task**: P12.6 Persistent Harness Acceptance and Closure
**Status**: **ACCEPTED / READY FOR TECHNICAL REVIEW**
**Handoff**: P12.6 acceptance complete

---

## 1. Scope & Ownership Boundary

This document records operational acceptance evidence for the DevOrchestrator ↔ AIBroker persistent service transport and verifies the established architectural boundary:

- **AIBroker** owns session allocation/reuse, provider/account/model selection, native harness execution processes, and worktree leases.
- **DevOrchestrator** alone owns task/stage/role lifecycle, planning gates, review decisions, remediation, owner gates, pause barrier enforcement, and stop intent.
- One DevOrchestrator execution call produces exactly one Broker dispatch and one execution-ledger record. AIBroker output is recorded strictly as execution evidence and never drives lifecycle transitions.

---

## 2. Tested Configuration & Transport Hardening

Persistent transport is activated when `runtime/aibroker-execution.json` contains a loopback `service_url` and optional `service_token`:

```json
{
  "python_executable": "C:\\path\\to\\python.exe",
  "broker_repo": "C:\\path\\to\\AIResourceBroker",
  "config_path": "C:\\path\\to\\resources.yaml",
  "process_timeout_seconds": 600.0,
  "probe_before_dispatch": true,
  "service_url": "http://127.0.0.1:8875",
  "service_token": "..."
}
```

### Hardening verified:
1. **Loopback-Only HTTP Enforcement**: Rejects non-loopback hosts (e.g. `192.168.1.10`, `example.com`), non-HTTP schemes (e.g. `https`, `file`), and query or fragment parameters.
2. **Credential Rejection**: URLs containing embedded userinfo (`http://user:pass@127.0.0.1`) are rejected fail-closed.
3. **HTTP Redirect Rejection**: Opener uses `_NoRedirectHandler` rejecting 301, 302, 303, 307, 308 redirects without following them.
4. **Credential / Token Redaction**: `service_token` is transmitted via `X-AIResourceBroker-Token` and explicitly redacted from diagnostics, URLError strings, and exception logs.
5. **Exact Request Identifier Quoting**: Request IDs in `/api/dispatches/{request_id}` and `/api/dispatches/{request_id}/interrupt` are URL-quoted with `safe=""` to safely handle slashes and special characters.
6. **CLI Compatibility Fallback**: Absence of `service_url` (or `None`) strictly preserves the subprocess CLI transport without regression.

---

## 3. Verified Capabilities & Limits

| Capability / Barrier | Tested Behavior | Acceptance Outcome |
|---|---|---|
| **Dispatch & Correlation** | End-to-end dispatch passes project, role, prompt, request/task identity, `managed_worktree`, timeout, and previous resource context; maps all result identifiers. Exactly 1 dispatch per execute. | **PASS** |
| **Lifecycle Neutrality** | TransitionExecutor records Broker output strictly as execution evidence; output containing lifecycle directives (`NEXT`, `REMEDIATE`, `OWNER_GATE`) cannot mutate DevOrchestrator state or advance tasks. | **PASS** |
| **Pause Barrier (Pre-launch)** | Pause in `OwnerControlStore` blocks subsequent launches before `port.execute`, recording `state="blocked"` in the ledger and producing zero Broker HTTP calls. | **PASS** |
| **Pause Barrier (Active)** | Pausing during an active harness run does not cancel or interrupt the running harness; pause blocks future launches only. | **PASS** |
| **Stop Interruption (Confirmed)** | Stop command with confirmed correlated interrupt evidence (`status` in `interrupted`, `failed`, `cancelled` and `interrupt_supported is True`) returns `state="accepted"`, `effect="pause_and_interrupt"`. | **PASS** |
| **Stop Interruption (Fail-Closed)** | Stop with unsupported (`interrupt_supported=False`), unconfirmed (`status="running"`), missing capability evidence (CLI fallback or missing `interrupt_supported`), mismatched request ID, 404 target not found, or server error returns `state="failed"`, retains durable pause (`effect="pause_future_launches"`), and does not claim `pause_and_interrupt`. | **PASS** |
| **Cross-Project Isolation** | Stopping project A interrupts only project A; project B's active executions and unpaused state remain completely untouched. | **PASS** |
| **Restart Reconciliation** | Succeeded dispatches project to `completed`; failed dispatches project to `failed`; running/unknown/404 dispatches project to `recovery_required` (no automatic replay); managed interrupt with unchanged repo truth qualifies for safe owner retry (`recovery_safe_retry=True`); managed interrupt with changed repo truth remains `recovery_safe_retry=False`. | **PASS** |
| **CLI Compatibility Fallback** | When `service_url` is absent, dispatch, status, and interrupt use subprocess execution without invoking the network service. | **PASS** |

---

## 4. Automated Verification Commands & Results

All acceptance checks run deterministically against an ephemeral loopback HTTP server fixture without external dependencies or live model providers.

### Focused persistent harness suites:
```powershell
python -m pytest tests_py/test_aibroker_execution_port.py tests_py/test_transition_executor_aibroker.py tests_py/test_p12_control_actions.py tests_py/test_p12_6_persistent_harness_acceptance.py -q
```
**Result**: `64 passed, 8 subtests passed in 33.02s` (100% PASS).

### Syntax & compilation:
```powershell
python -m compileall -q src ops tests_py
node --check web/app.js
node --check browser/chatgpt-web-adapter.user.js
git diff --check
```
**Result**: Clean; 0 errors.

---

## 5. Closure Status

P12.6 acceptance criteria are fully satisfied by automated test evidence, verified transport hardening, and documentation updates. No external prerequisites remain unverified.
