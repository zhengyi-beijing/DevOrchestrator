# DevOrchestrator Self-Hosted Development State

- Canonical worktree: C:\work\github\DevOrchestrator-dev.
- Canonical branch: main.
- Runtime mode: self-hosted canonical daemon on 8770 with AIBroker diagnostics on 8875.
- Historical detached worktree C:\work\github\DevOrchestrator is not an active controller.

Current task: **P14 Remote Execution Resilience & Recoverable Jobs** — **TECHNICAL REVIEW REMEDIATION COMPLETE / READY FOR TECHNICAL RE-REVIEW**.

Latest continuation state (2026-09-19):
- Completed Second-Round Technical Review Remediation for P14 Remote Execution Resilience & Recoverable Jobs:
  1. SSH Job Path Liveness & Remote Correlation (`service.py`, `transport.py`):
     - In `JobService.reconcile`, remote status polling now syncs remote `job` data (`supervisor` token/pid/started_at, `timestamps`, `state`, `host_identity`) into the local store.
     - Liveness for `transport != "local"` no longer checks local `is_pid_alive` against remote PIDs with null tokens; it evaluates remote `supervisor_alive` (surfaced in `LocalJobTransport.job_status`), remote `state == "running"`, and advancing locally observed `heartbeat_sequence`.
     - Active SSH jobs stay `running` and do not fall through to `never_started` or set `recovery_safe_retry = True`.
     - Stalled heartbeat sequences (> 15s / 3 intervals) transition cleanly to `unknown_recovery` with `failure_kind="heartbeat_stalled"` and `recovery_safe_retry=False`.
     - Positive evidence is strictly required for `never_started` (local: dead/missing pid + no start evidence; remote: confirmed unstarted remote record). If remote status polling fails with no prior start evidence, it transitions to `failed` with `failure_kind="transport_unreachable"` and `recovery_safe_retry=False` (never assuming safe retry).
     - In `_promote_terminal`, queued records transition `queued -> running -> completed` when producing exit code 0 to maintain legal `VALID_TRANSITIONS`.
  2. Concurrent `job.json` Synchronization & Store Locking (`supervisor.py`, `remote_helper.py`):
     - Eliminated raw uncoordinated `write_json(job_file, ...)` in `jobs/supervisor.py`; all mutations now route through `ExecutionJobStore.update(job_id, ...)` under `InterProcessFileLock(jobs.lock)` and `ExecutionJobStore.save_result`.
     - Concurrent `submit()` (`_record_pid`) and supervisor startup (`_start_sup`) no longer produce lost updates or reset state back to `queued`.
     - In `remote_helper.py`, replaced `if is_new or record.state == "queued":` with `if is_new:`, returning `already_exists: True` on subsequent calls and preventing duplicate supervisor spawns.
  3. Acceptance and Regression Test Coverage (`tests_py/test_p14_software_acceptance.py`, `tests_py/test_p14_job_transport.py`):
     - Added comprehensive SSH job submission, live reconcile, stalled heartbeat, mid-run supervisor crash, positive `never_started` check, unreachable transport fail-closed, and remote completion tests in `tests_py/test_p14_software_acceptance.py` (`test_ssh_job_submit_reconcile_and_liveness_recovery`).
     - Added `test_supervisor_and_submit_concurrent_lock_synchronization` testing concurrent supervisor start and `_record_pid` under lock without lost updates.
     - Added `test_remote_helper_job_start_idempotency_no_respawn` verifying idempotent `job_start` handling.
     - Fixed `test_alternate_config_roots_never_consulted` in `tests_py/test_p14_job_transport.py` to patch `os.environ.get` instead of mutating `os.environ` to avoid Windows 32k environment variable limits.
  4. Verification:
     - Focused P14 suite: 40 passed in 9.19s (`test_p14_durable_jobs.py`, `test_p14_job_retry_identity.py`, `test_p14_job_transport.py`, `test_p14_job_recovery.py`, `test_p14_software_acceptance.py`).
     - Adjacent regression: 71 passed, 6 subtests passed in 19.48s (`test_p13_execution_transport.py`, `test_p13_software_acceptance.py`, `test_p12_control_foundation.py`, `test_p12_control_actions.py`, `test_web.py`, `test_watchdog.py`, `test_cli.py`).
     - Full test suite: 787 passed, 45 subtests passed in 249.81s (`python -m pytest tests_py -q`).
     - Static checks: `python -m compileall -q src ops tests_py`, `node --check web/app.js`, `node --check browser/chatgpt-web-adapter.user.js`, and `git diff --check` passed cleanly with 0 defects.
     - Knowledge graph updated with `graphify update .` (3804 nodes, 10624 edges, 180 communities).
