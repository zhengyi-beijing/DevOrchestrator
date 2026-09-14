# P11 Reporting and Quantitative Acceptance

P11d exposes the P11b accounting and P11c provider/RDC evidence through a
single deterministic report. Reporting is read-only: it does not change
scheduler, routing, provider, cancellation, or project lifecycle policy.

## Report surfaces

- Dashboard/API: `GET /api/accounting` on the existing port 8770 service.
- CLI: `python -m dev_orchestrator.cli execution-report --window-start <UTC>
  --window-end <UTC> [--project-id ID] [--task-id ID] [--role ROLE]
  [--runtime-root PATH]`.
- Machine gate: add `--fail-on-gate`; the command exits non-zero for `fail` or
  `insufficient_evidence`.

The API accepts optional `start`, `end`, `project_id`, `task_id`, and `role`
query parameters. Without an explicit window, it uses the earliest matching
event through the current UTC observation time. Automated acceptance should
always supply an explicit window. Point observations at either window boundary
are included; interval durations are clipped to the window.

If execution accounting uses a custom event path, enabling the runtime writes
`execution-accounting/runtime.json`. Standalone CLI and dashboard readers use
that manifest rather than assuming the default ledger path.

## Provenance and unknown data

Each source is marked `measured` or `unavailable`. Calculated ratios,
continuity counts, failover latency, classifier results, bottleneck durations,
and gate values are marked `derived_from_measured`. The report always states
`inference: disabled`.

Missing event sources and missing provider/RDC fields produce warnings. They do
not become measured zeroes. A gate whose required evidence is missing is
`unavailable`, and the aggregate result becomes `insufficient_evidence` unless
another gate already fails.

The dominant bottleneck is the largest measured lost-time candidate. It carries
the durable event/request/invocation IDs used to calculate it and a bounded
follow-up recommendation. Recommendations are diagnostic only.

The `hypotheses` section compares the original P11 candidates: oversized RDC
commands, AI context switching, quota waits, lifecycle/reviewer stalls, and
multi-project contention. A hypothesis becomes observable only from its direct
measurements. In particular, a quota/rate-limit observation without an explicit
`retry_after_seconds` value does not become an invented wait duration, and an
RDC finding counts as multi-project contention only when its evidence names
more than one project.

`time_breakdown` contains measured rows keyed by project, task, and role. Each
row uses the same explicit report window and carries phase durations, accepted
and rejected productive time, EDR, plan-review churn, retry time, owner wait,
and `derived_from_measured` provenance.

## Default quantitative gates

These defaults are initial operational thresholds, not scheduler policy:

| Gate | Default |
|---|---:|
| Effective Development Ratio | at least 0.25 |
| Owner-wait / observed time | at most 0.25 |
| Retry / observed time | at most 0.20 |
| Context switches | at most 2 |
| Correlated provider failover latency | at most 300 seconds |
| RDC true deadlocks | 0 |
| RDC cross-project cancellation/reconnect contamination | 0 |

The cross-project contamination gate is unavailable until the selected window
contains explicit cross-project interaction evidence (an isolated-concurrency,
session-coupling, reconnect-contamination, or other multi-project finding). A
single project's clean results cannot prove cross-project isolation.

RDC classification thresholds are reported alongside every result: 5 seconds
for head-of-line wait, 30 seconds for starvation, 120 seconds of explicitly
observed no-output age for deadlock, and 65,536 bytes or 32 commands for an
oversized invocation.

## Representative DevOrchestrator acceptance

`tests_py/test_p11_reporting.py` builds a DevOrchestrator lifecycle containing
accepted Worker and validation time, technical review, owner wait, correlated
provider failover, context switching, and an RDC no-output observation. It
persists the rows through `ExecutionEventStore`, runs the public
`execution-report` CLI, and asserts:

- complete accounting/provider/RDC sections;
- EDR and time-loss calculations;
- dominant bottleneck plus evidence IDs;
- deterministic pass/fail/unavailable gates;
- non-zero `--fail-on-gate` behavior.

This acceptance uses DevOrchestrator itself. The paused `xray-hw-platform`
project is not resumed or mutated.
