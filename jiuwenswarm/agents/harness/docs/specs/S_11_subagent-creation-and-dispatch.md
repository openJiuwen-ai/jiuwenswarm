# S_11 子代理创建与分发

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_11 / subagent-creation-and-dispatch |
| 关联模块（agent-core 仓，机制主体） | `openjiuwen/harness/deep_agent.py`（`create_subagent` @1173、`_find_subagent_spec` @1361、`_bind_inherited_artifact_root` @1155）、`openjiuwen/harness/schema/config.py`（`SubAgentConfig` @302）、`openjiuwen/harness/subagents/`（七个预置工厂）、`openjiuwen/harness/tools/subagent/type_aliases.py`（`canonicalize_subagent_type`）、`openjiuwen/harness/tools/subagent/task_tool.py`（`_build_sub_session_id` @136、`_summarize_task_description` @40、卡片豁免 @408） |
| 关联模块（jiuwenswarm 仓，装配） | `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`（`_agent_def_to_subagent_config` @24265、`_bind_subagent_model_resolver` @7187、`_build_configured_subagents` @4680）、`interface_code.py`（`_build_configured_subagents` @1124）、`jiuwenswarm/agents/harness/flash/rails/flash_subagent_rail.py`（spec 自注入）、`jiuwenswarm/agents/swarm/providers/code_subagents.py`（swarm.* providers） |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**子代理实例的创建与分发**——从类型名到可执行 `DeepAgent` 实例的解析链：

- **唯一汇聚点**：`DeepAgent.create_subagent`（四参数签名）——`TaskTool`（同步 task_tool）与 `SessionSpawnExecutor`（异步 session spawn）经此创建子代理；`AgentTool`（`/agents` 自定义 agent，经 `code_agent_rail.py`）创建侧直接 `create_deep_agent` 绕过汇聚点，属已知偏离（[[S_14]] N7）
- **spec 匹配与归一化**：`_find_subagent_spec` + `canonicalize_subagent_type`（`general_agent` → `general-purpose`）
- **`SubAgentConfig` 声明契约**：预置工厂层（`subagents/` 七工厂）只产出 spec，不执行
- **create_kwargs 组装规则**：嵌套禁止、workspace 隔离推导、rails fork、超时不继承、restrict 收紧不放宽、multimodal 继承条件
- **factory dispatch 白名单**：`factory_name` 按名分发到四个具名工厂
- **jiuwenswarm 侧装配**：deep / code 两 profile 的 `_build_configured_subagents`、model resolver 绑定、AgentDefinition → SubAgentConfig 转换

不管：

- 子代理的持久运行时（状态机 / 容量 / LRU / 事件）→ [[S_12]]
- 子代理授权与审批（委托 rail 对 / 审批注册表）→ [[S_13]]
- 分发通道选择（SubagentRail 三模式互斥 / flash 变体 / session spawn 任务化 / 委托超时边界）→ [[S_14]]
- `TaskTool.invoke` 的执行流程（pending 集合、父轮中断后续跑）——工具执行语义归 [[S_14]] 的通道层；本 spec 只管其**创建侧**契约（session id 构造 / 描述摘要 / 卡片超时豁免）
- 父 Agent 本体的 subagents 注册表生命周期（`_control_registry` 属性挂载协议）→ [[S_12]]
- 预置 agent 的 rail 内部行为（`VerificationRail` 只读守卫等）——各 rail 自有契约

## 不变量

