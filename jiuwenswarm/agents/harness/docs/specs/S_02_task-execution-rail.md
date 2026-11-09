# S_02 — 任务执行 rail：todo 状态 → 前端任务事件与自动推进

| | |
|---|---|
| Spec ID | S_02 |
| 状态 | 当前实现的真实描述 |
| 修订日期 | 2026-10-28 |
| 关联模块 | `jiuwenswarm/agents/harness/common/rails/task_execution_rail.py`（3,113 行，主体）<br>`jiuwenswarm/agents/harness/code/rails/code_task_planning_rail.py`（304 行）<br>`jiuwenswarm/common/todo_snapshot.py`（串行 overlay + 前端快照，纯函数）<br>`jiuwenswarm/common/todo_generation.py`（代际过滤纯函数；token 语义见 S_01 N5）<br>上游依赖（agent-core，无本仓 AGENTS.md 覆盖）：`openjiuwen/harness/rails/base.py`（DeepAgentRail）、`openjiuwen/harness/rails/task_planning_rail.py`（CodeTaskPlanningRail 的父类） |
| 装配点 | deep 侧：`server/runtime/agent_adapter/interface_deep.py` `_build_task_execution_rail` + `_RailBuildInfo("_task_execution_rail", ...)`，config 门控 `enable_task_execution_rail`（默认 **False**），切换需重启实例（by design）；每轮 `bind_request` 经 `set_skill_turbo_request_metadata(meta)` 注入请求 metadata 副本。code 侧：`interface_code.py` 与 `swarm/providers/code_rails.py` 各返回一个 `CodeTaskPlanningRail()` |

## 1. 范围与边界

本 spec 管 **"todo.json 的状态变化如何变成前端任务事件，以及工作工具如何自动推进 todo 状态"**：

- `TaskExecutionRail` 的全部生命周期钩子与状态迁移检测（task.start / task.complete / task.update）
- 串行 todo 顺序约束（defer / flush / rebuild / overlay 四件套）
- 工作工具触发的 auto_advance（pending→in_progress）与 lazy_start（todo_create 延迟启动的补发）
- 工具-任务绑定（`_ACTIVE_TASK_ID` ContextVar + 打分匹配）
- skill_complete 阻断、`external_todo_completion_events` 外部完成路径
- 产物检测与后处理 hook（文件内 1,100+ 行，与 skill_turbo 的 `SkillTurboArtifactRail` 共享常量与入口）
- `CodeTaskPlanningRail`（code profile 的任务规划 rail：工具注册 + task_reminder attachment + 模型切换）

**不管**（指向其它 spec）：

- todo 数据层四工具与 todo.json CRUD → S_01
- 纯文本收尾拦截（close nudge）与 hold/drop/release 三帧 → **S_03**（同一文件 `task_execution_rail.py` 内的另一机制面，方法前缀 `_todo_close_*` / `_attach_todo_close_nudge` / `_arm/_finish/_retry` 均归 S_03）
- 计划审批双机制 → S_04
- flash 统一卡 / code 的 CodeTodo* 工具卡变体（`flash_todo_rail.py`、`code/tools/code_todo_tools.py`）→ S_05
- deepresearch 的 todo 落盘 → S_06

## 2. 核心不变量

**N1 事件映射固定**
- `pending → in_progress` 发 `task.start`；`in_progress → completed` 发 `task.complete`；`pending → completed`（跳过 in_progress）**先发 task.start 再发 task.complete**（前端 hidePending 隐藏 pending 行，漏发 start 会让该段直到冻结快照才可见）
- 每次 todo 工具调用后（`_sync_todo_and_emit_transitions` 尾部）总发一次 `task.update` 全量快照
- 迁移检测是 **before/after diff**（`_todo_map_before_tool` vs 工具执行后重读磁盘），不监听工具返回值

**N2 todo_create 永不立即发 task.start**
`defer_start_on_create = (tool_name == "todo_create")`：create 轮首项标 in_progress 只记状态不发事件（否则"只建待办+回复用户"的提醒流会把正文吞进任务段），首个**非 todo 工作工具**经 `_lazy_start_in_progress_todo_on_work_tool` 补发。todo_modify 与工作工具路径不受此约束。

