# P14 Remote Execution Resilience & Recoverable Jobs

Status: **PENDING DESIGN**

Goal: make long-running local/SSH/remote execution durable and recoverable across client, transport or tool-result interruption without duplicate side effects.

Scope:
- Use durable asynchronous jobs with stable job_id/request_id and explicit queued/running/completed/failed/cancelled/unknown-recovery states.
- Persist execution identity, project/task/stage correlation, process evidence, timestamps, bounded logs, heartbeat/progress and terminal result.
- Make retries idempotent and reconcile ambiguous timeouts before creating new work.
- Separate execution from message delivery; client disconnect must not terminate a healthy job.
- Add bounded logs, long-operation classification, cancel/retry/reconcile, daemon-restart recovery and watchdog/accounting integration.
- Preserve DevO lifecycle authority; ExecutionTransport owns execution evidence only.

Acceptance:
- Interrupt a representative long build/test, recover the same job_id/log/result later, and prove retries create no duplicate side effects.
- Recovery survives daemon/client/transport interruption within the documented bounds.

Design note: detailed executable design must be produced and independently reviewed before implementation.
