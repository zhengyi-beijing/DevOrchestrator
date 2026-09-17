# DevOrchestrator Self-Hosted Development State

- Canonical worktree: C:\work\github\DevOrchestrator-dev.
- Canonical branch: main.
- Runtime mode: self-hosted canonical daemon on 8770 with AIBroker diagnostics on 8875.
- Historical detached worktree C:\work\github\DevOrchestrator is not an active controller.

Current task: **P12.6 Persistent Harness Acceptance and Closure** ? **PENDING DESIGN / OWNER START REQUIRED**.

Staged roadmap handoff is now P12.5 -> P12.6. P12.5 is closed after independent technical review; P12.6 is the active staged task and requires explicit owner start before implementation.

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
