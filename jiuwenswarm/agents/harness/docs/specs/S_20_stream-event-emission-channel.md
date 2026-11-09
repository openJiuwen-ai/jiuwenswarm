# S_20 事件流主通道与发射契约

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_20 / stream-event-emission-channel |
| 关联模块（agent-core 仓） | `openjiuwen/core/session/stream/`（`base.py` 三 schema 与模式枚举 / `emitter.py` StreamEmitter + AsyncStreamQueue / `writer.py` StreamWriter 三型 / `manager.py` StreamWriterManager / `chunk_logging.py` 分块日志开关） |
| 关联模块（jiuwenswarm 仓） | `agents/harness/common/rails/stream_event_rail.py`（发射面：六个 `_emit_*` + sid 解析 + cwd rebind + symphony 桥接）；`agents/harness/common/rails/symphony/tool_stream_events.py`（SymphonyToolStreamHandler）；装配层：`server/runtime/agent_adapter/interface_deep.py`（`_build_stream_event_rail` @9620 / `_RailBuildInfo` 注册 @10618）、`server/runtime/agent_adapter/interface_code.py`（@730）、`server/runtime/agent_adapter/interface_flash.py`（`_FLASH_RAIL_KEEP` @238 继承）、`server/runtime/skill_turbo/executor.py`（@471）、`agents/harness/team/team_runtime_inheritance.py`（@202）、`agents/harness/common/auto_harness/service.py`（@276）、`agents/swarm/providers/builtin_rails.py`（`swarm.stream_event` 命名空间）；消费侧：`jiuwenswarm/cli/events.py`（终态/交互判定）、`jiuwenswarm/perf/todo_tracker.py`（todo 快照旁路） |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **前端流式事件从 rail 发射、经 agent-core stream 队列、到宿主消费的正向通道**——事件长什么样、何时发、经什么管道、谁装配、谁消费：

- **agent-core stream 底层契约**（`core/session/stream/` 五件）：`OutputSchema(type, index, payload)` / `TraceSchema` / `CustomSchema` 三型与 `BaseStreamMode` 枚举；`StreamEmitter`（END_FRAME 终止语义）+ `AsyncStreamQueue`（send 重试 / receive 超时 / close 强清）；`StreamWriter` 三型（schema 校验后 emit）；`StreamWriterManager`（三默认 writer + `stream_output` 迭代器 + 首帧/间隔超时）；`chunk_logging`（`OPENJIUWEN_LOG_STREAM_CHUNKS=1` 才开 per-chunk 追踪，默认只发 summary 行）
- **六类事件的发射时机与 payload 构造**（`stream_event_rail.py`）：`tool_call` / `tool_result` / `tool_update` / `todo.updated` / `context.usage` / `chat.ask_user_question`，全部经 `session.write_stream(OutputSchema(...))` 单一入口
- **sid 解析三序**：`ctx.extra[_SID_KEY]` 继承（子代理继承父 invoke）→ `_main_sessions` 身份反查 → `"default"` 兜底；`before_invoke` 捕获 conversation_id 并写 extra、注册 shell 会话 token
- **cwd rebind 双模式**：`set_runtime_cwd_paths` 存储（project_root / workspace 缺省回落 cwd）→ `_rebind_runtime_cwd(replace=True)` 在 `before_invoke`（gather 复制 ContextVar 引用前 `init_cwd`）→ `replace=False` 在 `before_model_call` / `before_tool_call`（mutate 共享 CwdState）；含 `_rebase_path_from_agent_workspace`（agent 内部子树前缀白名单不重写）与 whitespace 路径修复旁路
- **装配挂载七处**：deep / code / flash（继承不重建）/ skill_turbo executor / team_runtime_inheritance / auto_harness / swarm builtin_rails
- **symphony 工具进度桥接**：`SymphonyToolStreamHandler` 的 progress ContextVar 绑定、`tool_update` 进度发射、结果字段富化、`request_force_finish`
- **消费侧判定**：`cli/events.py` 的 `is_terminal_event` / `needs_user_input` / `event_kind`；`perf/todo_tracker.sync_main_agent_todos` 旁路（仅主 rail，`_member_name` 为空）

不管：

