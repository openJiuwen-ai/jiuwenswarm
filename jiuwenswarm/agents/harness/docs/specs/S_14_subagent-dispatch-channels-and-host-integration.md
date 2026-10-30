# S_14 子代理分发通道与宿主集成

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_14 / subagent-dispatch-channels-and-host-integration |
| 关联模块（agent-core 仓，机制主体） | `openjiuwen/harness/rails/subagent/subagent_rail.py`（`SubagentRail` @31、priority=95 @42、`_REFRESHABLE_TOOL_NAMES` @44、`_partition_runtime_subagents` @344、`uninit` @199）、`openjiuwen/harness/tools/subagent/session_tools.py`（`SessionsSpawnTool` @235、`SessionsCancelTool` @124、`SessionToolkit` @40）、`openjiuwen/harness/task_loop/session_spawn_executor.py`（`SessionSpawnExecutor` @33）、`openjiuwen/harness/tools/subagent/thinking_hook.py`（进程级 hook 注册表）、`openjiuwen/harness/tools/subagent/task_tool.py`（`_invoke_subagent` @362——被 patch 的窄缝）、`openjiuwen/harness/tools/subagent/subagent_tools.py`（runtime 三工具，语义归 [[S_12]]） |
| 关联模块（jiuwenswarm 仓，装配与变体） | `jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py`（`BrowserTaskPromptRail` @39）、`jiuwenswarm/agents/harness/flash/rails/flash_subagent_rail.py`（`FlashSubagentRail` @179）、`jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`（`_build_subagent_rail` @9860、`_resolve_enable_subagent_runtime` @10848、`_session_has_live_subagent_runtime` @3437、`release_subagent_runtime_for_session` @3468、statusline 注入 @4696-4716）、`jiuwenswarm/server/runtime/agent_adapter/interface_code.py`（`_RAIL_BUILD_NAMES` @287）、`jiuwenswarm/server/runtime/agent_adapter/code_agent_rail.py`（`AgentTool` @174、`CodeAgentRail` @369）、`jiuwenswarm/server/runtime/agent_adapter/statusline_setup_agent.py`、`jiuwenswarm/agents/harness/common/tools/subagent_executor/execution_timeout.py`（`run_subagent_with_timeout` @123）、`jiuwenswarm/server/runtime/debug_trace/task_tool_patch.py`、`jiuwenswarm/server/runtime/debug_trace/subagent_capture.py`、`jiuwenswarm/perf/subagent_hooks.py` |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**子代理的分发通道选择与宿主（jiuwenswarm）集成**——创建（[[S_11]]）完成后，子代理从哪个通道发出、执行边界在哪一层：

- **通道注册层**：`SubagentRail` 三模式互斥（runtime > async session > sync task_tool）与工具注册 / uninit / 刷新协议；`synchronous_subagent_types` 在 runtime 模式下保留指定类型的同步 task_tool
- **session spawn 任务化**：`SessionsSpawnTool` 只提交 `CoreTask`，执行在 `SessionSpawnExecutor`（task loop 调度），暂停恒不可、取消 + KV evict
- **自定义 agent 通道**：`CodeAgentRail` + `AgentTool`（`/agents`），不经 `deep_config.subagents` 注册表，直接 `create_deep_agent` 现场建实例
- **flash 变体**：`FlashSubagentRail` 三差异（spec 自注入 / 文案替换 / 刷新不回退）
- **deep 适配器装配链**：`_build_subagent_rail`（compat patch 失败降级 / rail 构建失败回落）、会话 TTL 保守探测、会话释放递归
- **委托执行边界**：`run_subagent_with_timeout`（五道闸 / 结果三态 / 永久退役 / 背压），及其三条通道的挂载差异
- **debug 捕获与 perf patch**：`task_tool_patch`（patch `_invoke_subagent` 窄缝）、`invoke_subagent_with_trace`（invoke / stream 二路、隔离排水）、`apply_create_subagent_perf_patch`（patch 汇聚点）
- **thinking_hook**：进程级单槽注册表

