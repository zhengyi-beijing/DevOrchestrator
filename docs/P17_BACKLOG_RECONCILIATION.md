# P17 Backlog Capability Reconciliation

Status: **FROZEN / ACCEPTED (PHASE W)**  
Task: P17 Single-Authority Goal Convergence Baseline  

Every retained architectural capability in the DevOrchestrator backlog is classified into exactly one of:
`IMPLEMENTED`, `PARTIAL`, `NOT_IMPLEMENTED`, `OBSOLETE`, or `MERGED`.

---

## 1. Capability Matrix

| Capability Area | Status | Current Production Implementation (P1-P16.14) | Target Convergence Disposition (P17 v0 Model & Beyond) |
| :--- | :--- | :--- | :--- |
| **1. Provider / Model / Account Routing** | `IMPLEMENTED` | AGY account pool (`agy_pool.py`), routing policies (`routing_policy.py`), and AIBroker provider routing with independence enforcement (`ai_roles`). | Retained as external telemetry. Evaluator consumes provider availability from `EvidenceSnapshot` and emits `FAILOVER_RESOURCE` or `ESCALATE_CAPABILITY`. |
| **2. Quota / Retry / Failover** | `MERGED` | Scattered across `execution_intent`, `ai_reviewer` remediation limits, and WebSol failover engine (`websol_failover.py`). | Merged into unified `ProblemBudget` and pure `decide()` logic (`WAIT_UNTIL` for timed quotas, `FAILOVER_RESOURCE` for transient provider errors). |
| **3. Lifecycle / Handoff** | `MERGED` | Split across `transition-executor.json`, `status.json`, and watchdog recovery reconcilers (`successor_consistency.py`). | Merged into single canonical `WorkRecord` with deterministic `propose_successor_publication` and `build_resumable_handoff`. |
| **4. Watchdog / Activity Recovery** | `PARTIAL` | `WatchdogCoordinator` monitors activity timestamps and triggers recovery commands via loopback inbox. | Transitioned from an independent controller to an evidence observer: watchdog emits `EvidenceItem` (e.g. process probe or stall evidence); pure evaluator decides recovery. |
| **5. Invariant / Fault Injection** | `IMPLEMENTED` | `control_plane_faults.py` registers `CPF-01` through `CPF-10` covering 6 lifecycle invariants; validated in `test_p1614_invariant_workflow.py`. | Fully retained and extended: mapped to 21 convergence invariant codes in `invariants.py` and replayed across all 26 historical corpus fixtures. |
| **6. Post-Worker Acceptance Continuation** | `PARTIAL` | Reviewer runs and emits `next` / `remediate`, but stalled on remediation exhaustion requiring manual continue. | Replaced by pure `is_goal_satisfied` and deterministic `PUBLISH_SUCCESSOR` / `WRITE_HANDOFF` without manual continue clock. |
| **7. Persistent Context** | `IMPLEMENTED` | `project_context.py` validates `project-context.json` with 7 canonical domains, SHA-256 digest, and prompt injection across roles. | Retained as evidence input. `EvidenceSnapshot` includes context digest and exact anchors. |
| **8. API / Web Management** | `IMPLEMENTED` | Web control server on port 8770 (`server.py`), REST endpoints, authenticated tokens, and Tampermonkey userscript integration. | Pure evaluator provides machine-readable CLI and JSON report models (`dev_orchestrator.convergence.cli`). |
| **9. Multi-Project Support** | `IMPLEMENTED` | Multiple project entries in `projects.json`, independent runtime roots, per-project locks and bindings. | WorkRecord schema strictly keys on `project_id`. Multi-project isolation is preserved across all models. |
| **10. Project Onboarding** | `PARTIAL` | Manual repo initialization with `.devorch/config.json` and CLI `init` helpers. | Future migration will use `WorkRecord.schema_version=1` initialization templates. |
| **11. DAG / Parallel Execution** | `NOT_IMPLEMENTED` | Only sequential single-goal progression per project currently implemented. | Future work. Single-authority WorkRecord handles one goal with linear successor handoffs. |
| **12. Multi-Node Deployment** | `NOT_IMPLEMENTED` | Localhost loopback daemon architecture only. | Deferred. Actuator guard models host identity in active leases to prevent split-brain execution. |
| **13. Mobile Gateway** | `PARTIAL` | P15 mobile gateway routes and notifications prototyped. | Retained as notification sink; does not mutate lifecycle authority. |
| **14. Operating / Resource Modes** | `PARTIAL` | AGY conserve/healthy/low modes in `agy_pool.py`. | Mapped into `Policy.capability_tiers` and budget-free wait boundaries. |
| **15. Incident-to-Regression Learning** | `IMPLEMENTED` | Automatic failure harvesting (`harvesting.py`) and candidate regression generation (`tests_candidate/`). | Extended in P17 with `CapabilityConstraint` preflight: checks learned rules and classifies recurrences as `LEARNING_REGRESSION`. |