- 暂停 / 恢复 / 中止原语、per-session 状态字典、早 checkpoint、取消结果收集 → [[S_21]]
- 上下文修复（`_fix_incomplete_tool_context`）、call_goal 注入与剥离、SkillTurbo HITL 改写 → [[S_21]]
- `TaskExecutionRail` 的任务事件面（task.start / complete / update）→ [[S_02]]
- todo 快照的代际过滤与序列覆盖（generation token / `filter_todos_for_display`）→ [[S_05]]（本 spec 只管发射时机与 payload 外壳）
- `__jiuwenswarm_session_id__` 键的会话生命周期语义与生产者声明 → [[S_08]]（本 spec 是该键的**生产者实现**，S_08 是消费侧契约）
- 中断原语三态 / ReAct 中断状态机 → [[S_15]]；SkillTurbo 引擎本体与 HITL 作答协议 → skill_turbo 域 [[S_06]] / [[S_09]]
- `SymphonyOrchestrationRail`（priority=98，提示词与候选注入）→ symphony 编排域，非事件流；本 spec 只管 `SymphonyToolStreamHandler` 的流生命周期面
- gateway / server 侧事件转发与 EventType 映射 → server 域（`cli/events.py` 注释提及的 chat.final 兼容封套语义在此登记为接缝，不展开）

## 不变量

- **N1（发射永不阻断主链路）**：六个 `_emit_*` 全部 try/except 包裹、失败仅 `logger.debug` 吞掉——流式事件是旁路观测，任何发射失败（session 关闭 / 队列满 / payload 构造异常）都不得让工具调用或模型调用失败。点名：`_emit_tool_call` / `_emit_tool_result` / `_emit_tool_update` / `_emit_todo_updated` / `_emit_context_usage` / `_emit_ask_user_question_if_interrupted` 六处 except 块是契约的一部分。
- **N2（write_stream 单一入口）**：rail 侧一切前端事件经 `session.write_stream(OutputSchema(type=..., index=0, payload=...))` 发射；`index` 恒为 0（分帧序由底层管理）；不得绕过 OutputSchema 直塞裸 dict。agent-core 侧 `StreamWriter.write` 先 `model_validate` 再 emit——校验失败抛 `STREAM_WRITER_WRITE_STREAM_VALIDATION_ERROR`，不静默。
- **N3（sid 解析三序不可换序）**：`_resolve_sid` 先读 `ctx.extra[_SID_KEY]`（子代理经父 invoke 的 extra 继承——父 abort 子停），miss 后用 `session is known_session` 身份反查 `_main_sessions`（主代理工具回调无 extra 时兜底），最后 `"default"`。反查命中必须**回写 extra**（同一 ctx 后续调用不再反查）。
- **N4（per-session 字典隔离）**：`_pause_events` / `_conversation_ids` / `_main_sessions` / `_inflight_tool_calls` / `_cancelled_tool_results` 按 sid 键控——共享 adapter 实例并发服务多会话，标量状态会造成 A 会话取消杀死 B 会话。`before_invoke` 不得把 `"default"` 哨兵存进 `_conversation_ids`（after_tool_call 用真值判断是否发 todo.updated）。
- **N5（cwd rebind 双模式语义）**：`replace=True` 只在 `before_invoke` 调（`init_cwd` 装新 CwdState，须赶在工具 `asyncio.gather` 复制 ContextVar 引用之前）；`replace=False` 在 `before_model_call` / `before_tool_call` 调（`set_cwd` / `set_project_root` / `set_workspace` mutate 共享实例，让已持有引用的 gather 兄弟看到更新）。两处调用点换位会分别导致"兄弟任务看不到 cwd"或"覆盖兄弟任务的并发 cwd"。
- **N6（context.usage 轻量模式不丢不阻塞）**：`JIUWENCLAW_ADAPTER_CKPT_LIGHT` 开启时 `after_model_call` 把 `_emit_context_usage` 丢进 `asyncio.create_task` 后台发射（`_bg_emit_tasks` 集合持有防 GC，done 回调自摘）；无 loop 时退回同步 await。跳过条件是 `extra["_context_usage_event_emitted"]`（agent-core 已发 v1 快照）——**不得**改判 `context_usage_report`（core 在 analyze/write_stream 之前赋值，发射失败后跳过会同时丢 v1 帧与 legacy 兜底）。
- **N7（symphony force_finish 条件）**：`request_force_finish` 仅当结果 `direct_display` 为真、渲染内容非空、且 `continue_after_display` 为假时调 `ctx.request_force_finish`；`_RESULT_FIELDS` 五字段（graph_status / graph_build / direct_display / continue_after_display / followup_action）从 raw_output dict 富化进 tool_result payload。
- **N8（flash 继承不重建）**：`interface_flash._FLASH_RAIL_KEEP` 含 `"_stream_event_rail"`——flash 复用 deep 父类已建实例，跳过每请求"卸载→重建"churn，生命周期交给 `_set_user_interaction_enabled`。从该集合移除会破坏 flash 会话的 sid 连续性与暂停状态。
- **N9（发射跳过名单）**：`before_tool_call` 对 `skill_acceleration_exec` 且 `RESUME_USER_INPUT_KEY` 非空（resume 重放）跳过 `_emit_tool_call` / `_emit_tool_update`；`is_todo_close_nudge(tc)` 同跳。`after_tool_call` 对 deepresearch_execute 的 TIE 结果、todo close nudge 提前 return。这些跳过是防重复事件（resume 侧已发过 / nudge 是内部机制）。
- **N10（装配失败降级为 None 不抛）**：`_build_stream_event_rail`（deep @9620）try/except——构建失败记 warning 返回 None，adapter 以 `_stream_event_rail is None` 分支继续运行（无流式事件但功能不瘫）。

