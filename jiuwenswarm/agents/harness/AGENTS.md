---
description: 给修改/检视 agents/harness 代码的 AI 助手与工程师：七子树地图、任务规划 / 可靠性 / subagent / 中断交互 / 事件流 / 原生多模态域 specs 索引（S_01~S_22）、公开入口、架构铁律、装配点与配置消费、测试与提交约束
---

# harness：代理执行挂件的多域聚合目录

harness 是 agent 执行链的**挂件层**：rails（before/after 钩子）、tools（模型可调工具）、
prompt（提示词组装）、词汇层函数（供 adapter 调用的纯函数群）。它不含 server 路由，
也不含 agent-core 循环本体——那些在 `server/` 与 `openjiuwen`。

本目录的特殊性在于**多域聚合**：七个子树（claw / code / common / docs / flash / team / work）
横跨任务规划、可靠性、subagent、中断交互、事件流等多个能力域，同一机制常横跨
common / code / flash——**spec 按改哪块机制切，不按目录树切**。归档规约见
[docs/AGENTS.md](docs/AGENTS.md)。

**当前 specs 覆盖任务规划域（S_01~S_06）、可靠性域（S_07~S_10）、subagent 域
（S_11~S_14）、中断交互域（S_15~S_19）、事件流域（S_20~S_21）与原生多模态域
（S_22）**。未归档的域（team /
skill_selection / memory / 观测性 / …）不等于不存在，只等于还没有契约文档；动这些域时
同样遵循"机制横跨多个子树"的找代码方式（subagent 域横跨 openjiuwen SDK 机制主体与
server 装配层；中断交互域的原语层在 agent-core `openjiuwen`、守卫与结算层在 server
runtime——spec 关联模块按仓分列；事件流域的正向通道底层在 agent-core stream 队列、
消费侧在 server runtime），新增 spec 从 S_23 起编（编号跨域连续，不复用）。

修改任何代码前先读对应 spec；改规约本身必须同步 specs（三条强制约束见文末）。

## Specs 索引（任务规划域 S_01~S_06 + 可靠性域 S_07~S_10 + subagent 域 S_11~S_14 + 中断交互域 S_15~S_19 + 事件流域 S_20~S_21 + 原生多模态域 S_22）

- `docs/specs/S_01_todo-storage-and-toolchain.md` — todo.json 整表存储、stock 四工具行为、代际 token、todo_compat monkeypatch 装配时序
- `docs/specs/S_02_task-execution-rail.md` — TaskExecutionRail 全景：状态→事件映射、串行顺序约束、auto_advance / lazy_start、产物检测、代际过滤
- `docs/specs/S_03_todo-close-flow-control.md` — close nudge 与 TodoCloseTextHold 的 hold / drop / release 三帧文本流控
- `docs/specs/S_04_plan-approval.md` — exit_plan_mode 审批门：四态语义、Web/TUI 分叉、模式恢复；含 p76 文本审批未装配遗留的登记
- `docs/specs/S_05_profile-todo-variants.md` — profile 变体（code 四子类 / flash 统一工具）、todo_resume 持久提醒与代际分工、user_todos；含 todo_toolkits 未装配遗留的登记
- `docs/specs/S_06_deepresearch-todo-persistence.md` — deepresearch 四阶段快照的路径校验与原子落盘
- `docs/specs/S_07_llm-call-failure-recovery.md` — LLM 调用层失败恢复：重试 rail 三原因分流 / per-call 预算 / 退避 jitter / 上下文溢出恢复 rail / 连续溢出熔断
- `docs/specs/S_08_tool-loop-circuit-breaking.md` — 工具循环熔断：四检测器先中先断（global / unknown / ping-pong / generic）+ ToolResultErrorDetector 共享错误判定
- `docs/specs/S_09_concurrent-safe-task-planning.md` — 并发安全与计划一致性：注册查表 / binding 双因子 / 三段式原子注册 / 迭代末双同步顺序铁律
- `docs/specs/S_10_input-side-guards.md` — 输入侧守卫：路径空白修复 squash 恰一匹配 / 读文件校验（二进制启发式双轨）/ 工作区配额门禁 rail
- `docs/specs/S_11_subagent-creation-and-dispatch.md` — 子代理创建与分发：create_subagent 唯一汇聚点、类型归一化、嵌套禁止、factory 白名单、SubAgentConfig 声明契约、jiuwenswarm 装配
- `docs/specs/S_12_subagent-runtime-and-lifecycle.md` — 子代理持久运行时：SubagentControl per-parent-session、状态机七态、容量与 LRU、超时三级、compat 类替换、镜像流与 3A 事件投影
- `docs/specs/S_13_subagent-authorization-and-approval.md` — 子代理授权与审批：wiring 零侵入、main scope 唯一准入、审批注册表容量五重约束、执行期手工合成裁决、拒绝指纹
- `docs/specs/S_14_subagent-dispatch-channels-and-host-integration.md` — 分发通道与宿主集成：SubagentRail 三模式互斥、session spawn 任务化、AgentTool 通道、flash 变体、委托边界五道闸、debug 捕获与 perf patch
- `docs/specs/S_15_interrupt-primitives-and-tri-state-decision.md` — 中断原语与三态决策（agent-core）：五状态键、ToolInterruptException 信号载体、BaseInterruptRail 三态封闭、Confirm/AskUser 基类、handler build→commit→resume→abandon、恢复分类四问
- `docs/specs/S_16_permission-interception-and-authorization-memory.md` — 权限拦截与授权记忆（agent-core）：拦截一切工具的判定序、auto_confirm / batch allow 双 key、授权三级记忆、persist 三真与写盘回滚、hosted confirm 三分类
- `docs/specs/S_17_host-assembly-and-interaction-conversion.md` — 宿主装配与交互转换（jiuwenswarm）：build_permission_rail 三级 rail 选择、hosted 通道分流（web/officeclaw/tui/ACP）、场景豁免链（ask_user 豁免）、三源分类转换、plan 附加字段、企业版永久记忆降级
- `docs/specs/S_18_interrupt-phase-and-recovery-guards.md` — 中断相位与恢复守卫（server runtime）：五相位状态机、终态守卫与 HITL 活性卡注册表、权限响应台账幂等、Team landed 结算、续跑注入 mode 白名单、SessionFlagProxy 双写
- `docs/specs/S_19_cancellation-settlement-and-finalization.md` — 取消结算与收尾（server runtime）：三级 settle 入口、纯 confirm 本地判定、合法 ToolMessage 插入（不产孤儿）、三通道降级写入、清键与持久化、`{id}#{n}` 序号代际
- `docs/specs/S_20_stream-event-emission-channel.md` — 事件流主通道与发射契约：agent-core stream 底层（三 schema / END_FRAME / 队列重试与强清）、六类事件发射时机、sid 解析三序、cwd rebind 双模式、symphony 工具进度桥接、装配挂载七处、消费侧终态判定
- `docs/specs/S_21_pause-checkpoints-and-context-repair.md` — 暂停检查点与上下文修复：pause/resume/abort 状态机（shell 进程树击杀）、per-session 六字典、早 checkpoint（post_agent_execute 不关流）、取消结果收集、占位检测（legacy 7 类文案）、call_goal 注入剥离（deepcopy 防泄漏）、SkillTurbo HITL 改写、上下文修复四计数
- `docs/specs/S_22_native-multimodal-image-input.md` — 原生多模态图片输入：模型能力三态解析（条目硬边界 / 显式兜底 / 内置窄表）、`MultimodalImageRail` 发送侧唯一决策点、像素物化与视图剥离、三预算最老优先降级与 `attached`/`omitted` 状态行、`ctx.extra` 已提供图片快照、read_file 重复读 / 视觉工具护栏、fallback notice 一致性

## 统一术语表