- Immediate continuation rule: conduct independent technical re-review on clean worktree.

Implementation summary:
- Added the opt-in `dev_orchestrator.accounting` package with a cross-thread/process serialized, fsynced JSONL event ledger; closed event/phase/role taxonomy; deterministic replay IDs; bounded corruption evidence; and explicit torn-tail quarantine/recovery.
- Added deterministic exclusive interval construction with clipping, right-censoring, overlap precedence, idle filling, explicit owner-gate correlation and no wall-clock double counting.
- Added accounting summaries for phase time, plan-review churn, retry wall time, owner wait, longest no-progress interval, rejected attempt time and EDR.
- EDR counts only AI execution and managed validation linked to an explicit technically accepted Worker/remediation attempt; rejected and unresolved attempts do not enter the numerator.
- Added structured, predicate-matched failure memory with canonical fingerprints, verification/provenance, capped prompt rendering, recurrence count/cost and fail-closed state loading.
- Seeded and injected the verified Windows PowerShell 5.1 `&&`/`||` lesson into matching Planner, Worker and Reviewer prompts when accounting is enabled.
- Instrumented Planner, plan review/remediation/retry, Broker/legacy Worker, legacy fallback retry, direct/browser technical review, browser queue and explicit owner-gate boundaries with observed correlation/resource identity.
- Added top-level `execution_accounting` opt-in and optional `project-continue --gate-id`; missing/disabled accounting preserves existing orchestration calls and prompts.
- Added the contract document `docs/EXECUTION_ACCOUNTING_CONTRACT.md` and dedicated concurrency, accounting, failure-memory, runtime and instrumentation test suites.

Verification status:
- Focused P11b and adjacent lifecycle regression suites pass.
- Full `python -m pytest tests_py -q`: 453 tests and 16 subtests passed.
- `python -m compileall -q src tests_py`, userscript syntax and `git diff --check` passed.
- Graphify AST update completed successfully: 2327 nodes, 6163 edges and 128 communities. Because the worktree had no tracked Graphify baseline, its newly generated cache/output was kept out of the P11b commit.

P11c result:
- AIBroker results now emit durable, idempotent provider evidence with exact
  request/dispatch/execution/resource/session correlation and optional explicit
  timing, quota, rate-limit, and first-output facts.
- Provider summaries report resource/provider/account/model/session continuity,
  unknown fields, resource switches, and evidence-qualified failover latency.
- Normalized RDC JSON/JSONL evidence can be imported into the accounting ledger;
  deterministic classifiers cover isolated concurrency, HOL blocking,
  starvation, session coupling, reconnect contamination, and no-output deadlock.
- Recovery target calculation is project-scoped and read-only.
- Focused P11c tests and the full Python regression suite pass.

P11d result:
- Added one deterministic P11 report across accounting, provider/context and RDC evidence, with explicit source provenance, unknown-data warnings and no inferred facts.
- Added project/task/role time breakdown, original-hypothesis comparison, ranked evidence-backed bottlenecks and seven machine-testable default acceptance gates.
- Added read-only `execution-report`, `/api/accounting`, and 8770 dashboard views for EDR, phase loss, provider/failover, RDC findings, hypotheses, gates and evidence IDs.
- Custom event-ledger paths are published by the enabled runtime; explicit CLI config reads remain side-effect free.
- Representative DevOrchestrator and deterministic DOM fixtures cover the report and rendered dashboard. Full regression passed with 487 tests and 16 subtests.
- Python compilation, both JavaScript syntax checks, `git diff --check`, and Graphify AST refresh (2512 nodes, 6675 edges, 131 communities) passed.