## 接口契约

### agent-core 侧（`core/session/stream/`）

```python
# base.py —— 三 schema 与模式
OutputSchema(type: str, index: int, payload: dict)   # 前端事件帧
TraceSchema / CustomSchema                            # 与 Output 并列的另两通道
BaseStreamMode.OUTPUT / TRACE / CUSTOM

# emitter.py —— 队列与终止
AsyncStreamQueue.send(data, attempt_timeout=0.2, max_retries=5)  # 重试耗尽仅 error 日志不抛
AsyncStreamQueue.receive(timeout=-1)   # -1 = 无限等待
AsyncStreamQueue.close(timeout=5.0)     # join 超时 → _force_clear 强清
StreamEmitter.END_FRAME = "all streaming outputs finish"
StreamEmitter.emit(data)   # closed 后 RuntimeError
StreamEmitter.close()      # 幂等；向队列投 END_FRAME

# writer.py —— 校验层
StreamWriter.write(stream_data)  # None → VALIDATION_ERROR；ValidationError → VALIDATION_ERROR；
                                 # _do_write 异常 → WRITE_STREAM_ERROR；emitter 已关 → warning 丢弃

# manager.py —— 消费迭代
StreamWriterManager(emitter, modes=None)  # 默认三模式全建
stream_output(first_frame_timeout=-1, timeout=-1, need_close=True)
  # 首帧超时 → STREAM_OUTPUT_FIRST_CHUNK_INTERVAL_TIMEOUT
  # 帧间隔超时 → STREAM_OUTPUT_CHUNK_INTERVAL_TIMEOUT
  # END_FRAME → close 队列 + summary 日志 + break
```

### rail 侧发射 API（`stream_event_rail.py`，全部 staticmethod 或实例方法）

| 方法 | 行号 | 事件 type | payload 外壳 | 触发点 |
|---|---|---|---|---|
| `_emit_tool_call` | @1504 | `tool_call` | `{"tool_call": {name, arguments, tool_call_id, display_name?, source_skill?}}` | before_tool_call（非跳过名单） |
| `_emit_tool_result` | @1537 | `tool_result` | `{"tool_result": {tool_name, tool_call_id, result, raw_output?, success?/status?/is_error?, source_skill?}}` | after_tool_call（非 TIE / 非 nudge） |
| `_emit_tool_update` | @1607 | `tool_update` | `{"tool_update": {tool_name, tool_call_id, arguments, status, source_skill?}}` | before_tool_call（in_progress） |
| `_emit_todo_updated` | @1630 | `todo.updated` | `{"todos": [...]}` | after_tool_call（todo 工具后） |
| `_emit_context_usage` | @1753 | `context.usage` | `{rate, context_max, tokens_used, role?, member_name?}` | after_model_call |
| `_emit_ask_user_question_if_interrupted` | @1579 | `chat.ask_user_question` | interrupt payload | after_tool_call（ask_user + TIE） |