| 术语 | 含义 |
| --- | --- |
| todo 主链 | `TodoStatus` 四值 `pending` / `in_progress` / `completed` / `cancelled`，todo.json 整表覆盖式存储 [[S_01]] |
| 变体存储 | `TodoToolkit` 用独立枚举 `waiting` / `running` / `completed` / `cancelled`（`running` 从未被写入），与主链两种存储互不混用 [[S_01]] |
| 代际 token | 会话级版本号。bump 后旧代 todo 在**执行层被过滤、展示层保留**（backlog）；agent-core 侧只读打标，写侧唯一 bump 点在 `todo_resume.bump_todo_generation_token` [[S_01]] [[S_05]] |
| 持久提醒 | `is_persistent_todo` 标记的 todo 项，跨轮驻留直到用户显式处理（backlog 查询 / drop 词汇命中）[[S_05]] |
| auto_advance / lazy_start | 工作工具触发的 `pending→in_progress` 推进；`todo_create` 延迟启动的补发启动 [[S_02]] |
| 串行顺序约束 | 前缀存在未完成项时，后项的 start/complete 入 deferred 暂存，前缀完成后按列表序补发；磁盘上的乱序是真实状态，不回写 [[S_02]] |
| 产物检测 | 工具调用产物路径的异步归集，不阻塞工具返回 [[S_02]] |
| close nudge | 全部 todo 完成后推送的收尾提示，同一 invoke 至多一次 [[S_03]] |
| hold / drop / release | `TodoCloseTextHold` 三帧文本流控：hold 先于流式文本下发、drop 只写状态字不删帧、release 按未送达清单补投 [[S_03]] |
| plan 审批四态 | 批准 / 拒绝 / 跳过 / 执行。Web 把"批准"拆成两条路径：执行=结束本轮+前端补发新消息；跳过=reject+force_finish 留在 plan [[S_04]] |
| 统一工具 | flash 的 `todo(action=...)` 4 合 1 工具，与外层 task-loop 的 `todo_` 前缀钩子有意共生共存 [[S_05]] |
| 兼容 shim | `todo_compat.py` 对 openjiuwen stock 工具的 monkeypatch 层，在 `interface_deep` import 时执行，时序敏感 [[S_01]] |
| 四阶段 | deepresearch 的 stage 常量与节点→阶段映射，只落全 completed 项 [[S_06]] |
| per-call 重试预算 | 重试计数挂在请求 `ctx.extra["_llm_retry_counts"]` 而非 rail 实例（实例复用不带状态），peek 与 consume 分离 [[S_07]] |
| 溢出恢复链 | 413 等溢出错误触发 force_compact 后**无论成败一律 request_retry(0)**，连续溢出达阈值实例级熔断 [[S_07]] |
| 循环断路器（非恢复型） | CircuitBreakerRail 阈值命中即 force-finish 会话，**没有 OPEN/HALF_CLOSED 恢复态**——恢复靠新会话 [[S_08]] |
| 裸错误前缀 | 工具结果文本以 `[ERROR]:` 开头且无结构化错误时的判定通道；功能前缀必须经 `register_plain_error_prefix` 注册，准入标准"失败独有" [[S_08]] |
| binding 双因子 | todo 挂件与 (workspace_dir, id(fs)) 的绑定身份 `sha256(...)[:24]`；`is fs` 不可放松为 `==` [[S_09]] |
| 双同步顺序 | 迭代末"先 super() plan→disk、再 disk→plan"的固定顺序，反序 = 历史 bug [[S_09]] |
| squash 恰一匹配 | 路径空白修复只对 args 中**恰好一个**可修复匹配就地改写，零匹配与多匹配歧义同判放弃 [[S_10]] |
| 二进制启发式双轨 | 读侧二进制判定 = 词表 11 marker（仅 ≤500 字符）+ 控制字符占比（前 4096 字符 >5%），双轨各有命中面 [[S_10]] |
| `_skip_tool` 协作 | before 钩子间的跨 rail 停摆信号：quota(95) 拒绝先置位，stream(80) 据此跳过 in_progress 事件；after 侧只改写结果**不**置位 [[S_10]] |
| 汇聚点 | `DeepAgent.create_subagent`（四参数签名）——TaskTool / SessionSpawnExecutor 创建侧的唯一入口（AgentTool 创建侧偏离汇聚点，见 [[S_14]] N7）；观测埋点 / 授权 wiring / perf patch 都 patch 它 [[S_11]] |
| 三模式互斥 | SubagentRail 按 runtime（subagent_spawn / wait / list）> async session（sessions_spawn / list / cancel）> sync task_tool 选一；`synchronous_subagent_types` 在 runtime 模式下保留指定类型走同步 [[S_14]] |
| 委托边界 | `run_subagent_with_timeout` 的五道闸 + 结果三态 + 实例级永久退役；三条通道的挂载差异（SessionSpawnExecutor 裸 invoke 无边界）见 [[S_14]] 通道表 |
| 状态机七态 | PENDING_INIT / RUNNING / COMPLETED / INTERRUPTED / ERRORED / CLOSED / NOT_FOUND；`is_final()` 不含 INTERRUPTED——中断可 resume；close 禁 RUNNING [[S_12]] |
| compat 类替换 | `install_subagent_control_compat_patch` 以类属性替换 SubagentControl → CompatibleSubagentControl（registry 自行构造实例）；patch 失败 → runtime 降级 [[S_12]] |
| 镜像流 | 子 chunk 镜像到父 session 流：白名单三类 + 剥键 + 200 字 / 300ms 合并 + 帧上限 2000；写失败只 warning，永不打断子代理 turn [[S_12]] |
| main scope 准入 | 仅父授权 Context 为 MAIN_AGENT_SCOPE_ID 才装配子代理授权链（杜绝授权继承）；scope 恒绑定主会话，不回落子 Agent 复合 session [[S_13]] |
| 拒绝指纹 | 同调用（skill + tool + 归一化 args 的 sha256）被拒 / 审批超时后入指纹集，重试直接拒绝；skill load / complete 时清空 [[S_13]] |
| 三态决策 | `InterruptDecision` 封闭三态：Approve（可带 new_args 改写 tool_args 后继续）/ Reject（`_skip_tool` 置位 + 结果二元组完整、工具体跳过）/ Interrupt（抛 AbortError(cause=ToolInterruptException) 弹卡）[[S_15]] |
| 五状态键 | 中断域五个固定键：INTERRUPTION_KEY（session 持久，存 ToolInterruptionState）、RESUME_USER_INPUT_KEY（replay 期 ctx.extra）、INTERRUPT_AUTO_CONFIRM_KEY（session 持久）、RESUME_START_ITERATION_KEY（ctx.extra）、RESUME_BATCH_ALLOW_KEYS（ctx.extra）；session 持久键与 ctx.extra 键不得互换存放位置 [[S_15]] |
| batch allow | replay 级批次授权：同工具同参数指纹（`{tool}:{sha256(canonical_args)[:12]}`）的兄弟调用共享授权；只注入 ctx.extra、永不写 session、replay finally 一并弹出——全新批次必须重新询问 [[S_15]] [[S_16]] |
| 双 key 体系 | session 级 auto_confirm key（用户显式记忆粒度，简单 shell 命令可为 `tool:subcommand`）与 replay 级 batch allow key（全参数指纹）语义分离，单个 allow_once 不得扩大到未审阅过参数的兄弟调用 [[S_16]] |
| 三级 rail 选择 | 权限 rail 构建的逐级回退：PermissionInterruptRail → SkillAuthorizationPermissionRail → SkillTurboPermissionRail；任一级 import / 配置异常回退上一级，整体构建异常 warning + None，不让 agent 构建失败 [[S_17]] |
| ask_user 豁免 | scene hook 对 `ask_user`（非数字分身）直接 approve——防权限 rail 把 ask_user 答案误当 ConfirmPayload 解析失败再弹权限卡死循环（issue #1976）[[S_17]] |
| 三源分类 | interrupt 结果转 `chat.ask_user_question` 前端卡片的优先序：structured ask_user（questions 扩展字段）→ plain ask_user → permission / confirm 启发式（markers / `**工具 \`` 前缀 / `_CONFIRM_INTERRUPT_TOOLS` 白名单）[[S_17]] |
| 五相位 | 中断相位状态机 idle / paused / resumed / cancelled / supplemented；终态 = {cancelled, supplemented}，只拒旧代应答不锁会话——新卡发出即重开 paused [[S_18]] |
| 活性卡 / 死卡 | HITL 活性注册表：发卡登记 base 当前代 + questions（活卡），应答消费即销毁挂起表条目（不进死卡集），cancel / supplement 全部转死；死卡应答被守卫拒绝 [[S_18]] |
| 权限响应台账 | `PermissionResponseLedger`：key=(session_id, response_id) 进程本地幂等去重，reserve → start（CAS 同一对象）→ settle → release 全序；Team InteractiveInput 点击仅 consume landed 为 True 才 complete，否则 abandon 保持可重试 [[S_18]] |
| 序号代际 | 同外层 call id 第 N 次中断的 `{id}#{n}` 计数，挂 adapter 实例跨请求存续——死卡正判与挂起表复用的根基，改请求级即去重失效 [[S_19]] |
| SessionFlagProxy | 中断恢复域一切 session 标志的统一读写入口：update 扇出全部注册实例、get 返回首个非 None；哨兵与标志必须落 runtime_session（before_invoke 实际读取者）[[S_18]] |
| sid（会话键） | StreamEventRail 的 per-session 字典键 = conversation_id（空则 `"default"` 哨兵）；`_resolve_sid` 三序：extra 继承 → `_main_sessions` 身份反查 → default [[S_20]] |
| 六类事件 | rail 经 `write_stream(OutputSchema)` 发射的六型前端事件：tool_call / tool_result / tool_update / todo.updated / context.usage / chat.ask_user_question [[S_20]] |
| cwd rebind 双模式 | `replace=True`（before_invoke，init_cwd 装新状态，赶在工具 gather 复制 ContextVar 引用前）与 `replace=False`（模型/工具 checkpoint，mutate 共享 CwdState）[[S_20]] |
| 早 checkpoint | 首次 LLM 调用前经 `post_agent_execute`（非 post_run，后者关流）落盘用户消息，防进程死亡丢失；extra 标记幂等 [[S_21]] |
| 占位 / 占位替换 | 中断工具的 `[Tool interrupted]` 双语文案 ToolMessage；修复规则：占位 + 后续同 id 真实结果 → 就地替换占位删后续 duplicate [[S_21]] |
| HITL 改写 | after_tool_call 把含 `__skill_turbo_hitl__` 标记的工具结果改写为 ToolInterruptException，让 harness 原生中断机制接管；改写后跳过 tool_result 发射 [[S_21]] |
| call_goal 注入 / 剥离 | 注入（模型侧）：deepcopy schema + model_copy 产新 ToolInfo 防执行侧泄漏；剥离（工具侧）：extract 后无条件清洗写回 [[S_21]] |
| 原生多模态解析 | adapter 每请求解析一次 `enable_read_image_multimodal`（模型条目声明 > 显式配置 > 内置窄表），rail / resolver / 工具护栏 / notice 只读该值 [[S_22]] |
| 已提供图片快照 | `ctx.extra["_jiuwenswarm_provided_image_paths"]`：invoke 级路径快照，工具跨任务/线程时替代 ContextVar 做"这张图已随消息提供"判定 [[S_22]] |
| attached / omitted | 图片状态行 `[image #n attached]` / `[image #n omitted: reason]`：模型可见的"给过/没给"事实来源，超预算按最老优先 omitted [[S_22]] |

