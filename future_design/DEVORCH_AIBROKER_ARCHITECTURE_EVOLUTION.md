# DevOrchestrator + AIBroker Architecture Evolution

Status: **FUTURE DESIGN / NON-BINDING**  
Version: **V1.1 draft - V2 resource/gateway update**
Date: 2026-09-17

## 1. Purpose

Evolve the current DevOrchestrator (DevO) + AIResourceBroker (AIBroker) system into a durable AI engineering control plane that can run multiple long-lived software projects with bounded unattended execution, explicit authority, recoverable state, measurable cost, and interchangeable agent runtimes.

This document does **not** authorize implementation and does not replace accepted P11/P12/P12.6 contracts. It defines a future target architecture and migration sequence.

## 2. Frozen principles

1. **DevO is the only lifecycle authority.** It owns project/task/stage state, plan freeze, transitions, review disposition, remediation, OWNER_GATE, STOP and completion.
2. **AIBroker is the only AI resource-policy authority.** It owns provider/account/model/resource selection, quota/cost policy, session allocation, resource leases, exact invocation and provider telemetry.
3. **Execution transports are adapters, not authorities.** Claude CLI, Codex CLI, AGY, DSH, RDC, ChatGPT Web, local models and future runtimes cannot directly advance lifecycle state.
4. **Model/session context is never authoritative project state.** Durable DevO records must be sufficient to reconstruct the next invocation after model, session or machine loss.
5. **Evidence outranks fluent summaries.** Reviews and recovery decisions consume immutable or content-addressed evidence wherever practical.
6. **No nested orchestrators.** External agent frameworks may implement bounded execution primitives behind an adapter, but may not own planning/remediation/task advancement for DevO-managed projects.
7. **Optimize cost only after capability and safety constraints pass.** A cheaper resource cannot bypass required model capability, reviewer independence, deadline or safety requirements.

## 3. Target architecture

```text
Human clients: ChatGPT / 8770 Web / Android / CLI / optional Telegram
                              |
                              v
+------------------------------------------------------------------+
| DevOrchestrator CONTROL PLANE                                    |
| ProjectActor | Task DAG | PlanFreeze | Transition | OWNER_GATE   |
| TaskSpec | ReviewContract | Event Journal | Snapshot | Watchdog   |
+-------------------------------+----------------------------------+
                                |
                                v
+------------------------------------------------------------------+
| AIBroker RESOURCE / POLICY PLANE                                 |
| Capability filter | account/model | lease | quota | dynamic cost |
| scarcity | deadline | context affinity | circuit breaker         |
+------------------+-----------------------------+-----------------+
                   |                             |
                   v                             v
       +-----------------------+      +--------------------------+
       | EXECUTION PLANE       |      | COMMUNICATION PLANE      |
       | native CLI adapters   |      | peer/session identity    |
       | Web/RDC compatibility |      | ask/ack/correlation      |
       | optional OMA/Pi       |      | heartbeat/event delivery |
       +-----------+-----------+      +-------------+------------+
                   +-------------------+-------------+
                                       v
                         EVIDENCE / OBSERVATION STORE
                  logs | diffs | tests | receipts | archives
```

## 4. Control-plane changes

### 4.1 Explicit plan freeze

Use a closed lifecycle around design authority:

`PLAN_DRAFT -> PLAN_REVIEW -> PLAN_REMEDIATE -> PLAN_FROZEN -> EXECUTE -> VERIFY -> REMEDIATE -> DONE`

After `PLAN_FROZEN`, an implementation reviewer may not reopen architecture because it prefers another design. Reopening requires new blocking evidence such as an impossible requirement, unavailable dependency, security contradiction, or failed executable assumption. Reopen requests are themselves durable DevO events.

### 4.2 Standard task contracts

Introduce versioned `TaskSpec`, `ExecutionResult` and `ReviewResult` schemas. `TaskSpec` carries project/task identity, role, goal, constraints, allowed scope, acceptance criteria, required validation, permissions, context references and deadline policy. `ExecutionResult` reports exact execution/resource/session identity, repository evidence, tests, output handles and blockers. `ReviewResult` binds findings to frozen acceptance criteria and evidence references.

Prompts become a rendering of these contracts rather than the primary protocol.

### 4.3 Project Actor isolation

Represent each active project as a single-writer `ProjectActor` with its own inbox, lifecycle projection, task graph, session bindings and recovery fence. Projects share AIBroker resources but not project-state locks. One stalled RDC/provider/session path must not block unrelated project actors.

