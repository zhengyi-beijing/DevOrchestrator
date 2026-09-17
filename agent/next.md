# P12.6 Persistent Harness Acceptance and Closure

Status: **READY_TO_RUN**

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

## Approved executable design

Close the existing AIBroker persistent-harness integration through provider-free loopback verification, narrowly harden any fail-open transport or interruption evidence paths found by that verification, preserve the CLI fallback and sole DevOrchestrator lifecycle authority, and publish reproducible acceptance evidence without redesigning either system.

### Implementation steps
- Add a dedicated P12.6 acceptance suite using an ephemeral loopback HTTP Broker fixture and the real runtime configuration loader and AIBrokerExecutionPort; exercise authenticated dispatch, status, and interrupt requests without invoking a live provider.
- Tighten the existing persistent-service adapter as needed to enforce the documented boundary: parse and require loopback HTTP service URLs, reject credentials and unsafe redirects, keep service tokens out of diagnostics, encode exact request identifiers, and preserve the subprocess CLI path whenever service_url is absent.
- Verify dispatch payload and result correlation end to end, including project, role, request/task identity, managed_worktree intent, timeout, prior-resource evidence, dispatch/decision/execution/session identifiers, and resource context; assert one DevOrchestrator execute call produces exactly one Broker dispatch and one execution-ledger record.
- Exercise TransitionExecutor with lifecycle-looking Broker output and prove the result is recorded only as execution evidence: AIBroker must not advance tasks, apply review decisions, create remediation, open or approve owner gates, or otherwise mutate DevOrchestrator lifecycle state.
- Add pause and stop acceptance coverage proving pause blocks every later launch before AIBrokerExecutionPort.execute, does not claim to cancel an already active harness, and leaves the durable pause barrier in place when interruption is unsupported, unavailable, malformed, or not correlated to the exact active request.
- Require an accepted interrupt outcome to carry exact Broker evidence for the targeted request and interruption result; mismatched, missing, ambiguous, or failed evidence must not be reported as pause_and_interrupt, while unrelated projects and executions remain untouched.
- Exercise restart reconciliation through the persistent status route: succeeded work projects to completed, explicit provider failure remains failed, running/unknown/unavailable evidence becomes recovery_required without replay, and only the documented managed-stop reason plus unchanged repository identity can qualify an explicit owner retry.
- Update the AIBroker integration contract and a focused P12.6 acceptance record with the tested configuration, ownership boundary, capability limits, commands, results, and any external prerequisite that could not be established; materialize task closure files only after all required evidence passes, otherwise retain P12.6 as blocked behind an explicit OWNER_GATE.
- Refresh the Graphify knowledge graph after implementation so the new acceptance, transport, lifecycle, and documentation relationships are indexed.

### Interfaces / contracts
- Machine-local runtime/aibroker-execution.json retains python_executable, broker_repo, config_path, optional database_path and timeout/probe settings; service_url plus service_token selects persistent transport, while absence of service_url selects the existing CLI compatibility transport.
- AIExecutionPort remains the provider-neutral boundary with execute(AIRoleRequest), status(request_id), and interrupt(request_id, reason); no provider, account, model, session-reuse, or worktree-lease policy moves into DevOrchestrator.
- Persistent transport uses the existing AIBroker routes POST /api/dispatch, GET /api/dispatches/{exact_request_id}, and POST /api/dispatches/{exact_request_id}/interrupt with X-AIResourceBroker-Token authentication and bounded timeouts.
- AIRoleResult and the transition-executor ledger retain one set of dispatch_id, decision_id, execution_id, session_id, resource_context, and status facts per semantic role request; these are evidence and never lifecycle commands.
- OwnerControlStore and ControlCommandCoordinator remain the sole pause/stop-intent path, while TransitionExecutor and the existing planner/reviewer coordinators remain the sole owners of task, stage, review, remediation, recovery, and owner-gate transitions.
- The acceptance deliverable is deterministic fixture-backed test output plus a checked-in closure record; it introduces no production scheduler, controller, provider mutation endpoint, or second source of lifecycle truth.

