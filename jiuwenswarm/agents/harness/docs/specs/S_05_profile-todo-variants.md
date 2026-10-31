# S_05 — Profile 级 todo 工具变体与 common 支线

| | |
|---|---|
| Spec ID | S_05 |
| 状态 | 当前实现的真实描述（含未装配遗留的登记） |
| 修订日期 | 2026-10-28 |
| 关联模块 | `jiuwenswarm/agents/harness/code/rails/code_task_planning_rail.py`（code 侧装配 rail，~290 行）<br>`jiuwenswarm/agents/harness/code/tools/code_todo_tools.py`（四子类，115 行）<br>`jiuwenswarm/agents/harness/code/prompt/code_todo_tool_prompts.py`（CC 对齐描述与 schema，174 行）<br>`jiuwenswarm/agents/harness/flash/tools/flash_todo.py`（UnifiedTodoTool 4 合 1，222 行）<br>`jiuwenswarm/agents/harness/flash/rails/flash_todo_rail.py`（flash 侧挂载 rail，310 行）<br>`jiuwenswarm/agents/harness/flash/permissions/tool_policy_projection.py`（统一名权限投影，~95-126）<br>`jiuwenswarm/agents/harness/common/tools/todo_compat.py`（openjiuwen 兼容 shim + 持久提醒，404 行）<br>`jiuwenswarm/agents/harness/common/tools/todo_resume.py`（持久提醒 / 代际 token / 续跑词汇层，476 行）<br>`jiuwenswarm/agents/harness/common/tools/user_todo_tool.py`（用户个人待办，400 行）<br>`jiuwenswarm/agents/harness/common/tools/todo_toolkits.py`（markdown 行式工具链，**未装配遗留**，367 行） |
| 装配点 | code：`server/runtime/agent_adapter/interface_code.py` `_build_code_task_planning_rail()`（~885）+ `agents/swarm/providers/code_rails.py`（~434，swarm 编排路径）<br>flash：`server/runtime/agent_adapter/interface_flash.py` `_build_task_planning_rail()`（~994，替换 stock `TaskPlanningRail` 的 4→1 位点）<br>compat：`interface_deep.py` 模块级 `install_todo_modify_compat_patch()`（~744，import 时执行，详见 [[S_01]]）<br>user_todos：`interface_code.py` 工具构建映射 `"user_todos" → _build_user_todos_tool`（~309/~1593）+ `agents/swarm/providers/tools.py` `build_user_todos`（~565）+ `resources/config*.yaml` 三份的工具清单 |
| 消费点 | 前端 `channels/web/frontend/src/utils/toolProtocol.ts`（工具名白名单 + `todo_` 前缀兜底）、`AvatarPermEditor.tsx`（权限编辑器列五个历史名）、subagent 展示层（`includes('todo')` 宽匹配） |

## 1. 范围与边界

本 spec 管 **"同一任务规划域在不同 profile 下的工具形态分叉，以及 common 侧三条与主线并存的支线"**：

- **code profile**：openjiuwen stock todo 工具的子类化——CC（Claude Code）对齐发生在 **prompt 层**（描述 / schema 换装），行为完全继承；外加一个 task_reminder 附件机制
- **flash profile**：4 合 1 统一工具 `todo(action=...)` + 独立挂载 rail——形态从"四个工具"收敛为"一个工具四动作"，且有意绕开外层 task-loop 的 `todo_` 前缀钩子
- **common 支线一（compat）**：`todo_compat.py` + `todo_resume.py`——openjiuwen 工具的 monkeypatch 兼容层与持久提醒词汇层（装配时序在 [[S_01]]，本 spec 管持久提醒语义与代际 token 分工）
- **common 支线二（user_todos）**：用户个人待办——与 agent 内部任务规划**域界分离**的独立存储树
- **common 支线三（todo_toolkits）**：markdown 行式工具链——**未装配遗留**，生产零消费（R1 登记）

**不管**（指向其它 spec / 域）：