### 4.4 Durable event journal + snapshots

Keep current projected state for simple reads, but add an append-only event journal for significant orchestration facts. Periodic snapshots accelerate restart. Do not require a full CQRS/event-sourcing rewrite initially. Events must be idempotent, correlation-scoped and sufficient to explain every transition, recovery and owner gate.

## 5. Context and evidence efficiency

### 5.1 Context Capsule

Build each invocation from a deterministic bounded capsule: frozen project context, current TaskSpec, plan version, repository identity, recent meaningful events, known failed attempts, relevant evidence handles and session affinity. Persistent sessions are an optimization; the capsule is the recovery contract.

### 5.2 Observation handles (SoL-Pi pattern)

Do not replay large logs, file dumps or tool results on every model turn. Archive eligible observations under a task/session evidence root, keep a stable handle plus bounded head/tail summary in active context, and support exact paged recall. This adapts NVIDIA SoL-Pi's ObservationPack principle without coupling DevO to Pi.

### 5.3 Evidence-preserving reduction

For long build/test/diagnostic logs, allow a cheaper model or deterministic reducer to produce a compact receipt only when every quoted/structured evidence item can be validated against the archived source. Failure to validate leaves the original evidence path active. Summaries never replace source evidence.

### 5.4 Semantic compaction boundaries

Consider context compaction when a subtask or lifecycle phase completes, not merely when the context window is nearly full. Compact only when projected future replay savings justify rewrite cost and recovery evidence remains reachable. This adapts SoL-Pi's Online Context Compact idea.

### 5.5 Action fusion

Allow safe local fusion of predictable action pairs such as `edit -> targeted validation` where no model decision is required between them. The fused operation must preserve both action evidence and may not hide validation failure. This targets avoidable model round trips, not lifecycle transitions.

## 6. AIBroker resource-model evolution

Split resources into `SubscriptionResource`, `MeteredResource` and `FreeQuotaResource` while keeping a common capability surface. Resource class describes the charging/quota semantics of an exact execution route; it is not the same thing as a provider or harness.

`SubscriptionResource` examples: Claude CLI subscription, Codex CLI via ChatGPT login, Gemini/AGY account, Copilot subscription. Track login/health, model availability, reset windows, remaining quota when observable, cooldown, concurrency and session affinity.

`MeteredResource` examples: DeepSeek API, paid OpenRouter routes and future pay-per-token providers. Track current tariff, currency, input/output/cache rates, provider timezone, effective date, rate limits and balance when observable.

`FreeQuotaResource` examples: OpenRouter models/routes currently exposed at zero input/output price, including explicit `:free` endpoints where available. Zero monetary price does **not** imply unlimited capacity or equivalent privacy. Track model/route identity, request/token/rate limits when observable, reset windows, availability volatility, context/tool capabilities, provider data policy, and whether the underlying provider/model is auditable after routing. Mutable limits must come from provider telemetry/configuration rather than being hard-coded into DevO.

OpenRouter is modeled as a **model gateway/provider adapter**, not as an agent harness. OpenCode is modeled as an optional **execution/harness adapter** that may consume OpenRouter or other model providers. AIBroker remains the resource-policy authority above both.

For low-risk implementation work, AIBroker may expose a `free_preferred` / CheapWorker execution profile that prefers eligible `FreeQuotaResource` routes after capability, privacy, safety and deadline filters pass. Architecture, planning and independent review must not be downgraded merely because a free route exists.

### 6.1 Dynamic effective-cost policy

Routing must first apply hard filters for capability, reviewer independence, safety, deadline feasibility and resource health. Eligible resources are then scored using explicit policy inputs such as:

`EffectiveCost = MonetaryCost + QuotaScarcity + LatencyCost + FailureRisk + ContextSwitchPenalty`

Do not treat this as a universal scalar truth: weights are policy/configuration and decisions must persist their input facts and scoring version.

### 6.2 Time-varying tariffs and deferred work

Support provider-defined tariff timezones and effective date/version rather than hard-coded local clock rules. Deferrable background tasks may enter a cost-optimized queue when their deadline permits a cheaper execution window. Interactive, dependency-blocking and safety work remains immediate.

### 6.3 Resource leases and circuit breakers