P12 design result:
- Froze one 8770 Control API architecture with the daemon and existing `ControlCommandCoordinator` as the sole lifecycle mutation authority.
- Split delivery into unified reads, authenticated/idempotent command transport, guarded actions/conversation consolidation, and operator UI/representative acceptance.
- Defined loopback-plus-secret/session security, CSRF/origin guards, exact state revisions, atomic replay/conflict behavior, crash recovery and always-on redacted audit evidence.
- Defined selective forward-porting from `feature/conversation-control-plane`; no wholesale branch merge and no second 8766 lifecycle-control authority.
- Design verification passed: staged-roadmap suite 22/22; full Python suite 487 tests plus 16 subtests; Python/JavaScript syntax, whitespace, and Graphify AST refresh passed.
- Owner authorized implementation on 2026-09-15. `xray-hw-platform` remains paused and unchanged.

P12 implementation result:
- Port 8770 now exposes stable `/api/v1/control/*` read envelopes and a one-call operator overview while preserving every legacy GET route; standalone web remains read-only.
- Unified-daemon POST uses loopback plus bearer or same-origin browser-session authorization, exact Host/Origin/CSRF checks, strict bounded JSON schemas, narrow ChatGPT CORS preflight, short-lived single-use pairing and revocable hash-only heartbeat capabilities.
- CLI lifecycle commands now use the same authenticated 8770 ingress, and the ChatGPT userscript redeems dashboard pairing codes into a heartbeat-only private capability; neither client bypasses daemon authority.
- Command submission is cross-process atomic and replay-safe, with canonical request hashes, conflict detection, complete expected identity on every mutation path, persist-before-ack semantics, terminal-audit-before-inbox-removal recovery and redacted append-only audit evidence. Corrupt/torn queue or audit records are preserved in quarantine and surfaced as degraded control health.
- Daemon-owned control implements safe continue, pause/resume, exact supported AIBroker stop, guarded runtime conversation bind/unbind/rebind, and exact Planner owner-gate approval. Approval requires a live bound conversation, inactive browser claim, clean unchanged repository and exact current gate/task identity; it never launches a Worker, and later progress still requires explicit `continue`. Retry and reconcile remain unavailable without exact safe adapters.
- Selective branch convergence retained no 8766 service and no second lifecycle authority. The old adjudicator was intentionally not restored: current plan remediation is strictly bounded and exhaustion durably enters `OWNER_GATE` fail closed.
- Durable owner pause is enforced at final Worker launch gates and suppresses later legacy static starts. Runtime conversation bindings override static migration fallback without disabling direct AIBroker projects.
- The dashboard renders server-advertised capabilities, active roles, resources/executions, bindings, P11 evidence and pending/settled command results, including confirmation for stop/owner-gate actions.
- Verification: 509 tests and 16 subtests passed; Python compilation, dashboard and browser-adapter JavaScript syntax, `git diff --check`, and Graphify refresh passed (2744 nodes, 7357 edges, 141 communities).

P12.5 result:
- Added `ops/self_host_acceptance.py` standard-library operational acceptance utility enforcing loopback-only HTTP endpoints and issuing GET requests only against `/api/v1/control/overview`, `/api/resources`, and `/api/executions`.
- Verifies daemon health/freshness, enabled/non-degraded control authority, exact project identity (`devorchestrator` on branch `main`), P11 execution accounting availability, and AIBroker resources/executions visibility through both the unified overview and direct 8875 endpoints.
- Preserves overview warnings as non-fatal diagnostics and returns deterministic JSON with categorized diagnostics excluding response bodies and secrets.
- Verified live deployment reports overall PASS against running daemon (PID 5712) and AIBroker on 8875.
- Documented canonical self-host deployment commands, expected exit behavior, and endpoint overrides in README.md.
- Added 16 focused tests in `tests_py/test_self_host_acceptance.py`.
- Full regression passed: 550 tests and 22 subtests. Python compilation, JavaScript syntax, and git diff check passed. Knowledge graph updated to 2865 nodes, 7785 edges, 140 communities.

