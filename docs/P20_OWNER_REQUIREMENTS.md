你正在 ZXZ-PC 上继续 DevOrchestrator 项目。

Canonical repository:
C:\work\github\DevOrchestrator-dev

本轮 owner decision：

正式启动并完成：

P20 — Unattended Execution Reliability & Self-Recovery

这是 P19 系列完成后的下一主线。
Owner 已明确批准把 P20 作为 P19.2 之后的新 roadmap successor。

你的目标不是“尽量实现 P20”，而是：

设计 → 实现 → 测试 → fault injection → live unattended soak →
Technical Review → remediation → acceptance → authoritative closeout

只有达到下面定义的 acceptance gate，P20 才允许 COMPLETE。

不要创建 P21。
P20 完成后 successor 保持 TBD / owner decision required。

============================================================
0. FIRST: 恢复真实现场，不信任历史摘要
============================================================

首先检查实际状态：

- git status
- git log -8
- daemon status
- project-status
- authoritative lifecycle
- watchdog
- active execution
- active AI roles
- owner control / paused state
- agent/CURRENT.md
- agent/HANDOFF.md
- agent/result.md
- agent/next.md
- agent/staged/roadmap.json
- docs/backlog.md
- P14.6 / P16.10 / P16.11 相关历史设计
- 当前 tests / runtime evidence

已知最近状态仅作为参考：

- P19.2 Remote MCP Interface 已 authoritative COMPLETE
- P19.2 Technical Review = ACCEPT
- P19.2 restart acceptance = PASS
- P19.3 read-only Browser Control Bridge 已完成
- lifecycle historical lineage 已修复
- daemon 正常运行
- P19.2 没有 successor
- project 可能仍处于 owner pause/freeze
- main 有本地未 push commits
- 不允许 push

如果实际状态与上述明显不一致：
先分析并报告。
不要通过覆盖 runtime 或 reset repo 强行恢复。

============================================================
1. P20 定义
============================================================

正式名称：

P20 — Unattended Execution Reliability & Self-Recovery

核心目标：

证明 DevOrchestrator 可以在没有人工反复输入“继续”的情况下，
连续执行多个 bounded software-development tasks，并能在常见异常、
AI failure、进程丢失、provider failure、daemon restart 和 handoff
异常后自动、安全、可审计地恢复。

对于不能安全自动恢复的情况：

必须稳定进入明确 OWNER_GATE。

绝对不允许：

- silent stall
- infinite retry
- duplicate Worker
- duplicate durable job
- duplicate successor activation
- lifecycle authority drift
- task regression
- fabricated successor
- silently ignoring an unresolved failure
- using RDC as hidden recovery
- weakening safety gates merely to make soak pass

============================================================
2. Roadmap structure
============================================================

把 P20 定义成以下正式 sequence：

P20.1 — Reliability Telemetry & Failure Taxonomy

P20.2 — Automatic Failure Harvesting & Regression Promotion

P20.3 — Deterministic Recovery & Heterogeneous Escalation

P20.4 — Fault-Injection Multi-Task Unattended Soak

P20.5 — Unattended Reliability Acceptance & Closeout

Recommended lifecycle:

P19.2
  -> P20.1
  -> P20.2
  -> P20.3
  -> P20.4
  -> P20.5
  -> TBD

如现有 roadmap schema 更适合一个 P20 umbrella + P20.x children，
可以创建 agent/staged/P20.md 作为 umbrella design document，
但 authoritative executable task sequence 必须清晰唯一。

必须：

- 使用正常 successor/handoff/lifecycle API 激活 successor
- 不直接编辑 runtime/transition-executor.json
- 不伪造 consumed handoff
- 不绕过 terminal lifecycle
- P19.2 -> P20 的 activation 必须留下 durable evidence

Owner 对 P20 successor 的批准就是本提示词。

============================================================
3. P20.1 — Reliability Telemetry & Failure Taxonomy
============================================================

目标：

建立统一、可机器处理、可长期聚合的无人值守可靠性数据模型。

优先复用已有：

- FailureMemory
- ExecutionRecorder
- Watchdog
- lifecycle authority
- transition executor
- AI role state
- transport/job state
- audit/event logs

不要建立第二 lifecycle authority。

定义一个明确的 FailureIncident contract，至少包含：