**N3 串行顺序约束**
`_has_earlier_incomplete`（index 更小且有未完成项）为真时，后项的 start/complete 一律入 `_todo_start_deferred` / `_todo_complete_deferred` 暂存；前缀完成后 `_flush_serial_todo_transitions` 按列表序补发。UI 侧 `overlay_serial_todo_statuses` 把"前缀未完时已完成的行"盖回 pending（只盖 completed→pending，前缀开放期间的 in_progress 也盖回）。磁盘上的乱序是真实状态，不回写。

**N4 deferred 不跨请求驻留内存**
`before_invoke` 清空两个 deferred 集合后，`_rebuild_serial_todo_deferred` 从磁盘快照重建（跨请求/HITL resume 后前缀完成仍能补发后项事件）。

**N5 绑定状态白名单与打分**
`_ACTIVE_TASK_ID` ContextVar 持 `todo:{raw_id}`。可绑定状态仅 `in_progress` / `pending` / `waiting`（`_BINDING_IN_PROGRESS` / `_BINDING_PENDING`；`completed`/`cancelled` 属 `_TODO_DONE_STATUSES` 不可绑定）。工作工具优先按 display_name/skill_name 打分匹配（content 双向包含 +100，display ≥4 字符与 todo_text 互含 +80，skill ≥3 字符包含 +50，pending 加成 +1，同分取更小 index），无匹配回退"首个 in_progress，否则首个 pending"。**匹配只改绑定，不开新 in_progress 行**。

**N6 auto_advance 的写路径约束**
工作工具命中绑定项仍 pending 时自动推 in_progress（省一轮纯状态翻转的 LLM 调用）。推之前查 `_has_earlier_incomplete`（串行保护）；todo.json 读-改-写全程持 `_todo_write_lock`（并行工作工具串行化，防丢更新）；只做单值翻转，不改其它字段。这是 **rail 对 todo.json 的唯一写点**（其余全部只读）。

**N7 rail 执行代际过滤**
`_load_todo_from_json` 走 `filter_todos_for_execution`（token 语义见 S_01 N5）：旧代条目不进 `_todo_map`（不绑定、不回放）；无 token 时 legacy 条目全放行。`before_invoke` 从 session 捕获本轮 token。

**N8 inherit_to_subagents = False（禁止继承进通用子代理）**
`init()` 把传入 agent 绑到 `self._deep_agent`；子代理的 `_ensure_initialized` 会用 **child** 重跑 `init_rail`，把共享实例永久重绑到子代理——此后 `_get_todo_workspace_path` 解析到 child 的空 workspace，`_load_todo_from_json` 恒为空，父侧所有 task.start 永久失联（历史上 PPT stage4+ 从 history.json 消失的根因）。有测试锚定：`test_inherit_to_subagents_is_false`。

**N9 产物检测永不阻塞 event loop**
同步 stat/正则全部移入线程（`asyncio.to_thread`）+ `ARTIFACT_DETECT_TIMEOUT_S = 2.0` 超时 + `cancel_event` 协作取消；超时/异常返回 None 跳过本次检测（fail-open，不阻断 rail 回调链）。超大正文（>64K）直接跳过正则扫描；只读内部工具（`READONLY_INNER_TOOLS`，12 个：evaluate_script/list_pages/snapshot/screenshot 等）短路返回空（防 633s stat 风暴）。

**N10 基线 diff 懒建 + 会话级禁用 + 工作区切换重置**
`WorkspaceBaselineState`：首个 bash 类工具执行前经双检锁 `ensure()` 建工作区快照（`MAX_SCAN_FILES = 2000` 超限放弃）；快照超时（`BASELINE_SNAPSHOT_TIMEOUT_S = 2.0`）或失败**一次即 disabled**（本会话降级文本提取，不重试）；状态本质 per-workspace，`ensure()` 发现 base 切换（ACP 请求级 workspace_dir 可变）时重置 snapshot/disabled 重建。hash 受 `_HASH_BUDGET_S = 0.3` 累计预算约束，耗尽保守判变更。

**N11 hook 去重与失败可重试**
已触发 hook 且 `(normcase abspath, mtime_ns, size)` 未变的文件跳过（防水印叠盖）；`fire_artifact_hook` 返回 False（Registry 未初始化/扩展抛错）时**不**记录去重身份，下轮可重试。