## 公开入口

### Rails（经 adapter 装配点构建，config 门控）

| 入口 | 位置 | spec | 说明 |
| --- | --- | --- | --- |
| `TaskExecutionRail` | `common/rails/task_execution_rail.py` | [[S_02]] | deep profile 任务执行 rail：事件映射 / auto_advance / 产物检测 / close nudge / 代际过滤 |
| `CodeTaskPlanningRail` | `code/rails/code_task_planning_rail.py` | [[S_02]] [[S_05]] | code 侧：init 注册四子类 + task_reminder 附件双阈值 + 模型切换 |
| `PlanApprovalInterruptRail` | `code/rails/code_plan_approval_interrupt_rail.py` | [[S_04]] | 审批门：审批、模式恢复、Web「执行」延迟收尾在一次中断+恢复内闭环 |
| `FlashTodoRail` | `flash/rails/flash_todo_rail.py` | [[S_05]] | flash 侧统一工具的独立挂载 rail |
| `NotifyingLLMRetryRail` / `TeamMemberNotifyingLLMRetryRail` | `common/rails/llm_retry_notify_rail.py` | [[S_07]] | LLM 调用重试 rail：三原因分流、per-call 预算、退避 jitter、通知 best-effort；TeamMember 变体加 404 三标记全命中规则 |
| `ContextOverflowRecoveryRail` | `common/rails/context_overflow_recovery_rail.py` | [[S_07]] | 上下文溢出恢复：force_compact + threshold_override 0.85，无论成败一律 retry(0)，连续溢出实例级熔断 |
| `CircuitBreakerRail` | `common/rails/execution_guard/circuit_breaker_rail.py` | [[S_08]] | 工具循环断路器（非恢复型）：四检测器先中先断，critical 命中即 force-finish；默认关 |
| `ConcurrentSafeSysOperationRail` / `ConcurrentSafeTaskPlanningRail` | `common/rails/concurrent_safe_rails.py` | [[S_09]] | 并发安全：注册前查表 + binding 双因子 + 三段式原子注册；deep 侧 task_planning 一律用其子类 |
| `WorkspaceQuotaRail` | `common/rails/workspace_quota_rail.py` | [[S_10]] | 配额门禁 rail（env `WORKSPACE_QUOTA_ENABLED`，默认关）：命令 7 工具满额拒绝 + du 卸线程池 |
| `SubagentRail`（agent-core） | `openjiuwen/harness/rails/subagent/subagent_rail.py` | [[S_14]] | 三模式互斥注册（runtime > async session > sync task_tool），priority=95；`_REFRESHABLE_TOOL_NAMES` 刷新白名单；deep 装配其子类 BrowserTaskPromptRail |
| `BrowserTaskPromptRail` | `common/rails/browser_task_prompt_rail.py` | [[S_14]] | SubagentRail 子类：`synchronous_subagent_types={"browser_agent"}` + browser 委派策略 task_prompt_extension |
| `FlashSubagentRail` | `flash/rails/flash_subagent_rail.py` | [[S_11]] [[S_14]] | flash 变体三差异：spec 自注入 / 系统提示段替换 / 工具卡描述重写；async 分支回落 stock |
| `SubagentSkillAuthorizationRail` / `SubagentPermissionRail` | `common/rails/subagent_skill_authorization_rail.py` / `common/rails/subagent_permission_rail.py` | [[S_13]] | 子代理授权投射版：委托门禁（Skill 加载授权）+ 执行期权限裁决（priority=90，手工合成引擎 + ASK 走 Future 委托） |
| `StructuredAskUserRail` | `common/rails/ask_user_rail.py` | [[S_15]] [[S_17]] | AskUserRail 子类：questions 扩展字段（≤8 问、`__other__` 自定义选项）+ 容错解码；deep / code / team 装配 |
| `CodeConfirmInterruptRail` | `code/rails/code_confirm_interrupt_rail.py` | [[S_15]] | ConfirmInterruptRail 子类：switch_mode / exit_plan_mode 确认弹卡（双语文案），code 装配（~910） |
| `PermissionInterruptRail`（agent-core） | `openjiuwen/harness/rails/security/tool_security_rail.py` | [[S_16]] | 拦截一切工具的权限 rail（priority=90，inherit_to_subagents）：判定序六步 / 双 key / 授权三级记忆 / persist 回滚；宿主侧装配与变体选择见 [[S_17]] |
| `JiuSwarmStreamEventRail` | `common/rails/stream_event_rail.py` | [[S_20]] [[S_21]] [[S_22]] | 事件流 rail（priority=80）：六类事件发射 / sid 解析 / cwd rebind / symphony 桥接（S_20）+ pause/abort 检查点 / 早 checkpoint / 上下文修复 / HITL 改写（S_21）+ read_file 已提供图片 / 视觉工具护栏与 invoke 级图片路径快照（S_22）；七处装配（deep/code/flash 继承/skill_turbo/team/auto_harness/swarm builtin_rails） |
| `MultimodalImageRail` | `common/rails/multimodal_image_rail.py` | [[S_22]] | 原生多模态发送侧唯一决策点（priority=75）：enabled 装像素物化 mutator（写历史）、disabled 装视图级过滤 mutator；同步系统提示 `multimodal_image_policy` 段；deep 经 `_build_multimodal_image_rail` 装配 |

### Tools（经 `add_ability` 注册进 agent）

| 入口 | 位置 | spec | 说明 |
| --- | --- | --- | --- |
| `UnifiedTodoTool` | `flash/tools/flash_todo.py` | [[S_05]] | flash 4 合 1 `todo(action=...)` |
| `CodeTodoCreateTool` / `CodeTodoListTool` / `CodeTodoGetTool` / `CodeTodoModifyTool` | `code/tools/code_todo_tools.py` | [[S_05]] | stock 四工具子类：行为完全继承，CC 对齐发生在 prompt 层 |
| `user_todos()` + `UserTodosParams` | `common/tools/user_todo_tool.py` | [[S_05]] | 用户个人待办：单例 + ContextVar + 独立存储树，与任务 todo 域界隔离 |
| `persist_deepresearch_task_update()` | `common/tools/deepresearch/todo_progress.py` | [[S_06]] | 四阶段快照落盘：路径校验 + 原子替换 |

### 词汇层函数（供 adapter / agent-core 调用的纯函数与副作用函数）