AIBroker grants bounded resource/session leases to executions. Timeout or provider failure releases/fences the lease. Repeated provider failures trip a resource-level circuit breaker/cooldown instead of letting DevO wait indefinitely. AIBroker may select a different eligible resource only under an explicit DevO retry/failover request; it must not create lifecycle transitions itself.

### 6.4 OpenRouter gateway and free-route policy

Add an `OpenRouterAdapter` behind the AIBroker provider boundary. It must support exact-model routes and discovery of currently eligible zero-price routes without encoding a permanent free-model list in source. Provider/model availability, limits and price are mutable facts and must be timestamped telemetry or configuration inputs.

Automatic routers such as `openrouter/free` may be used only for explicitly low-risk, disposable CheapWorker work where model identity variance is acceptable. For frozen-plan implementation, architecture, planning, independent review, security-sensitive work or any task requiring reproducibility, AIBroker should prefer an exact model/route and persist the returned model/provider identity. If the actual underlying route cannot be audited, the execution is ineligible for those roles.

Private-source eligibility is a hard filter. A free route is eligible for proprietary repositories only when its effective provider/data-retention policy satisfies the configured project policy; otherwise it is restricted to synthetic/public/disposable tasks. Cost score is evaluated only after this privacy filter.

Registration, API-key presence and account entitlement are operator-owned setup facts. AIBroker may report `UNCONFIGURED`, `AUTH_FAILED`, `RATE_LIMITED`, `EXHAUSTED` and `AVAILABLE`, but must not infer entitlement merely because OpenRouter is installed or an account exists.

## 7. Execution and infrastructure adapters

Define a narrow execution contract such as `submit(TaskSpec)`, `status(handle)`, `cancel(handle)`, `collect(handle)` and `health()`. DevO never branches on provider-specific transport details.

Preferred adapters include native Claude CLI, Codex CLI, AGY/Gemini CLI, OpenCode CLI/harness, local-model adapters and direct API/gateway adapters such as OpenRouter. ChatGPT Web and RDC remain compatibility/bootstrap/recovery adapters rather than the normal coding inner loop. OpenCode may execute a task, but it may not silently become the AIBroker routing authority; OpenRouter may route model API traffic, but it may not become the DevO lifecycle authority.

### 7.1 AI Toolbox boundary

AI Toolbox may be evaluated as an infrastructure/configuration layer for CLI/provider setup, MCP/Skills synchronization, session discovery, usage telemetry and optional gateway transport. It must not become a second resource-policy authority.

Cross-provider/model routing remains AIBroker-owned. If an AI Toolbox gateway is used, gateway retry should be transport-scoped; silent semantic fallback (for example Claude -> Gemini) is disabled unless the selected exact resource remains auditable and AIBroker explicitly authorized the fallback class.

Do not couple DevO to AI Toolbox internal SQLite schemas. Prefer documented API/CLI boundaries and keep native adapters available.

### 7.2 Payload by reference

Stop embedding very large prompts/logs into shell command lines or RDC commands. DevO writes versioned bounded task/context/evidence files and passes stable references. This removes command-length, quoting and transport-truncation failure classes and improves replayability.

## 8. Communication plane

Add a transport-neutral message envelope with `message_id`, project/task identity, sender/recipient peer identity, message type, correlation id and payload reference. Initial message types are `SEND`, `ASK`, `ACK`, `EVENT`, `CANCEL` and `HEARTBEAT`.

The communication plane delivers messages but cannot mutate lifecycle state. DevO interprets only validated messages/events through its control plane. A Repowire-style peer/session registry is worth a bounded POC for reliable long-lived coding-session addressing, but DevO retains authoritative project/session binding.

## 9. External framework policy

External frameworks are candidates for bounded adapters or design reuse, not new top-level orchestrators.

- **Open Multi-Agent:** evaluate task-DAG execution/checkpoint primitives behind an execution adapter. Prefer predeclared task execution over an independent coordinator that replans DevO goals.
- **Repowire:** evaluate peer identity, ask/ack correlation, delivery trace and mobile/Telegram observability as communication-plane ideas. Windows/WSL lifecycle cost must be measured before adoption.
- **AG2 / A2A:** treat primarily as an interoperability protocol reference. A future A2A adapter may expose or consume external agents without moving lifecycle authority out of DevO.
- **delegate-team:** optional delegation backend POC only; do not stack its planner/router above AIBroker or DevO.
- **NVIDIA SoL-Pi:** first adopt the four narrow efficiency ideas at the DevO harness boundary. Direct SoL-Pi dependency is justified only if Pi becomes a supported execution backend and measured benefit exceeds integration cost.