P12.5 review remediation result:
- Closed stale broker evidence gap: unified broker resources and executions now fail closed on `availability="stale"` or `stale=True` (and corresponding sources availability), as well as direct broker endpoints.
- Closed HTTP redirect gap: requests now use `NoRedirectHandler` preventing redirection to non-allowlisted or remote targets, returning explicit HTTP redirect errors without following.
- Closed credentials/userinfo gap: `validate_loopback_url` explicitly rejects userinfo/credentials, URLs are sanitized for safe diagnostics, and credentials are never leaked in error messages or subprocess outputs.
- Added 5 new regression tests in `tests_py/test_self_host_acceptance.py` (21 focused tests total passing).
- Full regression passed: 555 passed, 22 subtests passed. Python compilation, JavaScript syntax, and `git diff --check` passed cleanly. Knowledge graph updated to 2879 nodes, 7820 edges, 145 communities.
- Live deployment check against running daemon (PID 5712) and AIBroker (8875) verified PASS.

P12.5 self-host recovery hotfix result:
- Owner `continue` and `resume` preserve exact review-driven remediation after a
  `WorktreeUnsafeError` broker failure with explicit `broker_status=failed`,
  allocation IDs/resource context, null provider session and no usable output:
  a new remediation identity uses the configured remediation prompt plus durable
  original reviewer evidence; the failed execution remains immutable history.
- Recovery is fail-closed unless the applied REMEDIATE decision, task,
  branch/HEAD, clean current worktree, no-active-run state and absence of all
  usable provider session/output/work evidence match exactly. Broker allocation
  IDs and resource context alone do not imply useful provider work. Generic
  failed Workers and ambiguous attempts remain non-retryable.
- Documented unattended continuation authority without adding a second
  lifecycle authority. P12.5 now hands off to the bounded P12.6 persistent
  harness acceptance/closure spec, which verifies existing capability only.
- Verification: focused remediation/control/staged suites passed (55 tests);
  full `python -m pytest tests_py -q` passed (560 tests and 22 subtests), as
  did `python -m compileall -q src ops tests_py` and `git diff --check`.

P12.5 recovery review remediation result:
- Recovery selection is now anchored to the failed remediation and its durable
  applied reviewer decision, so a reviewed P1 remediation remains P1 even
  after `agent/next.md` advertises P2.
- A differing clean fingerprint is accepted only for a closed generated-only
  proof (`?? graphify-out/`), including the independently verified legacy
  fingerprint; tracked-source cleanup/revert and ambiguous historical rows
  fail closed.
- Recovery lineage, reviewed fingerprint evidence and explicit no-output
  fields are persisted in the new launch ledger row before the Broker thread
  starts. Verification: 58 focused tests and 563 tests plus 22 subtests in the
  full suite passed; compileall, both JavaScript syntax checks and diff check
  passed.

P12.5 recovery consumption remediation result:
- A failed remediation is durably consumed by a retry row's `recovery_of`
  lineage without mutating historical rows. Continue/resume cannot replay it
  while that retry is active or awaiting its normal technical-review
  transition; after the reviewed transition, ordinary P2 control is unblocked.
- Contradictory positive provider evidence (raw output, provider-work flag,
  output flag, first-output timestamp or session) overrides stale negative
  evidence and fails recovery closed.
- Verification: focused transition/control/remediation suites passed (39
  tests); full suite passed (566 tests and 22 subtests in 151.23s).

P12.5 post-reanchor closure remediation result (2026-09-17):
- Acceptance parsing now fails closed on malformed/null overview collections and incorrectly typed nested project identity objects; IPv6 loopback normalization preserves brackets.
- Reconcile replay is restart-idempotent after a durably persisted reviewer launch, avoiding false blocked settlement after a crash boundary.
- Exact current-HEAD re-anchored REMEDIATE work blocked only by transient lifecycle/active-worker state can be recovered by a new explicit owner `continue` without mutating historical evidence.
- Recovery precedence preserves the established descendant-recovery barrier when no exact re-anchor candidate exists.
- Focused regression: 54 tests and 9 subtests passed.
- Full regression: 583 tests and 31 subtests passed; compileall and `git diff --check` passed.
- Live self-host acceptance passed against daemon PID 29212 and AIBroker 8875.
- Current implementation is ready for independent technical re-review before P12.5 closure.

