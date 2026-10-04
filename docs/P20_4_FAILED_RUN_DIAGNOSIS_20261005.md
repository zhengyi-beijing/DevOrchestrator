# P20.4 失败运行诊断 — 2026-10-05

结论：P20 尚未完成。原始 run `p20-live-20261004-a` 已 FAILED，canonical P20.4 为 `OWNER_GATE / SOAK_EVIDENCE_UNRESOLVED`。直接退出原因是校时被误判为重启；更早发生的交接、重复启动、失败升级与证据覆盖缺陷，分别足以阻止验收。诊断没有启动替代运行、恢复作业、清除 gate、编辑原始计时或修补被测 primary。

证据见 [结构化现场与只读重放](C:/work/github/DevOrchestrator-dev/docs/evidence/P20_4_DIAGNOSIS_SOURCES_20261005.json) 和 [Windows 校时事件](C:/work/github/DevOrchestrator-dev/docs/evidence/P20_4_DIAGNOSIS_WINDOWS_CLOCK_20261005.json)。现场文件包含各输入的 SHA-256、角色/dispatch 身份、staged spec 字节、故障案例状态与纯函数重放结果。以下时间均为北京时间；原始证据使用 UTC。

## 事件链

| 时间 | 实际事件 | 后果 |
| --- | --- | --- |
| 10-04 21:37:21 | 原 supervisor PID 36400 打开唯一 run/segment | 正式测量开始，源码为 `6e7ddd4` |
| 21:48:55 | P204.1 原 Worker 完成 | 等待独立 Technical Review |
| 21:48:57 | 原 Technical Reviewer 启动 | 有尚未完成的审查义务 |
| 21:48:58 | `auto-ready:P204.1:c73688ddfca9` 再次启动同任务 Worker | 没有新的 owner start、remediation 或 retry lineage |
| 21:50:09 | 原审查 ACCEPT/NEXT | 软件实现通过审查；交接尚未完成 |
| 21:51:21 | 第二次 Worker 以认证失败结束 | 同一 AGY-1/Gemini 资源此前成功、此次要求登录 |
| 21:51:25 | 正常 NEXT_TASK actuation 拒绝 CRLF staged metadata | 无 handoff；后续四个任务没有启动 |
| 21:37–次日 02:38 左右 | fault driver 顺序观察 15 个案例，每例有限 1200 秒 | 9 个案例留有 injected effect，全部没有已验证 disposition |
| 10-05 03:56:48 | primary 观测文件超过 64 MiB | 之后每 tick 的观测写入失败，直到 run 结束缺失约 58 分钟 |
| 04:55:17 | Windows 系统时间前调约 1.705 秒 | supervisor 记录 `system_boot_discontinuity`，退出码 4 |
| 04:55:20 | canonical 正常绑定协调器看到 owned job FAILED | 稳定进入 OWNER_GATE，无 canonical active Worker |

## 已证实的诊断

### D01 — BLOCKING：把墙钟估算值当作启动身份

`timeline._current_boot_id()` 使用 `psutil.boot_time()` 字符串。Windows 的该值是估算时间，随系统校时变化；它不是独立于墙钟的启动身份。Windows Kernel-General Event 1 在原进程退出同一秒记录 `svchost.exe` 前调 1705 ms。Win32_OperatingSystem 的 LastBootUpTime 仍是 10-02 23:11:42，本窗口没有 Kernel-General Event 12。故没有证据表明电脑重启。

原 segment 的单调差值为 26273.938 秒，墙钟差值为 26275.647229 秒，差额 1.709229 秒与校时相符。对同一只读 run 做纯函数重放：固定原 boot 值时 continuity=true；只将 boot 估算值加 1.705104 秒时立即得到 `system_boot_discontinuity`。未编辑 run，也未把重放结果作为验收通过证据。

`3fa462f` 的 Windows 1 秒精度兼容修复只能处理小的跨进程估算差异，无法解决本次真实校时；原 supervisor 保留 `6e7ddd4` 导入代码，也没有在线替换。修复方向应是独立于墙钟的系统启动身份，加原进程 PID/create-time 与单调计时证明；真实重启、进程替换、单调回退仍应 fail closed。不能仅放宽容差、修改开始时间或重标 FAILED 为成功。

定位：[timeline.py](C:/work/github/DevOrchestrator-dev/src/dev_orchestrator/soak/timeline.py:15)。

### D02 — BLOCKING：生成模板不满足原生 staged spec 字节契约

所有五个原始 staged specs 仍含 CRLF。Windows `Path.write_text()` 的默认换行转换，以及缺少 LF Git 属性，造成文件在字节层面违反原生 LF-only 解析契约。Planner 能完成设计、Worker 能实现、Reviewer 能 ACCEPT，并不意味着 successor metadata 可以解析。