- **N1（唯一汇聚点）**：所有子代理实例创建必须经 `DeepAgent.create_subagent(subagent_type, subsession_id, browser_capabilities=None, model=None)`。新增分发路径**不得**绕过它直接 `create_deep_agent`——观测埋点（`observability/subagent.py`）、授权 wiring（[[S_13]]）、perf patch（[[S_14]] N18）都 patch 该汇聚点，绕过即全部失效。当前两条合法通道：`TaskTool` 与 `SessionSpawnExecutor`（docstring @1180 明示 "shared by TaskTool and SessionSpawnExecutor"）；`AgentTool`（code_agent_rail）虽经 `_agent_def_to_subagent_config` 转 spec，创建侧仍直接 `create_deep_agent` 绕过汇聚点——属**已知偏离**：观测 / 授权 / perf 对该通道**创建侧**不生效，执行侧以恒走 `invoke_subagent_with_trace` 补偿（[[S_14]] N7）。
- **N2（类型归一化前置）**：spec 匹配（`_find_subagent_spec` @1361）前**必须**先 `canonicalize_subagent_type`（`type_aliases.py` @19；唯一别名映射 `general_agent` → `general-purpose`）。factory 分发同样按归一化后的 `factory_name.strip().lower()` 匹配。新增别名必须同时更新 `_CANONICAL_SUBAGENT_TYPES`，不允许在匹配点各自做小写比较。
- **N3（嵌套结构性禁止）**：`create_kwargs` 强制 `subagents=None`、`enable_async_subagent=False`、`enable_subagent_runtime=False`（deep_agent.py @1272-1274）。子代理**不可再派生**二级子代理——开关不继承是结构性契约，不是配置默认值。若未来放开，`_control_registry` 的 per-parent 挂载与 [[S_12]] 状态机会连带失效，必须整域重审。
- **N4（超时不继承，硬编码 1800）**：子代理 `completion_timeout` 固定 1800.0（@1282，注释明示 600s 偏短）；父级默认 600.0 不传入。合法等待可跨多轮（wait 工具卡片 `timeout_s=None`，[[S_12]] N13 呼应）。
- **N5（workspace 隔离推导）**：spec.workspace 为字符串 / None → `Path(workspace)/_SUB_AGENTS_DIR/subsession_id`；为 `Workspace` 实例 → `Path(root_path)/_SUB_AGENTS_DIR/subsession_id`。子代理工作目录永远在父 workspace 下的 `sub_agents/` 子树，不共享父根。
- **N6（rails fork 优先）**：spec.rails 逐条组装时**优先**调 `fork_for_agent()`（callable 才调），否则原样共享（@1224-1231）。共享实例意味着子代理对有状态 rail 的副作用会回传父级——有状态 rail 必须实现 fork，否则其状态污染由调用方自担。
- **N7（restrict 收紧不放宽）**：`restrict_to_work_dir = spec.restrict_to_work_dir or parent.restrict_to_work_dir`（@1278）——任一 True 即生效。父级收紧时子代理不可能放宽。
- **N8（multimodal 继承三分支）**：spec 显式指定（非 None）优先；否则仅当 `shares_parent_model`（spec.model is None / 与父相同 / 带 `_browser_agent_parent_model` 标记）才继承父级 `enable_read_image_multimodal`；异构 model 省略 kwarg 交由 auto-probe（@1288-1298）。
- **N9（factory 白名单封闭）**：`factory_name` 归一化后仅四组合法值——`browser_agent` / `browser_runtime` → `create_browser_agent`（browser_capabilities 覆盖注入 factory_kwargs）、`code_agent` → `create_code_agent`、`research_agent` → `create_research_agent`、`mobile_gui_agent` / `mobile_agent` → `create_mobile_gui_agent`。未知值 → `DEEPAGENT_CREATE_SUBAGENT_NOT_FOUND` 错误（@1350-1353）。无 `factory_name` → deferred import `create_deep_agent` 通用路径。
- **N10（实例直通不重建）**：spec 已是 `DeepAgent` 实例 → 只 `_bind_inherited_artifact_root` 后直接返回（@1202-1206）。cwd 延迟到 `_ensure_initialized` 应用；`create_subagent` 本身不污染父 ContextVar。
- **N11（artifact root 继承取 cwd 优先）**：`_bind_inherited_artifact_root` 取 `get_cwd() or get_workspace()`（@1155）——注释明示原因：兄弟子代理跑完后 workspace ContextVar 可能指向兄弟的 `sub_agents/` 树，cwd 仍是共享 artifact root。
- **N12（sys_operation 双条件传递）**：仅当 `spec.sys_operation is not None and spec.workspace is not None` 才传入 create_kwargs（@1257-1261）——单边存在不传，防孤儿引用。
- **N13（描述隐私摘要）**：`_summarize_task_description`（task_tool.py @40）以 sha256_12 + 脱敏文本摘要任务描述——日志 / 观测**不落明文任务描述**。任务正文只进子代理请求体。
- **N14（sticky 会话 ID 确定性）**：`_build_sub_session_id`（@136，判定 @142）对 sticky 类型生成 `{parent}_sub_{type}`；否则复用 tool_call_id 或 `uuid4().hex[:8]`。sticky 判定走 `kv_cache_hooks.is_sticky_subagent_type`（@22-23，**含 `browser_agent` 与 `verification_agent`**——工具侧会话 ID 口径），与运行时托管的 `_runtime_sticky_subagent_type`（control.py @68，**仅 `verification_agent`**，浏览器进程复用归 BrowserServiceRegistry 管，见 [[S_12]]）是**两套集合、勿混用**。确定性 ID 是 [[S_14]] 验证链 FAIL→fix→re-verify 复用会话的前提。
- **N15（task_tool 卡片超时豁免）**：`create_task_tool` @408 显式 `card.properties["resilience"]["timeout_s"] = None`——豁免框架通用 300s 工具超时，子代理耗时由 `completion_timeout`（N4）与 [[S_14]] 委托边界兜底。删除该行 = 每个子代理调用 300s 必死。

