# P11d Reporting and Quantitative Acceptance (P11-D)

Status: **COMPLETE**

Owner authorization: **START P11d / 2026-09-13**

Goal: expose P11 evidence in the 8770 dashboard and turn it into quantitative bottleneck diagnoses and acceptance criteria.

Scope:
- Add project/task/role time breakdown, EDR, plan-review churn, retry/owner-wait/idle time, context/provider switching, quota/failover and RDC contention evidence to the dashboard/API.
- Highlight the dominant bottleneck with source-backed evidence and unknown-data warnings.
- Add representative-run acceptance using DevOrchestrator itself; do not resume xray-hw-platform unless separately authorized.
- Compare measured time loss against the original hypotheses: oversized RDC commands, AI/context switching, quota waits, lifecycle/reviewer stalls and multi-project contention.
- Produce actionable thresholds and follow-up recommendations without changing scheduler/provider policy in this phase.

Acceptance:
- Dashboard renders deterministic fixture data and clearly distinguishes measured, inferred-disabled and unavailable values.
- Representative DevOrchestrator run produces a complete report with EDR, top bottlenecks and evidence links/correlation ids.
- Quantitative acceptance gates are documented and machine-testable where feasible.
- Full regression and `git diff --check` pass; commit locally only, no push.

Out of scope:
- Broad scheduler/routing optimization.
- Resuming paused projects without owner authorization.

## Approved executable design

- Add a dependency-light reporting layer that combines the existing accounting interval summary with P11c provider/context and RDC analyses over one explicit UTC window and optional project/task/role filters.
- Preserve measured, derived, and unavailable provenance per metric; never turn absent provider or RDC evidence into zero-valued measured facts.
- Rank bottlenecks from exclusive accounting durations and explicit provider/RDC evidence, retaining correlation IDs that point back to durable ledger rows.
- Add read-only `/api/accounting` reporting on port 8770 and dashboard sections for EDR, phase time, provider/context, RDC contention, dominant bottleneck, warnings, and evidence links.
- Add a CLI report path for deterministic representative-run fixtures and machine-readable quantitative gates.
- Document default acceptance thresholds and make overrides explicit in report inputs; do not modify scheduler or provider policy based on the report.

## Completion evidence

- Added deterministic unified reporting, project/task/role time rows, original-hypothesis comparisons, evidence-backed bottleneck ranking, and quantitative gates.
- Added read-only CLI and port-8770 API/dashboard surfaces, including custom-ledger discovery without mutating report reads.
- Representative DevOrchestrator fixtures exercise accounting, provider/context, quota/failover, RDC, unknown-data, and rendered-dashboard behavior.
- Full regression passed with 487 tests and 16 subtests; Python/JavaScript syntax, `git diff --check`, and Graphify AST refresh passed.
- P11d has no staged successor. `xray-hw-platform` remains paused and unchanged.
