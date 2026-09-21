# AI Capability Benchmark Report — run_plan_42_299f8e08_24

- **Plan ID**: `plan_42_299f8e08_24`
- **Generated At**: `2026-09-21T11:09:37.800294+00:00`
- **Execution Source**: `pipeline_self_test` (Real Broker Evidence: `False`)
- **Total Trials**: `24` (Completed: `24`)

## Retrieval A/B Track Comparison

| Metric | Track A (Native) | Track B (Retrieval) | Delta (B - A) |
| --- | --- | --- | --- |
| **Correctness Mean** | 1.0000 | 1.0000 | +0.0000 |
| **Wall Time Mean (s)** | 0.00s | 0.00s | +0.00s |
| **Reported Tokens Mean** | 120.0 | 120.0 | N/A |
| **Observed Shell Calls** | 1.00 | 1.00 | +0.00 |
| **Zvec Calls** | 0.00 | 0.00 | — |
| **Valid Cited Spans** | 12 | 12 | — |
| **Invalid Cited Spans** | 0 | 0 | — |
| **False Findings Total** | 0 | 0 | — |

## Resource Breakdown

| Resource ID | Completed / Total | Track A Correctness | Track B Correctness | Wall Time (s) |
| --- | --- | --- | --- | --- |
| `agy/agy-1/gemini-3.8-flash-high` | 8/8 | 1.0000 | 1.0000 | 0.00s |
| `copilot/default/claude-sonnet-4.6` | 8/8 | 1.0000 | 1.0000 | 0.00s |
| `claude/default/opus` | 8/8 | 1.0000 | 1.0000 | 0.00s |

## Role Breakdown

| Role | Completed / Total | Track A Correctness | Track B Correctness |
| --- | --- | --- | --- |
| `planner` | 6/6 | 1.0000 | 1.0000 |
| `reviewer` | 6/6 | 1.0000 | 1.0000 |
| `worker` | 6/6 | 1.0000 | 1.0000 |
| `debugger` | 6/6 | 1.0000 | 1.0000 |