**N12 skill_complete 阻断**
`_todo_map` 存在未完成项（状态不在 `_TODO_DONE_STATUSES`）时，skill_complete 被 `_skip_tool` 拦截，tool_result 置提示文本要求先 todo_modify 收口。

**N13 CodeTaskPlanningRail 双阈值提醒与状态清理**
构造即 `enable_progress_repeat = False`（code 模式空闲提醒走 attachment 而非父类 repeat）。task_reminder attachment 仅当"距上次 todo 工具 ≥ `task_reminder_turns_since_management`(10) 轮 **且** 距上次提醒 ≥ `task_reminder_turns_between_reminders`(10) 轮"注入（section `task_reminder`，kind `TODO_REMINDER`，priority 60）；todo 工具调用即清零双计数并清 attachment；`after_invoke` 清会话状态；会话 LRU 上限 `max_tracked_task_reminder_sessions`(1000)，非正值钳为 1。

**N14 CodeTodo* 注册幂等**
`init()` 先按 `isinstance` 扫描 ability_manager 已注册的 CodeTodo* 实例复用，未注册的才 `add_ability`（stateful 工具 id 限定为 `{name}_{owner_id}`，由 teardown_tools 轮末回收）。

## 3. 接口契约

### 3.1 TaskExecutionRail（DeepAgentRail，priority = 85）

| 入口 | 签名要点 | 行为 |
|---|---|---|
| `before_invoke(ctx)` | — | 重置全部跟踪状态；捕获 generation token；`remember_turn_user_query`；磁盘加载 + 重建 deferred；有 pending/in_progress 项时发首帧 task.update；重绑 ContextVar |
| `before_model_call(ctx)` | — | 重绑 + **S_03** 的 hold 布防 |
| `after_model_call(ctx)` | — | **S_03** 的 nudge 挂接与 drop/release |
| `before_tool_call(ctx)` | — | 按 tool_name 分派：nudge 拒绝（S_03）/ todo 快照 / skill_complete 阻断 / 产物检测准备（记 start_time + `baseline.ensure`）/ 工作工具绑定 |
| `after_tool_call(ctx)` | — | todo 工具→`_sync_todo_and_emit_transitions`；工作工具→auto_advance + lazy_start + 重绑；产物工具→`_trigger_artifact_hooks` |
| `after_invoke(ctx)` | — | 清 `_todo_map_before_tool` + 重绑 |
| `init(agent)` | — | `self._deep_agent = agent`（供 workspace 解析；见 N8） |
| `get_current_task_id()` | → `str \| None` | 读 `_ACTIVE_TASK_ID` |
| `set_skill_turbo_request_metadata(metadata)` | — | adapter 每轮 bind_request 注入请求 metadata 副本（workspace 从副本直读，见 P1） |
| `external_todo_completion_events(session_id, task_id, *, parent_request_id)` | → `list[OutputSchema]` | rail 钩子外完成一项时构造 `[task.complete, task.update]`（不 write_stream，调用方自行下发）；目标缺失返回 [] |

### 3.2 模块级公开函数（产物检测，与 SkillTurboArtifactRail 共享）

| 函数 | 用途 |
|---|---|
| `detect_artifact_paths(tool_name, tool_args, tool_result, *, tool_start_time, workspace_base, cancel_event, baseline)` → `ArtifactDetection` | 统一产物检测入口：invoke_tool 解包 → 按工具类型分策略（image 权威路径 / write 从 args / send_file 从 args / code-exec 基线 diff 优先降级文本）→ 统一校验（UNC 排除 + 黑名单 + 存在性 + mtime ≥ start−2s 容差 + 工作区内） |
| `detect_artifact_paths_safe(ctx, session_id, tool_start_time, *, log_prefix, baseline)` → `ArtifactDetection \| None` | 线程 + 超时包装（N9） |
| `WorkspaceBaselineState.ensure(tool_name, tool_args, *, log_prefix, workspace_base)` | 基线懒建（N10） |
| `update_baseline_after_hook(snapshot, fired, paths, workspace_base)` | hook 原地改写后局部刷新基线条目 |
| `filter_unhooked` / `mark_hooked` / `fire_artifact_hook` | 去重身份维护 / 双事件触发（`IMAGE_ARTIFACT_POST_PROCESS` + `ARTIFACT_POST_PROCESS`） |
| `resolve_workspace_base()` / `extract_effective_project_dir(metadata)` / `pop_tool_start_times` | 工作区解析（ContextVar 回退） |
| `is_todo_close_nudge(tool_call)` / `get_current_task_id()` | **S_03** 使用 |
| 公开常量 | `ARTIFACT_DETECT_TIMEOUT_S`、`BASELINE_SNAPSHOT_TIMEOUT_S`、`MAX_SCAN_FILES`、`MAX_HASH_BYTES`、`WorkspaceSnapshot`、`ArtifactDetection`、`ARTIFACT_DETECTION_TOOL_NAMES` 及其组成部分、`SKILL_TURBO_OUTER_TODO_ACTIVE/PENDING_EXTRA_KEY` |