不管：

- 实例创建与 spec 匹配（汇聚点 / 嵌套禁止 / factory 白名单）→ [[S_11]]
- 持久运行时（SubagentControl 状态机 / 容量 / 镜像流 / compat 类替换协议）→ [[S_12]]——本 spec 只管 runtime 工具**注册**在哪个模式下发生
- 授权与审批（wiring / 审批注册表 / 权限 rail）→ [[S_13]]
- `SessionToolkit` 行数据的投影与落库（`subagent_stream.py` 子行落父 session）→ [[S_12]] N17
- debug_trace 目录的整体架构（stream_logger / directives / run 生命周期）→ server 域 [[S_03]]；本 spec 只管其中**子代理分发语义**（捕获选择 / 隔离排水 / 委托边界）
- swarm 成员的 subagent providers（`swarm.*` factory_name 解析）→ [[S_11]] 装配表与 `agents/swarm/AGENTS.md`

## 不变量

- **N1（三模式互斥，runtime 优先）**：`SubagentRail.init` 按互斥顺序选一：`enable_subagent_runtime`（`_runtime_mode` @81）> `enable_async_subagent` 且非 runtime（`_async_mode` @85）> 同步 `task_tool`。runtime 模式下 `_partition_runtime_subagents`（@344）把 `synchronous_subagent_types` 命中的子代理分给同步 task_tool、其余给 runtime 三工具（`subagent_spawn` / `subagent_wait` / `subagent_list`，语义归 [[S_12]]）；async 模式注册 `sessions_spawn` / `sessions_list` / `sessions_cancel` 并 `set_session_toolkit`。三模式注册的工具集**永不下沉混用**——async 工具不进 runtime 模式，runtime 工具不进 async 模式。
- **N2（uninit 释放协议分模式）**：runtime 模式 uninit 经 `loop.create_task(release_all_subagent_controls(agent, reason="rail_uninit"))` 派发（@209）；无运行事件循环时仅告警跳过（@204-207）。async 模式 uninit `set_session_toolkit(None)`（@218）。两个分支**不可互换**——runtime 分支不清 toolkit，async 分支不释放 control。
- **N3（refresh 白名单封闭）**：`_REFRESHABLE_TOOL_NAMES = {"task_tool", "sessions_spawn", "subagent_spawn"}`（@44）——DisabledToolsRail 刷新协议只更新这三个卡的 available_agents 与 `set_allowed_subagent_types`。新增分发工具**必须**加入该集合，否则 disabled_tools 变化后卡描述与实际白名单漂移。`refresh_available_agents`（@161）按当前模式重算分组，与 init 时的分区规则一致。
- **N4（session spawn 任务化，不内联执行）**：`SessionsSpawnTool.invoke`（@261）**不直接执行子代理**——校验 `enable_task_loop` 与 task_manager 后提交 `CoreTask(task_type=SESSION_SPAWN_TASK_TYPE, status=SUBMITTED)`（@327-336），`SessionToolkit.upsert_running` 记行，立即返回 pending。执行发生在 `SessionSpawnExecutor.execute_ability`（@46）由 task loop 调度。缺 task loop / task_manager 时抛 `TOOL_SESSION_TOOL_INVOKED`，不降级为同步执行。
- **N5（spawn 暂停恒不可，取消可）**：`SessionSpawnExecutor.can_pause` 恒 `(False, "Session spawn tasks do not support pause")`（@164-166）；`can_cancel` 恒 `(True, "")`。取消经 task scheduler `cancel_task`，Executor 侧 `cancel`（@176）只补 KV evict（affinity_enabled 才做，@188-209），task 不存在时跳过 evict 仍返回 True。
- **N6（KV affinity 子会话与 finally evict）**：affinity_enabled 时 `SessionSpawnExecutor` 用 `kv_cache_hooks.resolve_sub_session_id(task_id, parent_session_id, metadata)` 解析子会话 ID（@72-76），否则用 `meta["sub_session_id"]`；affinity 开启时子代理 inputs 附 `parent_session_id`（@115-116）。`execute_ability` 的 finally **无条件**（成功 / 失败 / 取消）执行 `evict_subagent`（@143-148）——affinity 缓存不因任务异常残留。
- **N7（AgentTool 通道偏离汇聚点）**：`AgentTool._create_sub_agent`（@188）直接 `create_deep_agent(**create_kwargs, **factory_kwargs)`（@270），**不经** `DeepAgent.create_subagent` 汇聚点——[[S_11]] N1 的观测埋点 / 授权 wiring / perf patch（三者都 patch 汇聚点）对该通道创建侧不生效。补偿：执行侧**恒**走 `invoke_subagent_with_trace`（同步 @331 / background @354），内部有 `attach_subagent_observability` 安全网（subagent_capture.py @76-78，幂等）。嵌套禁止与 [[S_11]] N3 精神一致：`subagents=None` + `enable_async_subagent=False` + `add_general_purpose_agent=False`（@254-256）；`sys_operation` 必须传父级（@251，传 None 会让子代理逃出用户配置的文件系统边界）。
- **N8（CodeAgentRail 与 SubagentRail 共存）**：`CodeAgentRail.priority = 90`（@376，低于 SubagentRail 的 95）只管 `/agents` 自定义 agent（工具名 `Agent`），不触碰内置 agent 工具。加载条件：AgentConfigService 中 `source != "builtin"` 且 `enabled == True`（@470-473）；无自定义 agent 时不注册 Agent 工具。`set_workspace_dir`（@392）热切换时**必须**同步重绑父 `deep_config.workspace`——只更新 rail 目录会让子代理仍从旧根派生。
- **N9（flash 变体三差异）**：`FlashSubagentRail` 继承 stock `SubagentRail`，仅 flash 模式构建（interface_flash.py @985，开关 `react.subagents.general_agent.enabled` 显式 true 才注入 spec）。三差异：(a) **spec 自注入**——init 时用 agent-core `_inject_general_purpose_subagent` 从父配置注入 general-purpose（@349-359，传入现有 specs 由助手按名去重，`cfg.rails` 为空时主动构造 `SlimSysOperationRail` 保 gp 文件面与主代理一致）；(b) **系统提示段替换**——`FLASH_TASK_SECTION`（按上下文压力判断委派，替代 stock 强制委派段；`PromptSection(name=SectionName.TASK_TOOL, priority=85)`，@388-397）；(c) **工具卡描述重写**——`FLASH_TASK_TOOL_DESCRIPTION` 含无条件并行规则（`_apply_flash_descriptions` @316，目标 `_SUBAGENT_TOOL_NAMES = {"task_tool", "sessions_spawn"}` @176）。async 分支（enable_async_subagent）回落 stock `before_model_call`（@385-387）——flash 语义不覆盖 session 工具。
- **N10（flash 文案不回退）**：`refresh_available_agents` 先 `super().refresh_available_agents(agent)`（同步 `set_allowed_subagent_types` 行为），再 `_apply_flash_descriptions` 覆写卡描述（@362-376）——行为同步但文案不回退为 stock 版。顺序不可颠倒。
- **N11（deep 装配链与降级序）**：`_build_subagent_rail`（@9860）装配序：解析 `react.subagent_runtime.enabled`（`_resolve_enable_subagent_runtime` @10848）→ 安装 compat patch（`install_subagent_control_compat_patch(mirror_child_stream=…)` @9880，**失败 → enable_runtime 强制 False**，@9883-9888——无 compat 类替换则 runtime 工具不可用，降级为 async/sync 模式）→ 构建 `BrowserTaskPromptRail`（@9890，SubagentRail 子类：`synchronous_subagent_types={"browser_agent"}` + browser 委派策略 task_prompt_extension；构造 kwargs 按 `signature(SubagentRail.__init__)` 兼容过滤，browser_task_prompt_rail.py @26-36）。rail 构建失败返回 None——回落 `create_deep_agent` 内建默认 SubagentRail。`_optional_enable_subagent_runtime`（@2429）只在 DeepAgentConfig 接受该参数时传入。
- **N12（宿主生命周期两钩，保守取向）**：`_session_has_live_subagent_runtime`（@3437）——`_subagent_controls` 缺失视为空闲（dest-stable SDK / 未 spawn 过）；`capacity().used > 0` 占用；capacity 探测**抛异常时视为占用**（@3459-3466，TTL 不得驱逐可能持有子代理的 Adapter）。`release_subagent_runtime_for_session`（@3468）——`clear_subagent_progress_batch` → 非 session-scoped 时**递归**委托给子 adapter（@3479-3483）→ `release_subagent_control(deep_agent, sid)`（异常吞掉仅告警 @3491-3496）。
- **N13（委托边界五道闸）**：`run_subagent_with_timeout`（execution_timeout.py @123）入口依序检查：跨会话复用禁止（`state.session_id != session_id` raise @138-139）/ `unavailable` 拒绝（@140-141）/ 重入拒绝（`state.active` @142-143）/ pending cleanup 背压（未完成任务 ≥ `_MAX_PENDING_CLEANUP=64` 时拒绝新调用 @144-145，准入阈值非驱逐上限）/ `completion_timeout` 有限正值校验（读 `child.deep_config.completion_timeout` 默认 1800.0，@147-150）。超时值**取自子代理配置**（[[S_11]] N4），不引入第二套常量。
- **N14（结果三态与永久退役）**：成功与 `interrupt` 结果原样返回（可 resume，同会话新时间预算）；error / 超时 / 取消路径 → `state.unavailable = True`（@165）**实例级永久退役**——重试需新实例（factory 路径每次新 delegation 建新实例；预建共享实例的集成方自行替换）。`_cancel_and_observe`（@95）三路并发（`task.cancel` + `child.abort` + `controller.stop`），`_CANCEL_GRACE_SECONDS=1.0` 宽限后未完成的进 `_PENDING_CLEANUP` 观察（done_callback 移除，异常仅告警）。`_CompletionDeadline`（@41）与子代理内部 TimeoutError 类型区分（@167-171）。
- **N15（失败原因不吞不溢）**：`raise_for_subagent_error`（@45）只取 `error` / `message` 字段构造异常（`subagent_failed: {reason}`），**不拷贝完整输出**进异常；interrupt 结果豁免。TaskTool 调用时故意保持 `succeeded=False`——KV affinity 下 SDK 清理据此 evict 失败 sticky 子代理的缓存而非 offload 复用（[[S_12]] 呼应）。
- **N16（task_tool 通道挂载在窄缝 patch）**：`apply_task_tool_debug_patch`（task_tool_patch.py @75，app_agentserver.py @240 启动应用）patch `TaskTool._invoke_subagent`（类级 setattr @98，幂等标记 `debug_trace_patch_applied` + `debug_trace_patch_mode="dispatch_only"`）。包装后：无 debug run → `run_subagent_with_timeout` 包 original_dispatch（@56-63）；有 debug run 且捕获开启 → `invoke_subagent_with_trace`（@64-70）。SDK 不暴露 `_invoke_subagent` 时标记 `unsupported_sdk` 放弃 patch——**TaskTool 行为不变但委托边界丢失**（此时仅 AgentTool 通道有边界）。所有父 agent 模式（含 native team members）共用此 patch。
- **N17（debug 二路分发与隔离排水）**：`invoke_subagent_with_trace`（subagent_capture.py @36）外层 `run_subagent_with_timeout`；内层（@69）无 debug logger 或未开启捕获 → 原生 `subagent.invoke`（@95-97）；开启捕获 → stream 驱动：`stream(inputs, session=None)` **隔离 session**（@111，传父 session 会让父 run 与本 helper 双排水者轮询分走 token），逐 chunk `ensure_subagent_available`（防取消后晚到 chunk 落入 debug 输出）+ `dbg.feed_subagent`，reduction 镜像 SDK `_result_from_stream_chunk`（@133，内联避免 protected 访问）；iterator 在**同任务**内 `aclose`（@120-124，保 rail ContextVar token 在原上下文 reset）。模块只读 ContextVar 不写（绑定生命周期归适配器）。
- **N18（perf patch 汇聚点与幂等）**：`apply_create_subagent_perf_patch`（subagent_hooks.py @66）包装 `DeepAgent.create_subagent`——与观测 / 授权 wiring patch 同一汇聚点（[[S_11]] N1）。幂等双标记（模块级 `_PATCH_APPLIED` + 类属性 `perf_summary_subagent_patch_applied`）；attach 失败仅告警不破坏 spawn（@91-92）；`perf.summary.enabled` 关闭时 attach 为 no-op。
- **N19（thinking hook 进程级永不抛）**：`register_subagent_thinking_hook` 进程级**单槽**注册表（thinking_hook.py @24，全局变量，后写覆盖前写）；`apply_subagent_thinking`（@35）无 hook 时 no-op，hook 抛异常吞掉只 debug 日志（@40-48）——产品 hook **不得**破坏 task_tool。core 不依赖产品 rail：未注册时 task_tool 对 `thinking` 参数无操作。