## 接口契约

### `DeepAgent.create_subagent`（agent-core）

| 参数 | 类型 | 语义 |
|---|---|---|
| `subagent_type` | str | 经 N2 归一化后匹配 `subagents` 列表 |
| `subsession_id` | str | 子会话 ID（构造方决定——TaskTool 按 N14，AgentTool 按 `{parent}_custom_{type}_{uuid8}`） |
| `browser_capabilities` | list[str] / None | 仅 browser factory 消费，覆盖注入 factory_kwargs |
| `model` | Model / None | 覆盖 spec.model 的显式模型 |

返回 `DeepAgent`（实例直通 / factory 产物 / 通用路径三出口，全部包 `_bind_inherited_artifact_root`）。

### `SubAgentConfig`（agent-core schema/config.py @302）

必填：`agent_card`（名字即类型匹配键，经 N2 归一化）、`system_prompt`。
选填（语义要点）：`tools` / `mcps` / `model` / `rails`（N6 fork 协议）/ `skills` / `workspace`（N5 推导输入）/ `sys_operation`（N12 双条件）/ `max_iterations` / `factory_name` + `factory_kwargs`（N9 白名单）/ `enable_task_loop` / `enable_plan_mode` / `parallel_tool_calls=True` / `restrict_to_work_dir=True`（N7）/ `enable_read_image_multimodal`（N8）。

### 预置工厂层（agent-core `subagents/`）

| 工厂 | max_iterations | 特征 |
|---|---|---|
| `build_plan_agent_config` | 25 | 只读约束；要求输出 Critical Files 段 |
| `build_explore_agent_config` | 15 | 工具固定 bash/glob/grep/list_files/read_file |
| `build_code_agent_config` | 15 | `_inject_builtin_plan_agents` 自动补 explore+plan；CodingMemoryRail 条件注入；`enable_task_planning=True` |
| `build_browser_agent_config` | 25 | `_coerce_browser_instance`；capabilities 校验 ValueError |
| `build_research_agent_config` | 15 | 最简 |
| `build_verification_agent_config` | 40 | **唯一 sticky**；强制 `VERDICT: PASS/FAIL/PARTIAL` 输出契约 |
| `build_mobile_gui_agent_config` | 30 | tiktoken multimodal patch |

预置层只产出 spec；运行时行为（超时 / 并发 / 状态机）全部下沉 [[S_12]]。

### jiuwenswarm 侧装配