| 入口 | 位置 | spec | 说明 |
| --- | --- | --- | --- |
| `todo_resume` 函数群 | `common/tools/todo_resume.py` | [[S_01]] [[S_03]] [[S_05]] | `bump_todo_generation_token` / `get_todo_generation_token`、`filter_todos_for_execution` / `filter_todos_for_display`、`is_persistent_todo` / `mark_persistent_todo` / `clear_persistent_todo`、`is_backlog_todo_query` / `is_resume_user_query` / `user_query_drops_reminder`、`build_interrupt_resume_*` 提示构造 |
| `install_todo_modify_compat_patch()` | `common/tools/todo_compat.py` | [[S_01]] | openjiuwen 工具 monkeypatch；**全局副作用型入口，import 时序红线见铁律 7** |
| `ToolResultErrorDetector` | `common/rails/execution_guard/circuit_breaker_rail.py` | [[S_08]] | 共享错误判定器（非 rail）：三级回退 has_error → has_explicit_success 压过 ctx → ctx_has_error；被熔断与其它模块复用 |
| `path_whitespace_fix` 函数群 | `common/rails/path_whitespace_fix.py` | [[S_10]] | 路径参数空白修复：squash 恰一匹配、修参不可变类型保持；宿主 `JiuSwarmStreamEventRail` |
| `read_file_validation` 函数群 | `common/rails/read_file_validation.py` | [[S_10]] [[S_22]] | 读侧四工具修复名单校验：fail-closed 放弃链、二进制双轨判定、parallel_read 聚合放行；原生图片模式另提供 `build_provided_image_read_error` / `build_native_image_tool_error` / `extract_paths_from_arguments` |
| `model_capabilities` 函数群 | `common/tools/model_capabilities.py` | [[S_22]] | 模型图片能力三态解析：`resolve_model_entry_support`（bool 优先 → modalities）→ `resolve_model_supports_image`（条目 → 内置窄表，text-only 前缀先判） |
| `user_prompt_builder` 图片函数群 | `common/prompt/user_prompt_builder.py` | [[S_22]] | 引用行 `build_image_reference_line` / `parse_image_references`、像素注入与三预算 `inject_image_content_from_references`、视图过滤 `filter_image_content_from_view`、视觉 token 估算、路径归一 `is_provided_image_path`、ContextVar 图片列表 |
| `run_subagent_with_timeout` | `common/tools/subagent_executor/execution_timeout.py` | [[S_14]] | 委托边界执行器：五道闸 + 结果三态 + 实例级永久退役；task_tool / AgentTool 通道经它调用，SessionSpawnExecutor 裸 invoke 无边界（差异见 [[S_14]] 通道表） |
| `install_subagent_authorization_wiring` | `common/tools/subagent_executor/authorization.py` | [[S_13]] | 授权 wiring：patch `create_subagent` 汇聚点装配子代理授权链（幂等 / 零侵入 / main scope 唯一准入 / 审批注册表） |
| `install_subagent_control_compat_patch` | `common/tools/subagent_compat.py` | [[S_12]] | compat 类替换：类属性换 SubagentControl → CompatibleSubagentControl（含镜像流开关）；patch 失败 → runtime 降级；**全局副作用型入口，import 时序红线同铁律 7** |
| `try_handle_subagent_chunk` | `server/runtime/agent_adapter/subagent_stream.py`（server 侧接缝） | [[S_12]] | 子代理 chunk 投影进父会话流：镜像白名单 / 剥键 / 合并 / 帧上限 |
| `invoke_subagent_with_trace` | `server/runtime/debug_trace/subagent_capture.py`（server 侧接缝） | [[S_14]] | debug 捕获执行器（invoke / stream 二路）：捕获开启时 `stream(inputs, session=None)` 隔离排水 |

真正的生产入口不是这些类本身，而是各 profile 的 adapter / 编排装配点（见下节）。工具类经 rail 的
`init(agent)` 或工具构建映射注册；`install_todo_modify_compat_patch` 与
`install_subagent_control_compat_patch` 都是 import 时执行的进程级 patch，改它们周围的
import 顺序等同于改契约。

## 模块地图

```text
agents/harness/
├── agent_observability.py        # 单代理观测性（config 门控；OTel 全局单 TracerProvider）
├── observability_runtime.py      # 观测性运行时件
├── claw/                         # 空占位子树（仅 __init__.py）
├── code/                         # code profile 挂件
│   ├── prompt/                   #   code_prompt_builder / code_todo_tool_prompts（CC 对齐描述）/ plan_approval（常量与意图分类）
│   ├── rails/                    #   code_task_planning_rail（S_02/S_05）/ code_plan_approval_interrupt_rail（S_04）/ code_agent_mode_rail（S_04 模式恢复）/ code_plan_approval_rail（p76 未装配遗留）/ code_confirm_interrupt_rail（S_15 确认卡）/ coding_artifact_post_process_rail
│   └── tools/                    #   code_todo_tools 四子类（S_05）
├── common/                       # 跨 profile 共享挂件（最大子树）
│   ├── rails/                    #   task_execution_rail（S_02）+ 可靠性域（S_07 重试与溢出恢复 / S_08 熔断 execution_guard/ / S_09 并发安全 / S_10 配额 rail 与输入守卫纯函数库）+ subagent 域（S_13 subagent_skill_authorization_rail / subagent_permission_rail、S_14 browser_task_prompt_rail）+ 中断交互域（S_15/S_17 ask_user_rail、S_17 interrupt/ 子树 interrupt_helpers 与 permissions/ 子树 config_loader / owner_scopes / tool_permission_context）+ 事件流域（S_20/S_21 stream_event_rail、symphony/tool_stream_events）+ 原生多模态域（S_22 multimodal_image_rail、stream_event_rail 读图护栏）+ 模型路由 / symphony 编排等
│   ├── tools/                    #   todo_resume / todo_compat / user_todo_tool（S_01/S_05）/ deepresearch/（S_06）/ subagent_compat.py（S_12 类替换）/ subagent_executor/（S_13/S_14 授权 wiring 与委托边界）/ model_capabilities.py（S_22 能力解析）/ web_search / xiaoyi_phone_tools 等 40+ 件
│   └── …                         #   auto_harness/ auto_memory/ long_horizon/ memory/ plugins/ prompt/（S_22 图片引用行与像素注入）recommendation/ skill_sleep/（其余未归档域）
├── docs/                         # 归档规约 AGENTS.md + specs/（S_01~S_22）+ features/
├── flash/                        # flash profile 挂件
│   ├── permissions/              #   tool_policy_projection（S_05 统一名权限投影三段式）/ flash_permission_rail / skill_install
│   ├── rails/                    #   flash_todo_rail（S_05）/ flash_subagent_rail（S_11/S_14 flash 变体三差异）/ flash_delivery_summary / flash_memory / slim_sys_operation
│   └── skill_selection/          #   27 件技能选型（bm25 / catalog / pipeline…，未归档域）
├── team/                         # 团队域（未归档）：a2x/ + handlers/ + rails/ 6 件 + team_manager 等 13 根级件——subagent 域已归档（S_11~S_14），机制主体在 common/flash/ 与 openjiuwen + server 装配层，不在本子树
└── work/                         # work profile 挂件
    ├── prompt/                   #   work_plan_prompts（S_04-N13 work 白名单）
    └── rails/                    #   work_agent_mode_rail（S_04 批准后模式恢复）
```

## 架构铁律

从二十二份 specs 的不变量中提炼的跨 spec 最高优先级约束。逐条细则以回链 spec 为准。

1. **代际 token 单向流**：agent-core 侧只读 token 打标过滤，写侧唯一 bump 点是
   `todo_resume.bump_todo_generation_token`。新增任何"重置任务列表"的路径必须走 bump，
   禁止手改 todo.json 绕过 [[S_01]] [[S_02]]。
2. **`inherit_to_subagents=False`**：todo 状态映射 attachment 的传染标志固定关闭，
   任务执行状态不向子代理传染。新增 attachment 类型时必须显式决定该标志的取值 [[S_02]]。
3. **状态机约束在工具层校验**：单 `in_progress`、同一 invoke 至多一次 create、
   状态枚举固定（主链 4 值 + 变体独立枚举，互不混用），违例统一
   `TOOL_TODOS_VALIDATION_INVALID` [[S_01]]。
4. **事件映射完整性**：`pending → completed`（跳过 in_progress）必须先发 `task.start`
   再发 `task.complete`——前端 hidePending 隐藏 pending 行，漏发 start 会让该段直到冻结
   快照才可见。新增状态迁移路径必须补齐事件 [[S_02]]。
