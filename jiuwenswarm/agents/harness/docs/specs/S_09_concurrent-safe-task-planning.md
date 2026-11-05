# S_09 并发安全与计划一致性

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_09 / concurrent-safe-task-planning |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/agents/harness/common/rails/concurrent_safe_rails.py`（两 rail 子类 + `_tp_light_skip_refresh`）；装配点 `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`（`_build_task_planning_rail` :9834、`_is_sandbox_bound_rail` :8058） |
| 关联模块（agent-core 仓，跨仓契约） | `openjiuwen.harness.rails.SysOperationRail`、`openjiuwen.harness.rails.task_planning_rail.TaskPlanningRail`（父类：guard 重置、todo_create 阻断等）、`openjiuwen.core.runner.Runner.resource_mgr`（进程级工具注册表）、`openjiuwen.harness.schema.task.TaskPlan / TodoItem / TodoStatus`、todo 三工具 |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**多 agent 并发共享注册表时的工具注册安全**与**todo.json ↔ TaskPlan 双向一致性**：

- `ConcurrentSafeSysOperationRail`：同名 bot_id / service_id 多并发 agent 共享 `Runner.resource_mgr` 时跳过重复 `add_tool`（只首个注册成功，其余会打 "resource already exist" ERROR，60 并发一次约 1560 行）
- `ConcurrentSafeTaskPlanningRail`（别名 `ConcurrentSafeFileSystemRail`）：todo 工具的 binding 化注册 + todo 写后刷新 TaskPlan + 迭代末 plan→disk→plan 双向同步 + 中断清 plan + terminal 不降级
- 轻量刷新开关 `_tp_light_skip_refresh`（env `JIUWENCLAW_ADAPTER_CKPT_LIGHT`）

不管：

- 父类 `TaskPlanningRail` 的行为本体（todo_create 阻断、guard 重置、串行补发）→ [[S_02]]
- todo 工具参数契约与 todo.json 存储 → [[S_01]]
- LLM 调用层 / 工具循环熔断 → [[S_07]] / [[S_08]]
- `Runner.resource_mgr` 注册表本体（agent-core 机制）
- flash 域替代品：`agents/harness/flash/rails/flash_todo_rail.py` 自述 "replaces ConcurrentSafeTaskPlanningRail"——flash 域独立 rail，不受本 spec 约束

**范围排除甄别**：本文件的 "concurrent safe" 指**进程内多 agent 实例并发注册**（同 resource_id 的 add_tool 竞争），不是多进程 / 分布式并发，也不是 asyncio 任务级数据竞争（后者靠 [[S_01]] N7 的 per-session 锁）。

## 不变量

- **N1（注册前查表）**：`init()` 先 `Runner.resource_mgr.get_tool(card.id)` 查存在性，只把**不存在的**工具批量 `add_tool`；工具卡仍逐一加入**本 agent 自己的** `ability_manager`——注册表全局唯一，能力表 per-agent。文件七工具（read / write / edit / glob / list_dir / grep / bash）按此模式。
- **N2（todo 工具 binding 化）**：`ConcurrentSafeTaskPlanningRail.init` 用 `sha256(json.dumps([workspace_dir, id(fs)]))[:24]` 作 binding，`scoped_agent_id = f"{agent_id or 'todo'}_{binding}"`——工具身份绑定 **workspace 目录 + fs 实例**，不止 agent_id。`matches_binding(tool)` 要求 `tool.workspace == workspace_dir and tool.fs is fs`（`is` 同一实例）。
- **N3（三段式注册原子性）**：先扫描 ability_manager 收集既有工具（binding 匹配的复用）→ 再对缺失类查注册表（已注册但 binding 不符或类型不符 → `_TodoBindingMismatchError` **抛出**）→ 全部校验通过后才**依次**执行：摘 stale 能力 → `add_tool(new)` → `add(card)`。late mismatch 不留半注册状态（注册表与能力表均未被触碰）。
- **N4（写后必刷）**：`after_tool_call` 对 `todo_create` / `todo_modify`（`_TODO_WRITE_TOOLS`）**无条件**重读磁盘刷新 TaskPlan——不 gate on tool_result（resume 路径可能带空 result 但写已发生；中断时文件未变、重读无害）。
- **N5（迭代末双同步顺序铁律）**：`after_task_iteration` 先 `super()`（plan→disk 落内存完成度）**再** `_refresh_task_plan_from_todos`（disk→plan）。**反序会让磁盘上的陈旧 PENDING 覆盖内存完成度，外层循环永远重放同一 query**（源码注释记录了该历史 bug）。
- **N6（中断清 plan）**：`_iteration_interrupted`（inputs.result 为 dict 且 `result_type == "interrupt"`）时清空 TaskPlan 并跳过一切同步——陈旧桥接不得驱动 OuterLoop。
- **N7（terminal 不降级）**：`_sync_todos_from_plan`（plan→disk 方向）永不把 `COMPLETED` / `CANCELLED` 的 todo 改回 pending / in_progress（跳过并计数日志）；persistent reminder（[[S_01]] N8 标记）不参与同步。
- **N8（内层回调不改共享 ctx）**：工具回调跑在内层 ReActAgent 上，而 TaskPlan 属于初始化 rail 的 DeepAgent——`after_tool_call` 发现 ctx.agent 无 `load_state` 时 `copy(ctx)` 换 `_state_agent` 再走 super，**原 ctx 不被变异**。
- **N9（轻量刷新开关语义）**：`JIUWENCLAW_ADAPTER_CKPT_LIGHT ∈ {1,true,yes,on}` 且内存 state 无 `task_plan` 时跳过 N5 的 disk→plan 刷新（该场景本次读盘本会返回空）。**有 plan 的 todo 流不受影响**；判定异常走原路径。性能背景：刷新实测每迭代 62ms 均值 / 739ms 峰值（PVC 文件读）。
- **N10（非 DeepAgent 静默跳过）**：`init` 对非（DeepAgent + deep_config + ability_manager）形态直接 return，不注册、不抛错。

## 接口契约

### ConcurrentSafeSysOperationRail（`SysOperationRail` 子类）

`init(agent)`：语言取 `system_prompt_builder.language`（缺省 cn）；`enable_read_image_multimodal` 三级（rail 属性 → `deep_config.enable_read_image_multimodal` → True）→ 造七工具 → N1 查表注册 → 逐卡入 ability_manager。别名 `ConcurrentSafeFileSystemRail`（configs / 旧文档命名兼容）。

### ConcurrentSafeTaskPlanningRail（`TaskPlanningRail` 子类）

| 成员 | 契约 |
|---|---|
| `init(agent)` | N2/N3 binding 注册；非 DeepAgent 走 N10；`sys_operation` / `workspace` 缺时从 deep_config 补设 |
| `after_tool_call(ctx)` | N8 换 agent → super → N4 写后刷 |
| `after_task_iteration(ctx)` | N6 中断清 → N5 双同步（中间夹 N9 轻量跳过） |
| `_sync_todos_from_plan(ctx)` | plan→disk：状态映射按 task.id 对齐；N7 保护；无变化不落盘 |
| `_refresh_task_plan_from_todos(ctx)` | disk→plan：剔除 reminders 后整体覆写 tasks + current_task_id（取首个 in_progress）；全为 reminder → 清 plan；plan 为 None 则新建（goal="refreshed from todo list"） |
| `_clear_task_plan(ctx)` | `state.task_plan = None` 落盘 |
| `_iteration_interrupted(ctx)` | `result_type == "interrupt"` 判定 |
| `_todo_to_task_item(todo)` | `model_copy(deep=True)` 深拷贝入 plan |

### 装配（interface_deep）

- `_build_task_planning_rail`：`react.task_planning` 节经 `resolve_task_planning_rail_kwargs`（agent-core）解析 kwargs → **一律** `ConcurrentSafeTaskPlanningRail(**rail_kwargs)`（deep 主链不直接用父类）；失败 warning + None。
- `_is_sandbox_bound_rail`：`ConcurrentSafeSysOperationRail` 按"动手类 rail"处理（跟 deep_config / 沙箱 sysop，不强制本地）。

## 数据结构

- `ToolCallRecord` 之类无（本文件无独立数据结构）
- binding 派生输入：`workspace_dir = str(workspace.get_node_path(WorkspaceNode.TODO))`、`fs = sys_operation.fs()`
- state：`task_plan: TaskPlan | None`（`tasks: list[TodoItem]` + `current_task_id`）
- 常量：`_TODO_WRITE_TOOLS = {"todo_create", "todo_modify"}`

## 执行路径全景

- **P1 文件 rail 注册**：agent init → 七工具构造 → `get_tool` 查重 → 首个 agent `add_tool`，后续 agent 跳过 → 各自 `ability_manager.add(card)`。
- **P2 todo rail 注册**：DeepAgent init → N2 算 binding → 扫 ability_manager 复用匹配 binding 的既有工具 → 缺失类查注册表（N3 校验）→ 摘 stale / 注册新 / 加能力 → `self.tools` 就位。
- **P3 todo 写后**：`todo_create` / `todo_modify` 返回 → N8 内层 ctx 处理 → N4 重读 todo.json 刷 TaskPlan。
- **P4 迭代末**：非中断 → super（plan→disk，N7 保护）→ N9 判定 → disk→plan 刷 TaskPlan。
- **P5 中断**：`result_type == "interrupt"` → 清 plan → return（不同步）。

## 异常分支与退出路径

| 分型 | 触发 | 行为 |
|---|---|---|
| binding 冲突 | 注册表已有同 id 但类型 / binding 不符 | `_TodoBindingMismatchError` 抛出（`logger.exception`），**注册表 / 能力表零改动**（N3） |
| 注册失败 | 其它 add 异常 | warning 日志，`self.tools` 可能不全（不抛） |
| 读盘失败 | `load_todos` 抛错 | 两个方向均 debug 日志跳过（fail-open，不动 state） |
| 空表 | todos 空 / 全是 reminder | sync 跳过；refresh 清 plan |
| 非 DeepAgent | agent 形态不符 | init 静默 return（N10） |
| 内层无 state 能力 | ctx.agent 无 load_state 且无 `_state_agent` | after_tool_call return |
| 轻量开关判定异常 | env / load_state 抛错 | 走原路径（N9 fail-open） |

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| 复用注册表已注册实例后新轮 todo_create | 实例的 invoke guard 被重置、新表替换 | 复用实例带旧 guard 拒绝合法 create | `test_registered_create_instance_is_reset_and_new_round_replaces_list` |
| checklist 场景 | 保留原外层执行计划桥接 | 桥被误清 | `test_checklist_retains_original_outer_execution_plan_bridge` |
| 迭代末内存完成度 | 先落盘再刷新（N5） | 反序覆盖完成度 → 外层无限重放 | `test_after_task_iteration_persists_memory_completion_before_disk_refresh` |
| persistent reminder 混在执行任务中 | 不入 plan、保持 pending | reminder 被当执行任务驱动 | `test_persistent_reminder_stays_pending_outside_the_plan` |
| 文件只剩 reminders | 清空执行计划 | 空计划残留驱动 | `test_reminder_only_file_clears_the_execution_plan` |
| 内层回调 | copy ctx 换 owner agent，原 ctx 不变异（N8） | 共享 ctx 被并发污染 | `test_inner_callback_syncs_owner_plan_without_mutating_context` |
| 既有 plan 同步 | terminal todos 保留（N7） | 完成度被陈旧 plan 降级 | `test_existing_plan_sync_preserves_terminal_todos` |
| rail 未初始化 | 内层回调跳过 | —— | `test_uninitialized_rail_skips_inner_callback` |
| late binding 冲突 | 无半注册（N3） | 注册表 / 能力表脏状态 | `test_late_binding_conflict_fails_without_partial_registration`（参数化） |
| 不同 workspace / fs 的注册表项 | binding 隔离互不干扰 | 跨 agent 偷工具 | `test_registry_keeps_workspace_and_filesystem_bindings_isolated` |

## 修改检查单

- 改 N5 顺序 → `test_after_task_iteration_persists_memory_completion_before_disk_refresh` 必须仍然通过（该测试就是反序 bug 的回归钉）
- 改 binding 派生（workspace_dir / id(fs) 入参）→ N3 冲突路径 + 隔离用例回归；`matches_binding` 的 `is fs` 身份判定不可放松为 `==`
- 改 `_TODO_WRITE_TOOLS` → N4 写后刷覆盖面变化（新 todo 写工具需入列）
- 改 N7 terminal 保护 → `test_existing_plan_sync_preserves_terminal_todos` 同步
- 改 N9 开关（env 名 / 判定条件）→ "有 plan 不跳过"语义保持；性能语义（62ms/739ms）重新实测
- 改中断判定（`result_type == "interrupt"`）→ 与 agent-core 中断协议对齐
- 改 `after_tool_call` 的 N8 copy 逻辑 → 共享 ctx 变异用例回归
- 装配侧改用非 ConcurrentSafe 子类 → 并发压测下 "resource already exist" ERROR 回潮

## 与其它 spec 的关系

- [[S_01]]（todo 存储）：本 spec 的两个同步方向消费 S_01 的 `load_todos` / `save_todos` / 锁 / `is_persistent_todo`；reminder 语义（N7 排除项）源自 S_01 N8
- [[S_02]]（任务执行 rail）：父类 `TaskPlanningRail` 行为本体的契约归 S_02；本 spec 只登记子类的增量（注册安全 + 一致性加固）
- [[S_07]] / [[S_08]]：可靠性域兄弟件——本 spec 管"并发与一致性"维度，异常源是注册竞争与状态漂移，非 LLM / 工具失败
- `jiuwenswarm/server/runtime/agent_adapter/AGENTS.md`：装配职责（`_build_task_planning_rail` 一律走子类、sandbox 绑定识别）
- flash 域：`flash_todo_rail.py` 是本 rail 的替代品（flash 场景），其契约归 flash 域文档