P12.5 independent closure review (2026-09-17):
- Independent reviewer: `copilot/default/claude-sonnet-4.6`; exact-run execution `4c8f55f2-bfd6-470e-bf81-bdeb537a2c68`; Copilot session `6f61368b-f089-43e3-8986-6fc2167da180`. The reviewer execution completed with exit code 0 and no file modifications.
- Verdict: `NEXT`; blocking findings: none. The reviewer accepted all five post-reanchor closure criteria, recovery lineage/consumption, fail-closed guards, command idempotency, and barrier precedence at HEAD `05129fc8c5ae90d19e3b3e20c2ffe1b757a83fce`.
- Non-blocking notes only: cosmetic blocked-reason precedence when two barriers coincide; harmless `None` member in `consumed_sources`; theoretical reconcile replay equality if both task IDs are absent, constrained away by valid reconcile target requirements.
- P12.5 is CLOSED. Active handoff is P12.6, which remains `PENDING DESIGN` and `OWNER START REQUIRED`.

P12.6 persistent harness acceptance result (2026-09-17):
- Implemented loopback-only HTTP URL validation in `AIBrokerExecutionPort._service_call`, rejecting non-HTTP schemes, non-loopback hosts, query/fragment parameters, and credentials.
- Blocked HTTP redirects via `_NoRedirectHandler` and redacted service tokens from diagnostics and exception logs.
- Quoted exact request identifiers safely in status and interrupt routes; 404 responses return `None`.
- Hardened `ControlCommandCoordinator._stop` to require exact correlated Broker evidence (`status` in `interrupted`, `failed`, `cancelled` and `interrupt_supported is not False`); unsupported, unconfirmed, missing, or mismatched evidence fails closed with retained durable pause (`effect="pause_future_launches"`, `state="failed"`).
- Added dedicated acceptance suite in `tests_py/test_p12_6_persistent_harness_acceptance.py` using an ephemeral loopback HTTP server fixture covering authenticated dispatch, correlation, lifecycle neutrality, pre-launch pause barrier, active-run pause, exact stop interrupt, fail-closed stop, cross-project isolation, restart reconciliation projections, and CLI fallback.
- Updated `docs/AIBROKER_INTEGRATION_CONTRACT.md` and authored `docs/P12_6_PERSISTENT_HARNESS_ACCEPTANCE.md`.
- Focused persistent harness regression: 64 passed, 8 subtests passed.
- Full regression: 601 passed, 33 subtests passed in 198.18s. Python compilation, JavaScript syntax checks, and `git diff --check` passed cleanly.
- Knowledge graph refreshed via `graphify update .`: 3046 nodes, 8390 edges, 152 communities.

P12.6 review remediation result (2026-09-17):
- Hardened `ControlCommandCoordinator._stop` to require positive capability-qualified interrupt evidence (`interrupt_supported is True` along with exact request correlation and status in `{"interrupted", "failed", "cancelled"}`); missing or False `interrupt_supported` fails closed, retaining durable pause (`effect="pause_future_launches"`, `state="failed"`).
- Sealed the CLI fallback stop gap: AIBroker CLI `interrupt-dispatch` reports `status="failed"` without persistent harness proof; it now fails closed with retained pause instead of incorrectly reporting `pause_and_interrupt`.
- Corrected trailing whitespace in `docs/P12_6_PERSISTENT_HARNESS_ACCEPTANCE.md` lines 3-6 so `git diff --check` passes with zero whitespace defects.
- Updated `FakeInterruptPort` in `tests_py/test_p12_control_actions.py` to include `interrupt_supported: True`, and added regression test cases covering missing `interrupt_supported`, CLI fallback result shape, and `interrupt_supported: True` with unconfirmed status.
- Added Case 4 to `test_stop_fails_closed_when_interrupt_evidence_is_unsupported_or_unconfirmed` and added dedicated `test_stop_cli_fallback_retains_pause_and_fails_closed_without_persistent_capability` in `tests_py/test_p12_6_persistent_harness_acceptance.py`.
- Focused persistent harness regression: 65 passed, 8 subtests passed.
- Full regression: 602 passed, 33 subtests passed in 196.61s.
- `python -m compileall -q src ops tests_py`, node syntax checks on `web/app.js` and `browser/chatgpt-web-adapter.user.js`, and `git diff --check` passed cleanly.
- Updated `docs/P12_6_PERSISTENT_HARNESS_ACCEPTANCE.md` focused test results (65 passed, 8 subtests passed) and added full regression evidence (602 passed, 33 subtests passed).
- Verified canonical worktree is clean without untracked `graphify-out/` to ensure clean lifecycle transition.