5. **串行顺序不回写**：前缀未完时后项 transition 入 deferred 暂存补发；磁盘上的乱序是
   真实状态，禁止"顺手修正" [[S_02]]。
6. **auto_advance 唯一写点在 `TaskExecutionRail`**：任何其它组件想做"自动推进下一项"
   都是在制造第二个真相源 [[S_02]]。
7. **monkeypatch 时序红线**：`install_todo_modify_compat_patch` 在 `interface_deep`
   模块级 import 时执行。改 import 顺序、改 patch 目标符号名、把 patch 挪进函数体，
   都可能让 shim 在某些路径下失效且测试不易察觉 [[S_01]] [[S_05]]。
8. **审批门优先级三明治**：审批确认 > write 权限 > bash 写拦截。调整任何一层的判定
   顺序都会破坏 plan 模式只读保证 [[S_04]]。
9. **Web 四态语义分叉是契约**：Web「执行」= 批准 + 收尾 + 前端补发新消息（避免同轮狂奔），
   Web「跳过」= reject + force_finish 留在 plan。TUI 无此拆分 [[S_04]]。
10. **hold / drop / release 帧约束**：drop 之后永不改回 release；未送达帧按清单重试补投；
    foreign（非本 hold 消费方）文本不缓冲 [[S_03]]。
11. **flash 统一名与前缀钩子共生**：`todo(action=...)` 这个统一名同时被权限投影三段式与
    前端工具名白名单依赖。改工具名 = 同时改投影、白名单、历史名兼容三处 [[S_05]]。
12. **fail-open 与 fail-closed 的分野**：todo 读路径 fail-open（读失败不阻塞对话，
    返回空表）[[S_01]]；deepresearch 落盘路径 fail-closed（路径校验失败即拒绝——它写
    用户文件，宁可拒绝不可写错位置）[[S_06]]；可靠性域同分野——通知发送 best-effort
    [[S_07]]，读侧校验与配额拒绝 fail-closed [[S_10]]；中断域同分野——权限判定序
    fail-closed（engine 异常 raise、解析失败再问）[[S_16]]，相位守卫与三级 rail 选择
    fail-open（守卫故障放行、装配失败回退 None）[[S_17]] [[S_18]]。给新代码选错误策略时
    先想清楚属于哪类。
13. **per-call 重试预算挂在请求 ctx**：重试计数存 `ctx.extra["_llm_retry_counts"]`，
    rail 实例跨请求复用不带状态；peek 与 consume 分离，改预算口径必须保持这一归属 [[S_07]]。
14. **溢出恢复无论 compact 成败一律 request_retry(0)**：禁止"compact 失败就不重试"；
    同名键识别 processor 用非 isinstance 判定，连续溢出计数在实例级 [[S_07]]。
15. **断路器默认关且只断不恢复**：`execution_guard.circuit_breaker` 默认 false，
    与 agent_ras 双挂互斥；命中即 force-finish 会话，无 OPEN/HALF_CLOSED 恢复态——
    别把它当恢复型熔断器用 [[S_08]]。
16. **错误判定三级回退与裸前缀准入**：has_error → has_explicit_success 压过 ctx →
    ctx_has_error；新增"失败独有"的文本前缀必须经 `register_plain_error_prefix` 注册，
    禁止直接改 `_PLAIN_ERROR_PREFIXES` [[S_08]]。
17. **迭代末双同步顺序**：先 super() plan→disk 再 disk→plan，反序 = 历史 bug
    （`test_after_task_iteration_persists_memory_completion_before_disk_refresh` 是回归钉）[[S_09]]。
18. **binding 身份双因子不可放松**：`sha256([workspace_dir, id(fs)])[:24]`；`is fs`
    判等不可改 `==`，注册前必查 `Runner.resource_mgr.get_tool` [[S_09]]。
19. **squash 恰一匹配与修参不可变**：路径修复只在恰好一个匹配时就地改写（零匹配与
    歧义同判放弃），且不改变参数的不可变类型（tuple 不变 list）[[S_10]]。
20. **`_skip_tool` 是 before 钩子间的停摆契约**：quota(95) 先于 stream(80) 执行，
    拒绝时先置 `_skip_tool`，后续 rail 据此跳过事件；after 侧归一只改写结果**不**置位 [[S_10]]。
21. **`create_subagent` 是创建侧唯一汇聚点**：TaskTool / SessionSpawnExecutor 经它创建
    （观测埋点 / 授权 wiring / perf patch 都 patch 它）；AgentTool 创建侧直接
    `create_deep_agent` 的偏离以执行侧走 `invoke_subagent_with_trace` 补偿；新增通道禁止
    绕开汇聚点自建 agent [[S_11]] [[S_14]]。
22. **嵌套禁止与白名单准入**：子代理上下文中禁止再创建子代理（三键判定见 [[S_11]]）；
    子代理类型必须落在 factory 白名单内，新增类型 = 改白名单并同步工具卡描述 [[S_11]]。
23. **compat patch 失败即降级不中断**：`install_subagent_control_compat_patch` 装不上 →
    `enable_subagent_runtime` 强制 False，链路降级 async / sync 模式继续装配 [[S_12]]。
24. **授权不继承，main scope 唯一准入**：仅父授权 Context 为 MAIN_AGENT_SCOPE_ID 才装配
    子代理授权链；scope 恒绑定主会话，禁止回落子 Agent 复合 session [[S_13]]。
25. **审批拒绝由 after hook 收口**：before hook 拒绝不直接 force_finish，只置
    `ctx.extra["_subagent_force_finish"]`；同调用被拒 / 审批超时后入拒绝指纹，重试直接
    拒绝 [[S_13]]。
26. **委托边界五道闸与实例级永久退役**：跨会话复用禁止 / unavailable 拒绝 / 重入拒绝 /
    pending cleanup 背压 / completion_timeout 校验缺一不可；error / 超时 / 取消 →
    `state.unavailable=True` 永久退役，成功与 interrupt 原样返回（interrupt 可 resume）[[S_14]]。
27. **debug 捕获必须隔离排水**：`invoke_subagent_with_trace` 开捕获时以
    `stream(inputs, session=None)` 执行——传父 session 会出现双排水者轮询分走 token [[S_14]]。
28. **`_REFRESHABLE_TOOL_NAMES` 是封闭集**：DisabledToolsRail 刷新协议只认 `task_tool` /
    `sessions_spawn` / `subagent_spawn` 三张卡；新增分发工具必须加入该集合并同步卡描述，
    否则卡描述与白名单漂移 [[S_14]]。
29. **中断信号唯一载体与 cause 链**：工具执行被中断的唯一表达 =
    `ToolInterruptException`；rail 侧只能经 `_raise_interrupt` 抛
    `AbortError(cause=ToolInterruptException(...))`，runner 从 cause 还原中断语义——
    不得另造异常类型或平行信号 [[S_15]]。
30. **三态决策封闭**：`InterruptDecision` 只有 Approve（可带 `new_args` 改写
    tool_args 后继续）/ Reject（`_skip_tool` 置位且结果二元组完整、工具体跳过）/
    Interrupt（触发铁律 29 中断）三态；新增决策类型必须同时改 `_apply_decision` [[S_15]]。
31. **batch allow 生命周期 = replay 本身**：批次授权只注入 `ctx.extra`、**永不写
    session state**、replay finally 与 resume 注入键一并弹出——批次结束授权即失效，
    全新批次必须重新询问；注入键泄漏到下一轮 = rail 误判已获用户授权 [[S_15]] [[S_16]]。
32. **权限拦截一切工具且判定序固定**：`before_tool_call` 不按 `tool_names` 过滤
    （名单拦截 = 未注册工具绕过权限）；六步判定序（scene_hook → engine →
    session auto_confirm → batch allow → hosted confirm → 弹卡）不可重排 [[S_16]]。
33. **双 key 语义分离与授权不扩张**：session 级 auto_confirm key（用户显式记忆粒度，
    简单 shell 命令可为 `tool:subcommand`，复杂结构 `""` 不可记忆）与 replay 级
    batch allow key（全参数指纹）不得混用；单个 allow_once 不得扩大到用户未审阅过参数
    的兄弟调用；persist 需三真且写盘失败回滚内存 [[S_16]]。
34. **三级 rail 选择 fail-open**：PermissionInterruptRail →
    SkillAuthorizationPermissionRail → SkillTurboPermissionRail 逐级回退，任一级
    import / 配置异常回退上一级，整体构建异常 warning + None——权限装配失败不得让
    agent 构建失败 [[S_17]]。