schema_version
incident_id
project_id
task_id
execution_id / plan_id / review_id where applicable
lifecycle_generation
lifecycle_transition_id
recovery_epoch_id
failure_signature
failure_family
failure_class
stage
role
provider
model
resource/account
first_seen_at
last_seen_at
occurrence_count
evidence
root_cause_hint
previous_attempts
recovery_strategy
recovery_attempt
recovery_result
recovery_latency
terminal_disposition
owner_gate_id if any
regression_candidate_id
related_incident_ids

failure_signature 必须稳定、规范化、去除随机 ID / 时间戳 / 路径噪声。

至少建立以下 failure classes：

- malformed_ai_output
- planner_timeout
- worker_process_lost
- reviewer_rejected
- reviewer_timeout
- provider_quota_exhausted
- provider_unavailable
- aibroker_unavailable
- daemon_restart_during_execution
- daemon_restart_during_handoff
- stale_head
- unexpected_dirty_worktree
- missing_execution_record
- lost_handoff
- duplicate_request
- lifecycle_projection_mismatch
- retry_without_progress
- transport_unavailable
- unknown_requires_owner

正式 telemetry 至少包含：

manual_continue_count
automatic_recovery_count
automatic_recovery_success_count
owner_gate_count
duplicate_execution_count
duplicate_job_count
duplicate_handoff_count
duplicate_successor_activation_count
failure_incident_count
repeated_failure_family_count
recovery_latency_seconds
task_completion_count
review_acceptance_count
daemon_restart_count
lifecycle_invariant_escape_count

“invariant escape”定义为：

错误状态实际影响 execution / successor / authority 后，
才被发现。

被 gate 在执行前拦住的不算 escape。

Acceptance P20.1：

- schema 有明确 contract
- restart-safe
- append/replay/idempotency 行为明确
- bounded storage / retention strategy
- unit tests + restart tests
- 不泄露 token/prompt secret/credential

完成后：
Technical Review。
不允许未经 review 自动进入 P20.2。

============================================================
4. P20.2 — Automatic Failure Harvesting & Regression Promotion
============================================================

目标：

DevO 遇到新失败时，不再只是写日志，而是自动形成：

failure
  -> normalized incident
  -> failure family
  -> recovery result
  -> regression candidate

接入至少：

Planner
Worker
Reviewer
Watchdog
TransitionExecutor
daemon/restart
AIBroker/resource routing
ExecutionTransport
DurableJob

相同 failure signature：

必须聚合同一个 family，
不能每次产生无关联的新问题。

实现 RegressionCandidate contract，至少：

candidate_id
incident/family source
minimal reproduction context
expected invariant
actual result
proposed regression class
replayability
verification_status:
  proposed
  reproducible
  verified
  rejected
  promoted

重要：

不要让运行中的 DevO 自动修改 production test source code。

“promotion”应优先意味着：

incident evidence
  -> bounded replay fixture/corpus
  -> deterministic verification
  -> verified regression candidate

是否把 candidate 写成永久 tracked unit test，
可以由 Worker 在后续 remediation 中完成，
但必须经过 normal review。

必须建立：

runtime failure incident store
runtime regression candidate store
bounded retention / compaction

如果项目已有合适 store，复用它。

P20.2 Acceptance：

- 同一故障多次发生得到一个 family
- restart 后 family identity 不变
- incident 不会因 replay 重复计数
- regression candidate 可重放
- verified/rejected 状态持久
- secret sanitization 有测试
- corrupted incident record fail closed

Technical Review 后才能进入 P20.3。

============================================================
5. P20.3 — Deterministic Recovery & Heterogeneous Escalation
============================================================

目标：

把目前分散在 Watchdog / routing / retry / provider handling 中的规则，
收敛成明确的 Recovery Policy。

不要建立新的 lifecycle state machine。

Recovery Policy 只负责：

given:
  current lifecycle authority
  incident
  failure family
  previous attempts
  resource availability
  policy

return:
  bounded recovery decision

至少支持：

RETRY_SAME_STRATEGY
RETRY_CHANGED_STRATEGY
SWITCH_RESOURCE
HETEROGENEOUS_ESCALATION
RESTART_COMPONENT
RECONCILE_RUNTIME
OWNER_GATE
NO_ACTION_ALREADY_RECOVERED

关键规则：

A. 同 failure signature 不允许盲目：

agy-1 -> agy-2 -> agy-3

使用同模型、同 prompt、同 strategy 无限轮询。

B. 相同 strategy 最多有限次数。

C. 重复 failure 必须发生至少一种变化：

- strategy
- prompt/context
- model
- provider
- repair plan

否则进入 escalation。

D. 高风险 / semantic / architecture / repeated failure：