- todo.json 存储布局、openjiuwen stock 四工具、`TodoLockManager`、monkeypatch 的双命名空间换类细节 → [[S_01]]
- TaskExecutionRail 的执行地图绑定、`filter_todos_for_execution` 的消费侧编排 → [[S_02]]
- todo 收口与关单判定 → [[S_03]]
- deepresearch 的 todo 落盘变体 → [[S_06]]
- flash 的统一 memory / 统一 read 等其它 4→1 / N→1 收敛工具 → flash 工具面（非任务规划域）

## 2. 核心不变量

### 2.1 code profile（CC 对齐层）

**N1 子类只换 card，不改行为**
`CodeTodoCreateTool` / `CodeTodoListTool` / `CodeTodoGetTool` / `CodeTodoModifyTool` 四子类仅覆写 `__init__` 替换 card（描述与 input schema 全部来自 `CODE_TODO_TOOL_PROMPTS`），`invoke` 与全部执行逻辑继承 openjiuwen stock。CC 对齐是 **prompt 层工程**：复杂度分级（中等 2-3 项 / 复杂最多 4-6 项 / 琐碎工作跳过）、outcome 而非 file 粒度、避免 todo-only 轮次、verification 并入最后里程碑——这些约定只通过工具描述约束模型，不引入行为分叉。

**N2 tool_id 键控与幂等注册键**
`tool_id = f"{prefix}_{agent_id}"`（agent_id 有则用、无则回退 uuid）。同一进程内多个 code agent 各自的 todo 工具互不冲突；native rebuild 时 `init()` 先按 `isinstance` 扫 `ability_manager` 已注册实例，只补缺失的四个（已注册的复用原实例），不重复注册、不泄漏裸 id。注册统一走 `add_ability`（限定为 `{name}_{owner_id}`，`teardown_tools` 轮末回收）。

**N3 progress_repeat 强制关闭，空转提醒改走附件**
`CodeTaskPlanningRail(TaskPlanningRail)` 构造时硬编码 `enable_progress_repeat=False`——父类的静态进度复读关闭；"模型多轮没碰 todo"的提醒改由 **task_reminder 附件**承担，不受 `react.task_planning` / `TODO_PROGRESS_REPEAT` 配置门控。父类的 advance（计划推进）schedule + inject 仍保留。

**N4 task_reminder 双阈值注入**
`turns_since_management ≥ 10` **且** `turns_since_reminder ≥ 10` 才注入附件（`section="task_reminder"`，`kind=TODO_REMINDER`，`priority=60`，`source="jiuwenswarm.code_task_planning.task_reminder"`）；任一 `todo_` 前缀工具调用即清零 management 计数并清除附件。跟踪会话数上限 1000（LRU 式列表裁剪）。code 模式**不用**父类的静态 todo system section——提示面只保留这一个动态附件。

**N5 todo 工具调用识别用前缀**
`_is_todo_tool_call` 以 `tool_name.startswith("todo_")` 判定——code 侧保持四工具名不变（与 flash 统一名形成对照，见 N7）。

### 2.2 flash profile（统一工具层）

**N6 UnifiedTodoTool 必须是 Tool 子类**
不能做成 LocalFunction：`invoke` 需要接收 `session` kwarg 并透传给四个引擎（todo.json 按 session 键控）；LocalFunction 没有 session 通道，会静默写错 / 读错 session 的文件。模块 docstring 登记了这条硬约束。

**N7 统一名 `todo` 与 `todo_` 前缀钩子的共生约束**
`_UNIFIED_TODO_NAME = "todo"`（不带前缀）。外层 task-loop 钩子（TaskExecutionRail 系）按 `todo_` 前缀识别 todo 调用，统一名**有意绕过**这些钩子；同时 flash 前端的 todo 刷新链路工具名常量已包含 `todo`（docstring 登记）。改动统一名或拆回四工具，必须同步核对这些前缀判定与前端常量，否则外层钩子会静默漏判 / 误判。前端 `toolProtocol.ts` 的内联协议清洗白名单列五个**历史名**（todo_create/complete/insert/remove/list）+ `todo_` 前缀兜底（review 意见：新增 `todo_*` 工具无需同步白名单）。