P12.6 closure review remediation result (2026-09-17):
- Remediated Technical Review finding from `ai_review:closure:p126:final2:36eb632d1b7f`:
  - Successor status contract: `agent/staged/P12.7.md` status corrected to `Status: **PENDING DESIGN**` (from `READY_TO_RUN`), satisfying `read_successor` contract and preventing terminal handoff blocks.
  - Successor roadmap link regression: `tests_py/test_staged_roadmap.py` now asserts `read_successor(checkout_root, "P12.6")` returns successor `P12.7`, spec path `agent/staged/P12.7.md`, and `Status: **PENDING DESIGN**`; `test_real_repo_p126_to_p127_staged_contract` verifies `READY_TO_RUN` and approved design markers are absent.
  - End-to-end handoff lifecycle regression: `tests_py/test_staged_handoff.py` added `test_p126_to_p127_staged_handoff_contract_and_lifecycle` reproducing both the defect (`READY_TO_RUN` causing `state="blocked"` with missing pending design reason) and the fix (`PENDING DESIGN` advancing to `state="handoff"` with `next_task_id="P12.7"` and unblocking deferred planning).
- Verification:
  - Focused suites passed: 85 passed, 9 subtests passed (`test_p126_review_retry.py`, `test_transition_executor_aibroker.py`, `test_p12_6_persistent_harness_acceptance.py`, `test_staged_roadmap.py`, `test_staged_handoff.py`).
  - Full suite passed: 619 passed, 40 subtests passed in 214.13s (`python -m pytest tests_py -q`).
  - `python -m compileall -q src ops tests_py`, node syntax checks on `web/app.js` and `browser/chatgpt-web-adapter.user.js`, and `git diff --check` passed cleanly with 0 defects.
  - Canonical worktree is clean and ready for final independent technical re-review.

P12.7 web control surface visual refresh result (2026-09-17):
- Refactored `web/index.html`, `web/style.css`, and `web/app.js` into a dense single-page dashboard referencing OpenCode Data visual/information-architecture principles without copying branding, assets, or product metrics.
- Preserved all 26 legacy DOM IDs, GET API contracts, daemon mutation authority, CSRF/origin/Host headers, and CSP (`default-src 'self'`).
- Implemented pure exported helpers (`buildControlTarget`, `describeGuardedAction`, `computeFreshnessState`, `computeIncidentCount`, `computeKPIs`, `severityRank`, `compareSeverityThenIdThenTime`).
- Wired control buttons through `buildControlTarget` with target validation and disabled state handling.
- Added native confirmation dialogs for the five lifecycle-changing guarded actions (`stop`, `retry`, `rereview`, `reconcile`, `approve_owner_gate`).
- Replaced `Promise.all` with `Promise.allSettled` in `refresh()` to prevent partial fetch failures from masking errors as healthy.
- Added dedicated test suite `tests_py/test_web_ui_refresh.py` (8 tests) covering target building, confirmation prompts, disabled states, helpers, fake-DOM rendering fixtures, required IDs, security checks, and read-only GET behavior.
- Focused web & control regression passed: 55 passed, 6 subtests passed; `tests/web-selftest.ps1` passed; full regression passed: 629 passed, 40 subtests passed in 206.09s.
- `python -m compileall -q src ops tests_py`, node syntax checks on `web/app.js` and `browser/chatgpt-web-adapter.user.js`, and `git diff --check` passed cleanly.
- Knowledge graph updated via `graphify update .`: 3110 nodes, 8624 edges, 148 communities.
- Canonical worktree clean and ready for independent technical review.