## 接口契约

### `SubagentRail` 构造参数（agent-core @46）

| 参数 | 默认 | 语义 |
|---|---|---|
| `enable_async_subagent` | False | async session 模式（被 runtime 模式压倒） |
| `enable_subagent_runtime` | False | runtime 模式（最高优先） |
| `task_prompt_extension` | None | 同步 task_tool 提示段扩展回调（ctx, language）→ str / None |
| `synchronous_subagent_types` | None | runtime 模式下保留同步 task_tool 的子代理名集合（frozenset，去空白） |

### 三模式注册表

| 模式 | 触发条件 | 注册工具 | 提示段（before_model_call） |
|---|---|---|---|
| runtime | `enable_subagent_runtime` | `subagent_spawn` / `subagent_wait` / `subagent_list`（+ 命中 `synchronous_subagent_types` 的 `task_tool`） | SUBAGENT_TOOLS 段；无 task_tool 时前置 task 系统提示作父标题；`task_prompt_extension` 仅在无 task_tool 时生效（@285-286） |
| async session | `enable_async_subagent` 且非 runtime | `sessions_spawn` / `sessions_list` / `sessions_cancel`（共享 `SessionToolkit`） | SESSION_TOOLS 段 |
| sync task | 其余 | `task_tool` | TASK_TOOL 段（含 extension） |