**N8 FlashTodoRail 独立继承，不继承 TaskPlanningRail**
直接继承 `DeepAgentRail`（priority=90，替换旧的 `ConcurrentSafeTaskPlanningRail`）：**不做 TaskPlan 同步、不实现 after_task_iteration**——TaskPlan 同步与推进提醒在 flash 有意丢弃；但 **close nudge（[[S_03]] 机制）已接入**：构造 `TodoCloseGate(_UNIFIED_TODO_NAME)`（@60），before_invoke 重置 gate → before_model_call `_close_gate.arm` → after_model_call `_close_gate.attach`（文本收尾被替换为一次内部 todo 调用）→ before_tool_call 拦截 nudge 工具调用并注入消息 → after_tool_call 加载 in_progress 条目。挂载面 = init 建引擎并注册统一工具、before/after_model_call 提示注入与 nudge 装挂、before/after_tool_call nudge 拦截与加载、uninit 反注册。

**N9 四引擎共享一把锁，引擎永不单独注册**
init 建四个 openjiuwen 引擎实例（create/list/get/modify，共享**单个** `TodoLockManager`），仅 `UnifiedTodoTool` 进 `ability_manager`。modify 引擎实例化的实为 `CompatibleTodoModifyTool`（N13 的 monkeypatch 换类后的产物）——容忍 deleted/canceled 状态变体。

**N10 注册只走 add_ability（原子重绑）**
统一卡是 stateful（`card.stateless` 默认 False），`add_ability` 内部以 `refresh=True` 在 resource_mgr 原子重绑（后注册者胜）。**禁止**先 `get_tool` 再 `add_tool` 的检查后写入——那对进程全局注册表是非原子的，并发建同名 agent 时会竞出 "already exist"。

**N11 权限投影三段式：统一名 → 引擎名 → 历史 fallback 名**
权限面 `project_operations("todo", args)`：先校验 `action ∈ {create, list, get, modify}`（非法 raise），产出引擎名操作 `todo_{action}`；modify 再校验 `modify_action ∈ {update, append, delete, cancel, insert_before, insert_after}` 并产出 `todo_modify`，同时**追加历史 fallback 名**——append/insert_* → `todo_insert`，delete/cancel → `todo_remove`，update 且含 completed 条目 → `todo_complete`。旧权限配置（`config.yaml` 三份、前端 `AvatarPermEditor` 五历史名）与新统一名都能匹配。

**N12 参数错误返回结构化 ToolOutput，不 raise**
未知 action、modify 缺 id 等参数错误返回带错误文案的 ToolOutput——flash 工具面约定（与 `flash_read_tool` / `flash_glob_tool` 的"整包一次性产出、不走增量流式"同族）。权限投影层（N11）的非法值才 raise（那是调用方 bug，不是模型输入）。

### 2.3 common 支线一：兼容 shim 与持久提醒（todo_compat + todo_resume）

**N13 monkeypatch 幂等 + 时序红线**
`install_todo_modify_compat_patch()` 以模块级标志防重入；在 `interface_deep.py` **模块 import 时**执行，必须早于任何 `TodoModifyTool` 实例化（flash 的 modify 引擎因此天然拿到兼容类）。装配时序与双命名空间换类细节归 [[S_01]]；本条在 S_05 的意义是：flash 统一工具的 modify 通道 = 兼容类的入口。

**N14 持久提醒双视图**
`filter_todos_for_execution`（执行地图视图，TaskExecutionRail 绑定用）**排除** persistent 标记条目；`filter_todos_for_display`（list/read 视图）**保留**（persistent 无条件保留，非 persistent 按代际 token 过滤）。"记一条待办"永远不进执行地图，但 todo_list 可见。快照面（`snapshot_includes_persistent_todos`）：list/get 恒展示；create 仅当产物含 persistent 条目；modify 仅当目标 id 命中 persistent——技能计划自身的快照留在执行地图上。