## 10. Disposable execution loops

NVIDIA's SoL-Pi research reports that fixed compiled workflows became brittle at scale and a growing long-lived coordinator accumulated branches/tests until new experiments became expensive. Their later pattern uses a small validated template instantiated per experiment and discarded afterward.

DevO should apply this selectively: keep durable authority/state/policy in the long-lived control plane, but instantiate short-lived, versioned execution-loop templates for bounded implementation/review/experiment work. A task loop may evolve locally during one run, but only its result/evidence is retained; ad-hoc orchestration code does not automatically become permanent DevO control logic.

This is not permission for disposable loops to bypass TaskSpec, plan freeze, permissions, resource leases, review contracts or OWNER_GATE.

## 11. Reviewer redesign

Separate architecture review from implementation verification. Before plan freeze, an architecture reviewer may challenge design assumptions. After plan freeze, the default technical reviewer is an evidence checker against frozen acceptance criteria, repository truth, tests and runtime evidence.

A post-freeze reviewer can request exact remediation. It can request plan reopen only with new blocking evidence. Review output must identify the failed criterion, expected state, observed state and evidence reference rather than merely propose a preferred redesign.

## 12. Watchdog and recovery evolution

Extend the accepted watchdog from elapsed-time detection toward evidence-based progress: heartbeat, provider execution state, repository activity, tool/test milestones and expected-long-operation declarations. Recovery remains staged and budgeted: probe -> adapter/session recovery -> exact eligible re-dispatch/failover -> OWNER_GATE.

Never infer progress from UI activity alone. Never allow automatic recovery to create an unbounded retry loop or generic task replay after ambiguous provider work.

## 13. Client/API direction

Port 8770 remains the stable operator/control surface. Web, ChatGPT integration, CLI, Android and optional messaging clients are stateless clients of the same authority. Add an SSE/WebSocket event stream only as a projection of durable DevO events; reconnecting clients reconcile from authoritative state.

P13 starts with a conversation-session binding UX before the larger Android client. The user-facing concept is project_session_binding: which DevO project the current ChatGPT conversation is operating on. It is distinct from AIBroker resource_binding, which selects or locates an AI/provider session such as chatgpt/default/sol. A project-session binding must never decide which model/provider executes a role.

The preferred ChatGPT UX is an explicit visible command such as /project DevO. The Web client resolves the current browser conversation binding locally and calls the existing 8770 bind/rebind authority. New-conversation migration should therefore be a safe rebind operation rather than a shell/UUID workflow. Persistent page state should expose BOUND/UNBOUND/STALE/conflict explicitly. Project/task/review/handoff state remains durable in DevO/Harness, so changing a ChatGPT conversation changes only the transport/control session, not project authority or execution context.

P13 Android should consume this API rather than duplicate lifecycle logic. Before building a large native application, validate whether the stable API plus lightweight notification/Telegram integration covers most remote observation and OWNER_GATE needs.

## 14. Migration phases

**Phase A - Authority hardening (P0):** formalize PlanFreeze/reopen evidence, TaskSpec/ExecutionResult/ReviewResult and bounded post-freeze reviewer semantics.

**Phase B - Persistence (P0):** event journal + snapshots + idempotent correlation model; prove restart/replay without relying on model conversation state.

**Phase C - Runtime boundary (P1):** complete execution-adapter normalization, payload-by-reference and explicit transport capabilities; keep current AIBroker persistent harness compatible.

**Phase D - Resource scheduler (P1):** resource lease, subscription scarcity, metered dynamic tariff, `FreeQuotaResource`, OpenRouter gateway adapter, `free_preferred` CheapWorker policy, privacy/data-policy eligibility, deadline/deferrable policy, circuit breaker and context affinity.

**Phase E - Multi-project isolation (P1):** ProjectActor single-writer queues and cross-project non-blocking acceptance.

**Phase F - Context/evidence efficiency (P1):** observation handles, evidence-preserving reducer, semantic compaction and safe action fusion with A/B measurement.

**Phase G - Communication/API clients (P2):** peer/session messaging, event streaming and P13 mobile/notification clients.

**Phase H - Interoperability experiments (P2/P3):** bounded OMA, Repowire, AI Toolbox, Pi/SoL-Pi and A2A adapters. Promote only measured winners; no framework is a mandatory dependency by default.

