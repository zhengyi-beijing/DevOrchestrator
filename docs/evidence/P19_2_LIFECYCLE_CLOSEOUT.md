# P19.2 Technical Review and authoritative lifecycle closeout

Date: 2026-10-03. Repository: `C:\work\github\DevOrchestrator-dev`.
Reviewed implementation HEAD: `4c40ce1dd9697c5d24acef87080620c77f390180`.
Verdicts: restart acceptance **PASS**; Technical Review **ACCEPT**.

## Scope and findings

Reviewed the staged specification, executable plan in `agent/next.md`, CURRENT,
result, MCP design, implementation, tests, existing acceptance artifacts, and
Git history. The executable plan bounds `devo_start_task` to allowlisted native
jobs through P18; it is not a second Planner/Worker or lifecycle authority.
P19.3's later read-only browser adapter does not widen this interface. Direct
MCP availability in a client product is not an endpoint implementation defect.

Blocking findings were reproduced with failing tests before implementation,
then repaired without widening scope:

- `ControlSecurity.create_mcp_capability` / `validate_mcp_token` and
  `MCPHttpEndpoint._dispatch_method`: empty scope formerly became wildcard,
  and malformed scopes could authorize tools. Empty now means deny-all;
  malformed stored or dispatch scopes fail closed.
- `LocalMachineTransport.read_file`: a repository containing runtime exposed
  owner credentials. Canonical control trees and runtime backups are denied,
  including alternate job runtimes, resolved aliases, and hardlinks to the
  canonical token/capability store. Ordinary contained file reads still work.
- `MCPHttpEndpoint.handle_post` / `_dispatch_method`, dashboard MCP DELETE:
  unsupported protocol headers, invalid RPC envelopes, stale notifications,
  and arguments violating published schemas are rejected before dispatch.
- `MCPHttpEndpoint._call_tool`: remote job polling/cancellation previously lost
  host identity and selected local. Both tools now accept and forward the
  starting host; start results retain `host_id`. A transport-boundary regression
  checks SSH identity and repeated cancellation without a corresponding local
  job. This is not a claim of live SSH-peer acceptance.

The security-fix workflow included independent read-only investigation and
patch review. The patch reviewer reproduced the hardlink case; it was repaired.
Windows could not create a symlink in this environment; the configuration-file
alias case was additionally checked by emulating resolution in read-only review.
Owner-made arbitrary credential copies outside protected roots remain an
operator storage constraint, documented in the design.

NON_BLOCKING: project-status raw repository readiness remains READY_TO_RUN and
its compatibility activity projection displays `next_action=execute`. The
authoritative `lifecycle_state` and activity stage are COMPLETE/complete,
orchestrator activity is terminal_closure, durable context next_action is none,
and continue/retry/rereview/reconcile controls are disabled by terminal state.
This display fallback does not launch work or bypass authority; no unrelated
projection redesign was performed.

## Restart acceptance

Canonical command throughout:
`dev-orchestrator start-daemon --config config/projects.json --runtime-root runtime --interval 5`.
Restarts used `dev-orchestrator stop-daemon --runtime-root runtime`.

Before repairs, daemon PID 12136 started at 13:30:44 UTC with several real ticks,
then restarted as PID 32020 and ticked through 13:32:39 UTC. Authority remained
P19.2 READY_TO_RUN, generation 10; executions 37, historical handoffs 17,
historical transitions 17. No active owner/execution, duplicate historical
handoff, predecessor launch or invariant failure appeared.

Patched daemon PID 13352 started at 13:55:56 UTC and ticked through 14:04:54 UTC
with the same lifecycle identities and counts. Live MCP acceptance ran
13:56:01–13:56:10 UTC: six tools, twelve operations, five native operations,
all selecting local, zero RDC calls. Runtime-only artifacts:
`runtime/p192-closeout-live-acceptance-20261003.json` and
`runtime/p192-closeout-live-operations-20261003.ndjson`.

## Authoritative settlement and terminal restart

With the daemon stopped and the reviewed implementation committed on clean
main, guards checked current task, generation, empty owner/gate/execution,
all six invariants, and `resolve_successor(..., 'P19.2')=end_of_roadmap`.
The existing `TransitionExecutor._record_settled` API recorded:

- Request: `technical-review:P19.2:closeout:20261003:4c40ce1dd969`.
- Recorded at: `2026-10-03T14:07:41.688182+00:00`.
- Outcome: task_complete; authority COMPLETE; generation still 10.
- Executions: 37 -> 38 (one intentional settled record only).
- Transitions and historical handoffs: unchanged at 17/17.

