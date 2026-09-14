# Provider, Context, and RDC Evidence Contract

P11c extends the opt-in execution-accounting ledger with exact provider-result
and normalized RDC invocation evidence. It does not change resource routing,
provider fallback, scheduler policy, or project lifecycle decisions.

## Provider result evidence

When execution accounting is enabled, the AIBroker subprocess boundary writes
one deterministic `provider_result_observed` event for every valid Broker
result. The event preserves the observed request, dispatch, decision,
execution, resource, provider, account, model, and session identifiers.

Broker-supplied `started_at`, `finished_at`, `first_output_at`, quota, and
rate-limit observations are retained when present. Missing fields remain absent
and are counted as unavailable by reports. Error strings are never parsed to
invent quota, attempt, or first-output facts.

Provider/context summaries compare results only inside a deterministic
correlation group. Explicit prior resource context takes precedence; otherwise
the preceding result in the same group is used. Each resource/provider/account/
model/session dimension is reported as hit, switch, or unknown.

A failover is reported only when all of the following evidence exists:

- a failed or no-candidate result;
- a later result in the same correlation group;
- both resource identifiers are known and differ;
- the failed result's finish and next result's start timestamps are known and
  ordered.

Failover latency is the exact gap between those two timestamps.

## RDC evidence import

`python -m dev_orchestrator.cli import-rdc-evidence --input <path> --config
<projects.json> --runtime-root <runtime>` imports a JSON array or JSONL file.
Execution accounting must be enabled. Each input row requires:

- `project_id`, `invocation_id`, `status`, and `occurred_at`;
- status in `queued`, `running`, `succeeded`, `failed`, `cancelled`, or
  `unknown`.

Optional exact facts include request/session/connection identity, connection
generation, command bytes/count, submitted/start/first-output/finish times,
cancellation owner/time, reconnect time, and affected invocation IDs. Counts
must be non-negative integers and timestamps must carry a timezone. A
deterministic `rdc:<project_id>:<invocation_id>` event ID makes identical imports
idempotent and conflicting replays fail closed.

## Deterministic RDC classifiers

The default thresholds are part of the reported evidence:

- head-of-line wait: 5 seconds;
- starvation wait: 30 seconds;
- no-output deadlock age: 120 seconds;
- oversized command: 65,536 bytes or 32 commands.

Classifiers use the following explicit contracts:

- `head_of_line_blocking`: a queued invocation waits beyond the threshold behind
  an overlapping oversized invocation on the same connection generation;
- `starvation`: an invocation waits beyond the threshold while at least two
  later submissions on the same connection generation start before it;
- `session_coupling`: an invocation explicitly records cancellation by another
  project;
- `reconnect_contamination`: a reconnect explicitly lists an affected
  invocation belonging to another project;
- `deadlock`: a running/unknown invocation has neither first output nor finish
  by the deadlock threshold;
- `isolated_concurrency`: completed invocations from distinct projects overlap
  without recorded cancellation/reconnect contamination.

Recovery target calculation filters by exact `project_id` and returns only
that project's queued/running/unknown invocations. It is read-only and cannot
mutate another project's evidence.