**N15 删除意图判定：动词 + 名词共现**
持久提醒仅当用户**点名删除**或**清全部**时才移除。`user_query_drops_reminder` 要求删除动词（删/去掉/不要了/移除/清空/退掉/划掉…）与「待办/提醒」名词**共现**，或命中显式短语（取消待办/不要这条提醒…）；**「取消」单独出现判为放弃执行计划，不带走提醒**。`user_query_drops_all_reminders` 在此之上再要求清全标记（都删/全删/所有待办/待办都…）——单独的「都」太宽。

**N16 代际重打**
`CompatibleTodoModifyTool._update_todos` 更新条目时**重打 generation_token**——被修改的旧代条目重新归入当代，续跑轮可见（否则修改动作本身会把条目"改丢"）。

**N17 词汇层纯度与代际 token 分工**
`todo_resume.py` 是纯词汇层：无 I/O、无 adapter/session 生命周期依赖。代际 token 的**生产者**是 agent_adapter 的 prepare hook（新一轮非续跑用户消息 → `bump_todo_generation_token`）；**消费者**是 TaskExecutionRail / StreamEventRail 的广播层与 openjiuwen todo 工具（打标 / 过滤）。`TODO_RESUME_SNAPSHOT_PENDING_KEY` 是 prepare 侧的快照暂存键。该体系取代旧的 stale ids / pre-invoke ids / skip 标志多层逐 id 比对。

### 2.4 common 支线二：用户个人待办（user_todo_tool）

**N18 stateless 模块单例 + per-channel ContextVar**
`user_todos` 以 `@tool(stateless=True)` 装饰为模块单例——**所有注册方必须共享同一实例**（多次注册会造出 ContextVar 视图不一致的副本）；channel 隔离靠请求级 ContextVar（`channel_id` / `created_by`），`_sanitize_channel_id` 防路径穿越（channel_id 直接拼进文件路径）。adapter 构建时 `set_global_workspace_dir` + `set_global_channel_id`，请求处理时再 set ContextVar。

**N19 独立存储树**
`{workspace}/memory/user_todos/{channel_id}.md`（YAML frontmatter + markdown 正文），与任务规划域的 `{workspace}/todo/{session_id}/todo.json` 是**两棵不相交的存储树**——字段语义也不同：TodoItem dataclass 带 `priority` / `due_at` / `remind_at` / `created_by`，六动作 list/get/create/update/delete/search。

**N20 域界声明**
`user_todos` 是**用户个人待办**（人记给自己的提醒），工具描述明示"勿用于 agent 内部任务规划（内部用 todo_create/todo_insert）"。域界靠 prompt 声明 + 字段语义 + 存储树三重隔离，无强制闸。

### 2.5 遗留登记

**R1 todo_toolkits.py 未装配，生产零消费**
`TodoToolkit`（markdown 行式五工具 todo_create/todo_complete/todo_insert/todo_remove/todo_list，`todo.md` 存 `get_agent_sessions_dir()/{session_id}/`，按 session 的文件锁 `_session_locks` ClassVar + `_meta_lock`，行格式 `- [ ] 1. task | status [| result]`）在全仓**无任何 import / 实例化**：仅 `avatar_rail.py` 提示词文本顺带提及工具名（非代码消费）+ 文件自身 docstring 声称 "registered in the openJiuwen Runner via TodoToolkit.get_tools()"——**该装配路径已不成立**。处置决策见修改检查单第 6 条。

## 3. 接口契约