### 三条通道 × 委托边界挂载

| 通道 | 创建侧 | 执行侧包装 | 委托边界 | debug 捕获 |
|---|---|---|---|---|
| `task_tool`（所有父模式） | `create_subagent` 汇聚点（[[S_11]]） | `task_tool_patch` patch `_invoke_subagent` | 有（非 debug 路径 `run_subagent_with_timeout`） | 有（debug 路径 `invoke_subagent_with_trace`）；SDK 无窄缝时两者全无（`unsupported_sdk`） |
| `AgentTool`（/agents） | **直接 `create_deep_agent`**（N7 偏离） | 恒 `invoke_subagent_with_trace` | 有（@59 外层） | 有（含 observability 安全网） |
| `SessionsSpawnTool`（async 模式） | `create_subagent` 汇聚点（[[S_11]]） | **裸 `subagent.invoke`**（session_spawn_executor.py @117） | **无**——不挂 `run_subagent_with_timeout`，边界由子代理自身 `completion_timeout` 与 task scheduler 承担 | 无（不进 debug 捕获） |

runtime 通道（`subagent_spawn` / `subagent_wait`）的执行语义归 [[S_12]]（turn 超时 / 熔断 shield），不属本表。

### `SessionsSpawnTool` 输入（agent-core session_tools.py）

