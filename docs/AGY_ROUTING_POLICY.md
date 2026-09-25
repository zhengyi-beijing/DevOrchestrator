# AGY-First AI Resource Pool Benchmark & Routing Policy

Status: OPERATIONAL / BENCHMARKED  
Date: 2026-09-25  
Authoritative Workflow Policy: `docs/development-workflow.md`  
Related Contracts: `docs/AGY_PROJECT_ISOLATION_CONTRACT.md`

---

## 1. Overview & Core Philosophy

DevOrchestrator manages three near-zero marginal-cost Antigravity (AGY) accounts (`agy-1`, `agy-2`, `agy-3`). These accounts represent high-throughput, low-cost AI compute capacity. However, utilizing free/low-cost execution must never degrade pipeline reliability, weaken reviewer independence, or induce infinite failure loops.

This policy governs the scheduling, routing, safety gates, and heterogeneous escalation across the AGY pool:

1. **Shared Resource Pool**: The three AGY accounts are treated as a shared quota- and time-window-constrained compute pool, not statically bound to fixed Planner, Reviewer, or Worker roles.
2. **Reviewer Independence Is Inviolable**: Near-zero marginal cost execution must never weaken final or high-risk review gates. When strict provider independence is required, reviewers must route outside the AGY provider.
3. **No Blind Account Rotation**: Retrying a task with the same failure signature across `agy-1`, `agy-2`, and `agy-3` without a change in strategy or context is strictly forbidden. Repeated identical failures must escalate heterogeneously to paid models (`codex`, `claude`).
4. **Empirical Capability Boundaries**: Routing is governed by measured task-class capability, real-time availability, and quota telemetry rather than static rankings.

---

## 2. Resource Pool Architecture & Telemetry

### 2.1 Pool Structure
Each of the three AGY accounts (`agy-1`, `agy-2`, `agy-3`) is configured with:
- Dedicated profile reference and credential isolation (`windows-credential-slot`);
- Concurrency limit: 1 in-flight request per account;
- Sliding rate window: 60 requests per 15-minute (900s) rolling window;
- Supported models: `gemini-3.8-flash-high` (all roles) and `claude-opus-4-6-thinking` (high-reasoning reviewer/adjudicator).

### 2.2 Quota States & Dynamic Degradation
Account telemetry tracks rolling window utilization and transitions through discrete quota states:
- `HEALTHY`: Window utilization < 65%. Normal scheduling with priority.
- `CONSERVE`: Window utilization 65% - 84%. Pool prefers accounts with lower utilization.
- `LOW`: Window utilization 85% - 99%. Warning state; non-critical batch jobs deferred if heterogeneous capacity is available.
- `EXHAUSTED`: Window utilization >= 100% or active 429/quota error. Account locked until window reset or cooldown expiry.

### 2.3 Cooldown Management
When an account encounters a transient provider failure (`rate_limited`, `quota_exhausted`, `provider_temporarily_unavailable`):
- The account is automatically armed with a cooldown (default 300s, rate-limit 900s);
- In-flight and incoming dispatches bypass the cooled account;
- Once the timestamp expires, the account automatically recovers to `HEALTHY` upon window reset.

---

## 3. Capability Boundary Matrix

Routing recommendations classify task classes and runtime contexts into three definitive dispositions:

| Role / Context | Disposition | Selected Tier | Rationale & Policy |
| :--- | :--- | :--- | :--- |
| **`planner`** | `SUPPORTED` | `AGY_POOL` | High token-window capacity, strong architectural structuring and task decomposition. |
| **`worker`** | `SUPPORTED` | `AGY_POOL` | Highly effective on targeted implementations, bug fixes, refactorings, and unit test creation. |
| **`debugger`** | `SUPPORTED` | `AGY_POOL` | Accurate root cause analysis, stack trace diagnosis, and cross-file defect correlation. |
| **`evidence_packaging`** | `SUPPORTED` | `AGY_POOL` | Rapid synthesis of forensic incident packets, fingerprinting, and telemetry formatting. |
| **`reviewer` (Account Indep.)** | `SUPPORTED` | `AGY_POOL` | When `independence="account"`, review executed on a different AGY account (e.g. worker on `agy-1`, reviewer on `agy-2`) is permitted for low-risk changes. |
| **`reviewer` (Provider Indep.)** | `UNSUPPORTED` | `INDEPENDENT_REVIEWER` | When `independence="provider"` (high-risk code, regression owner promotion, security changes), AGY models cannot review AGY work. Escalates to `claude/default/opus` or `codex/default/gpt-5.6-sol`. |
| **`repeated_same_failure`** | `UNSUPPORTED` | `HETEROGENEOUS_ESCALATION` | Identical normalized failure signature on retry without strategy change. Blind rotation to another AGY account is blocked; escalates to paid baseline. |
| **`peak_quota_exhaustion`** | `UNCERTAIN` | `HETEROGENEOUS_ESCALATION` | When all 3 AGY accounts are in cooldown or exhausted, requests dynamically spill over to paid heterogeneous baselines. |

---

## 4. Same-Failure Retry & Escalation Policy