### 3.3 CodeTaskPlanningRail（TaskPlanningRail 子类）

构造参数：`task_reminder_turns_since_management=10`、`task_reminder_turns_between_reminders=10`、`max_tracked_task_reminder_sessions=1000`。钩子：`init`（注册 CodeTodo*，N14）、`before_model_call`（`_inject_pending_todo_reminder` + `_switch_model_if_needed` + 双阈值判定）、`after_tool_call`（先 super 再清零）、`after_invoke`（finally 清会话状态）。

### 3.4 common 纯函数层

- `overlay_serial_todo_statuses(items, *, done_statuses)`：UI 快照乱序盖回（N3）
- `todo_item_generation_token(item)` / `filter_todos_by_generation(items, token)`：代际提取/过滤（token 语义 S_01）
- `load_todo_snapshot_for_frontend(session_id, *, todo_root, generation_token)`：前端恢复用只读快照（session_id 白名单正则 + 取消行剔除 + overlay）

## 4. 数据结构

**TaskExecutionContext**（dataclass）：`task_id`（`todo:{raw_id}`）/ `task_content` / `task_index` / `total_tasks` / `parent_request_id` / `start_time` / `source="todo"` / `status`（running/succeeded/failed/skipped）。

**`_todo_map` 值结构**：`{content, activeForm, status(小写), index, total}`；key 为 todo id（缺 id 回退列表序号字符串）。

**流 payload**：
- `task.start`：task_id / task_content / task_index / total_tasks / parent_request_id / timestamp / source
- `task.complete`：task_id / task_content / status / duration_ms / error / timestamp / source / parent_request_id（无 active context 时 duration_ms=0）
- `task.update`：tasks[] / total_tasks / completed_tasks / in_progress_tasks / pending_tasks / timestamp [/ parent_request_id]

**WorkspaceSnapshot**：`dict[normcase 相对路径 → (绝对路径, mtime_ns, size, sha256|None)]`，sha256 懒计算。

**rail 实例状态**（`__init__`）：`_todo_map` / `_todo_map_before_tool` / `_active_tasks` / `_todo_started` / `_todo_generation_token` / `_tool_start_times`（按 tool_call_id）/ `_hooked_artifacts` / `_baseline` / `_skill_turbo_request_metadata` / `_todo_write_lock` / `_todo_complete_deferred` / `_todo_start_deferred` / close 三态（→ S_03）。实例随 rail 跨请求复用（per-session 粒度由 before_invoke 重置保证）。

## 5. 执行路径全景

**P1 装配与请求隔离**：config `enable_task_execution_rail`（默认 False）→ `_build_task_execution_rail` → `init(agent)` 绑 `_deep_agent`。每轮 `bind_request`：`set_skill_turbo_request_metadata(dict副本)`。工作区解析顺序：metadata 副本 `effective_project_dir`（直读，**不** set/reset ContextVar，避免与 StreamEventRail 的 token reset 乱序）→ 回退 `resolve_workspace_base()` 读 ContextVar。rail priority=85 先于 StreamEventRail 执行（priority 大者先），故 before 阶段读不到后者的 ContextVar 重绑——这是副本直读存在的理由。

**P2 before_invoke**：清状态 → 捕 token → 记录本轮用户 query（S_01 词汇层）→ `_init_task_tracking`（execution 过滤加载）→ `_rebuild_serial_todo_deferred` → 有活跃项发首帧 task.update → `_bind_context_to_in_progress_task`。