| 公开面 | 定义位置 | 消费方 |
|---|---|---|
| `CodeTaskPlanningRail` | `code/rails/code_task_planning_rail.py` | `interface_code._build_code_task_planning_rail` / `swarm/providers/code_rails.py` |
| `CodeTodoCreateTool` 等四子类 | `code/tools/code_todo_tools.py` | 仅 `CodeTaskPlanningRail.init` |
| `CODE_TODO_TOOL_PROMPTS` + `get_code_todo_*_input_params()` ×3 | `code/prompt/code_todo_tool_prompts.py` | 四子类 `__init__` |
| `UnifiedTodoTool(engines, language, agent_id)` | `flash/tools/flash_todo.py` | `FlashTodoRail.init` |
| `FlashTodoRail` | `flash/rails/flash_todo_rail.py` | `interface_flash._build_task_planning_rail` |
| `_TODO_NAME_REPLACEMENTS`（四对：`todo_create → todo(action=create)` 等） | `flash/tools/flash_todo.py` | `FlashTodoRail.before_model_call` |
| `project_operations("todo", args)` | `flash/permissions/tool_policy_projection.py` | flash 权限面 |
| `install_todo_modify_compat_patch()` | `common/tools/todo_compat.py` | `interface_deep` 模块级（[[S_01]]） |
| `is_persistent_todo` / `mark_persistent_todo` / `clear_persistent_todo` | `common/tools/todo_resume.py` | todo_compat / TaskExecutionRail / 广播层 |
| `filter_todos_for_execution` / `filter_todos_for_display` | `common/tools/todo_resume.py` | TaskExecutionRail（[[S_02]]）/ StreamEventRail |
| `is_backlog_todo_query` / `user_query_drops_reminder` / `user_query_drops_all_reminders` | `common/tools/todo_resume.py` | agent_adapter prepare hooks / todo_compat |
| `bump_todo_generation_token` / `get_todo_generation_token` | `common/tools/todo_resume.py` | prepare hook（生产）/ 广播层（消费） |
| `remember_turn_user_query` / `read_turn_user_query` | `common/tools/todo_resume.py` | prepare hook（写）/ todo_compat（读） |
| `has_active_todo_items` / `is_resume_user_query` / `format_todo_snapshot_lines` / `build_interrupt_resume_*` / `todo_create_blocked_message` / `todo_create_read_failed_message` | `common/tools/todo_resume.py` | 中断续跑与 todo_create 守门文案（中断交互域消费） |
| `user_todos` 工具 + `get_decorated_tools()` + `set_global_workspace_dir` / `set_global_channel_id` / `set_created_by` | `common/tools/user_todo_tool.py` | `interface_code._build_user_todos_tool` / `swarm build_user_todos` |
| `TodoToolkit` / `get_tools()`（**未装配**） | `common/tools/todo_toolkits.py` | 无生产消费（R1） |

## 4. 数据结构

- **code**：无新结构——复用 openjiuwen `TodoItem` / `TodoStatus`；prompt 层 `_CODE_TODO_ITEM_PROPERTIES_EN` 定义五字段（id / content / activeForm / description / status）
- **flash**：统一工具参数 = `action` + 引擎原生参数（modify 再带 `modify_action` 六种：update / delete / cancel / append / insert_before / insert_after）；`_UNIFIED_TODO_ACTIONS = ("create", "list", "get", "modify")`
- **todo_toolkits（未装配）**：`TaskStatus`（WAITING / RUNNING / COMPLETED / CANCELLED）、`TodoTask(idx, tasks, status, result)`、行格式 `- [ ] 1. task | status [| result]`、`TODO_FILENAME="todo.md"`
- **todo_resume**：`PERSISTENT_TODO_META_KEY="persistent"`（挂在条目 `meta_data` 内）、`TURN_USER_QUERY_KEY="todo_turn_user_query"`、`TODO_RESUME_SNAPSHOT_PENDING_KEY`、代际 token（uuid str，存 session state，键来自 openjiuwen `TODO_GENERATION_TOKEN_SESSION_KEY`）
- **user_todo_tool**：`TodoItem` dataclass（id / title / status / priority / due_at / remind_at / created_at / updated_at / created_by / description）+ YAML frontmatter markdown

## 5. 执行路径全景

**P1 code 装配与提醒循环**
`interface_code._build_code_task_planning_rail()` 或 swarm `code_rails` → `CodeTaskPlanningRail()`（构造即关 progress_repeat）→ `init(agent)`：幂等扫描 + 补注册四子类 → 每轮 `before_model_call`：双阈值判定 → 命中则 `_load_fresh_todos` + `add_section` 注入 task_reminder 附件 → `after_tool_call`：`todo_` 前缀命中即清零计数 + 清附件 → `after_invoke`：finally 清 session 态。