真实 actuation 明确拒绝：`ROADMAP_SUCCESSOR_INCONSISTENT / STAGED_SUCCESSOR_CLAIM_UNUSABLE / spec contains CRLF/CR bytes`。`3fa462f` 已为新模板和案例写入增加 LF 与 `.gitattributes`，并有真实 RED/GREEN 与 checkout 验证；原被测项目未被人工修补，历史拒绝仍保留。

进入下一次真实测量之前，需要用原生 successor resolver 验证整条生成 roadmap，并覆盖 Planner 冻结、Git checkout、successor activation 后的实际字节。现有 1/5 是 verifier 对实现及 Review ACCEPT 的计数，不能等同于任务已完成正常交接。

### D03 — BLOCKING：审查未结束时允许同任务 static ready 再启动

首次 Worker 锚定 HEAD `05ec31a…`；提交后 HEAD 为 `c73688d…`。`_advance_unlaunched_ready()` 使用当前 HEAD 判断是否 already_run，新 HEAD 不匹配首次执行的 launch HEAD。`_active_project()` 仅看 launching/running Worker，未把 pending review、active Reviewer 或 accepted-but-unapplied decision 当作启动屏障；launch guard 也未拒绝 REVIEWING。

真实 ledger 显示 Reviewer 在 21:48:57 启动，第二次同任务 Worker 在 21:48:58 建立 launching/running 记录。Broker 为第二次请求建立了真实 dispatch/execution，最终认证失败。因此这是一次真实的、无新授权 lineage 的重复任务启动。Broker dispatch 的串行执行意味着这里不声称两次 provider Worker 同时执行；问题是 task/role ownership 与启动许可被绕过。

应按 task/source lineage 和未结审查义务进行 launch fencing。HEAD 改变本身不能构成再次执行同一已完成实现的授权；合法 remediation/retry 保留其显式 lineage。

定位：[static ready 启动](C:/work/github/DevOrchestrator-dev/src/dev_orchestrator/core/transition_executor.py:5226)、[生命周期 launch guard](C:/work/github/DevOrchestrator-dev/src/dev_orchestrator/core/transition_executor.py:1698)。

### D04 — BLOCKING：Planner 失败被 authority 投影隐藏；局部 gate 没有统一到 authority

15 个案例中，Planner 为 failed 或 recovery_required，但 authoritative lifecycle 均留在 PENDING_DESIGN；其 authority gate、Watchdog gate 和 recovery attempts 均为空。

对 F01 真实 ledger 的只读重放证实：角色投影为 PLAN_FAILED；随后 `overlay_lifecycle_authority()` 覆盖成 PENDING_DESIGN；`evaluate_stall()` 返回 monitored=false、breached=false。PENDING_DESIGN 不在 Watchdog 的 active lifecycle states 中，因此时间再久也不会触发这条恢复路径。`_sync_planner_terminals()` 只把 control history 变为 blocked，没有转成 authoritative recovery/gate。

primary 的 blocked review obligation 则暴露另一分支：authority 只有在 `repo_task != owner_task` 时才为 blocked obligation 升级 gate。这里二者都是 P204.1，authority 继续 REVIEWING，只有 Watchdog 里有 NEXT_TASK_WITHOUT_HANDOFF gate。fault verifier 要求持久 authoritative gate，不能把局部日志或 control blocked 伪装为通过。

应让当前 task 的 terminal role failure 通过唯一 lifecycle authority 进入有界 recovery 或稳定 OWNER_GATE；同时覆盖相同 task 的 blocked review obligation。保留 P20.3 对历史旧 task 的防误触机制。

定位：[投影覆盖](C:/work/github/DevOrchestrator-dev/src/dev_orchestrator/core/transition_executor.py:1134)、[同 task gate 缺口](C:/work/github/DevOrchestrator-dev/src/dev_orchestrator/core/transition_executor.py:991)、[control terminal 同步](C:/work/github/DevOrchestrator-dev/src/dev_orchestrator/core/control_commands.py:382)。

### D05 — BLOCKING：资源条件与 failover 没有形成可用的 unattended 执行路径

真实 provider 错误包括 AGY Claude 429 周配额耗尽、AGY-1 Gemini 要求认证，以及 no eligible resource。它们不是单纯由时钟导致。

F01 Plan Reviewer 已失败两个同 provider/model 账号后，`same_pm_failures >= 2` 直接结束整个 review；该轮 Broker decision 的候选仍把 AGY-1 Claude 标为 eligible，尚未尝试。eligible 是 Broker 当时的候选判断，不保证该账号实际能够成功；但“两账号失败等于全部账号耗尽”的理由并不成立。