P12.7 review remediation result (2026-09-17):
- Remediated all 6 Technical Review findings from `ai_review:auto-cf45dc90052f1a5988604625:execute`:
  - Added first-viewport `#daemonBadge` and `#watchdogBadge` in header status strip (`web/index.html`, `web/style.css`, `web/app.js`), ensuring daemon and watchdog health are immediately visible alongside monitor and freshness badges.
  - Hardened `computeFreshnessState` to fail closed to `'Disconnected'` whenever monitor fetch fails (`fetchFailed=true`), `!monitor`, `monitor.available===false`, or `monitor.process_alive===false`, preventing false `'Live'` display when overview has `observed_at`.
  - Hardened `computeIncidentCount` to detect top-level `watchdog.degraded === true` and increment incident count by 1, correctly reporting incidents when watchdog is degraded with 0 projects.
  - Hardened `renderWatchdogDiagnostics` to omit fabricated/unknown placeholders (`—`), display `State` only when returned by API, render `Degraded: yes/no`, render `Auto recovery: unavailable` when undefined instead of fabricating `disabled`, and omit placeholder `Observed: —`.
  - Recorded explicit manual 1366x768 viewport verification:
    - Topbar (daemon, monitor, watchdog, freshness badges, last refresh, refresh button) and 5 KPI cells fit in the first viewport (155px height vs 768px).
    - Contrast ratios: bright text `#f0f6fc` on `#0d1117` (15.8:1), body `#c9d1d9` on `#161b22` (10.4:1), muted `#8b949e` (5.1:1), status colors (OK `#3fb950` 6.7:1, Warn `#d29922` 6.9:1, Bad `#f85149` 5.4:1, Info `#58a6ff` 6.8:1) exceeding WCAG AA/AAA standards.
    - Visible keyboard focus via `*:focus-visible` (2px solid `#58a6ff` with 2px offset).
    - Guarded action confirm dialogs with labeled identity lines and consequence descriptions; 0 fetches on cancel.
  - Added comprehensive regression test `test_partial_failure_states_in_kpis_header_and_diagnostics` in `tests_py/test_web_ui_refresh.py` (9 tests total now) covering monitor failure, degraded watchdog with no projects, broker/accounting unavailable, and daemon/watchdog health badges. Also updated `test_freshness_kpi_and_incident_helpers` and `test_dom_ids_security_and_opencode_exclusion`.
- Verification:
  - Focused web/control suites passed: 56 passed, 6 subtests passed (`test_web.py`, `test_accounting_dashboard.py`, `test_unbound_web_ui.py`, `test_web_ui_refresh.py`, `test_p12_control_actions.py`, `test_p12_control_foundation.py`, `test_control_commands.py`).
  - `tests/web-selftest.ps1`: PASS.
  - Full test suite passed: 630 passed, 40 subtests passed in 205.12s (`python -m pytest tests_py -q`).
  - Python compilation (`compileall`), node syntax checks (`web/app.js`, `browser/chatgpt-web-adapter.user.js`), and `git diff --check` passed cleanly with 0 defects.
  - Knowledge graph updated with `graphify update .` (3114 nodes, 8631 edges, 157 communities).
  - Canonical worktree is clean without untracked `graphify-out/` to ensure clean lifecycle transition.

