# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator`.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Development branch: `feature/self-hosted-dev`.
- Stable branch: `feature/browser-bridge-multiproject`.

Current task: **P12 Unified AI Control Surface** — **COMPLETE**.

Staged roadmap: `P11d -> P12 -> null`. P12 was owner-authorized and completed on 2026-09-15; no successor is staged.

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
