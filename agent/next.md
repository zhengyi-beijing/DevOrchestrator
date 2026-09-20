# NEXT — P15 Mobile Observability & Guarded Control

Status: **PENDING DESIGN**

Sequence:
P14.5 (closed) -> Watchdog recovery-epoch cleanup (closed) -> P14.6 Unattended Execution Stabilization Gate (closed) -> P15

## P14.6 Unattended Execution Stabilization Gate closure

Closed after implementing and verifying all unattended execution stabilization phases:

Acceptance evidence:
1. **Planner Protocol Normalization & Schema Repair**:
   - Planner follows Raw Capture -> JSON Extract -> Normalize -> Schema Validate -> Semantic Validate -> Reviewer pipeline.
   - Bounded schema/extraction repair allowed without consuming semantic failure budget. Ambiguous multiple JSON objects fail closed.
2. **Reviewer & Worker Resource Failover**:
   - Both AI Reviewer and TransitionExecutor retry on resource failures (`ROLE_RESOURCE_FAILURES`: `quota_exhausted`, `rate_limited`, `provider_temporarily_unavailable`, `resource_unavailable`) on alternative resources (`f"{id}:failover-{attempt}"`, tracking `failover_from_resource_ids`).
   - Resource failovers do not consume semantic remediation budgets or require manual continue.
   - Worker checks repository truth before failover; if repository was modified or dirtied, failover is safely refused.
   - Contract alignment: `AIRoleRequest.previous_resource_context` accepts `ResourceContext | Mapping[str, Any] | None` (NB-8 resolved).
3. **Recovery Epoch Cross-Component Agreement**:
   - `resolve_recovery_epoch` extended to resolve active plan, review, and worker executions from `runtime_root`.
   - Watchdog, `project_runtime_status`, `build_project_status`, and `project_control_view` all produce identical `recovery_epoch` dictionaries and `recovery_epoch_id` SHA256 hashes.
4. **Closed-Loop Predecessor Handoff & Unlaunched READY_TO_RUN Launch**:
   - `TransitionExecutor._advance_completed_predecessor_handoffs` creates `auto-handoff` or `auto-settled` when a predecessor task is marked complete in the repository without human intervention, with deduplication guards against active decisions, existing handoffs, and pending reviews.
   - `TransitionExecutor._advance_unlaunched_ready` automatically launches unlaunched `READY_TO_RUN` tasks without human intervention.
   - `TransitionExecutor.overlay_managed_runs` hardened so historical terminal failures do not relabel newly ready tasks as `WORKER_FAILED`.
5. **Acceptance Suite & Verification**:
   - Acceptance test suite `tests_py/test_p14_6_unattended_gate.py`: 6 passed in 4.63s covering reviewer failover success, reviewer failover exhaustion, worker clean failover, worker dirty refusal, unattended predecessor-to-successor launch, and cross-component recovery epoch agreement.
   - Focused and adjacent suites: `test_staged_handoff.py` (16 passed), `test_transition_overlay.py` & `test_transition_executor_aibroker.py` (27 passed, 8 subtests), `test_watchdog_recovery.py` & `test_transition_executor.py` & `test_ai_reviewer.py` (100 passed, 15 subtests), `test_staged_roadmap.py` (24 passed).
   - Full repository regression: 895 passed, 87 subtests passed in 302.78s.
   - `compileall -q src tests_py`, `git diff --check`, and `graphify update .` all passed cleanly.

## P15 goal

Provide an Android-native, failure-independent observation and bounded-control client without creating a second lifecycle authority.

Refer to `agent/staged/P15.md` for initial scope and acceptance criteria.