P12.7 second review remediation result (2026-09-18):
- Remediated findings from `ai_review:ai_review:auto-cf45dc90052f1a5988604625:execute`:
  - Hardened `computeIncidentCount` and `computeKPIs` in `web/app.js` to treat watchdog payloads with `available === false` as missing rather than present-and-empty. When all sources (summary, control overview, watchdog) are unavailable or missing, `computeIncidentCount` and `computeKPIs` return `'unavailable'` rather than displaying `0`.
  - When summary succeeds but watchdog fails (`{available: false}` as built by `refresh()`), `computeIncidentCount` correctly evaluates project incidents without treating missing watchdog data as present-and-empty.
  - Hardened `renderWatchdogBadge` in `web/app.js` to display `'No watchdog projects'` when watchdog reports empty projects (`projects: {}`), distinguishing an empty/unrun watchdog from `'Watchdog healthy'`.
  - Expanded `test_partial_failure_states_in_kpis_header_and_diagnostics` in `tests_py/test_web_ui_refresh.py` with cases 6 and 7 covering all three sources failed and summary OK with watchdog unavailable using the `{available: false}` objects that `refresh()` actually builds. Updated `test_freshness_kpi_and_incident_helpers` to verify both `'Watchdog healthy'` and `'No watchdog projects'`.
- Verification:
  - Focused web/control suites passed: 56 passed, 6 subtests passed (`test_web.py`, `test_accounting_dashboard.py`, `test_unbound_web_ui.py`, `test_web_ui_refresh.py`, `test_p12_control_actions.py`, `test_p12_control_foundation.py`, `test_control_commands.py`).
  - `tests/web-selftest.ps1`: PASS.
  - Full test suite passed: 630 passed, 40 subtests passed in 205.73s (`python -m pytest tests_py -q`).
  - Python compilation (`compileall`), node syntax checks (`web/app.js`, `browser/chatgpt-web-adapter.user.js`), and `git diff --check` passed cleanly with 0 defects.
  - Knowledge graph updated with `graphify update .` (3115 nodes, 8632 edges, 161 communities).
  - Canonical worktree is clean without untracked `graphify-out/` to ensure clean lifecycle transition.

P12.7 third review remediation result (2026-09-18):
- Remediated findings from `ai_review:rereview:closure3`:
  - Acceptance 2 (guarded-action CAS projection): Hardened `cmd_project_control` in `src/dev_orchestrator/cli.py` to mirror the daemon coordinator's per-action validation projection. For `retry` and `rereview`, expected CAS identity is derived using the orchestration lifecycle overlay (`overlay_orchestration_lifecycle` with `ai-reviewer.json` and `summary.json`), matching what `ControlCommandCoordinator._advance_command` observes (e.g. `REVIEW_FAILED`). For all other controls (e.g. `pause`), raw per-project snapshots are retained so commands are not rejected as stale project identity.
  - Daemon reviewer fallback: Hardened `ControlCommandCoordinator._advance_command` in `src/dev_orchestrator/core/control_commands.py` to fall back to durable on-disk reviewer state (`ai-reviewer.json`) when `self.reviewer` is not injected.
  - CLI CAS regression tests: Added `test_project_control_retry_uses_orchestration_lifecycle_overlay` and `test_project_control_rereview_uses_orchestration_lifecycle_overlay` in `tests_py/test_cli.py` verifying that both actions derive `expected.lifecycle_state = "REVIEW_FAILED"`.
- Verification:
  - Focused web/control/cli suites passed: 101 passed, 16 subtests passed (`test_cli.py`, `test_web.py`, `test_accounting_dashboard.py`, `test_unbound_web_ui.py`, `test_web_ui_refresh.py`, `test_p12_control_actions.py`, `test_p12_control_foundation.py`, `test_control_commands.py`, `test_p126_review_retry.py`, `test_p127_closure_rereview.py`).
  - `tests/web-selftest.ps1`: PASS.
  - Full test suite passed: 658 passed, 45 subtests passed in 228.65s (`python -m pytest tests_py -q`).
  - Python compilation (`compileall`), node syntax checks (`web/app.js`, `browser/chatgpt-web-adapter.user.js`), and `git diff --check` passed cleanly with 0 defects.
  - Knowledge graph updated with `graphify update .` (3146 nodes, 8781 edges, 159 communities). Canonical worktree is clean without untracked `graphify-out/` to ensure clean lifecycle transition.
