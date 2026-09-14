# DevOrchestrator Self-Hosted Development State

- Stable controller: `C:\work\github\DevOrchestrator`.
- Development worktree: `C:\work\github\DevOrchestrator-dev`.
- Development branch: `feature/self-hosted-dev`.
- Stable branch: `feature/browser-bridge-multiproject`.

Current task: **P11b Execution Accounting Foundation / EDR / Failure Memory** — **COMPLETE**.

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