**V2 inclusion decision:** OpenRouter free-route support is a planned V2 Phase-D feature, not merely a future experiment. Minimum V2 scope is: configure/authenticate an OpenRouter account/API key; discover/refresh free-route eligibility; represent free quota/limits/health separately from paid routes; apply project privacy filters; route eligible low-risk worker tasks with an auditable decision record; and fall back only through an explicit DevO retry/failover decision. OpenCode support remains an execution-adapter choice and is not required to obtain OpenRouter support.

## 15. Required measurements before implementation promotion

Future changes should be promoted only with baseline and paired evidence where practical. At minimum measure: end-to-end task completion rate; owner interventions per task; plan-review rounds; provider/model switches; context bytes/tokens replayed; cache/session continuity; model turns; AI monetary cost; subscription scarcity consumption; free-route requests/token throughput and exhaustion events; free-vs-paid worker completion/verification quality; stall/recovery count; duplicate execution count; and cross-project blocking time.

Efficiency changes require a capability floor. Token/cost reductions do not count as improvements if completion, verification coverage or evidence quality falls below the predeclared tolerance. This follows the central validation discipline demonstrated by SoL-Pi rather than adopting its benchmark numbers as DevO expectations.

## 16. Principal risks and failure conditions

1. **Dual authority:** any gateway/framework that silently reroutes models or advances work can invalidate audit and lifecycle safety.
2. **Over-engineering:** implementing every external framework feature would recreate a growing coordinator; prefer minimal contracts and disposable bounded loops.
3. **False context compression:** lossy summaries can hide evidence; source handles and exact recall are mandatory for critical facts.
4. **Quota inference:** subscription limits are often only partially observable; unknown must remain explicit and must not be fabricated from elapsed time.
5. **Cost-policy instability:** provider prices and windows change; tariff data needs source/effective version and editable configuration.
6. **Recovery duplication:** session loss plus ambiguous provider work can create duplicate edits; exact execution/repository evidence and fencing remain mandatory.
7. **Windows/WSL complexity:** Repowire/Pi/Unix-oriented runtimes may add more operational failure surface than they remove on ZXZ-PC.
8. **Security/log retention:** gateways and observation archives may persist prompts, source and credentials; retention, redaction and filesystem permissions require explicit policy.
9. **Free-route volatility:** zero-price models can change availability, rate limits, capabilities or upstream provider without notice. Free resources are opportunistic capacity, never a correctness dependency.
10. **Free-route data policy:** zero monetary cost can trade against retention/training/privacy terms. Proprietary source must be blocked unless the effective route policy satisfies the project's configured data policy.

## 17. Immediate low-cost validation spikes

- `EXP-SOLPI-01`: implement a DevO-local prototype of observation handles on synthetic large tool output; compare replay bytes/tokens and exact-recall correctness without installing Pi.
- `EXP-AITOOLBOX-01`: ZXZ-PC isolated install; validate Claude/Codex official-account discovery, usage/config surfaces and gateway audit behavior with semantic fallback disabled.
- `EXP-OMA-01`: disposable repo; execute a predeclared task DAG with checkpoint/resume while disabling independent goal replanning.
- `EXP-REPOWIRE-01`: Mac first; two real coding sessions, ask/ack, reconnect and delivery trace; measure whether it materially improves session addressing.
- `EXP-A2A-01`: define only a small interoperability contract; no AG2 runtime dependency.
- `EXP-OPENROUTER-01`: isolated disposable repo; connect one OpenRouter account, enumerate current free routes, run fixed read/edit/test/remediate tasks against one exact free model and the automatic free router, record actual model/provider metadata, rate-limit/exhaustion behavior, tool-call compatibility, and verify that privacy-policy filtering can make an otherwise free route ineligible. Compare against one paid/known worker baseline before enabling `free_preferred`.

No spike may mutate the production DevO scheduler or become a required dependency until its evidence is reviewed.

## 18. External references considered

- NVIDIA NVLabs SoL-Pi: https://github.com/NVlabs/SoL-Pi
- SoL-Pi research page: https://nvlabs.github.io/SoL-Pi/
- AI Toolbox: https://github.com/coulsontl/ai-toolbox
- OpenRouter documentation/model routing: https://openrouter.ai/docs
- OpenRouter free-model collection: https://openrouter.ai/collections/free-models
- OpenCode provider/runtime documentation: https://opencode.ai/docs/

The target architecture intentionally remains framework-neutral. External projects are evidence and implementation options, not ownership authorities.