F02 的 1 秒真实 timeout 后，Planner failover 请求把所有配置内可执行 planner 资源都排除了，实际 Broker decision 为 no eligible resource。需要区分请求的高质量/角色约束、失败资源排除和真实账号可用性，保持有限尝试预算，并在无合法资源时落到明确 authority gate。认证本身是否需要 owner 登录，需在后续恢复前核验；本次诊断没有发新 AI 请求、改账号配置、探测消耗配额或降低 reviewer independence。

定位：[同模型账号提前耗尽](C:/work/github/DevOrchestrator-dev/src/dev_orchestrator/core/ai_planner.py:1716)。具体候选/decision IDs 见结构化证据。

### D06 — BLOCKING：证据容量和指标定义无法证明 hard-zero

primary normal observations 文件为 67125464 bytes，超过固定 67108864-byte 上限；最后一条为 03:56:48。之后 daemon summary 持续含 `_post_tick_observation_error=ValueError`，但 daemon.json 的 last_error 仍为 null。直到失败结束约 3508.98 秒都没有正常观测。

此外，21:48:51–21:51:25 存在 153.899538 秒空档，与 Review/第二次 Worker 的时间窗口重叠。空档已证实，但没有当时 tick 堆栈或阶段耗时证据，不能确定其具体阻塞点。Broker dispatch 在全局锁内执行会串行排队，不过 GET dispatch status 不使用该锁；因此不能直接断言该锁就是观测空档的根因。

`duplicate_execution_count` 只统计采样时 state=running、同 source_request_id/task 下的多个 ledger entry。不同 source 的同 task 再启动，或者后来已经结束的非法启动，都会漏计。这轮返回的 duplicate_worker=0 不等于从未发生重复启动。UNKNOWN 覆盖不能当零。

修复需精简固定 schema 观测，按有界分段与可验证索引保留全窗口证据，容量耗尽升级到可见 authority gate；指标按 durable launch/role intervals 与合法 retry/remediation lineage计算。独立验收前补充这些负面场景。

定位：[完整 Watchdog 快照与容量检查](C:/work/github/DevOrchestrator-dev/src/dev_orchestrator/incidents/observations.py:46)、[重复执行计数](C:/work/github/DevOrchestrator-dev/src/dev_orchestrator/incidents/reliability_telemetry.py:320)。

### D07 — BLOCKING：案例超时及监督失败路径遗留进程

15 个 case daemon 都在原 supervisor 退出后仍然存活，PID 与其记录的启动时间相符。primary daemon 也仍运行。它们继续 tick 不代表 soak 验收继续。

`_case()` 的 1200 秒超时直接 return None；只有 `_finish()` 在成功形成 disposition 时 stop-daemon。超时/异常缺少 finally 清理，原 supervisor 失败也没有清理这些后台子项目。需要把 case 进程纳入 durable ownership，成功、超时、异常、父作业退出均使用校验 PID/create-time 的正常 shutdown 路径，保留全部 ledger 和原始证据。本次诊断未停止或重启它们。

定位：[案例超时路径](C:/work/github/DevOrchestrator-dev/src/dev_orchestrator/soak/live_faults.py:92)。

## 修复和恢复顺序

1. 先修 D03/D04，验证 pending review、accepted decision、同 task blocked obligation、当前 Planner terminal failure 均不能丢失 authority 或再次 static launch；历史 task 不得误 gate 新 task。
2. 修 D01/D06，证明正常校时不会变成 reboot，真实重启仍拒绝；全窗口观测能够覆盖至少 12 小时，缺失证据有可见失败；负面启动历史不会被零值指标隐藏。
3. 完成 D02 原生多任务端到端预检与 D07 的有限、幂等进程清理。重放先在纯软件临时 fixture 中进行，不把 replay 当 live acceptance。
4. 核实 D05 的合法高质量 Planner/独立 Reviewer/Worker 资源和认证；恢复前确认能够覆盖五任务及十五案例，所有 fallback 保持有限预算。
5. 完成 focused regression 和独立 Technical Review，再通过正常 owner/control/job API 决定恢复。原 FAILED run 不能恢复成成功计时：保留其身份、失败记录和 source lineage，任何后续独立运行都必须明确记录新的运行边界，不能复用、拼接或重置旧时钟来声称验收。

本轮产出是诊断与证据，不是实施或最终验收。没有 push、runtime authority 改写、Broker 配置改动、替代 soak 或 P21。现有 92-test GREEN 和 10/10 CPF deterministic PASS 仍有效，但不足以覆盖以上 live 缺陷。
