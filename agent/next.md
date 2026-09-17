# P12.6 Persistent Harness Acceptance and Closure

Status: **PENDING DESIGN**

Design authorization: **P12.5 completion handoff / 2026-09-16**

Implementation authorization: **OWNER START REQUIRED**

Goal: close the existing P12.6 AIBroker persistent-harness capability through
bounded verification and operational evidence. This task verifies the
already-implemented persistent service transport; it does not redesign or
rewrite the harness, provider routing, lifecycle authority, or worktree lease
model.

Scope:

- Verify the documented `runtime/aibroker-execution.json` persistent-service
  path (`service_url` and `service_token`) for normal role execute, exact status
  and exact managed interrupt transport, while preserving the CLI compatibility
  path when that configuration is absent.
- Verify the ownership boundary: AIBroker owns session/resource allocation and
  managed-worktree lease evidence; DevOrchestrator alone owns task/stage/review/
  remediation transitions, owner gates and stop intent.
- Collect deterministic automated and loopback operational evidence for
  persistent transport, pause barrier behavior, restart/recovery projection and
  capability-qualified interruption. Do not require a live provider mutation
  when synthetic or loopback evidence proves the contract.
- Close documentation and acceptance evidence only for verified behavior and
  record any unavailable external prerequisite as an OWNER_GATE or explicit
  closure blocker.

Non-goals:

- No replacement of the existing persistent harness or AIBroker transport.
- No new lifecycle controller, scheduler, provider/account/model selection,
  automatic provider failover, or second control authority.
- No expansion to non-loopback deployment, credentials changes, or unrelated
  P12 control-surface implementation.

Acceptance:

1. Existing persistent-service and CLI compatibility tests pass without
   changing their ownership contracts.
2. Evidence proves one Broker dispatch remains one resource execution and that
   DevOrchestrator does not interpret Broker results as lifecycle transitions.
3. Pause, restart and interrupt verification demonstrate the documented
   capability limits and fail closed when exact evidence is unavailable.
4. Full required regression/compile/syntax/diff checks pass, or a bounded
   blocker states the exact unavailable prerequisite.