- `display_name` 优先级：模型 `call_goal` 文案 → `build_tool_display_name(name, arguments)` 规则兜底
- `source_skill`：`_resolve_source_skill(session)`（经 `get_current_skill_name`）
- `success` / `status` / `is_error` 仅在 `_infer_tool_result_error` 判定非 None 时写入（success 取反、error 时三键齐发）
- `raw_output`：`_structured_tool_result_payload(result)` 非 dict-like 时为 None；symphony 富化只发生在 raw_output 非 None 分支

### 装配 API

```python
# deep / code：_RailBuildInfo 声明式注册
_RailBuildInfo("_stream_event_rail", self._build_stream_event_rail)   # deep @10618 / code @730

# 集成面（deep 为主，code 子集）
set_checkpointer(cp)                    # 早 checkpoint 用（见 S_21）
set_skill_turbo_adapter(adapter)        # HITL 桥接（见 S_21）
set_skill_turbo_request_metadata(meta)  # 请求 metadata 转绑（见 S_21）
set_runtime_cwd_paths(cwd=, project_root=, workspace=)  # 本 spec N5
cleanup_session(sid)                    # 会话计数归零时清全部 per-session 态（见 S_21）
```

## 数据结构

```python
# 事件帧（agent-core 定义，rail 消费）
OutputSchema(type: str, index: int, payload: dict)
# END_FRAME 哨兵（str "all streaming outputs finish"）——与业务 payload 类型不同，
# manager 靠 is-comparison 识别，不得用 == 判业务帧

# rail 实例态（本 spec 相关部分；暂停/取消态见 S_21）
_main_sessions: dict[str, Session]        # sid → 主会话对象（身份反查用）
_conversation_ids: dict[str, str]         # sid → 真实 conversation_id（todo.json 主键）
_main_todo_tool: TodoListTool | None      # 跨会话共享（同 workspace 同实例）
_symphony_stream_handler: SymphonyToolStreamHandler
_bg_emit_tasks: set                       # 类级！轻量模式后台发射任务防 GC
_runtime_cwd / _runtime_project_root / _runtime_workspace: str | None
# 谁写：adapter 在请求任务里 set_runtime_cwd_paths
# 谁读：_rebind_runtime_cwd（invoke / model / tool 边界）
# 何时清：下一次 set 覆盖（per-request，无显式清理）

# ctx.extra 键（invoke 内全程存续，子代理继承）
"__jiuwenswarm_session_id__"              # _SID_KEY（S_08 契约的生产侧）
"__jiuwenswarm_shell_session_token__"     # shell 会话 token（after_invoke pop 复位）
```

## 执行路径全景