**P3 todo 工具闭环**：before_tool_call 记 `_todo_map_before_tool` 快照 → 工具执行（S_01）→ after_tool_call `_sync_todo_and_emit_transitions`：重读磁盘 → 逐项 diff（create 轮 defer / blocked 入 deferred / 常规发 start；completed 补 start+complete）→ 非 create 轮 `_flush_serial_todo_transitions` → 更新 `_todo_map` → `_bind_context_after_todo_sync`（in_progress 优先于"completed 锚点后下一 pending"）→ `snapshot_includes_persistent_todos` 决定 include_persistent（S_01 N8：查询/改提醒轮走 display 列表且不回写 `_todo_map`）→ 发 task.update。

**P4 工作工具闭环**：`_resolve_work_tool_todo_id`（打分，N5）→ after_tool_call：auto_advance（N6）→ lazy_start（N2 补发）→ 重绑 → 产物检测（P6）。`skill_acceleration_exec` 额外导出 `SKILL_TURBO_OUTER_TODO_ACTIVE/PENDING_EXTRA_KEY` 到 ctx.extra（外层 todo 显示所有权 + `_pending_items_after` 后续未完成项快照，供停止提示条件化）。

**P5 串行补发**：flush 在每次非 create 的 todo sync 尾部触发；跨请求经 rebuild 恢复（N4）；UI 一致性由 overlay 保证（N3）。

**P6 产物检测**：before 阶段记 start_time + `baseline.ensure`（N10）→ after 阶段 `detect_artifact_paths_safe`（N9）→ `filter_unhooked`（N11）→ `fire_artifact_hook`（双事件）→ `mark_hooked` + 基线更新（`update_baseline_after_hook` 放线程）。

**P7 ContextVar 绑定链路**：`before_invoke` / `before_model_call` / `before_tool_call`（工作工具）三处 rebind；`_emit_task_start_event` / `_emit_task_complete_event` 内部也 set/clear。`get_current_task_id()`（模块级）供 stream 侧归因。

**P8 CodeTaskPlanningRail**：init 注册（N14）→ before_model_call：先父类 `_inject_pending_todo_reminder` + `_switch_model_if_needed`（读 fresh todos 的 in_progress 项 `selected_model_id` 切模型；生产构造不带 model_selection，该分支不可达，且 set_llm 不同步 compressor models 是登记在案的已知 gap）→ 双阈值判定注入/清除 attachment（N13）→ after_tool_call 清零 → after_invoke 清理。

**P9 外部完成**：`external_todo_completion_events` 读盘构造事件对（含 deferred 集合清理与 overlay 后的 update），供 rail 钩子之外的完成路径（调用方负责下发）。

## 6. 异常分支与退出路径

| 分支 | 行为 | 违反后果 |
|---|---|---|
| todo.json 读失败/非 list | `_sync` 直接 return；`_init_task_tracking` 仅 debug 日志 | 事件丢失但不崩溃（fail-open） |
| 事件 write_stream 抛错 | 捕获记 debug，继续 | 前端少一帧，主流程不受影响 |
| auto_advance 读写失败 | return / warning，不推进、不发 start | 状态留在 pending，模型下轮可显式 todo_modify |
| 产物检测超时/异常 | 返回 None 跳过（N9） | 产物 hook 漏一次，不阻塞 |
| 基线快照超时/超限 | `disabled = True`，降级文本提取（N10） | 检测精度降级，行为与基线引入前一致 |
| hook 触发失败 | 返回 False，不 mark_hooked（N11） | 下轮重试 |
| workspace 路径解析失败 | `_get_todo_workspace_path` 返回 None，下游短路 | rail 静默失效（日志 debug） |
| CodeTodo* 注册异常 | warning 后继续（已注册的保留） | code 模式缺 todo 工具 |
| `external_todo_completion_events` 目标缺失 | warning + [] | 调用方拿到空列表自行跳过 |

## 7. 边界 case 清单

