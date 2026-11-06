# S_01 todo 存储与工具链

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_01 / todo-storage-and-toolchain |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/agents/harness/common/tools/todo_resume.py`、`todo_compat.py`、`todo_toolkits.py`；装配点 `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`（`install_todo_modify_compat_patch` 模块级调用） |
| 关联模块（agent-core 仓，跨仓契约） | `openjiuwen/harness/tools/todo.py`（主链四工具）、`openjiuwen/harness/schema/task.py`（`TodoItem` / `TodoStatus`）、`openjiuwen/harness/workspace/workspace.py`（workspace 布局中的 `todo` 目录） |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**任务规划域的 todo 数据层**：

- 主链四工具 `todo_create` / `todo_list` / `todo_get` / `todo_modify` 的参数契约、校验规则与错误语义（实现在 agent-core，契约从 jiuwenswarm 侧登记——agent-core 仓当前无 AGENTS.md 覆盖 `tools/todo.py`，本 spec 是其唯一契约文档）
- 持久化格式与并发控制：`todo.json` 文件、per-session 锁、`SysOperation` I/O
- 代际 token（请求隔离）的**打标 / 过滤 / 词汇层**：谁读、谁写、未打标语义
- persistent reminder（用户口述待办）的标记、保护与解除链
- jiuwenswarm 兼容层：`CompatibleTodoModifyTool`（客户端 `deleted` 形态归一 + reminder 保护）与 `TodoCreateTool` / `TodoListTool` 的两个 invoke patch
- `TodoToolkit` markdown 简化变体的契约登记（独立存储、独立枚举）

不管：

- rail 行为（`TaskPlanningRail` / `TaskExecutionRail` 的 guard 重置时机、todo_create 阻断决策、串行阻塞补发）→ [[S_02]]
- todo 收口文案与三帧流控 → [[S_03]]
- 计划审批双机制 → [[S_04]]
- profile 侧工具卡变体（`CodeTodo*` / `flash_todo` 的卡片文案）→ [[S_05]]
- deepresearch 子进程的 todo 落盘 → [[S_06]]
- 代际 token 的 **bump 生产者**逻辑（agent_adapter prepare hook 的触发时机）→ `jiuwenswarm/server/runtime/agent_adapter/AGENTS.md`
- 快照结构与会话级 todo 快照恢复 → `jiuwenswarm/common/AGENTS.md` 的 S_14（common 体系编号）；`filter_todos_by_generation` / `todo_item_generation_token` 实现在 `jiuwenswarm/common/todo_generation.py`，本 spec 词汇层只 re-export

**范围排除甄别**（名实不符件，不属于任务规划域，勿在本域 spec 内扩展）：

- `agents/harness/common/tools/user_todo_tool.py`：channel 域**用户个人待办**（`user_todos` 工具，per-channel 隔离，`priority` / `due_at` / `remind_at` 字段，YAML frontmatter markdown 存储）——与任务规划域的 todo.json 主链完全独立
- `agents/harness/common/tools/task_tools.py`：experience 经验记忆域（`experience_retrieve` / `learn` / `clear`）

## 不变量

- **N1（持久化路径唯一）**：主链 todo 数据只落 `{workspace}/todo/{session_id}/todo.json` 一处。工具内部拼接为 `workspace 参数 / {session_id} / todo.json`，装配层传入的 workspace 即 workspace 布局中的 `todo` 目录（`openjiuwen/harness/workspace/workspace.py` 定义）。上下文重注入读取同一拼接（`openjiuwen/core/context_engine/processor/forked/compressor/reinjection/builders.py` 的 `build_todo_reinjected_content`）。
- **N2（状态枚举固定）**：主链 `TodoStatus` 恰为 4 个值：`pending` / `in_progress` / `completed` / `cancelled`。`TodoToolkit` 变体用独立枚举 `waiting` / `running` / `completed` / `cancelled`（且 `running` 从未被写入——变体没有"开始执行"动作），两套枚举、两种存储互不混用。
- **N3（单 in_progress）**：`todo_modify` 的 `update` / `append` / `insert_after` / `insert_before` 落盘前必须通过 `_validate_single_in_progress`：当前代可见条目中 `in_progress` 至多 1 个。统计口径 = 代际过滤后（无 token 时全表），跨代残留的 in_progress 不占当前代名额。`todo_create` 不做此校验——首任务自动置 `in_progress` 是唯一合法入口。
- **N4（同 invoke 至多一次 todo_create）**：`TodoCreateTool._created_in_invoke` 集合 guard，第二次 `todo_create` 直接拒绝（`TOOL_TODOS_VALIDATION_INVALID`）。guard 由 agent-core `TaskPlanningRail.before_invoke` 调 `reset_invoke_marker` 重置（见 [[S_02]]）。违反后果：整表覆盖改写全部任务 id，task.run 段与前端任务列表失配乱序。
- **N5（代际 token 单向流）**：agent-core 侧对 token 只做三件事——读（`_session_generation_token`）、打标（create/update/append/insert 写入条目）、过滤（list 视图）——**从不 bump**。bump 唯一入口是 `todo_resume.bump_todo_generation_token`（uuid4 → `session.update_state`），生产者是 agent_adapter 的 prepare hook（fresh 非续跑轮才 bump；resume 轮保持原 token，见 `openjiuwen/harness/tools/todo.py` 模块头跨仓契约注释）。
- **N6（fail-open）**：无 `get_state` 的 session、state 里无 token、token 非字符串、条目未打标——四种情况全部放行不过滤。任何代际过滤代码不得把"缺信息"当"旧代"处理。
- **N7（锁）**：主链 per-session `asyncio.Lock`（`TodoLockManager`，全局锁只保护锁字典创建）；`load_todos` / `save_todos` 全程持锁；四工具必须共享同一 lock_manager 实例（`create_todos_tool` 工厂保证）。变体 `TodoToolkit` 用 per-session `threading.Lock`（同步 I/O）。
- **N8（reminder 不可被无关轮删除）**：`meta_data.persistent = true` 的条目（用户口述待办），仅当本轮 query 有删除意图**且**点名该条（id 或 content ≥4 字符的子串命中）或明确全清时才允许删除/取消；无删除意图的轮次一律保留。判定函数 `_keep_persistent_reminder`。
- **N9（触碰即归当前代）**：`update` / `append` / `insert_*` 写入的条目在 token 非空时重打当前 token（"复活/改写的条目对当前轮可见"）；`update` 把条目置为 `in_progress` 时同步摘除 persistent 标记（reminder 进入执行地图）。
- **N10（I/O 走 SysOperation）**：`load_todos` / `save_todos` 一律经 `SysOperation.fs()` 的 `read_file` / `write_file`（沙箱模式下文件落在沙箱内），不做本地 `os.path` 存在性预检查。变体 `TodoToolkit` 是例外（裸 `open`，仅非沙箱场景）。

## 接口契约

### 主链四工具（`openjiuwen/harness/tools/todo.py`）

四工具均为 `TodoTool` 子类：构造参数 `(operation: SysOperation, workspace, language="cn", agent_id, lock_manager)`；`invoke(inputs, **kwargs)` 从 `kwargs["session"]` 取 `session_id`（缺失即 `TOOL_TODOS_INVOKE_FAILED`）；`stream` 一律 `TOOL_STREAM_NOT_SUPPORTED`。工具卡文案经 `build_tool_card` 生成（profile 变体见 [[S_05]]）。

| 工具 | 输入 | 语义 | 返回 |
|---|---|---|---|
| `todo_create` | `tasks: [{content 必填非空, id?, activeForm?, description?, selected_model_id?}]` | **整表覆盖**。id 优先取模型提供值，缺省 uuid4；批内 id 重复拒绝。首任务自动 `in_progress`，其余 `pending` | message 含逐条 `task_id / content` 与 `Next step: Immediately execute task '<首任务>'` |
| `todo_list` | 无参 | 过滤 `completed` / `cancelled`，再过滤旧代（N6 口径） | `{"tasks": [{id, content, status, depends_on}]}` 精简四字段 |
| `todo_get` | `id: str` 必填 | 全表精确匹配，**不做**状态/代际过滤 | `{"todo": <TodoItem.to_dict()>}` 完整结构；未找到 `TOOL_TODOS_INVOKE_FAILED` |
| `todo_modify` | `action` + 各 action 参数（下表） | 六 action 分派 | message 字符串 |

`todo_modify` 六 action：

| action | 参数 | 校验要点 |
|---|---|---|
| `delete` | `ids: [str]` 非空 | 物理删除；未命中不报错，返回 `No tasks deleted: ...` |
| `cancel` | `ids: [str]` 非空 | 置 `cancelled`；未命中同上 |
| `update` | `todos: [{id 必填, 其余字段可选}]` | 部分字段更新；id 缺失或未找到的处理在主链与兼容层**不同**（见下） |
| `append` | `todos: [{content, status, id 必填}]` | `description` / `activeForm` 可缺省兜底空串；与现有 id 重复拒绝 |
| `insert_after` | `todo_data: {target_id, items}` | 目标状态须 `in_progress` / `pending` |
| `insert_before` | `todo_data: {target_id, items}` | 目标状态须 `pending` |

错误码全集：`TOOL_TODOS_INVOKE_FAILED`（session 缺失 / get 未找到 / 内层异常统一包装）、`TOOL_TODOS_VALIDATION_INVALID`（参数形态 / 重复 id / 二次 create / 多 in_progress）、`TOOL_TODOS_LOAD_FAILED`、`TOOL_TODOS_SAVE_FAILED`、`TOOL_STREAM_NOT_SUPPORTED`。

### jiuwenswarm 兼容层（`todo_compat.py`）

`CompatibleTodoModifyTool(_OpenJiuWenTodoModifyTool)` 重写三处，`install_todo_modify_compat_patch()` 在 `interface_deep.py` **模块 import 时**执行（必须早于任何 `TodoModifyTool` 实例化）：

1. 双命名空间换类：`openjiuwen.harness.tools.TodoModifyTool` 与 `openjiuwen.harness.tools.todo.TodoModifyTool` 都指向 `CompatibleTodoModifyTool`
2. `_install_backlog_create_patch(TodoCreateTool)`（幂等标志 `backlog_create_patched`）：invoke 前快照（`_load_todos_or_empty`，读失败降级空表）→ 原 invoke → `restore_persistent_reminders`（把仍活跃的 reminder 按 id 去重追加回被整表覆盖的新表）→ `persist_backlog_create`（本轮是 backlog query 时，把新表中非 persistent 条目全部标记 persistent + 置 pending，并从 message 删除 `Next step:` 行、追加 reminder 说明后缀）
3. `_install_backlog_list_patch(TodoListTool)`（幂等标志 `backlog_list_patched`）：原 invoke 后 `merge_persistent_list_items` 把被代际过滤掉的活跃 reminder 以精简四字段合并回 `tasks`

`CompatibleTodoModifyTool` 自身三处重写：

- `invoke`：读 `TURN_USER_QUERY_KEY` 存入 `_TURN_QUERY` ContextVar（跟调用走不挂实例，防并发串轮），finally reset
- `_delete_todos` / `_cancel_todos`：命中 id 的条目先过 `_keep_persistent_reminder`（N8），保留的在返回消息中报告 `Kept N reminder(s)`
- `_update_todos`：与主链的差异——`todos` 非列表、id 缺失、id 未找到**直接 raise**（主链是聚合错误继续）；`"canceled"` 归一为 `"cancelled"`；`status ∈ {"deleted", "delete"}` 视为物理删除、`"cancelled"` 视为取消（客户端把删除写成 update payload 的兼容形态）；drop 意图的条目过 N8 保护；置 `in_progress` 时 `clear_persistent_todo`；token 重打与主链对齐；最终消息由 updated / deleted / kept 三段拼接

### 词汇层（`todo_resume.py`，纯函数无 I/O）

| 入口 | 语义 |
|---|---|
| `bump_todo_generation_token(session) -> str` | 生成 uuid4 写 state 并返回（N5 唯一 bump 点） |
| `get_todo_generation_token(session) -> str \| None` | fail-open 读 |
| `is_resume_user_query(query) -> bool` | 6 个 `_RESUME_QUERY_PATTERNS` 正则（继续/重试/continue 系列 + 宽松式"继续…任务/工作/执行/完成/做/跑/吧"）；query 去标点后 **超过 32 字符直接不判 resume**；显式重规划短语（如"从头开始"）不匹配 |
| `is_backlog_todo_query(query) -> bool` | 命中 7 条 `_BACKLOG_QUERY_MARKERS`（"添加一个待办"…"提醒我"）且不命中 6 条 `_EXECUTE_NOW_MARKERS`（"现在就做"…"立即执行"） |
| `user_query_drops_reminder(query) / user_query_drops_all_reminders(query)` | 删除意图 = 命中 10 条 `_REMINDER_DROP_PHRASES` 之一，或（命中 10 个 `_REMINDER_DROP_VERBS` 之一 **且** 命中 2 个 `_REMINDER_DROP_NOUNS` 之一）；全清 = 有删除意图且命中 9 条 `_REMINDER_DROP_ALL_MARKERS` 之一 |
| `is_persistent_todo / mark_persistent_todo / clear_persistent_todo` | `meta_data["persistent"] is True`（严格 `is True`）的读 / 写 / 摘 |
| `filter_todos_for_execution(items, token)` | 执行地图视图：代际过滤（re-export 自 common）**再剔除 reminders** |
| `filter_todos_for_display(items, token)` | 显示视图：当前代条目 + 全部 reminders |
| `snapshot_includes_persistent_todos(tool_name, arguments, items)` | `todo_list` / `todo_get` 恒含；`todo_create` 当且仅当新表含 reminder；`todo_modify` 当且仅当目标 ids 与 reminder ids 相交 |
| `has_active_todo_items(todos)` | 存在 `pending` / `in_progress` |
| `remember_turn_user_query / read_turn_user_query` | 本轮用户原话存取（state 键 `todo_turn_user_query`），compat 层保护判定依赖它 |

消息/提示词常量（消费时机归 [[S_02]]，文案本体归本 spec）：`TODO_CREATE_BLOCKED_CN/EN`（阻断 todo_create 引导 todo_modify 续跑，提及 `force=true` 逃生门）、`TODO_CREATE_READ_FAILED_CN/EN`、`INTERRUPT_RESUME_DECISION_PROMPT_CN/EN`、`INTERRUPT_RESUME_TODO_REMINDER_CN/EN`。

### TodoToolkit 变体（`todo_toolkits.py`）

五工具 `todo_create` / `todo_complete` / `todo_insert` / `todo_remove` / `todo_list`，经 `LocalFunction` 注册。**当前两个仓均无生产调用方**（仅 docstring 示例与 import 留存）——改它之前先确认没有外部使用者。与主链差异：markdown 存储（`{agent_sessions_dir}/{session_id}/todo.md`，行格式 `- [ ] 1. task | status [| result]`）、`TaskStatus` 独立枚举（N2）、threading 锁、`todo_create` 在文件已存在时**报错拒绝**（与主链整表覆盖语义相反）、按 idx 定位而非 id、无代际 token、无 persistent 机制。

## 数据结构

`TodoItem`（`openjiuwen/harness/schema/task.py`，pydantic）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | 模型提供的短语义串优先，uuid4 兜底 |
| `content` / `activeForm` / `description` | str | 摘要 / 进行时态 / 详情，缺省空串 |
| `status` | TodoStatus | 四值枚举（N2） |
| `depends_on` | List[str] | 前置任务 id（预留） |
| `result_summary` | Optional[str] | 完成摘要（预留） |
| `meta_data` | Optional[Dict] | `persistent: true` 即 reminder 标记（N8） |
| `selected_model_id` | Optional[str] | 执行该任务的模型 |
| `generation_token` | Optional[str] | 代际标记（N5/N9） |

`todo.json` = `TodoItem.to_dict()` 的 JSON 数组（`ensure_ascii=False, indent=2`）。旧文件缺字段时 `from_dict` 逐项兜底，不要求迁移。

session state 三键：`todo_generation_token`（常量定义在 agent-core `tools/todo.py`，jiuwenswarm 侧 import 共享——跨仓改键名两边同时改）、`todo_turn_user_query`、`jiuwenclaw_todo_resume_snapshot_pending`。

变体 `TodoTask`：`idx`（1-based）/ `tasks` / `status` / `result` 四字段；解析常量 `PARTS_MIN_COUNT_WITH_RESULT = 3`、`PARTS_INDEX_RESULT = 2`（`|` 分段）。

## 执行路径全景

- **P1 装配与 patch**：openjiuwen harness workspace 布局建 `todo` 目录 → `create_todos_tool(operation, workspace, ...)` 工厂造四工具（共享一个 `TodoLockManager`）→ jiuwenswarm `agent_adapter/interface_deep.py` import 时 `TodoModifyTool = CompatibleTodoModifyTool; install_todo_modify_compat_patch()`（双命名空间换类 + 两 patch，幂等标志防重复安装）。
- **P2 todo_create**：`TodoCreateTool.invoke` → `_reject_recreate_within_invoke`（N4）→ 逐项校验（content 必填 / id 去重 / 首任务 in_progress）→ `save_todos` 整表覆盖 → patch 层 `restore_persistent_reminders` → `persist_backlog_create`。
- **P3 todo_list**：`TodoListTool.invoke` → `load_todos` → 滤 completed/cancelled → 滤旧代（N6）→ 精简四字段 → patch 层 `merge_persistent_list_items`。
- **P4 todo_get**：`TodoGetTool.invoke` → `load_todos` → 全表精确 id 匹配 → 完整 `to_dict`。
- **P5 todo_modify**：`CompatibleTodoModifyTool.invoke`（设 `_TURN_QUERY`）→ 六 action 分派 → 各 action 内 N8 保护 / N9 重打 / N3 校验 → `save_todos` →（finally reset ContextVar）。
- **P6 代际 token 生命周期**：agent_adapter prepare hook 在 fresh 非续跑轮调 `bump_todo_generation_token`（P 详见 agent_adapter AGENTS.md）→ 本轮工具调用打标（P2/P5）→ 视图过滤（P3 内嵌 + `filter_todos_for_execution/display` + 广播层，广播层归 agent_adapter / [[S_02]]）。
- **P7 reminder 生命周期**：`is_backlog_todo_query` 判定本轮 → P2 的 `persist_backlog_create` 标记 → P5 三路保护（delete/cancel/update）→ 解除（点名删除 / 全清 / 置 in_progress）。

## 异常分支与退出路径

| 分型 | 触发 | 行为 |
|---|---|---|
| 参数无效 | tasks 空 / content 缺 / id 重复 / ids 非法 / action 未知 / 二次 create（N4）/ 多 in_progress（N3） | `TOOL_TODOS_VALIDATION_INVALID`，不落盘 |
| 读写失败 | `read_file` / `write_file` 非 0 或抛异常 | `TOOL_TODOS_LOAD_FAILED` / `TOOL_TODOS_SAVE_FAILED`；沙箱内文件不存在同样走此路径（N10，不预检） |
| 未命中 | delete / cancel 的 ids 全部不存在 | **不报错**，返回 `No tasks ...: None of the provided IDs ... were found` 消息 |
| update 部分失败 | 主链：某条 id 未找到 | 聚合进消息继续（`Updated N task(s). M item(s) failed: ...`） |
| update 硬失败 | 兼容层：todos 非列表 / id 缺失 / id 未找到 | 直接 raise（与主链聚合语义**有意不同**，兼容层面向客户端严格形态） |
| patch 前快照读失败 | `_load_todos_or_empty` | 降级空表（无 reminder 可恢复），非"not found"的异常记 warning 不中断 |
| stream 调用 | 任何四工具 | `TOOL_STREAM_NOT_SUPPORTED` |

违反不变量的后果：N4 → 任务 id 全量改写、task-run 段失配、前端列表乱序；N8 → 用户口述提醒被无关轮静默删除；N5 反向（agent-core bump）→ resume 回放丢失原代 todo 可见性。

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| "继续excel表格任务" 等长续跑语 | 宽松正则命中 resume；>32 字符一律不判 resume | 宽松正则误吞普通指令 | `test_persistent_todo.py` 词表用例；resume 判定由 agent_adapter 侧测试覆盖 |
| 客户端发 `{"status": "deleted"}` | 兼容层 `_update_todos` 转物理删除；`"canceled"` 归一 `cancelled` | 主链直连（未装 patch）时该形态被拒 | `test_persistent_todo.py::test_modify_to_in_progress_clears_persistent_mark` 等 |
| 删除意图轮的批量 cancel | 未点名的 reminders 保留并报告 Kept | 用户以为删了 | `test_bulk_cancel_on_a_delete_turn_keeps_unnamed_reminders` |
| todo_create 整表覆盖撞上 reminders | 先快照后覆盖再按 id 去重追加活跃 reminder；已 completed/cancelled 的 reminder 不恢复 | 重复追加 / 死条目复活 | `test_restore_drops_finished_reminder_and_duplicate_id`、`test_ordinary_create_keeps_existing_reminder` |
| 旧代 reminder 在 todo_list | `merge_persistent_list_items` 合并回显示 | reminder 随代际过滤消失 | `test_list_merge_returns_old_generation_reminder` |
| 无 token 会话（旧数据/异常态） | 全量放行（N6） | 过滤误伤 | agent-core `TestTodoGenerationToken::test_list_without_session_token_shows_all_active`、`test_create_with_non_string_token_is_fail_open` |
| 跨代 in_progress 残留 | 不占当前代名额（N3 口径） | append 被误拒 | `test_modify_append_tolerates_superseded_in_progress` |
| 变体已有 todo.md 再 create | 报错并提示用 todo_insert（与主链整表覆盖相反） | 两链语义混淆 | 变体无专属测试（无调用方） |
| `id` / `content` 短于 4 字符的 reminder | `_query_names_todo` 不做子串点名匹配（≥4 才参与） | 短 id 误命中 | `test_named_delete_leaves_the_other_reminder` |
| backlog query 同时含"现在就做" | `_EXECUTE_NOW_MARKERS` 优先，判为执行不判待办 | 提醒被当任务跑 | `test_backlog_query_detects_add_todo_and_rejects_execute_now` |

## 修改检查单

- 改 `TodoItem` 字段 → 连带 `to_dict` / `from_dict` 兜底（旧 todo.json 反序列化）、`STATUS_ICONS`、`reinjection/builders.py` 的快照重注入、agent-core `test_todo.py` 序列化用例
- 改 `TodoStatus` 枚举值 → 前端 / 事件流 / 变体枚举（N2）联动 + 用户契约 `docs/zh/任务规划.md`
- 改词表（`_BACKLOG_QUERY_MARKERS` 等 6 张）或 resume 正则 → `test_persistent_todo.py` 词表用例同步 + 自查误判面（宽松正则每扩一条都要过一遍常见指令）
- 改 patch 安装时机 → 确认仍早于任何 `TodoModifyTool` 实例化（`interface_deep.py` import 顺序）
- 改 todo.json 路径拼接 → common S_14 快照读取、`reinjection/builders.py` 重注入、用户契约文档三处同步
- 改 N3 校验口径 → 跨代容忍用例（`TestTodoGenerationToken`）必须仍然通过
- 改 `TODO_GENERATION_TOKEN_SESSION_KEY` 键名 → agent-core 与 jiuwenswarm 两侧常量同时改（单一常量跨仓共享）
- 改变体 todo.md 行格式 → `PARTS_MIN_COUNT_WITH_RESULT` / `PARTS_INDEX_RESULT` 与旧文件解析兼容

## 与其它 spec 的关系

- [[S_02]]（任务执行 rail）：本 spec 的 guard（N4）由 rail 的 `before_invoke` 重置；`TODO_CREATE_BLOCKED` 等消息常量的**消费时机**（何时阻断 todo_create）归 S_02；`filter_todos_for_execution` 的执行地图消费方是 TaskExecutionRail
- [[S_03]]（收口流控）：三帧判定读取本 spec 的 todo 状态与 `has_active_todo_items`
- [[S_05]]（profile 变体）：`CodeTodo*` 四子类与 flash todo 复用本 spec 的存储 / 锁 / 代际契约，只换工具卡与装配
- [[S_06]]（deepresearch 落盘）：独立落盘链，不经过 todo.json
- `jiuwenswarm/common/AGENTS.md` S_14（common 体系编号）：`filter_todos_by_generation` / `todo_item_generation_token` 的实现与快照结构归其管辖，本 spec re-export
- `jiuwenswarm/server/runtime/agent_adapter/AGENTS.md`：bump 生产者（`stale_todo_cleanup_helpers.py`）、广播层过滤、patch 安装点（`interface_deep.py`）的装配职责
- 用户侧契约：`docs/zh/任务规划.md`（四工具 / 路径 / 四状态的用户表述）——语义变更时同步修订
- agent-core 仓：`tools/todo.py` 无本仓 AGENTS.md 覆盖，本 spec 即其契约文档；跨仓改动（agent-core 侧）同样受本 spec 约束并按 `docs/AGENTS.md` 提交约定归档