35. **ask_user 豁免防死循环**：scene hook 对非数字分身的 `ask_user` 直接 approve
    （issue #1976）——"拦截一切工具"的 rail 若无此豁免，会把 ask_user 答案误当
    ConfirmPayload 解析失败再弹权限卡死循环；新增同类全量拦截 rail 必须审视豁免链 [[S_17]]。
36. **五相位只拒旧代不锁会话，守卫 fail-open**：终态 = {cancelled, supplemented} 只表示
    上一代中断已死，新卡发出即重开 paused；状态机与守卫是守卫不是运行时依赖——非法
    转换记 warning 仍写入、守卫异常放行，故障不得阻断发卡 / 应答主流程 [[S_18]]。
37. **权限响应台账全序幂等**：reserve（key=(session_id, response_id)，重复即拒）→
    start（CAS 同一对象才授予 runtime 入场权）→ settle → release；Team
    InteractiveInput 点击仅 landed 为 True 才 complete，否则 abandon 保持可重试 [[S_18]]。
38. **SessionFlagProxy 双写与哨兵落点**：中断恢复域一切 session 标志经 proxy 扇出
    （临时 session 落盘 + runtime session 本轮可见）；哨兵必须落 `runtime_session`
    （before_invoke 实际读取者）——标在临时 session 上等价于没标 [[S_18]]。
39. **取消不产孤儿、序号代际挂 adapter 实例**：cancel 永不视为批准（只写 cancelled
    ToolMessage 占位 + 清 `INTERRUPTION_KEY`）；cancelled ToolMessage 只插在其
    tool_call_id 对应 assistant 消息之后，不产孤儿不重复；`{id}#{n}` 序号计数挂
    adapter 实例跨请求存续——改请求级即去重失效 [[S_19]]。
40. **发射永不阻断主链路**：六个 `_emit_*` 全部 try/except 包裹、失败仅 debug 日志——
    流式事件是旁路观测，任何发射失败（session 关闭 / 队列满 / payload 异常）都不得让
    工具或模型调用失败；一切前端事件经 `session.write_stream(OutputSchema(...))`
    单一入口，不得绕过 schema 直塞裸 dict [[S_20]]。
41. **abort 是三合一，reset_for_new_task 故意不清 abort**：`abort(sid)` 必须同时置标志
    + set pause event（否则暂停中任务永久挂起）+ 击杀 shell 进程树；取消后的新任务入口
    靠 `reset_abort()` 清标志——提前清会让已取消的 in-flight 复活继续跑 [[S_21]]。
42. **call_goal 注入必须复制不可回写**：deepcopy params + `model_copy` 产新 ToolInfo；
    就地改共享 properties 会让执行侧拿到 `call_goal=None` 的 `**kwargs` 直接
    TypeError；`model_copy` 失败宁可跳过该工具，绝不回写原始实例 [[S_21]]。
43. **HITL 改写后跳过发射，失败降级占位**：改写为 TIE 后 return——中断态无结果可发，
    误发会让前端显示工具失败、主动 emit 会双发卡片；改写整体失败时原 marker dict
    不可透传模型（会被判失败回退加速通道），必须降级为占位文案 [[S_21]]。
44. **占位替换方向固定**：占位 ToolMessage + 后续同 id 真实结果 → 就地把占位替换为
    真实并删后续 duplicate（占位被真实替换，而非 duplicate 规则删真实保占位）；新增
    任何工具中断占位文案必须同步登记 legacy 模板清单 [[S_21]]。
45. **原生多模态发送决策唯一在 rail**：像素是否真正发给模型只由
    `MultimodalImageRail.before_model_call` 决定；`deep_config.enable_read_image_multimodal`
    是唯一解析真源，rail / ReadFileTool resolver / stream 护栏 / fallback notice 只读不推断 [[S_22]]。
46. **模型条目声明是硬边界**：模型条目 `supports_image=False` / `modalities` 无 `image`
    时显式 `true` 不生效（避免请求必然失败）；条目声明支持时显式 `false` 仍可关闭（VQA 降级）；
    内置表只做窄前缀匹配，text-only 前缀先于 image 前缀判定 [[S_22]]。
47. **跨任务的图片判定不读 ContextVar**：已提供图片只认 `before_invoke` 写入
    `ctx.extra` 的路径快照；新增读取方不得改回 ContextVar（工具跨任务/线程会丢）[[S_22]]。

## 生命周期与配置消费

**装配点（真正的生产入口）**：

| profile | 装配点 | 装配内容 |
| --- | --- | --- |
| deep | `server/runtime/agent_adapter/interface_deep.py` | 任务规划：`_build_task_execution_rail`（config `enable_task_execution_rail`，**默认 False**，切换需重启实例 by design）；`_build_work_plan_approval_rail`（deep 与 work profile 共用）；模块级 `install_todo_modify_compat_patch()`（import 时执行）。可靠性：`_build_llm_retry_rail`（~9873）/ `_build_context_overflow_recovery_rail`（~9408，连续溢出阈值装配传 3）/ `_build_circuit_breaker_rail` / `_build_task_planning_rail`（~9594，**一律** ConcurrentSafeTaskPlanningRail 子类）。subagent：`_build_subagent_rail`（~9620，BrowserTaskPromptRail 子类）+ 模块级 `install_subagent_control_compat_patch`（~9640，import 时执行）/ `_bind_subagent_model_resolver`（~6958）/ `_bind_subagent_authorization_wiring`（~6964）/ `_build_configured_subagents`（~4511）。中断交互：`_build_structured_ask_user_rail`（~9663，StructuredAskUserRail）/ `build_permission_rail`（~10796，三级 rail 选择 + host 闭包族 + 发卡相位标记与守卫方法族，见 [[S_17]]/[[S_18]]）。事件流：`_build_stream_event_rail`（@9380，失败降级 None）+ `_RailBuildInfo` 注册（@10378）+ 集成面注入（checkpointer @13809 / adapter @13807 / metadata @13297 / cwd @13151 / abort 调用点 @14973 / cleanup @3511，见 [[S_20]]/[[S_21]]）。原生多模态：`_resolve_enable_read_image_multimodal`（每请求三态解析）+ `_build_multimodal_image_rail`（~10015，失败降级 None）+ 三预算传参（~11007）+ `_build_image_tool_fallback_notice`（仅解析 False 时提示，见 [[S_22]]） |
| code | `server/runtime/agent_adapter/interface_code.py` | `_build_code_task_planning_rail()`（~885）；`_build_plan_approval_rail()`（~1339，纳入热重载覆盖）；`user_todos` 工具构建映射（~309/~1593）。可靠性：`_FIXED_RAIL_NAMES`（~452，llm_retry / overflow 恢复固定名单）。subagent：`_build_configured_subagents`（~1124，经 `_RAIL_BUILD_NAMES` ~287 纳入热重载覆盖）。中断交互：`_build_structured_ask_user_rail`（~893）/ `CodeConfirmInterruptRail`（~910，switch_mode / exit_plan_mode 确认卡） |
| team 编排 | `agents/swarm/providers/code_rails.py` | code rails 镜像（~434）+ `build_team_plan_approval`（~375，`_is_team_plan_leader` 门控）+ `build_user_todos`（tools.py ~565）。中断交互：StructuredAskUserRail 与权限 rail 装配（~413-420；team 继承侧 `team/team_runtime_inheritance.py` ~213） |
| flash | `server/runtime/agent_adapter/interface_flash.py` | `_build_task_planning_rail()`（~994，替换 stock `TaskPlanningRail` 的 4→1 位点）。subagent：`_build_subagent_rail`（~973，FlashSubagentRail）/ `_bind_subagent_model_resolver`（~353）。中断交互：`build_permission_rail`（~420） |
| skill_turbo | `server/runtime/skill_turbo/executor.py` | 可靠性：`_build_llm_retry_rail`（~946，skill_turbo 自建重试 rail） |
| swarm | `agents/swarm/` | 可靠性：`config_specs.py` `_llm_retry_params`（~397，:547/:638 两处 spec 构造）；`builtin_rails.py` llm_retry builder（~98）与 `swarm.workspace_quota` 无参 builder（~86-91）；`_COMMON_RAIL_NAMES`（~99）与 `_CODE_RAIL_NAMES`（~149）均含 quota |
| 全局 patch | `server/app_agentserver.py` + `perf/install_hooks.py` | subagent：`apply_task_tool_debug_patch()`（app_agentserver ~240，进程启动一次性，task_tool 窄缝 debug 捕获装配，幂等）；`apply_create_subagent_perf_patch`（subagent_hooks.py ~66，perf 启动链经 install_hooks ~18 装配，patch 汇聚点，幂等双标记） |