优先 heterogeneous escalation。

结合已有资源策略：

Luna -> mechanical
Terra -> engineering
Sol -> complex / semantics

AGY pool：
低边际成本资源，
但不能弱化 reviewer independence。

E. Provider-independent reviewer 仍必须保持独立。

F. 不确定是否安全恢复：

OWNER_GATE

而不是“试试看”。

Recovery state 必须 durable：

daemon restart 后不能忘记：

- 已尝试几次
- 试过哪些 strategy
- 哪些 failure signature 已重复
- 是否已 escalation
- 是否已经 owner-gated

Acceptance P20.3：

- deterministic tests
- restart/replay tests
- same failure anti-loop tests
- provider unavailable tests
- quota tests
- reviewer reject tests
- stale HEAD tests
- lost Worker tests
- no duplicate execution
- no duplicate handoff
- no duplicate durable job

Technical Review 后进入 P20.4。

============================================================
6. P20.4 — Fault-Injection Multi-Task Unattended Soak
============================================================

这是 P20 最关键的一步。

不能用“单元测试都过了”代替 soak。

============================================================
6.1 Dedicated soak project
============================================================

不要用 DevOrchestrator 自己的 canonical repo 做破坏性 fault injection。

创建一个隔离的、可重复生成的：

P20 unattended soak project

要求：

- disposable Git repo
- 由 tracked harness/template 创建
- runtime/test artifact 不污染 production repo
- 使用真实 DevO lifecycle
- 使用真实 Planner / Worker / Reviewer pipeline
- 使用真实 AIBroker/resource routing
- 使用真实 durable jobs
- 使用真实 Git commits
- 不使用 mock AI 作为最终 live acceptance

可以使用 mock/fake provider 做 deterministic unit/fault tests，
但 P20.4 live acceptance 必须至少包含真实 AI pipeline。

============================================================
6.2 Real bounded development tasks
============================================================

soak project 中至少连续完成：

>= 5 个真实 bounded software-development tasks

每个 task 应真正包含：

goal
implementation
tests
commit
review
acceptance

例如可以构造一个小型 Python/C++ fixture 项目，
任务类型至少覆盖：

- implement small feature
- fix failing edge case
- refactor without behavior change
- parser/data validation bug
- add bounded API + tests

不要让任务只是：

“写一个文件”
“sleep”
“echo”

必须能真实触发：

Planner -> Worker -> tests -> Reviewer

============================================================
6.3 Fault injection
============================================================

实现一个严格隔离的 fault injection mechanism。

默认 production 必须 OFF。

仅在：

explicit P20 soak mode / test fixture

下启用。

必须无法被远程普通客户端无意开启。

至少注入并验证：

1. malformed Planner/AI output
2. Planner timeout
3. Worker process killed once
4. Reviewer reject
5. provider quota exhausted
6. provider temporarily unavailable
7. AIBroker temporarily unavailable
8. daemon restart while task is active
9. daemon restart around successor handoff
10. stale HEAD
11. unexpected dirty worktree
12. lost/missing execution projection
13. lost handoff/recovery scenario
14. duplicate retry request
15. repeated same failure signature

不要求所有故障都自动恢复。

正确结果只能是：

A. automatic recovery
或
B. explicit correct owner gate

任何 silent stall 都算 FAIL。

============================================================
6.4 Soak duration
============================================================

最终 live soak：

wall-clock continuous unattended runtime >= 6 hours

不要：

- 修改时间戳
- 缩短时间然后声称 6 小时
- mock monotonic clock
- 用单测模拟 6 小时
- 把历史多次测试时间累计成 6 小时

Acceptance evidence 必须有真实：

started_at
ended_at
monotonic/wall duration
daemon restart timestamps
task timeline
incident timeline
recovery timeline

如果当前 Codex 会话无法持续 6 小时：

必须把 soak 作为 durable DevO-owned run 启动，
让它独立于 Codex CLI session 生存。

同时写：

resume/checkpoint/handoff

保证下一次 Codex invocation 可以继续读取该 soak 状态。

绝不能因为会话即将结束而假装 P20.4 COMPLETE。

============================================================
7. P20.5 — Hard Acceptance Gate
============================================================

只有以下所有条件满足才能完成 P20。

Hard acceptance:

continuous unattended runtime:
  >= 6 hours

real bounded tasks completed consecutively:
  >= 5

manual "continue" interventions:
  0

manual lifecycle repair interventions:
  0

unexpected duplicate Worker executions:
  0

