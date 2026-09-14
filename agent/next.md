# P11b Execution Accounting Foundation (P11-B)

Status: **COMPLETE**

Owner authorization: **START P11b / 2026-09-13**

Goal: instrument DevOrchestrator so development time loss is measured from durable evidence instead of guessed. This phase establishes the accounting substrate only; provider/context/RDC diagnostics remain P11c and dashboard/representative-day acceptance remains P11d.

Scope:
- Add a durable, append-only P11 event store under the runtime root with cross-thread/process serialization, explicit write failure handling, and bounded corruption/torn-tail reporting. No silent event loss.
- Define a closed event taxonomy for project/task/role/request correlation and lifecycle intervals: planning, plan review, plan remediation, queue, AI execution, retry, managed validation, technical review, owner wait, and idle.
- Instrument existing Planner, plan Reviewer, Worker, remediation Worker, technical Reviewer, control/owner-gate, and validation boundaries using real timestamps already observable by DevO.
- Preserve request_id, task_id, role, source_request_id, dispatch/execution ids and resource identity where available; unavailable fields remain unknown rather than inferred.
- Add deterministic interval construction with clipping, right-censoring and overlap precedence so the same wall-clock instant is counted once.
- Compute time breakdown, plan-review churn, retry wall time, owner-wait time, longest no-progress interval, and Effective Development Ratio.
- Define EDR as accepted productive work / observed wall-clock window. Productive work is AI execution plus managed validation that belongs to a Worker/remediation attempt ultimately accepted by technical review; rejected attempts are reported separately, not counted as productive.
- Add the failure-memory foundation: structured lessons with fingerprint, environment predicates, symptom, root cause, preferred/avoided action, confidence, verification state, timestamps and recurrence count.
- Seed the verified RDC PowerShell 5.1 lesson: avoid `&&`/`||`; use PowerShell-safe sequencing or cmd.exe as appropriate.
- Inject matching verified lessons into Planner, Worker and Reviewer prompts with provenance and a hard size cap; record repeated-known-failure recurrence cost.

Acceptance:
- Event-store concurrency test proves no lost/duplicate sequence under threads plus subprocess writers; torn trailing data is flagged and recoverable.
- Synthetic interval tests prove clipping, right-censoring, precedence and no double counting.
- Deterministic lifecycle fixture includes plan reject -> REMEDIATING_PLAN -> approve -> Worker -> technical review and produces the expected plan-review/retry/accepted-work timings.
- EDR tests prove rejected Worker/remediation attempts do not enter the productive numerator and accepted attempts do.
- Owner-wait accounting uses explicit gate correlation; unmatched gates remain right-censored.
- Failure-memory tests prove deterministic predicate matching, provenance/size cap, seeded PowerShell lesson injection, and recurrence recording.
- Existing orchestration behavior is unchanged when P11 accounting is disabled/not configured.
- Run focused tests, then full `python -m pytest tests_py -q` and `git diff --check`.
- Commit locally on `feature/self-hosted-dev`; do not push or resume the paused xray-hw-platform project.

Out of scope:
- Provider/context continuity and quota/failover diagnosis (P11c).
- RDC transport metrics and multi-project isolation probes (P11c).
- 8770 reporting/dashboard and representative-day acceptance (P11d).
- Broad scheduler/routing optimization or AIBroker provider policy changes.
- Automatic selection of arbitrary backlog items as the next task; cross-task materialization is a separate control-plane gap and must only use an explicitly approved staged roadmap.

## Approved executable design

- Add `dev_orchestrator.accounting` as a dependency-light foundation package. Keep its store, interval accounting, failure memory and runtime wiring independent from dashboards and provider diagnosis.
- Persist compact schema-v1 JSONL events below the runtime root. Allocate the next sequence and append it under a process-local reentrant lock plus an OS file lock; loop on short writes, flush and fsync before success.
- Reject unknown event types, phases, roles, invalid timestamps and conflicting deterministic event replays. Never append past detected corruption.
- Return bounded corruption evidence from reads. Treat only an incomplete final record as a recoverable torn tail; quarantine its bytes before explicit truncation. Never discard a complete malformed line.
- Use deterministic `event_id` values at replay-prone browser boundaries so crash retries return the original event rather than append a duplicate.
- Model starts and ends with explicit interval IDs. Pair only matching IDs, clip to the report window, right-censor unmatched starts, report unmatched ends, split at every boundary and select one phase by the fixed precedence table. Fill uncovered time as idle.
- Compute EDR only from `ai_execution` and `managed_validation` intervals whose explicit attempt outcome is `accepted`. Report rejected attempt time separately, and do not allow outcomes after the report window to change that window's numerator.
- Compute plan-review churn from explicit rejected plan outcomes; compute retry wall time from retry intervals without allowing overlap to double-count the exclusive phase breakdown. Derive owner wait only from matching explicit gate IDs.
- Store immutable lesson content under a canonical SHA-256 fingerprint with environment predicates, confidence, verification state, provenance, timestamps, recurrence count and recurrence cost. Fail closed on malformed memory state.
- Seed one verified Windows PowerShell 5.1 lesson that forbids direct `&&`/`||` use and recommends PowerShell-safe sequencing/exit-code checks or `cmd.exe` when cmd syntax is required.
- Match predicates deterministically, inject verified lessons only, include fingerprint and provenance, and enforce the configured character cap. Record recognizable recurrences both in memory and in the event ledger.
- Add a top-level opt-in `execution_accounting` configuration. Missing or disabled configuration constructs no store and preserves the old coordinator calls and prompt bytes.
- Pass optional recorder/failure-memory dependencies into Planner, direct technical Reviewer, TransitionExecutor and ControlCommandCoordinator. Inject lessons into Planner, plan Reviewer, Worker, remediation Worker, direct Reviewer and browser Reviewer prompts.
- Record Planner/plan-review/remediation/retry, Broker and legacy Worker execution, legacy fallback retry, direct/browser technical review, browser queue and owner-gate boundaries. Preserve observed project/task/stage/role/request/source/dispatch/execution/session/resource identifiers without fabricating missing values.
- Extend `project-continue` with optional `--gate-id`; only that explicit correlation closes an owner-wait interval.
- Document configuration and accounting semantics in `docs/EXECUTION_ACCOUNTING_CONTRACT.md`; cover concurrency, corruption recovery, interval math, EDR, lifecycle fixture, prompt injection, recurrence and disabled compatibility with focused tests.