| Case | 期望 | 测试锚点 |
|---|---|---|
| todo_create 首项 in_progress 不发 start | defer | `test_todo_create_defers_task_start_for_in_progress` |
| todo_modify 置 in_progress 仍发 start | 发 | `test_todo_modify_still_emits_task_start` |
| 首个工作工具补发 deferred start | 发 | `test_lazy_start_opens_deferred_in_progress_on_work_tool` |
| deferred in_progress 项直接 completed | start+complete 连发 | `test_deferred_in_progress_completed_emits_start_then_complete` |
| pending 直达 completed | start→complete | `test_pending_to_completed_emits_start_then_complete` |
| 外部完成构造事件对 | [complete, update] | `test_external_todo_completion_events_build_complete_and_update` |
| 并行工具 skill_name 匹配优先于首个 in_progress | 绑定到匹配项 | `test_resolve_prefers_skill_name_over_first_in_progress` |
| 前缀未完时后项 completed 在 UI 被盖回 pending | overlay | `test_overlay_hides_later_completed_while_earlier_in_progress` |
| 跨请求从磁盘重建 deferred | 重建 | `test_rebuild_serial_deferred_from_disk_snapshot` |
| todo.json 无 index 字段时 task_index 回退列表序 | 回退 | `test_task_update_uses_list_index_when_json_has_no_index_field` |
| inherit 标志 | False | `test_inherit_to_subagents_is_false` |
| 基线：新增/未变/touch/变更/临时文件排除/metadata 解析/hook 刷新/None 透传/无 base | 各自语义 | `test_task_execution_baseline.py` 18 个（`test_detect_via_baseline_*` / `test_diff_snapshot_touch_*` / `test_update_baseline_after_hook_*` 等） |
| 产物：write 优先 args / py 回退 / 非_py 忽略 / 图像白名单 / 弱键回退正文 / 结构化短路 / 只读内部工具短路 / 超大文本跳过 / 正常 stdout 命中 | 各自语义 | `test_task_execution_artifact_paths.py` 9 个 |
| code rail：progress_repeat 强制关 / 阈值前不注入 / 阈值后注入 / todo 工具清零 / 模型选择保留 / fresh todos / 非正值钳 1 / 状态上限 / 父类清理失败仍清 | 各自语义 | `test_code_task_planning_rail.py` 10 个 |

## 8. 修改检查单

1. 改事件类型名或 payload 字段 → 前端协议 + skill_turbo 消费方 + `external_todo_completion_events` 三处同步
2. 改 deferred 逻辑 → `_rebuild_serial_todo_deferred`、`overlay_serial_todo_statuses`、`_flush_serial_todo_transitions` 必须一起审（三处共同保证串行语义）
3. 改 auto_advance → 确认 `_todo_write_lock` 覆盖完整读-改-写 + `_has_earlier_incomplete` 前置（N6 是 rail 唯一写点，别加第二个）
4. 改打分/绑定规则 → 跑 `test_task_execution_parallel_todo_start.py`；注意"匹配不开新行"约束
5. 改产物检测工具白名单/黑名单 → `SkillTurboArtifactRail`（skill_turbo 域）共享同名常量，两边同步
6. 改基线机制 → 超时禁用、工作区切换重置、hash 预算三者的语义别丢（见 N10）
7. 改 CodeTodo* 注册 → 保持 isinstance 复用幂等；确认 teardown 回收路径
8. 改 reminder 阈值 → 双阈值是 AND 语义（since_management 与 between_reminders 各自计数）
9. `inherit_to_subagents` 若要放开 → 必须先解决 N8 的重绑问题

## 9. 与其它 spec 的关系

- **S_01**（todo 存储与工具链）：本 rail 是其数据层的最大消费者；generation token、persistent reminder、`filter_todos_for_execution/_for_display`、`snapshot_includes_persistent_todos` 语义均以 S_01 为准
- **S_03**（todo 收口流控）：同文件内的 close nudge + hold/drop/release 三帧 + adapter 侧 `TodoCloseTextHold` 缓冲；`_started_in_progress_tasks` 是两者共享的判定入口
- **S_04**（计划审批）：审批流不经过本 rail 的 todo 工具闭环
- **S_05**（profile 变体）：CodeTodo* 工具卡与 flash 统一卡；`CodeTaskPlanningRail` 的注册行为在本 spec，工具本体在 S_05
- **S_06**（deepresearch）：其 todo 落盘不走本 rail 的事件闭环
- `server/runtime/skill_turbo/AGENTS.md`：`SkillTurboArtifactRail` 复用本文件的产物检测入口与常量
- `server/runtime/agent_adapter/AGENTS.md`：装配点、config 门控、bind_request 注入