- **P1 装配**：deep `_build_stream_event_rail`（@9620，失败降级 None）→ `_RailBuildInfo` 注册（@10618）→ adapter 集成面注入（checkpointer @14084 / adapter @14082 / metadata @13542 / cwd @13396）；code 同构（@730 / @1475-1497）；flash 经 `_FLASH_RAIL_KEEP` 继承（@238）；skill_turbo executor 独立实例（@471）；team_runtime_inheritance 团队继承（@202，member_name / role 参数）；auto_harness（@276）；swarm `builtin_rails` 以 `swarm.stream_event` 注册
- **P2 invoke 边界**：`before_invoke`（@933）→ sid 捕获（conversation_id 或 default，`_conversation_ids` / `_main_sessions` 登记）→ extra 写 `_SID_KEY` → `set_shell_session_id` 存 token → `_rebind_runtime_cwd(replace=True)`；`after_invoke`（@970）→ pop token 复位 shell 会话 → `reset_skill_turbo_context` → adapter 兜底清空
- **P3 模型边界**：`before_model_call`（@1006）→ pause wait + abort 检查（S_21）→ `_rebind_runtime_cwd(replace=False)` → call_goal schema 注入（S_21）→ 图片剥离 + 上下文修复（S_21）→ 早 checkpoint（S_21）；`after_model_call`（@1179）→ `_context_usage_event_emitted` 检查 → light 模式分支 → `_emit_context_usage`
- **P4 工具边界**：`before_tool_call`（@1208）→ pause/abort（S_21）→ cwd mutate → write_file/edit_file rebase + whitespace fix → call_goal 剥离（S_21）→ 跳过名单判定 → `_emit_tool_call` + `_emit_tool_update` → symphony bind_progress → inflight 登记；`after_tool_call`（@1312）→ skill_turbo 复位 → HITL 改写（S_21）→ `_emit_tool_result` + symphony force_finish → `_emit_ask_user_question_if_interrupted` → todo 工具则 `_emit_todo_updated`
- **P5 todo 旁路**：`_emit_todo_updated`（@1630）→ `_get_todo_tool`（deep_config.workspace 固定路径，避开 Runner.resource_mgr 覆盖）→ `load_todos` → 代际过滤（S_05）→ 主 rail 才 `sync_main_agent_todos`（perf 旁路，`run_perf_safe` 包裹）→ `overlay_serial_todo_statuses` → write_stream
- **P6 symphony 桥接**：`bind_progress`（before_tool_call）→ `bind_tool_progress` ContextVar → 工具体内 progress_callback → `_emit_progress`（tool_update + beam_search_event）；`enrich_result_payload`（_emit_tool_result 内）；`request_force_finish`（after_tool_call 内）
- **P7 底层管道**：`session.write_stream(OutputSchema)` → StreamWriter（校验）→ StreamEmitter.emit → AsyncStreamQueue.send（重试）→ 宿主 `stream_output` 迭代消费 → END_FRAME 终止
- **P8 消费侧**：cli `events.py` 判定（chat.error / chat.final+content / processing_status=false 为终态；ask_user_question / plan.approval_required 需用户输入）；gateway stream cancel / disconnect cancel 测试守护

## 异常分支与退出路径

| 异常 | 行为 | 后果 |
|---|---|---|
| 任一 `_emit_*` 内部异常 | except + debug 日志 | 事件丢失，主链路继续（N1） |
| 队列 send 重试耗尽（5×0.2s） | error 日志，不抛 | 帧丢失；背压观测靠 summary 行 |
| emitter closed 后 emit | RuntimeError | 被 N1 的 except 吞掉 |
| `stream_output` 首帧超时 | 抛 FIRST_CHUNK_INTERVAL_TIMEOUT | 宿主侧错误帧 |
| `stream_output` 帧间隔超时 | 抛 CHUNK_INTERVAL_TIMEOUT | 宿主侧错误帧 |
| close join 超时（5s） | `_force_clear` 强清 + error | 残帧丢弃，队列可复用 |
| `_build_stream_event_rail` 失败 | warning + None | 无流式事件，功能不瘫（N10） |
| cwd rebind 失败 | warning + exc_info | 工具用默认 workspace 路径（路径错但可运行） |
| `set_shell_session_id` 失败 | debug | shell 进程树击杀（S_21 abort）退化为全局 |
| light 模式 create_task 失败（无 loop） | 退回同步 await | 短暂阻塞回复路径 |

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| 子代理工具回调无 `_SID_KEY` | `_main_sessions` 身份反查回写 | 反查 miss 落 default，暂停状态错会话 | test_subagent_stream_events.py |
| conversation_id 为空 | sid=default 但**不**存 `_conversation_ids` | todo.updated 不发（真值判断） | test_stream_event_rail_todo.py |
| resume 重放 skill_acceleration_exec | 跳过 tool_call/tool_update 发射 | 不跳会双发卡片 | test_skill_turbo_hitl_resume_react.py |
| todo close nudge | before/after 双跳过 | 不跳会向前端泄漏内部 nudge | test_task_execution_todo_close_nudge.py |
| 工具结果含 `__skill_turbo_hitl__` | 改写 TIE 后 return，不发 tool_result | 误发会让前端显示失败 | test_skill_turbo_hitl_tie_rewrite_repro.py |
| flash 请求切换 | `_FLASH_RAIL_KEEP` 保留实例 | 移除会丢 sid 连续性 | test_flash_permission_display_args.py |
| symphony 结果 continue_after_display | 不 force_finish | 误 finish 截断后续轮次 | test_stream_event_rail_symphony.py |
| 100 并发 context.usage | light 模式后台发射 | 同步发射拖慢回调秒级 | （perf 观测，无单测） |
| 队列 END_FRAME 后再 emit | RuntimeError 被 N1 吞 | 会话末尾事件静默丢 | （底层契约，manager 测试域） |
| source_skill 解析失败 | 空 str 不写字段 | 前端无技能归因，无功能影响 | test_stream_event_rail_source_skill.py |

