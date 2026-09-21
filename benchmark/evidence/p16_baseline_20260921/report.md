# AI Capability Benchmark Report — run_plan_42_299f8e08_24

- **Plan ID**: `plan_42_299f8e08_24`
- **Generated At**: `2026-09-21T10:30:49.770890+00:00`
- **Total Trials**: `24` (Completed: `24`)

## Final Promotion Decision: **🔴 NO_PROMOTE**

- **Decision**: `NO_PROMOTE`
- **Reason Codes**: `capability_unsupported`
- **Failed Gates**: `capability_gate, benefit_gate, privacy_gate, staleness_gate, cost_gate, quality_gate`

### Gate Results Summary

| Gate | Passed | Status | Details |
| --- | --- | --- | --- |
| `capability_gate` | **FAIL** | `failed` | zvec installation unsupported: zvec executable not found on system or PATH |
| `benefit_gate` | **FAIL** | `not_applicable_due_to_capability_failure` | zvec retrieval unsupported |
| `privacy_gate` | **FAIL** | `not_applicable_due_to_capability_failure` | zvec retrieval unsupported |
| `staleness_gate` | **FAIL** | `not_applicable_due_to_capability_failure` | zvec retrieval unsupported |
| `cost_gate` | **FAIL** | `not_applicable_due_to_capability_failure` | zvec retrieval unsupported |
| `evidence_gate` | **PASS** | `evaluated` | distinct resources=3 |
| `fallback_gate` | **PASS** | `passed` | native track completed 12 trials |
| `quality_gate` | **FAIL** | `not_applicable_due_to_capability_failure` | retrieval track not executed due to capability failure |

## Retrieval A/B Track Comparison

| Metric | Track A (Native) | Track B (Retrieval) | Delta (B - A) |
| --- | --- | --- | --- |
| **Correctness Mean** | 0.8750 | 0.8750 | +0.0000 |
| **Wall Time Mean (s)** | 6.17s | 6.17s | +0.00s |
| **Reported Tokens Mean** | 492.5 | 492.5 | N/A |
| **Observed Shell Calls** | 2.00 | 2.00 | +0.00 |
| **Zvec Calls** | 0.00 | 0.00 | — |
| **Valid Cited Spans** | 24 | 24 | — |
| **Invalid Cited Spans** | 0 | 0 | — |
| **False Findings Total** | 0 | 0 | — |

## Resource Breakdown

| Resource ID | Completed / Total | Track A Correctness | Track B Correctness | Wall Time (s) |
| --- | --- | --- | --- | --- |
| `agy/agy-1/gemini-3.8-flash-high` | 8/8 | 0.8750 | 0.8750 | 8.50s |
| `copilot/default/claude-sonnet-4.6` | 8/8 | 0.8750 | 0.8750 | 4.20s |
| `claude/default/opus` | 8/8 | 0.8750 | 0.8750 | 5.80s |

## Role Breakdown

| Role | Completed / Total | Track A Correctness | Track B Correctness |
| --- | --- | --- | --- |
| `planner` | 6/6 | 0.8500 | 0.8500 |
| `reviewer` | 6/6 | 0.8500 | 0.8500 |
| `worker` | 6/6 | 0.9000 | 0.9000 |
| `debugger` | 6/6 | 0.9000 | 0.9000 |