`subagent_type` / `task_description` 必填；`model_name` / `model_tier` 可选（非空才入 metadata）；`browser_agent` 专属 `browser_capabilities`（缺省 `[]`，非字符串列表抛错）。metadata 五键：`subagent_type` / `task_description` / `sub_session_id`（`{parent}_sub_{token_hex(4)}`）/ `parent_session_id` / 可选 browser / model 键。

### 宿主集成点（jiuwenswarm）

| 集成点 | 位置 | 行为 |
|---|---|---|
| `_build_subagent_rail` | interface_deep.py @9860 | deep profile 装配 `BrowserTaskPromptRail`（N11 降级链） |
| `_optional_enable_subagent_runtime` | interface_deep.py @2429 | 仅当 DeepAgentConfig 接受该参数才传入（SDK 版本兼容） |
| statusline 注入 | interface_deep.py @4696-4716（`_build_configured_subagents` 内） | `_is_subagent_default_enabled`（显式 false 才关）→ `build_statusline_setup_agent_config`：`enable_task_loop=False`（TaskTool 无 Session 对象）、`restrict_to_work_dir=False`（写 `~/.jiuwenswarm-tui/`）、`rails=[SysOperationRail()]`、`factory_kwargs={"auto_create_workspace": False}` |
| `_RAIL_BUILD_NAMES` | interface_code.py @287 | code profile：`SubagentRail → _build_subagent_rail` / `CodeAgentRail → _build_code_agent_rail` 名字映射装配 |
| `apply_task_tool_debug_patch` | app_agentserver.py @240 启动 | 全局一次性 patch（N16） |
| `apply_create_subagent_perf_patch` | perf 启动链 | 全局一次性 patch（N18） |
| `FlashSubagentRail` | interface_flash.py @985 | 仅 flash 模式构建（N9） |