### Validation plan
- Run the focused persistent transport and integration suites, including tests_py/test_aibroker_execution_port.py, tests_py/test_transition_executor_aibroker.py, tests_py/test_p12_control_actions.py, and the new P12.6 loopback acceptance tests.
- Verify the loopback fixture observed the exact HTTP methods, paths, token header, payload correlations, one-dispatch count, status lookup, interrupt target and reason, and that persistent requests never spawned the CLI subprocess.
- Verify CLI compatibility separately with service_url absent, including dispatch, status, interrupt, timeout handling, correlation failures, malformed responses, and not-found behavior.
- Verify pause-before-launch, pause-during-active-execution, exact stop, unsupported or ambiguous interruption, cross-project isolation, restart projection, unchanged-repository managed interruption, and no-automatic-replay cases.
- Run python -m pytest tests_py -q and python -m compileall -q src ops tests_py.
- Run node --check web/app.js and node --check browser/chatgpt-web-adapter.user.js, then run git diff --check and inspect the final diff for unintended lifecycle, provider-routing, credential, runtime-secret, or generated-output changes.
- Run graphify update . and confirm it succeeds; if Graphify or an external AIBroker contract prerequisite is unavailable, record the exact unavailable prerequisite as the bounded closure blocker rather than claiming acceptance.
- Execute validation commands as separate PowerShell statements with explicit exit-code checks; do not use && or || in Windows PowerShell 5.1, preserving VERIFIED_FAILURE_MEMORY provenance seed:p11b:rdc-powershell-5.1.

### Risks / failure modes
- A permissive URL or redirect implementation could leak the service token or escape the loopback boundary; parsed endpoint validation, redirect rejection, and redacted diagnostics must fail closed.
- Synthetic fixtures can drift from the installed AIBroker response contract; fixture fields and capability assertions must be tied to documented Broker responses, and unverifiable external behavior must remain an OWNER_GATE rather than inferred evidence.
- Asynchronous pause, stop, and restart tests can become timing-dependent; use synchronization barriers and persisted-state assertions instead of sleeps or provider timing.
- Treating the presence of an AIBroker engine or interrupt method as proof of successful interruption could overstate safety; acceptance requires exact correlated Broker evidence and retains pause on failure.
- Over-tightening persistent configuration could accidentally break the established CLI fallback; explicit configuration-loading and fallback regressions are required.
- Implementation remains unauthorized until the owner supplies the required P12.6 start authorization; planning and read-only inspection do not satisfy that gate.

### Out of scope
- Replacing or redesigning the persistent AIBroker harness, its service API, native provider processes, session allocation, worktree lease ownership, or cleanup model.
- Adding provider, account, model, quota, failover, scheduling, or automatic retry policy to DevOrchestrator.
- Adding a second lifecycle controller or allowing Broker output to drive task, stage, review, remediation, owner-gate, pause, or stop transitions.
- Non-loopback deployment, TLS or remote-access design, credential rotation, live provider mutation, or changes to machine-local provider configuration.
- Unrelated P12 control-surface features, dashboard redesign, broader remediation changes, hardware-project actions, or resuming any paused external project.

### Independent plan review
- Approved: APPROVE. The plan is execution-ready and matches the bounded P12.6 task: it verifies the already-existing persistent AIBroker transport rather than redesigning lifecycle authority, provider routing, session allocation, or worktree leasing. Repository inspection confirms the cited production interfaces exist as described: runtime/aibroker-execution.json selects persistent transport when service_url is present and retains CLI compatibility otherwise; AIBrokerExecutionPort implements POST /api/dispatch, GET /api/dispatches/{request_id}, POST /api/dispatches/{request_id}/interrupt, execute/status/interrupt, token authentication, managed_worktree metadata, timeout propagation, prior resource context and result correlation. The existing integration contract already defines AIBroker ownership of provider/session/worktree resources, DevOrchestrator ownership of lifecycle and pause/stop intent, one-dispatch/at-most-one-resource semantics, restart reconciliation, and exact managed-interrupt requirements. The proposed loopback fixture, correlation assertions, pause/stop/restart cases, CLI fallback coverage and full regression provide concrete verification paths for every acceptance criterion. The narrowly stated hardening is justified by current implementation evidence rather than scope inflation: service URL validation currently uses a permissive string-prefix check and urllib's default redirect behavior, and ControlCommandCoordinator._stop currently reports pause_and_interrupt for any non-exception interrupt return without validating exact correlated interruption evidence. Those are bounded fail-closed corrections within the documented machine-local persistent-harness contract if acceptance tests expose them. NON_BLOCKING: the implementation should derive the exact accepted interrupt response fields from the installed AIBroker contract/fixture rather than inventing a new response schema, and preserve the current behavior that service_url is the transport selector even if service_token validation remains optional unless the actual Broker service contract requires it. Graphify availability should not by itself create an OWNER_GATE unless it is genuinely required for closure evidence; record it as a tooling blocker according to the task's bounded-closure policy. The explicit OWNER START REQUIRED remains a separate implementation authorization gate and does not make this plan unexecutable or require plan-review rejection.
