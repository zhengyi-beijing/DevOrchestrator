# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator`.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Development branch: `feature/self-hosted-dev`.
- Stable branch: `feature/browser-bridge-multiproject`.

Current task: **P11d Reporting and Quantitative Acceptance** — **COMPLETE**.

Next staged task: **none** (`P11d -> null` in the staged roadmap).

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