duplicate durable jobs caused by replay/restart:
  0

duplicate consumed handoffs:
  0

duplicate successor activations:
  0

wrong-task execution:
  0

fabricated successor:
  0

lifecycle invariant escapes:
  0

supported injected faults:
  100% reach either
    automatic recovery
    OR
    explicit correct owner gate

silent stalls:
  0

infinite retry loops:
  0

restart task regression:
  0

lost lifecycle authority after restart:
  0

every injected/new failure:
  has FailureIncident

every repeated failure:
  associated with existing failure family

every recovery:
  has durable strategy/outcome evidence

every owner gate:
  explains exact reason and required owner decision

最终计算：

automatic_recovery_success_rate
mean_recovery_latency
p95_recovery_latency
incidents_per_task
repeated_failure_rate
review_acceptance_rate

这些指标记录即可，
但上述 hard-zero 项不能靠平均值掩盖。

============================================================
8. Owner gate acceptance semantics
============================================================

非常重要：

P20 的目标不是“100% 所有错误自动修掉”。

以下情况进入 OWNER_GATE 是正确结果：

- 无法唯一判断 successor
- Git history / lifecycle lineage ambiguous
- unsafe dirty worktree
- hardware authorization required
- same semantic failure 已多次 escalation
- conflicting authoritative evidence
- recovery would require weakening security/safety invariant

P20 acceptance 要验证：

owner gate 是：

- 明确的
- durable
- stable after restart
- non-spamming
- one owner
- actionable
- 不会后台继续执行

============================================================
9. Browser / MCP / transport scope
============================================================

P20 不重新设计：

P19.1 Web
P19.2 MCP
P19.3 Browser Bridge
P18 ExecutionTransport

只在可靠性需要时修 bug。

P20 不新增：

- unrestricted shell
- generic arbitrary URL tool
- new lifecycle authority
- new browser protocol
- large Web UI redesign
- mobile native app
- automatic real X-ray/conveyor actuation
- arbitrary hardware control
- new model leaderboard product

P20 的重点只有：

reliability
recovery
failure learning
unattended execution

============================================================
10. Hardware safety
============================================================

DevO 与其他项目可能包含：

X-ray source
conveyor/VFD
detector
serial control
lab hardware

P20 fault injection 和 soak：

绝不能自动触发任何真实硬件动作。

真实硬件操作继续要求已有 explicit human authorization。

Dedicated soak project 必须是纯软件项目。

============================================================
11. Security requirements
============================================================

不得因为 P20 weakening：

- bearer/capability checks
- owner control
- lifecycle authority
- path allowlists
- CORS/origin
- transport policy
- provider independence
- idempotency
- Git clean guards
- hardware authorization

FailureIncident / logs / evidence：

不得保存：

- bearer token
- password
- API key
- full secret-bearing environment
- sensitive prompt credentials

增加 sanitization tests。

============================================================
12. TDD / implementation workflow
============================================================

每个 P20.x 使用：

DESIGN
-> write contract/tests first
-> verify RED
-> implement
-> focused GREEN
-> relevant regression
-> Technical Review
-> remediation if needed
-> local commit
-> authoritative lifecycle transition

不要一次写完所有 P20 再测试。

每阶段都应该留下：

agent/CURRENT.md
agent/result.md
appropriate docs
tests
runtime/evidence where applicable

推荐 commit：

feat(p20.1): add reliability incident telemetry
feat(p20.2): harvest failures into regression candidates
feat(p20.3): add deterministic recovery escalation policy
test(p20.4): add unattended fault-injection soak harness
docs(p20.5): record unattended reliability acceptance

实际 message 可根据实现调整。

============================================================
13. Required regression
============================================================

至少覆盖：

- lifecycle authority
- transition executor
- successor consistency
- invariant workflow
- watchdog
- control commands
- Planner
- Reviewer
- AIBroker
- agent router
- AGY routing
- durable jobs
- transport selector
- daemon restart
- P18/P19 control regressions where touched

每阶段至少：

python compileall
focused pytest
relevant full pytest
git diff --check

最终 P20.5：

尽可能运行完整 tests_py regression。

不得通过：

skip
xfail
扩大 timeout
删除断言
弱化 gate

来隐藏真实失败。

============================================================
14. Technical Review
============================================================

每个阶段必须独立 review。

P20.5 最终 review 应特别检查：