## 数据结构

- `_CallState`（execution_timeout.py @34）：`session_id` / `active` / `unavailable`，挂子代理实例属性 `_jiuwenswarm_delegation_state`（@27）
- `_PENDING_CLEANUP: set[asyncio.Task]`（@31）+ `_MAX_PENDING_CLEANUP = 64`（@30，准入阈值）+ `_CANCEL_GRACE_SECONDS = 1.0`（@28）
- `SessionTaskRow`（session_tools.py @29）：`task_id` / `sub_session_id` / `description` / `status`（running / completed / error / canceled）/ `result` / `error`
- 子会话 ID 各通道式：task_tool 按 [[S_11]] N14；session spawn `{parent}_sub_{token_hex(4)}`；AgentTool `{parent}_custom_{type}_{uuid8}`（@186）
- debug source label 两式：`subagent:builtin:{type}`（task_tool_patch @30）/ `subagent:custom:{type}`（code_agent_rail @335）

## 执行路径

1. 模式选择（N1）：`_runtime_mode` → `_async_mode` → sync，`synchronous_subagent_types` 分区
2. 工具注册 → `ability_manager.add_ability`；async 模式另 `set_session_toolkit`
3. 分发：sync → `TaskTool.invoke` →（patch 后）`run_subagent_with_timeout` / `invoke_subagent_with_trace`；async → `SessionsSpawnTool` 提交 CoreTask → task loop → `SessionSpawnExecutor.execute_ability` → `create_subagent` → 裸 invoke；自定义 → `AgentTool.invoke` → `_create_sub_agent`（直接 create_deep_agent）→ `invoke_subagent_with_trace`（background 则 `asyncio.create_task(_run_async)`）
4. 边界执行（N13-N15）：五道闸 → asyncio.wait 超时 → 成功 / interrupt 返回；失败 / 超时 → 退役 + 三路取消 + 背压
5. 卸载（N2）/ 宿主释放（N12）：uninit 分模式释放；会话删除走 `release_subagent_runtime_for_session` 递归