**P2 flash 装配**
`interface_flash._build_task_planning_rail(config)`（兼容带参与不带参两种调用）→ `FlashTodoRail()` → `init(agent)`：取 workspace TODO 节点 + `shared_lock = TodoLockManager()` + 四引擎 + `UnifiedTodoTool` → `add_ability` 原子注册 → `before_model_call`：stock `build_todo_section` + 四对名称替换（`todo_create → todo(action=create)`…）注入，section 为 None 时移除。

**P3 flash 统一调用**
模型发 `todo(action=..., ...)` → `invoke` 透传 session → 按 action 分发引擎 → modify 再按 modify_action 分发 → 结构化 ToolOutput 整包返回（无增量流式）→ 权限面 `project_operations` 展开引擎名 + 历史 fallback 名。

**P4 compat patch 与 flash 的接合**
`interface_deep` import → `install_todo_modify_compat_patch()`（幂等）→ openjiuwen 命名空间 `TodoModifyTool = CompatibleTodoModifyTool` → flash 的 modify 引擎实例化时即兼容类（deleted/canceled 容忍）。细节归 [[S_01]]。

**P5 持久提醒生命周期**
用户说「记一条待办」→ prepare hook `remember_turn_user_query` 存轮次 query → `TodoCreateTool.invoke`（被 patch）读 turn query → `is_backlog_todo_query`：命中 backlog 标记（添加待办/记个待办/提醒我…）且**无** execute-now 标记（现在就做/马上做/开始执行…）→ `persist_backlog_create`：状态改 pending + `mark_persistent_todo` + 去 execute-now 提示 → 后续轮：prepare hook bump token，旧代条目被代际过滤，但 `filter_todos_for_display` 保留 persistent / `filter_todos_for_execution` 排除 → 新计划 `todo_create` 整文件替换时 `restore_persistent_reminders` 回填活跃提醒 / `todo_list` 的 `merge_persistent_list_items` 合并被 generation 过滤掉的提醒 → 用户点名删除或清全部（N15 判定）→ `_delete_todos` / `_cancel_todos` 解除豁免。

**P6 user_todos 流**
adapter 构建时 `set_global_workspace_dir` + `set_global_channel_id`（cron 上下文）→ 请求级 ContextVar set → 模型调 `user_todos`（六动作）→ YAML frontmatter markdown 读写 → 返回 dict。ImportError 时工具整体 skip（不阻断 agent）。

**P7 todo_toolkits（未装配，登记 only）**
`TodoToolkit(session_id).get_tools()` → 五个 LocalFunction → `todo.md` 行式读写（session 文件锁）。生产不可达（R1）。

## 6. 异常分支

- **E1** flash 统一工具参数错误 → 结构化 ToolOutput 错误文案（N12）；权限投影层非法 action / modify_action → raise ValueError（调用方 bug）
- **E2** `FlashTodoRail.init` 缺 ability_manager / workspace / sys_operation → warning + return（rail 空转，不炸 agent）；注册统一工具失败 → warning，engines 已建但不暴露
- **E3** `CodeTaskPlanningRail.init` add_ability 异常 → warning，已注册工具保留
- **E4** `user_todos` 模块不可导入 → info + 返回 None（工具缺失不阻断）
- **E5** `todo_create` 读计划失败 → `TODO_CREATE_READ_FAILED` 文案（指引 todo_list / 重试，勿覆盖）
- **E6** `todo_create` 遇活跃计划（pending/in_progress）→ `TODO_CREATE_BLOCKED` 文案（指引 todo_list + todo_modify 续跑；仅用户明确要求重来时 force=true）
- **E7** `is_resume_user_query`：query 为空或长度 > 32 直接 False——续跑启发式只认短消息；显式 replan 短语（从头开始）不匹配

## 7. 边界 case 清单（测试锚点）