### 4.1 The Blind Rotation Anti-Pattern
In naive multi-account systems, when `agy-1` fails to produce a working patch or solve a logical riddle, the system rotates to `agy-2` with the exact same prompt and context. Because both accounts run the same underlying model weights and context, `agy-2` almost invariably produces the identical error, consuming free quota without progress.

### 4.2 Normalized Failure Signatures
DevOrchestrator extracts a normalized `FailureSignature` from every failed run:
$$\text{Signature} = \text{SHA256}(\text{category} \mathbin{\Vert} \text{error\_class} \mathbin{\Vert} \text{root\_cause\_hint})[:16]$$

### 4.3 Escalation Rules
1. **Identical Signature + Unchanged Strategy**:
   - If the previous attempt on AGY failed with signature $S$, and the retry request presents the same signature $S$ with `strategy_changed=False`, the policy **blocks all AGY accounts**.
   - The request immediately escalates to `RoutingTier.HETEROGENEOUS_ESCALATION` (default: `codex/default/gpt-5.6-sol` or `claude/default/opus`).
2. **Strategy Changed (e.g. Failure Memory Injection)**:
   - If the orchestrator alters the strategy (such as injecting a harvested failure memory rule, narrowing the scope, or updating hints), `strategy_changed=True`.
   - A second AGY attempt is permitted on an alternate AGY account.
   - If the second attempt fails, heterogeneous escalation is mandatory. Under no circumstances may an unbounded loop occur (maximum 2 remediation rounds per workflow policy).

---

## 5. Reviewer Independence Contract

Per `docs/development-workflow.md`:
> "Put deep correctness review after implementation. Technical Review findings normally go directly to remediation and regression, not back to full planning."

To guarantee that the cost savings of AGY do not compromise verification rigor:
1. **Worker Provider Tracking**: The execution lineage carries the exact provider, account, and resource ID of the worker.
2. **Gated Review Dispatch**:
   - If `request.independence == "provider"` and worker was `agy`: AGY is eliminated from candidate evaluation. An external model (`claude/default/opus` or `codex/default/gpt-5.6-sol`) is required.
   - If `request.independence == "account"`: The specific AGY account used by the worker is excluded, but idle alternate AGY accounts remain eligible.
   - Candidate promotion and regression owner commits always require independent review acceptance.

---

## 6. Empirical Benchmark Replay Results

The representative benchmark suite (`AGYBenchmarkReplayRunner`) executes 5 canonical DevO tasks across all primary role classes:
1. `replay_planner_arch`: Architecture boundaries and module ownership planning.
2. `replay_worker_cache`: LRUCache `get_or_set` implementation and edge-case handling.
3. `replay_debugger_root_cause`: Multi-layer currency handling defect root cause diagnosis.
4. `replay_evidence_packaging`: Incident packet synthesis and forensic telemetry packaging.
5. `replay_reviewer_compat`: Technical API compatibility review under strict independence criteria.

### 6.1 Matched Comparison: AGY-First vs Non-AGY Baseline

| Metric | AGY-First Pool | Non-AGY Baseline (Paid) | Variance / Delta |
| :--- | :--- | :--- | :--- |
| **Total Tasks** | 5 | 5 | 0 |
| **Coverage (Completed & Accepted)** | 100.0% | 100.0% | 0.0% |
| **First-Pass Acceptance Rate** | 100.0% (clean) / 80.0% (fault) | 100.0% | 0.0% / -20.0% |
| **Heterogeneous Escalation Rate** | 0.0% (clean) / 20.0% (fault) | 0.0% | +20.0% on fault |
| **Repeated-Failure Rate** | **0.0%** | **0.0%** | **0.0%** |
| **Median Wall Time** | 0.010 s (mock) | 0.015 s (mock) | -0.005 s |
| **P95 Wall Time** | 0.010 s (mock) | 0.015 s (mock) | -0.005 s |
| **Relative Cost Savings** | **80.0% - 100.0%** | 0.0% (baseline) | **> 80% reduction** |

*Note: In the injected fault scenario (simulating a verification failure on `agy-1`), the same-failure retry barrier prevented blind rotation to `agy-2`, escalated to heterogeneous model `codex/default/gpt-5.6-sol`, and achieved 100% final acceptance with 0% repeated failure.*

---

## 7. CLI Reference & Operational Commands

### 7.1 Inspect Pool Telemetry
```powershell
python -m dev_orchestrator.cli pool-status
```
Outputs JSON snapshot of total accounts, available accounts, cooldown states, and window usage counters.

### 7.2 Run Benchmark Replay
```powershell
python -m dev_orchestrator.cli agy-benchmark
# Or with injected failure on agy-1 to verify heterogeneous escalation:
python -m dev_orchestrator.cli agy-benchmark --simulate-failure replay_worker_cache
```

### 7.3 Evaluate Routing Recommendation
```powershell
# Worker task routing (AGY-first)
python -m dev_orchestrator.cli agy-route --role worker

# Reviewer with provider independence from AGY worker (routes to Claude/Codex)
python -m dev_orchestrator.cli agy-route --role reviewer --independence provider --worker-provider agy --worker-account agy-1

# Retry after failure with same signature (routes to Heterogeneous Escalation)
python -m dev_orchestrator.cli agy-route --role worker --failure-signature 4b825dc9 --failed-provider agy
```
