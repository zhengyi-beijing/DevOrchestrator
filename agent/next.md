# P11 Execution Accounting and Bottleneck Profiling

Status: **PENDING DESIGN**

Owner authorization: **START P11 / 2026-09-13**

Goal: quantify DevOrchestrator development wall-clock cost before changing scheduling policy, using P10 watchdog data as one input and adding finer-grained execution accounting.

Scope:
- Persist queue/provider/context/model/test/review/retry/quota/owner/transport/idle timing per project/task/role.
- Track provider, model and session continuity; compute Context Hit Rate and identify context churn caused by resource/model/session switching.
- Measure failover latency from quota/rate-limit/provider-unhealthy detection to fallback execution start and expose avoidable waiting/retry.
- Measure RDC/transport invocation count, command size, first-output latency, duration, output size and failure/retry count.
- Detect lifecycle overhead: repeated planning, unresolved reviews, prolonged REVIEWING/BLOCKED, repository-truth churn and no-progress intervals.
- Compute Effective Development Ratio and longest no-progress interval.
- Add 8770 views for time breakdown, top bottlenecks, context hit rate, provider switches, failover latency, RDC statistics and longest stall.
- Produce evidence-backed diagnoses such as reviewer_stall, quota_failover_delay, context_churn, transport_overhead and lifecycle_churn.

Acceptance:
- Analyze a representative DevO + xray-hw-platform development day.
- Quantitatively confirm or reject: RDC large-command overhead, AI context churn, quota-without-timely-failover, and lifecycle/reviewer stall hypotheses.
- Instrument and measure first. Do not perform broad scheduler optimization in P11; use measured bottlenecks to define later remediation.
- Preserve self-hosting isolation: modify only `C:\work\github\DevOrchestrator-dev`; do not modify/restart/replace the stable controller except through the established promotion flow after acceptance.

Execution instruction: begin P11 now through the normal DevOrchestrator lifecycle. Continue automatically through planning, implementation, verification, review and bounded remediation until P11 reaches an accepted phase gate or an OWNER_GATE condition.