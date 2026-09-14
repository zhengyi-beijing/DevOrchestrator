# Execution Accounting Foundation (P11b)

P11 accounting is opt-in. Add a top-level block to `config/projects.json`:

```json
{
  "execution_accounting": {
    "enabled": true,
    "failure_memory": true,
    "prompt_max_chars": 2000,
    "event_path": "execution-accounting/events.jsonl"
  },
  "projects": []
}
```

With the block absent or `enabled: false`, the daemon constructs no accounting
store and existing orchestration calls and prompts are unchanged.

## Durable evidence

`ExecutionEventStore` writes compact JSON objects under the runtime root. Each
record has a schema version, process-wide contiguous sequence, durable record
timestamp, observed event timestamp and explicit correlation fields. Writes
are serialized across threads and processes and flushed to stable storage.
Unknown event types, phases and roles are rejected. Existing corruption blocks
new writes rather than being skipped.

Reads return events plus bounded `CorruptionReport` values. An incomplete final
record is reported as a torn tail. `recover_torn_tail()` is the only recovery
operation: it quarantines the incomplete bytes before truncating them. A
complete malformed line is never automatically discarded.

The closed interval phases are planning, plan review, plan remediation, queue,
AI execution, retry, managed validation, technical review, owner wait and idle.
Lifecycle records carry only observed project/task/request/source/dispatch/
execution/session/resource identities; unavailable identities are omitted.

## Accounting

`build_intervals()` pairs only explicit interval IDs, clips to the requested
window, right-censors missing ends and applies the documented phase precedence
to produce an exclusive wall-clock timeline. Idle fills uncovered time, so the
phase breakdown cannot double count.

`summarize_accounting()` reports the exclusive phase breakdown, plan-review
churn, retry wall time, owner wait, longest no-progress span, rejected-attempt
time and Effective Development Ratio. EDR is accepted productive seconds over
the observed window. Productive seconds are AI execution and managed validation
whose explicit attempt outcome is `accepted`; rejected or unresolved attempts
never enter the numerator.

Owner wait starts and ends only through the same explicit gate ID. An unmatched
gate remains right-censored.

## Failure memory

`FailureMemory` stores fingerprinted structured lessons with environment
predicates, symptom, root cause, preferred and avoided actions, confidence,
verification state, provenance, timestamps, recurrence count and recurrence
cost. Matching is deterministic and only verified lessons are injected by
default. The injected block has a hard character cap and carries both the
fingerprint and provenance.

The initial verified lesson covers Windows PowerShell 5.1: do not use `&&` or
`||` directly; use PowerShell-safe sequencing and exit-code checks, or invoke
`cmd.exe` when cmd syntax is required. Matching repeated failures update the
lesson and append a correlated `failure_recurrence` cost event.