| 装配点 | 位置 | 行为 |
|---|---|---|
| `_agent_def_to_subagent_config` | interface_deep.py @24265 | AgentDefinition（`/agents` 自定义）→ SubAgentConfig；disallowed_tools 合并进 spec.tools |
| `_bind_subagent_model_resolver` | interface_deep.py @7187 | 把 adapter 的 `_resolve_model_for_subagent` 挂到 DeepAgent 实例（TaskTool 经 `resolve_task_tool_model` 消费） |
| `_build_configured_subagents`（deep） | interface_deep.py @4680 | research + browser（react.subagents 配置门控）+ statusline 默认注入 |
| `_build_configured_subagents`（code） | interface_code.py @1124 | 固定 explore/plan + 按配置 code/browser |
| `FlashSubagentRail` spec 自注入 | flash_subagent_rail.py | flash 模式用 `_inject_general_purpose_subagent` 从父配置自注入 general-purpose（开关 `react.subagents.general_agent.enabled`，显式 true 才注入）→ [[S_14]] |
| swarm providers | agents/swarm/providers/code_subagents.py | `swarm.code_agent` / `swarm.browser_agent` 经 `SubAgentSpec.factory_name` 在 `spec.build()` 时解析；explore/plan 由 openjiuwen 提供；browser 每成员独立 browser_key |

## 数据结构

- 子会话 ID 三式：sticky `{parent}_sub_{type}`（N14）/ 工具式 tool_call_id 或 uuid8 / 自定义式 `{parent}_custom_{type}_{uuid8}`（AgentTool）
- workspace 子树：`{parent_workspace}/sub_agents/{subsession_id}`（N5）
- `factory_kwargs` 透传通道：browser_capabilities 覆盖（N9）

## 执行路径

1. 调用方构造 `subsession_id`（N14 三式之一）
2. `create_subagent` → `_find_subagent_spec`（N2 归一化）→ 无 spec 则 `DEEPAGENT_CREATE_SUBAGENT_NOT_FOUND`
3. spec 为 DeepAgent 实例 → N10 直通
4. workspace 推导（N5）→ rails fork（N6）→ create_kwargs 组装（N3/N4/N7/N8/N12）
5. factory dispatch（N9）或通用路径 → `_bind_inherited_artifact_root`（N11）→ 返回

## 异常分支

- 类型未注册：`DEEPAGENT_CREATE_SUBAGENT_NOT_FOUND`（含 "Unsupported subagent factory" 变体）
- browser capabilities 非法：工厂内 `ValueError`
- model 解析失败（TaskTool 侧）：`resolve_task_tool_model` 回退链见装配层，错误上抛工具结果

## 边界 case

- `self._deep_config is None`（mock / 半初始化）：`_find_subagent_spec` 返回 None → NOT_FOUND
- test mock bare `object()`：`_bind_inherited_artifact_root` 对无属性对象容错（@1155-1171）
- 兄弟子代理跑完后 workspace ContextVar 指向兄弟子树：artifact root 仍取 cwd（N11）
- 父 Agent 无 model：swarm providers 跳过注入（`ctx.extras["_parent_model"]` 缺失即 skip）

## 修改检查单

改 `create_subagent` / `SubAgentConfig` / 预置工厂 / `_agent_def_to_subagent_config` 前：

- [ ] 嵌套禁止三键（N3）是否仍强制？
- [ ] factory 白名单（N9）与 `_find_subagent_spec` 匹配键是否同步？
- [ ] 新增预置 agent 是否需要进 sticky 集合（N14 工具侧 `is_sticky_subagent_type` 与 [[S_12]] 运行时 `_runtime_sticky_subagent_type` 是两套口径，分别评估）？
- [ ] workspace 推导（N5）与 restrict（N7）收紧语义未被放宽？
- [ ] 卡片超时豁免（N15）仍在 create_task_tool？
- [ ] TaskTool / SessionSpawnExecutor 两条汇聚点通道（N1）未被新代码绕过？AgentTool 的偏离（[[S_14]] N7）未被顺手修正、其执行侧补偿（恒 `invoke_subagent_with_trace`）仍在？
- [ ] jiuwenswarm 侧 `_build_configured_subagents`（deep @4680 / code @1124）两处配置门控是否同步？

## spec 关系

- 上游：无（本域入口 spec）
- 下游：[[S_12]]（创建后的运行时托管）、[[S_13]]（创建时经 wiring 包装注入授权 rail 对）、[[S_14]]（三条通道的分发选择与委托边界）
- 跨域：harness 域 [[S_09]]（ConcurrentSafeTaskPlanningRail 不涉子代理创建）；swarm 成员的 subagent providers 见 `agents/swarm/AGENTS.md`