- 是否出现新的第二 lifecycle authority
- failure recovery 是否可能重复执行
- recovery policy 是否 deterministic
- retry 是否能无限循环
- restart 是否会丢失 attempt state
- old incidents 是否会重复 actuation
- AGY blind rotation 是否真正被阻止
- reviewer independence 是否保持
- fault injection 是否完全隔离 production
- soak evidence 是否真实 wall-clock
- manual_continue_count 是否真实为 0
- owner gate 是否 fail closed

Final verdict only:

ACCEPT
or
CHANGES_REQUIRED

CHANGES_REQUIRED 必须 remediation 后再 review。

============================================================
15. Git rules
============================================================

- canonical repo only:
  C:\work\github\DevOrchestrator-dev

- branch:
  main

- 不 reset
- 不 rebase
- 不 clean
- 不 force
- 不 push
- 不修改 origin
- 不删除历史 evidence
- 不删除现有 permission-denied .p193-* dirs
- 不丢失用户已有工作
- 不把 runtime secret 加入 Git

允许：

每个 completed/reviewed P20.x 创建 local commit。

最终 repo 应：

tracked worktree clean

============================================================
16. Runtime safety
============================================================

在修改 daemon/runtime-sensitive code 前：

- 查 active execution
- 查 active owner
- 如有需要，正常 pause
- 等 quiescent
- runtime mutation 前备份关键 ledger

不要在 Worker 正运行时直接重启 daemon，
除非当前测试明确就是 daemon-restart fault injection，
且运行在隔离 soak project。

============================================================
17. P20 closeout
============================================================

只有 P20.5 ACCEPT 后：

- P20 authoritative terminal COMPLETE
- active_owner = null
- active_execution = null
- owner_gate = null
  （除非 soak 最终明确验证的是一个独立 test-project gate，
   不能污染 DevO 自身 authority）
- lifecycle invariants all hold
- watchdog clean
- daemon restart 后仍 terminal
- no P20 rerun
- no duplicate history
- tracked worktree clean

不要创建 P21。

明确记录：

"No successor has been invented.
Roadmap re-baseline requires owner decision."

============================================================
18. If soak is still running
============================================================

如果 6-hour soak 尚未完成：

不要回答“P20 完成”。

更新 durable handoff/status：

P20.4 = RUNNING

并记录：

- soak run ID
- start time
- elapsed time
- completed tasks
- incidents
- recovery results
- current execution
- next scheduled fault
- exact resume command/procedure

然后退出本轮 Codex session。

下一次 Codex 必须从 durable state 恢复，
不得重新从 0 开始 6 小时。

============================================================
19. Final report format
============================================================

只有 P20 全部完成时，最终输出：

P20 RESULT
==========
Overall:
  ACCEPT / CHANGES_REQUIRED

Lifecycle:
  task:
  state:
  generation:
  owner_gate:
  active_owner:
  active_execution:

P20.1:
  result:
  key implementation:
  tests:

P20.2:
  result:
  incidents/families:
  regression candidates:
  tests:

P20.3:
  result:
  recovery policies:
  escalation behavior:
  tests:

P20.4:
  result:
  soak_run_id:
  start:
  end:
  wall_clock_duration:
  real_tasks_completed:
  injected_faults:
  automatic_recoveries:
  owner_gates:
  silent_stalls:
  manual_continue_count:

P20.5 Acceptance:
  continuous_runtime >= 6h:
  completed_tasks >= 5:
  manual_continue = 0:
  duplicate_worker = 0:
  duplicate_job = 0:
  duplicate_handoff = 0:
  duplicate_successor = 0:
  invariant_escape = 0:
  silent_stall = 0:
  infinite_retry = 0:
  restart_regression = 0:
  failure_incident_coverage = 100%:

Recovery metrics:
  automatic_recovery_success_rate:
  mean_recovery_latency:
  p95_recovery_latency:
  incidents_per_task:
  repeated_failure_rate:
  review_acceptance_rate:

Verification:
  focused tests:
  regression tests:
  compile:
  diff check:
  technical review verdict:

Git:
  HEAD:
  branch:
  clean:
  ahead/behind:
  commits created:
  pushed: NO

Successor:
  NONE
  Roadmap re-baseline requires owner decision.

============================================================

现在开始。

先完成现场恢复和 P20 roadmap/design。

不要一上来写大段代码。

先把现有 P14.6 / P16.10 / P16.11 能复用的机制列出来，
明确哪些直接复用、哪些存在缺口，
再冻结 P20.1-P20.5 design，然后按 TDD 顺序执行。

除真正 OWNER decision 外，不要等待人工说“继续”。