## 修改检查单

- 改任一 `_emit_*` 的 payload 字段 → 必须连带检查前端 / gateway 的事件解析与 `cli/events.py` 的 `event_kind` 分类，以及对应 `test_stream_event_rail_*` 断言
- 改 `_resolve_sid` 三序或 `_SID_KEY` 键名 → 必须连带检查 [[S_08]]（消费侧契约）与 [[S_21]]（暂停状态按 sid 键控）
- 改 cwd rebind 调用点或 replace 语义 → 必须连带检查 `before_invoke`（gather 前）与 `before_tool_call`（gather 后）的时序注释，跑 test_path_whitespace_fix.py
- 改 `_FLASH_RAIL_KEEP` 集合 → 必须连带验证 flash 会话的 sid 继承与暂停状态存续
- 改 agent-core `AsyncStreamQueue` 超时/重试参数 → 必须连带评估高并发背压（N6 注释的 100 并发实测）与 gateway 流取消测试
- 新增前端事件 type → 必须在 `cli/events.py` 的 `event_kind` / `is_terminal_event` 登记分类，否则落入 "other" / 非终态
- 改 `_emit_todo_updated` 的过滤或 perf 旁路 → 必须连带检查 [[S_05]]（代际过滤）与 perf/test_todo_tracker.py
- 改 symphony `_RESULT_FIELDS` 或 force_finish 条件 → 必须连带跑 test_stream_event_rail_symphony.py

## 与其它 spec 的关系

- [[S_21]]：同一 rail 的另一半——暂停检查点 / 上下文修复 / call_goal / HITL 改写。本 spec 管正向发射，S_21 管异常流一致性；`before_model_call` / `before_tool_call` / `after_tool_call` 三个钩子内两 spec 的职责交错，行号互引
- [[S_02]]：TaskExecutionRail 的任务事件（task.start / complete / update）是独立事件族，不经本 rail 的 `_emit_*`；两族在 todo 工具调用点交汇（S_02 的 close nudge 判定被本 spec N9 引用）
- [[S_05]]：todo 快照代际过滤与序列覆盖——本 spec `_emit_todo_updated` 的过滤步骤实现方在 common/todo_snapshot，契约归 S_05
- [[S_08]]：`__jiuwenswarm_session_id__` 键的会话生命周期语义——本 spec 是生产者（`before_invoke` 写入），S_08 声明消费侧
- [[S_15]] / [[S_19]]：中断原语与取消结算——本 spec 的 `chat.ask_user_question` 发射依赖 S_15 的 TIE 提取（`_extract_tool_interrupt`）；rail 侧 abort 信号传播归 [[S_21]]，server 侧 ReAct 消息结算归 S_19
- [[S_06]] / [[S_09]]：SkillTurbo 引擎与 HITL 作答协议——本 spec 只管发射跳过名单（N9）与 ContextVar 绑定时机；改写语义归 [[S_21]]
- [[S_10]]：输入侧守卫已把 StreamEventRail 本体预划本域（其边界声明与本 spec 范围节一致）；WorkspaceQuotaRail 等输入侧 rail 与本 rail 的 priority 顺序（80）见 S_07
- jiuwenswarm 其它 AGENTS.md 体系：`server/runtime/agent_adapter/AGENTS.md`（装配集成面：cwd 注入 / metadata 转绑 / abort 调用点）、`jiuwenswarm/common/AGENTS.md`（todo_snapshot / tool_display 的实现归属）、`server/runtime/skill_turbo/AGENTS.md`（ContextVar 绑定族实现）——接缝在此登记，不复制内容