The existing `set_next_action` API set none/terminal_success. No ledger was
hand-edited, no review decision removed, no handoff fabricated, no successor
selected, and the existing owner pause/freeze was preserved.
A fresh TransitionExecutor replay left executions/transitions unchanged;
lifecycle recovery metadata may refresh and is not byte-identical by contract.

Terminal daemon PID 40412 started at 14:08:16 UTC and ticked through 14:08:48 UTC.
A fresh MCP session successfully polled job `job-b710484b3a4801f9`; replaying
`mcp-accept-comp-20261003T135601Z` returned that same completed job, creating no
new job. Cancelling `job-829e557a0cf6f3a9` twice succeeded. An actual request for
`runtime/control/api-token` was denied. Probe capabilities were revoked and
sessions deleted through their normal APIs; no tokens are included here.

Daemon restarted again as PID 42844 at `2026-10-03T14:10:05.679565+00:00`.
Observed at `2026-10-03T14:10:58.256981+00:00`, latest real tick
`2026-10-03T14:10:53.239802+00:00`:

- current_task_id P19.2; lifecycle_state COMPLETE; generation 10.
- owner_gate null; active_owner null; active_execution null; active_roles [].
- repository_projection task_id P19.2; matches_authority true.
- executions 38; historical handoffs 17; historical transitions 17.
- watchdog state ok; unresolved_invariants []; no last_error; continue disabled.
- Owner pause remains true; no automatic rerun or new recovery continuation.

The same PID later ticked through `2026-10-03T14:15:32.698965+00:00`, more than
five minutes after this terminal restart, with COMPLETE/generation 10,
38 executions, 17 transitions and no new daemon error.

All six findings returned holds=true using the actual executor, planner,
reviewer and review-decisions ledgers (not a snapshot missing decisions):

1. CURRENT_TASK_MATCHES_ACTIVE_EXECUTION — PASS
2. TERMINAL_TASK_HAS_NO_RUNNING_EXECUTION — PASS
3. PENDING_DESIGN_NOT_EXECUTING — PASS
4. SUCCESSOR_HANDOFF_LINEAGE_VALID — PASS
5. NEXT_TASK_WITHOUT_HANDOFF — PASS
6. SINGLE_ACTIVE_LIFECYCLE_OWNER — PASS

## Verification

- `python -m pytest tests_py -q --basetemp runtime/test-tmp/p192-full-final-20261003`:
  **1702 passed, 130 subtests passed, 0 failed**, 590.14 seconds.
- Required lifecycle/successor/invariant/MCP/control/security/daemon/projection
  subset before repairs: **276 passed, 12 subtests passed, 0 failed**.
- The same required subset after final repairs and terminal closeout:
  **282 passed, 33 subtests passed, 0 failed**, 111.15 seconds. Exact command:
  `python -m pytest tests_py/test_p1613_successor_consistency.py tests_py/test_p1614_invariant_workflow.py tests_py/test_p19_2_remote_mcp.py tests_py/test_p18_selector_and_security.py tests_py/test_p18_transports.py tests_py/test_p18_external_control.py tests_py/test_p14_durable_jobs.py tests_py/test_p13_mcp_adapter.py tests_py/test_p13_control_logs.py tests_py/test_p19_web_console.py tests_py/test_control_commands.py tests_py/test_daemon.py tests_py/test_daemon_transition_integration.py tests_py/test_lifecycle_projection.py tests_py/test_project_status.py -q --basetemp runtime/test-tmp/p192-required-final-20261003`.
- Final MCP/P18 transport subset: **46 passed, 21 subtests passed, 0 failed**.
- Final P12 foundation/MCP subset: **33 passed, 21 subtests passed, 0 failed**.
- `python ops/p19_2_remote_mcp_acceptance.py --runtime-root runtime --output runtime/p192-closeout-live-acceptance-20261003.json --operations-output runtime/p192-closeout-live-operations-20261003.ndjson`: PASS.
- `python -m compileall -q src tests_py ops`: PASS.
- `git diff --check`: PASS.
- `graphify update .`: PASS, with existing permission-denied directory warnings.

The first full run found one pre-existing P12 fixture failure: an unbound
heartbeat must not establish project authority. The failure was reproduced on
original HEAD c5ba8a7 in an isolated worktree. The fixture now proves the unbound
response has no project/health, then creates an explicit owner binding before
checking health. Production gates were not weakened. The temporary clean
baseline worktree was removed normally; existing .p193-* directories remain.

No push, reset, rebase, clean, force or origin change was performed.
No successor has been invented. Roadmap re-baseline requires owner decision.