**生命周期层次**：

- 进程级：compat patch（import 时一次性）
- 实例级：rails 经 adapter 构建，config 门控，切换需重启（deep 侧 by design）
- 请求级：`TodoCloseTextHold` 在每条用户请求的 stream() 循环内新建；
  `TaskExecutionRail.bind_request` 每轮注入请求 metadata 副本
- 轮级（invoke）：rail 的 `before_invoke` 重置跟踪状态并捕获代际 token，`after_invoke`
  清理 session 态
- 可靠性域的状态归属：重试预算**请求级**（`ctx.extra`，per-call）、断路器历史
  **per-session**、连续溢出计数**实例级**、binding **实例级**（跨请求持有）
- subagent 域的状态归属：SubagentControl 注册表**实例级**（条目按 parent-session 隔离，
  容量上限 + LRU 清理）、子代理状态机**会话级**（七态，interrupt 可 resume）、拒绝指纹
  **skill 生命周期级**（skill load / complete 时清空）[[S_12]] [[S_13]]
- 中断交互域的状态归属：五状态键分两处——`INTERRUPTION_KEY` /
  `INTERRUPT_AUTO_CONFIRM_KEY` **session 持久**，`RESUME_*` 三键**请求级**
  （ctx.extra，replay finally 弹出）；相位记录**session 级**、权限响应台账与 HITL
  活性卡注册表**进程本地**（重启清零，同卡应答可重试 / 由磁盘态守卫兜底）、
  `{id}#{n}` 序号代际**adapter 实例级**（跨请求存续）[[S_15]] [[S_18]] [[S_19]]
- 事件流域的状态归属：per-session 六字典（abort / pause_events / conversation_ids /
  main_sessions / cancelled_tool_results）**按 sid 键控、rail 实例级**（共享 adapter
  并发多会话靠它隔离）；`_inflight_tool_calls` 实例级**跨会话共享**；早 checkpoint
  幂等标记**invoke 级**（ctx.extra）；`_bg_emit_tasks`（light 模式后台发射）**类级**
  防回收集合 [[S_20]] [[S_21]]
- 原生多模态域的状态归属：图片路径快照**invoke 级**（`ctx.extra[
  "_jiuwenswarm_provided_image_paths"]`，工具跨任务/线程的唯一可靠来源）、本次图片列表
  **同调用栈 ContextVar 级**（prompt 组装用，用完 reset）、多模态 window mutator
  **ContextWindow 级**（属性 `_jiuwenswarm_multimodal_image_window_mutator` 防重复安装）、
  `attached`/`omitted` 状态行**随历史持久化**（模型可见事实来源）[[S_22]]

**配置**：`enable_task_execution_rail` 是任务规划域唯一的实例级开关；code / flash / plan
approval 的装配不带独立开关（装配即启用）。`user_todos` 还需 `resources/config*.yaml`
三份的工具清单登记。code 侧 task_reminder 不受 `react.task_planning` /
`TODO_PROGRESS_REPEAT` 门控（构造时硬编码 `enable_progress_repeat=False`，改用附件机制，
见 [[S_05]]）。可靠性域的开关：`execution_guard.llm_retry_rail.enabled`（yaml true /
code fallback False；`single_char_repeat_count` 两处默认须对齐——yaml 90 / code 100）、
`execution_guard.circuit_breaker`（默认关，warning ≤ critical ≤ global 序关系校验，违者
builder 拒挂返回 None）、`WORKSPACE_QUOTA_ENABLED`（env，默认关）、
`JIUWENCLAW_ADAPTER_CKPT_LIGHT`（env，轻量刷新开关）。`ContextOverflowRecoveryRail`
无配置节（装配硬编码阈值），加配置 = 改契约 [[S_07]]。subagent 域的开关按优先级互斥取一：
`enable_subagent_runtime`（runtime 模式）> `enable_async_subagent`（async session 模式）>
同步 task_tool [[S_14]]；compat patch 装配失败时 `enable_subagent_runtime` 强制 False
（降级链）[[S_12]]。中断交互域无独立实例级开关——权限 rail 由 permissions 配置的
`enabled` 门控（agent 模板 body 与 `get_effective_permissions_config()` 双源二分，假即
不装，见 [[S_17]] N1）；ask_user / confirm rail 装配即启用。事件流域的开关：
`JIUWENCLAW_EARLY_CHECKPOINT`（env，默认开，0/false/no/off 关早 checkpoint）与
`TOOL_RESULT_DISPLAY_MAX_CHARS`（env，默认 60000、上限 100000、0 不截断）归 [[S_21]]/
[[S_20]]；发射本体装配即启用、无独立开关。原生多模态域的开关：`react.enable_read_image_multimodal`
（三态：显式 bool > 模型条目声明 > 内置能力表；false 即走视觉工具链）、
`react.multimodal_image_budget`（总字节）/ `multimodal_image_token_budget`（视觉 token）/
`multimodal_image_max_count`（张数，默认 8），后两者 0 = 不限，按最老优先降级 [[S_22]]。

## 测试

pytest.ini 的 addopts 含全仓 coverage 与 `-v`，日常跑请覆盖为仅保留 asyncio 模式
（**不能整个清空**——`--asyncio-mode=auto` 也在 addopts 里，清空会让 async 用例全部报
"not natively supported"）。命令风格沿仓内多数文档的既有惯例（powershell +
`.venv\Scripts\python.exe`）；`server/runtime/AGENTS.md` 采用 bash + 泛化 `python` 的
等价写法（venv 路径各人不同，不统一规定）——两式并存、语义一致，按本机环境取其一：