## 异常分支

- `TOOL_SESSION_TOOL_INVOKED`：session 工具三连——缺 task loop / task_manager、inputs 非 dict、缺 session / task_id / task 不存在
- `subagent_cleanup_backlog`：pending cleanup ≥ 64 时新调用被拒（N13）
- `completion_timeout`：`RuntimeError(f"completion_timeout: subagent session=…, limit=…s, coroutine_cleanup=…")`（N14）
- `subagent_failed: {reason}`：失败结果转异常（N15）
- `subagent unavailable after failed or cancelled execution`：退役实例被再次调用 / 取消后晚到 chunk（N13/N14）
- `subagent instance cannot be reused across sessions` / `already has an active invocation`：状态闸（N13）
- AgentTool 侧：`TOOL_TASK_TOOL_INVOKED`（缺 session / 参数缺失 / 类型未注册 / 创建失败 / 执行失败五式，错误信息含可用列表）

## 边界 case

- SDK 不暴露 `_invoke_subagent`：`unsupported_sdk` 模式，task_tool 通道无委托边界与捕获（N16 尾注）
- debug helper 在 supervisor task 内拿不到 per-request ContextVar：回退 `get_debug_trace_logger_for_session(sid)` 查找（subagent_capture.py @88-94）
- 会话 adapter 容量探测抛异常：保守视为占用，TTL 不驱逐（N12）
- background AgentTool 失败：只记日志不上抛（`_run_async` @349-363），结果不可回收
- `SessionsCancelTool` 在 scheduler cancel 失败：返回 success=False 且**不**改 toolkit 状态（session_tools.py @199-211）
- compat patch 安装失败：enable_runtime 降级 False，session 走 async/sync 模式（N11）

## 修改检查单

改 `SubagentRail` / 三通道 / 委托边界前：

- [ ] 三模式互斥顺序（N1）未被新开关打破？新增模式必须定义与三模式的优先关系
- [ ] 新增分发工具是否进 `_REFRESHABLE_TOOL_NAMES`（N3）？卡描述会随 disabled_tools 刷新
- [ ] 新增通道是否过 `create_subagent` 汇聚点（[[S_11]] N1）？不过则按 N7 补执行侧包装并记录偏离
- [ ] 新通道是否需要 `run_subagent_with_timeout` 包装？SessionSpawnExecutor 的裸 invoke（通道表）是现状记录，不是许可
- [ ] `_CompletionDeadline` 与子内部 TimeoutError 的区分（N14）仍在？
- [ ] 退役语义（N14：实例级永久）与 `_MAX_PENDING_CLEANUP` 背压未被"自动重置"破坏？
- [ ] 隔离排水（N17 `stream(session=None)`）未被"方便传父 session"回改？
- [ ] flash 三差异（N9）与刷新不回退（N10）仍只在 flash 模式生效？
- [ ] compat patch 失败降级链（N11）——降级后 runtime 工具不再注册？
- [ ] 会话 TTL 探测（N12）异常仍保守视为占用？

## spec 关系

- 上游：[[S_11]]（创建汇聚点——本 spec 各通道在创建完成后接管分发与执行边界）、[[S_12]]（runtime 模式的执行语义：状态机 / turn 超时 / 镜像流；本 spec 只管注册选择）
- 下游：无（本域出口 spec）
- 跨域：[[S_13]]（授权 wiring 与审批在通道工具上生效）；server 域 [[S_03]]（debug_trace 目录整体架构，本 spec 只管其子代理分发语义）；harness 域 [[S_07]]-[[S_10]]（可靠性 rails，与子代理通道正交）