| 主题 | 锚点 |
|---|---|
| code prompts schema 与描述 | `tests/unit_tests/agents/harness/code/test_code_todo_tool_prompts.py` |
| 统一卡 get 动作 / 缺 id / 未知 action | `tests/unit_tests/agentserver/test_flash_adapter_tool_face.py`（`test_unified_todo_card_exposes_get_action` / `test_unified_todo_get_forwards_id` / `test_unified_todo_get_requires_id` / `test_unified_todo_unsupported_action_returns_error`；eager_tools 合并含 `todo` 见 ~465/~485） |
| 统一工具权限生命周期 | `tests/unit_tests/agentserver/test_flash_permission_lifecycle.py`（~389-402：UnifiedTodoTool + `permission_adapter({"todo": "allow", "todo_create": "allow"})`） |
| todo 投影历史 fallback 名 | `tests/unit_tests/agentserver/test_flash_permission_actions.py`（~369-375：modify + todo_insert / todo_remove / todo_complete） |
| 持久提醒全链路 | `tests/unit_tests/agentserver/rails/test_persistent_todo.py` |
| 代际 token 过滤（跨 [[S_01]]/[[S_02]]） | `tests/unit_tests/agentserver/rails/test_todo_updated_stale_filter.py`、`test_stale_todo_skip_flag_on_runtime_session.py` |
| 前端内联协议清洗白名单 | `channels/web/frontend/src/utils/toolProtocol.ts`（五历史名 + `todo_` 前缀兜底） |

## 8. 修改检查单

1. **改 code todo 描述 / schema** → 只动 `code_todo_tool_prompts.py`（四子类与 rail 不动，N1）；跑 `test_code_todo_tool_prompts.py`
2. **改 flash 统一工具名 / 参数** → 同步：外层 task-loop 的 `todo_` 前缀钩子判定、前端工具名常量（`toolProtocol.ts` 白名单与前缀、`AvatarPermEditor` 五名、todo 刷新链路常量）、`tool_policy_projection` 的 action 白名单、`_TODO_NAME_REPLACEMENTS` 四对——任何一处漏改都是静默行为分叉（N7/N11）
3. **flash 新增第五动作** → `engines` dict + `UnifiedTodoTool` 分发 + `_UNIFIED_TODO_ACTIONS` + 权限投影白名单 + 测试
4. **动 persistent 标记语义** → 同步 `todo_resume` 三判定函数 + `todo_compat` 豁免逻辑 + 双视图过滤 + `test_persistent_todo.py`
5. **monkeypatch 时序红线** → `install_todo_modify_compat_patch` 必须保持 `interface_deep` 模块级调用；任何在 patch 前实例化 `TodoModifyTool` 的代码都会拿到未打补丁的类（N13）
6. **todo_toolkits 处置决策**（R1）→ 确认零消费后整文件删除（连带核对 `avatar_rail.py` 提示词文本的工具名提及），或补装配 + 补测试——当前状态名实不符，是最大的理解陷阱
7. **user_todos 新增动作** → `UserTodosParams` + `_handle_user_todos` 分发 + 工具描述同步（域界声明勿删，N20）
8. **调 code 提醒阈值** → 三个构造参数（`task_reminder_turns_since_management` / `task_reminder_turns_between_reminders` / `max_tracked_task_reminder_sessions`）有默认值，改默认需评估附件注入频率对上下文占用的影响

## 9. 与其它 spec 的关系

- **[[S_01]]**：本 spec 的 compat 层与代际 token 是 S_01 主线（todo.json + openjiuwen 工具链）之上的行为覆写；monkeypatch 装配时序详见 S_01
- **[[S_02]]**：`filter_todos_for_execution` 是 TaskExecutionRail 绑定执行地图的输入；flash 统一名绕过的正是 S_02 域的外层钩子（有意设计，非缺陷）
- **[[S_03]]**：收口判定只走 todo.json 主线；统一名 / 历史名投影不影响收口语义
- **[[S_04]]**：无直接耦合（计划审批域）；code 侧 plan 白名单排除的 task_tool 与本 spec 工具族无交集
- **[[S_06]]**：deepresearch 的 todo 落盘变体（下一份）