```powershell
# 组1 code 侧 + rails 域（S_01/S_02/S_03/S_05：存储工具链 / 执行 rail / close nudge / persistent /
#     代际过滤；rails/ 目录内另含可靠性域 3 件——S_08 熔断 / S_08 错误检测器 / S_09 并发安全——
#     一并跑无害，可靠性域全量见组4）
.venv\Scripts\python.exe -m pytest tests/unit_tests/agents/harness/code/ tests/unit_tests/agentserver/rails/ -o addopts="--asyncio-mode=auto"

# 组2 审批门与交互轮（S_04：plan 审批 / plan 模式编排 / 交互轮循环推进收敛 / web plan）
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/test_plan_approval.py tests/unit_tests/agentserver/test_plan_mode_orchestration.py tests/unit_tests/agentserver/test_interaction_task_plan_loop.py tests/unit_tests/test_web_plan_mode.py -o addopts="--asyncio-mode=auto"

# 组3 事件流 + close hold + flash 权限投影 + deepresearch 落盘 + 续轮绑定（S_02/S_03/S_05/S_06 散件）
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/test_stream_event_rail_todo.py tests/unit_tests/agentserver/test_todo_close_text_hold.py tests/unit_tests/agentserver/test_flash_permission_actions.py tests/unit_tests/agentserver/test_flash_permission_display_args.py tests/unit_tests/agentserver/test_flash_permission_lifecycle.py tests/unit_tests/agentserver/test_flash_permission_persistence.py tests/unit_tests/agentserver/test_deepresearch_todo_progress.py tests/unit_tests/agentserver/test_deepresearch_stream_tool.py tests/unit_tests/agentserver/test_deepagent_task_plan_binding_patch.py -o addopts="--asyncio-mode=auto"

# 组4 可靠性域 + 原生多模态域（S_07/S_08/S_09/S_10：重试 rail / 溢出恢复 / 路径修复 /
#     读校验 / 装配投影；S_22：能力解析 / 图片 rail 视图 / 读图护栏 / resolver 装配；
#     common/rails 目录内另含 model_routing 等相邻域 3 件，一并跑无害）
.venv\Scripts\python.exe -m pytest tests/unit_tests/agents/harness/common/rails/ tests/unit_tests/agentserver/test_code_adapter_context_overflow_recovery_rail.py tests/unit_tests/server/runtime/skill_turbo/test_llm_retry_rail_mount.py tests/agents/swarm/test_team_member_llm_retry.py -o addopts="--asyncio-mode=auto"

# 组5 subagent 域（S_11~S_14：创建分发 / 持久运行时 / 授权审批 / 通道与宿主集成；
#     agentserver 侧 subagent 相关 14 件 + code rail 装配投影 1 件 + harness compat 1 件 + perf 1 件；
#     test_subagent_live_ws_browser 需 live 环境，无环境时自动 skip 属预期）
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/test_subagent_execution_timeout.py tests/unit_tests/agentserver/test_subagent_sys_operation_inheritance.py tests/unit_tests/agentserver/test_subagent_stream_events.py tests/unit_tests/agentserver/test_subagent_skill_authorization_config.py tests/unit_tests/agentserver/test_subagent_runtime_wiring.py tests/unit_tests/agentserver/test_subagent_runtime_acceptance.py tests/unit_tests/agentserver/test_subagent_model_tier.py tests/unit_tests/agentserver/test_subagent_history_routing.py tests/unit_tests/agentserver/test_subagent_live_ws_browser.py tests/unit_tests/agentserver/test_subagent_approval_stream_resume.py tests/unit_tests/agentserver/test_flash_subagent_rail.py tests/unit_tests/agentserver/test_emit_subagent_approval_stream.py tests/unit_tests/agentserver/test_deep_adapter_hot_reload_subagent_inherits_parent_env.py tests/unit_tests/agentserver/test_statusline_setup_subagent.py tests/unit_tests/agentserver/test_code_agent_rail.py tests/unit_tests/agents/harness/test_subagent_compat.py tests/unit_tests/perf/test_subagent_hooks.py -o addopts="--asyncio-mode=auto"

# 组6 中断交互域（S_15~S_19：原语三态 / 权限拦截与授权记忆 / 宿主装配与转换 / 相位守卫与
#     台账 / 取消结算；agentserver 侧 23 件——五件核心（终态守卫 / 相位信号 / 台账 /
#     受理序回归 / 取消结算）+ 装配与转换 18 件，另含 harness 权限持久化 2 件 + common
#     问答收口 1 件 + skill_turbo ask_user 门 1 件；system_tests/test_structured_ask_user
#     需完整服务环境，不日常单测）
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/test_hitl_superseded_invalidation.py tests/unit_tests/agentserver/test_hitl_resume_signal_protocol.py tests/unit_tests/agentserver/test_permission_response_dedup.py tests/unit_tests/agentserver/test_facade_mode_compatibility.py tests/unit_tests/agentserver/test_permission_interrupt_cancel.py tests/unit_tests/agentserver/test_interrupt_helpers.py tests/unit_tests/agentserver/test_interface_interrupts.py tests/unit_tests/agentserver/test_code_confirm_interrupt_rail.py tests/unit_tests/agentserver/test_ask_user_validation.py tests/unit_tests/agentserver/test_task_interaction_ask_user_stream.py tests/unit_tests/agentserver/test_team_ask_user_resend.py tests/unit_tests/agentserver/test_spawn_fallback_suspend_and_interrupt.py tests/unit_tests/agentserver/test_repro_hitl_double_resume.py tests/unit_tests/agentserver/test_repro_seq_ask_user_cards.py tests/unit_tests/agentserver/test_deep_adapter_interrupt.py tests/unit_tests/agentserver/test_chat_interrupt_tenant_pool.py tests/unit_tests/agentserver/test_chat_interrupt_team_cancel.py tests/unit_tests/agentserver/test_skill_authorization_permission_rail.py tests/unit_tests/agentserver/test_permissions_workspace_access.py tests/unit_tests/agentserver/test_permissions_tools_list.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agents/harness/test_permissions_persist_file_guard.py tests/unit_tests/agents/harness/test_permissions_agents_isolation.py tests/unit_tests/common/test_ask_user_response.py tests/unit_tests/server/runtime/skill_turbo/test_ask_user_rail_gates.py -o addopts="--asyncio-mode=auto"

# 组7 事件流域（S_20/S_21：发射通道 / sid 解析 / symphony 桥接 / 暂停与取消 / HITL 改写 /
#     上下文修复；rail 直测 4 件 + skill_turbo HITL 3 件 + 取消 4 件 + 修复与守卫 4 件；
#     部分用例与组6（cancel / skill_turbo）交叠，跑两遍无害）
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/test_stream_event_rail_tool_result.py tests/unit_tests/agentserver/test_stream_event_rail_todo.py tests/unit_tests/agentserver/test_stream_event_rail_symphony.py tests/unit_tests/agentserver/test_stream_event_rail_source_skill.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agentserver/test_skill_turbo_resume_usage.py tests/unit_tests/gateway/test_message_handler_stream_cancel.py tests/unit_tests/gateway/test_message_handler_disconnect_cancel.py tests/unit_tests/agentserver/test_permission_interrupt_cancel.py tests/unit_tests/agentserver/test_chat_interrupt_team_cancel.py tests/unit_tests/agentserver/test_deepresearch_execute_tool.py tests/unit_tests/agents/harness/common/rails/test_path_whitespace_fix.py tests/unit_tests/agentserver/rails/test_todo_updated_stale_filter.py tests/unit_tests/perf/test_todo_tracker.py -o addopts="--asyncio-mode=auto"
```

改动 → 必跑映射：存储 / 工具行为（S_01）→ 组1+组3；rail 事件与推进（S_02）→ 组1+组3；
close 流控（S_03）→ 组1（nudge）+组3（text hold）；审批门（S_04）→ 组2；flash 变体与
权限投影（S_05）→ 组1+组3；deepresearch 落盘（S_06）→ 组3；LLM 调用层失败恢复
（S_07）→ 组4；工具循环熔断（S_08）→ 组1（熔断与检测器件）+组4；并发安全（S_09）→
组1；输入侧守卫（S_10）→ 组4；subagent 域（S_11~S_14：创建 / 运行时 / 授权 / 通道与
宿主集成）→ 组5；中断交互域（S_15~S_19：原语三态 / 权限拦截 / 宿主装配 / 相位守卫 /
取消结算）→ 组6；事件流域（S_20：发射通道 / sid / cwd / symphony；S_21：暂停 /
checkpoint / 修复 / HITL 改写）→ 组7（改发射或取消路径时另跑组6 的 cancel 件与
组3 的 stream_event_rail_todo）；原生多模态域（S_22：能力解析 / `MultimodalImageRail` /
像素注入与视图剥离 / 读图与视觉工具护栏 / ReadFileTool resolver）→ 组4，
另跑 `tests/unit_tests/server/runtime/agent_adapter/test_model_capability_resolution.py`
与 `test_image_tool_fallback.py`（能力三态解析与提示一致性）。

修复类改动必须附**能复现原故障的测试**（先红后绿）；修契约类问题（事件映射 / 时序 /
过滤口径）必须列出全部同构调用点逐个确认。各 spec 的"修改检查单"节列出场景矩阵
（如 [[S_04]] 的挂起期保持 plan、[[S_02]] 的串行补发、[[S_03]] 的三帧边界 case），
改对应机制时逐场景对照。

## 代码风格

- 类型注解：PEP 585 内置泛型（`list[X]`、`dict[K, V]`）+ PEP 604 联合类型（`X | None`）；
  禁止从 `typing` 导入已被内置语法替代的别名。
- 注释 / 日志 / docstring 中文，代码符号英文；关键机制处注释写"为什么"而非"是什么"
  （rail 内大量此类注释是契约的一部分，清理时勿顺手删除）。

## 设计文档归档与双向同步（本模块强制）

归档目录结构与命名规约见 [docs/AGENTS.md](docs/AGENTS.md)。三条强制约束：

1. feature 三连续提交：代码 + 测试 + spec 在同一提交序列内落地（bugfix 单提交例外，
   但 spec 若受影响必须同步）。
2. 规约变动必更新 specs：改了铁律 / 不变量 / 装配点 / 事件契约中的任何一项，
   对应 spec 的修订日期与内容必须同步更新，否则禁止合入。
3. 读到不一致当场修：代码与 spec 不一致时，以代码为准修文档；确属代码错的，修代码并
   附能复现原故障的测试（先红后绿）。

相邻文档：跨仓共享机制的契约在 `jiuwenswarm/common/AGENTS.md`（其 S_14 覆盖会话生命周期
与自升级，含 todo 快照与清理的底层机制——todo 整表存储契约在本域 [[S_01]]——与 harness 域
[[S_14]]（subagent 分发通道）编号同名，跨域引用写全路径消歧；其 S_07
workspace-safety 覆盖配额门禁底层——harness 域 [[S_10]] 的 WorkspaceQuotaRail 是该门禁的
薄挂载）；adapter 装配层的散件说明在 `server/runtime/agent_adapter/AGENTS.md`（subagent
域的 server 侧接缝——镜像流排水 `try_handle_subagent_chunk` 与 debug 捕获
`invoke_subagent_with_trace`——也落在该层，接缝在 [[S_12]]/[[S_14]] 登记）；skill_turbo 与
本目录正交（其 AGENTS.md 管 skill 增强域，不涉任务规划；S_07 重试 rail 在其 executor 的
装配属可靠性域接缝）。
