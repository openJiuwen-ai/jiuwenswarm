# S_21 暂停检查点与上下文修复

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_21 / pause-checkpoints-and-context-repair |
| 关联模块（agent-core 仓） | `openjiuwen/core/sys_operation/shell_process_registry.py`（`set_shell_session_id` / `kill_shell_processes_for_session_tree` / `reset_shell_session_id`）；`openjiuwen/core/single_agent/interrupt/`（`ToolInterruptException` / `ToolCallInterruptRequest` / `RESUME_USER_INPUT_KEY`） |
| 关联模块（jiuwenswarm 仓） | `agents/harness/common/rails/stream_event_rail.py`（本 spec 本体：pause/resume/abort 原语 @808-927、早 checkpoint @1026-1080、call_goal 注入 @1082-1132 与提示词 @1134-1176、上下文修复 @1849-2021、占位检测 @641-786、HITL 改写 @1338-1437）；`jiuwenswarm/common/tool_display.py`（`inject_call_goal_schema` / `extract_call_goal`）；`server/runtime/skill_turbo/skill_turbo_tools.py`（`_SKILL_TURBO_HITL_*` 常量族 / TIC get/set）；`server/runtime/agent_adapter/interface_deep.py`（`rail.abort` @15248 / cleanup 调用 @3672）；`server/runtime/skill_turbo/context_binding.py`（skill_turbo 9 个 ContextVar 绑定） |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **StreamEventRail 的暂停 / 恢复 / 中止状态机、检查点落盘、中断后的上下文一致性修复**——同一 rail 的异常流（与 [[S_20]] 的正向发射流互补）：

- **pause / resume / abort / reset 原语**（@808-927）：`pause`（event clear）/ `resume`（清 abort + event set）/ `abort`（标志 + event set + shell 进程树击杀）/ `reset_abort` / `reset_for_new_task`（任务间复位，**故意不清 abort**）/ `cleanup_session`（五字典全清）
- **per-session 状态字典**（`__init__` @349-382）：`_abort_requested` / `_pause_events` / `_conversation_ids` / `_main_sessions` / `_inflight_tool_calls` / `_cancelled_tool_results` 六字典按 sid 键控；`_checkpointer`（租户域）/ `_skill_turbo_adapter` / `_skill_turbo_request_metadata` 实例级三件
- **两个暂停检查点**：`before_model_call`（@1006-1010）与 `before_tool_call`（@1208-1212）——`_pause_event.wait()` + abort 标志 raise `asyncio.CancelledError`
- **早 checkpoint**（`_maybe_early_checkpoint` @1026-1080）：`JIUWENCLAW_EARLY_CHECKPOINT` env 开关 / `ctx.extra` 幂等标记 / heartbeat 会话跳过 / `save_contexts` + `post_agent_execute`（**非** `post_run`）
- **取消结果收集**（@885-927）：`collect_cancelled_tool_updates`（inflight → 占位结果）/ `get_cancelled_tool_results` / `clear_cancelled_tool_results`，供 server 中断响应转发前端
- **上下文修复**（`_fix_incomplete_tool_context` @1849-2021）：四计数规则化重放（inserted / orphan_removed / duplicate_removed / placeholder_replaced）+ malformed JSON arguments 规范化 + 工具 parameters 缺省补全；调用点两处——`before_model_call`（@1022）与 `on_model_exception`（@1497）
- **占位检测**（@641-786）：`_tool_interrupted_message` 双语模板 + `_is_tool_interrupt_placeholder`（当前模板匹配 + legacy 7 类文案兜底，normalize 空白后比较）
- **call_goal 注入 / 剥离**：注入 `_inject_tool_call_goal_schema`（@1083，deepcopy + `model_copy`）+ `_ensure_tool_call_goal_prompt`（@1134，系统提示词段落）；剥离在 `before_tool_call`（@1239 `extract_call_goal` → 清洗后写回）
- **SkillTurbo HITL 改写**（after_tool_call @1338-1437）：结果 marker 主信号 → TIE 重写 → 跳过 `_emit_tool_result`；ContextVar TIC 兜底与 stale drop；改写失败降级占位

不管：

- 正向事件发射（六 `_emit_*` / sid 解析三序 / cwd rebind / symphony 桥接 / 装配挂载）→ [[S_20]]
- agent-core 中断原语（五状态键 / 中断状态机 / 三态决策 / `build_interrupt_state`）→ [[S_15]]
- server 侧取消结算（ReAct 消息写入 / 序号代际 / pending 收集 / 合法 ToolMessage 结算）→ [[S_19]]——本 spec 的 `collect_cancelled_tool_updates` 是其上游信号，S_19 是下游消费者
- SkillTurbo 引擎本体与 HITL 作答协议（作答注入 / resume 流程）→ skill_turbo 域；本 spec 只管 rail 侧的改写桥接点
- shell 进程注册表的实现（进程树追踪 / 击杀机制）→ agent-core `sys_operation` 域；本 spec 只管调用契约（abort 时按 sid 击杀）
- `__jiuwenswarm_session_id__` 键的全局消费侧契约 → [[S_08]]

## 不变量

- **N1（abort 三合一语义）**：`abort(sid)` 必须同时（a）置 `_abort_requested[sid]=True`、（b）`_get_pause_event(sid).set()`、（c）击杀该 sid 的 shell 进程树。缺 (b) 会让暂停中的任务永远停在 `_pause_event.wait()`——abort 是"解除暂停再终止"，不是静默挂起。shell 击杀失败仅 `logger.debug`（exc_info），进程存活但模型链路仍会被 (a) 的 checkpoint 检查终止。
- **N2（reset_for_new_task 故意不清 abort）**：取消后新任务入口必须能启动（event.set 解除 wait），但 abort 标志**保持 True** 直到下一任务的 `process_message_*_impl` 调 `reset_abort()`——这保证 in-flight 的 checkpoint（`before_model_call` / `before_tool_call`）仍能观察到标志并 raise `CancelledError`，不会把取消误当作正常继续。`reset_for_new_task` 同时清 `_conversation_ids[sid]` / `_main_sessions[sid]`，让 sid 归还 default 回落路径。
- **N3（resume = 清 abort + 解除暂停）**：`resume(sid)` 先 `_abort_requested.pop(sid, None)` 再 event.set——顺序无关但缺 pop 会导致"恢复后立刻在下一 checkpoint 被 abort"。
- **N4（清理分两级）**：`reset_for_new_task` 是**任务间**复位（保 abort，见 N2）；`cleanup_session` 是**会话终局**清理（adapter Counter 归零时调，五字典全清：abort / pause_events / conversation_ids / main_sessions / cancelled_tool_results——防长生命周期 adapter 上多会话字典无界增长）。`_inflight_tool_calls` 不在其中：跨会话共享（同 workspace 同工具实例），由 `collect_cancelled_tool_updates` 与 `after_tool_call` 完成时逐项摘除。
- **N5（早 checkpoint 幂等且不关流）**：（a）落盘用 `post_agent_execute`，**绝不用 `post_run`**（后者会 close 流）；（b）`ctx.extra[_jiuwenswarm_early_checkpoint_done]` 标记防同 invoke 的后续 ReAct 迭代重复落盘；（c）heartbeat 会话（cid 前缀 `"heartbeat"`）跳过；（d）`JIUWENCLAW_EARLY_CHECKPOINT` ∈ {0/false/no/off} 全关；（e）tenant `self._checkpointer` 优先，`CheckpointerFactory` 兜底；（f）失败仅 warning 不阻断模型调用——checkpoint 是崩溃恢复增强（防用户消息丢失），不是前置条件。
- **N6（call_goal 注入必须复制不可回写）**：注入 schema 前 `copy.deepcopy(params)`，再 `model_copy(update={"parameters": cloned})` 产新 ToolInfo——绝不就地改原 ToolInfo 的 `parameters` / `card.input_params`：ToolInfo 与执行侧 schema 共享内层 properties，就地注入后 `call_goal=None` 会被 SchemaUtils 补上、`LocalFunction` 以 `**kwargs` 传给 `send_file` 等实现直接 TypeError。`model_copy` 抛异常（ValidationError 等）则跳过该工具（warning），**不回写原始实例**。批量替换 `ctx.inputs.tools` 失败（AttributeError / TypeError）仅 warning——注入结果整批丢弃，主链路无损。
- **N7（call_goal 剥离无条件清洗）**：`before_tool_call` 对每个 tool_call 取 `extract_call_goal(arguments)` 后，**无论是否填了 call_goal 都写回清洗后的 arguments**（`tc.arguments` + `ctx.inputs.tool_args` 双写）——防执行侧拿到该字段被 schema 拒收。绝不碰 `display_name`（team 成员名等业务字段）。写回失败（AttributeError / TypeError）仅 warning 继续发射。
- **N8（HITL 改写主信号是结果 marker）**：工具返回值 dict 里的 `__skill_turbo_hitl__` 标记（deepcopy 安全、随返回值穿越任何执行上下文）是**主信号**；ContextVar 中的 ToolInterruptException 是旧路径兜底。检测到任一信号即把 `ctx.inputs.tool_result` 改写为 `ToolInterruptException`（request 来自 marker dump 的 `model_validate` 或 TIC.request）+ `ctx.inputs.tool_msg` 改写为 `_SKILL_TURBO_HITL_PLACEHOLDER`——让 harness 原生 HITL（`build_interrupt_state`）检测并暂停。**改写后 return，跳过 `_emit_tool_result`**：中断态无结果可发，误发会让前端显示工具失败。卡片由 harness `__interaction__` 转换统一发出（外层 tool_call_id），此处主动 emit 会双发两张 ask_user 卡片。
- **N9（HITL 改写失败降级占位）**：改写整体异常（含 `ToolCallInterruptRequest.model_validate` 失败）时，若原结果仍是 marker dict 则降级为 `_SKILL_TURBO_HITL_PLACEHOLDER` 文本——**原 marker dict 不可透传给模型**（无语义 JSON 会被误判失败并回退 skill_tool 通道）。降级同样配 ToolMessage 占位。
- **N10（上下文修复窗口规则与占位替换语义）**：修复只认"assistant tool_calls 块到下一 UserMessage 之间"为即时响应区；规则四计数——（a）expected tcid 在窗口内无 ToolMessage → 在 assistant 后插入 `[Tool interrupted]` 双语占位（或用已收集的真实结果替换）；（b）孤儿 ToolMessage（id 不在该块的 expected 集合）→ 删除；（c）同块重复 id → 删后续；（d）占位 ToolMessage 且后续存在同 id 真实 ToolMessage → **就地把占位替换为真实结果并删后续 duplicate**（占位被真实替换，而非 duplicate 规则删真实保占位——skill_turbo / deepresearch 的"占位 + resume 完成态同 id 两条"靠此收敛）。重建经 `pop_messages(size=len(messages))` + 逐条 `add_messages`；整体失败仅 warning。
- **N11（JSON 规范化 byte-for-byte 保留合法输入）**：`_ensure_json_arguments` 对**合法** JSON 必须原样返回（比较后仅真变化才重写 `tc.arguments`）——reasoning 模型的忠实回放依赖字节级一致；真正被重写的只有缺引号 / 不平衡括号类 malformed 输入。权威修复在 ability_manager 执行时，此处是安全网。工具 `parameters` 缺省 / 缺 type 时补 `{"type": "object", "properties": {}}` 空壳。
- **N12（占位判定由内容不由 id 单独判定）**：`_is_tool_interrupt_placeholder` 先用 `_tool_interrupt_placeholders_by_id` 的当前双语模板精确匹配（tool_call_id → 工具名 → 期望文案），miss 后走 legacy 模板清单兜底（en 旧版 / en 当前 / 中文 / skill_turbo 三个历史版 / deepresearch 一个，均 normalize 空白后比较）——历史会话数据里的旧文案必须仍被识别为占位，否则会被当真实结果参与 N10(d) 的替换方向判断。**新增占位文案必须同步登记进 legacy 清单**。

## 接口契约

### 状态机 API（`stream_event_rail.py` @808-927，interface.py 直接调用）

```python
rail.pause(session_id)                # event.clear() —— 模型/工具 checkpoint 阻塞
rail.resume(session_id)              # abort 标志 pop + event.set()
rail.abort(session_id)               # 标志置 True + event.set() + kill_shell_processes_for_session_tree(sid)
rail.reset_abort(session_id)         # 仅 pop abort 标志（新任务入口调用）
rail.reset_for_new_task(session_id)  # event.set() + conversation_ids/main_sessions pop；abort 不动（N2）
rail.cleanup_session(session_id)     # 五字典全 pop（N4）；adapter Counter==0 时调

rail.collect_cancelled_tool_updates(session_id)  # inflight → cancelled_tool_results 占位（见下）
rail.get_cancelled_tool_results(session_id)      # list 拷贝，不消费
rail.clear_cancelled_tool_results(session_id)    # 取走后清空
```

### 检查点（两处对称）

```python
# before_model_call @1006 / before_tool_call @1208，入口相同三步：
sid = self._resolve_sid(ctx, ctx.session)          # 三序见 S_20 N3
await self._get_pause_event(sid).wait()             # 暂停阻塞（事件创建即 set，默认畅通）
if self._abort_requested.get(sid, False):
    raise asyncio.CancelledError("Agent abort requested")
```

### 取消结果收集（占位结果结构）

```python
# collect_cancelled_tool_updates 逐 inflight 项产出：
{
    "tool_name": tc.name,
    "tool_call_id": tc_id,
    "result": "[Interrupted] Tool execution cancelled by user.",
    "status": "error",
}
# inflight 登记项：{"tool_call": tc, "session": session, "session_id": sid}
# collect 按 info["session_id"] == session_id 过滤（session_id 为空则收集全部）
```

### 早 checkpoint（`_maybe_early_checkpoint`）

```python
# 前置守卫（全部满足才落盘）：
_early_checkpoint_disabled_by_env() is False        # JIUWENCLAW_EARLY_CHECKPOINT 非 0/false/no/off
ctx.extra.get("_jiuwenswarm_early_checkpoint_done") is None   # 本 invoke 未落过
cid 非 "heartbeat" 前缀会话
session / agent / context_engine 均非 None          # session 取 _parent 归一
# 落盘序列：
await context_engine.save_contexts(actual_session)
await cp.post_agent_execute(inner)    # cp = self._checkpointer or CheckpointerFactory.get_checkpointer()
ctx.extra["_jiuwenswarm_early_checkpoint_done"] = True
```

### call_goal 注入 / 剥离

```python
# 注入（before_model_call 内，@1082）：per-tool 决策树
params 非 dict            → 原样透传
props 已含 "call_goal"     → 原样透传（幂等）
cloned = deepcopy(params); inject_call_goal_schema(cloned)
  cloned == params        → 原样透传（注入无效果）
  有 model_copy           → model_copy(update={"parameters": cloned})，changed=True
  model_copy 抛异常        → warning + 原样透传（不回写）
changed                   → ctx.inputs.tools = next_tools（失败仅 warning）

# 剥离（before_tool_call 内，@1239）：
model_display, cleaned_args = extract_call_goal(tc.arguments or {})
tc.arguments = cleaned_args; ctx.inputs.tool_args = cleaned_args   # 无条件双写
# model_display 仅流入 _emit_tool_call 的展示名（S_20 P4）
```

### HITL 改写（after_tool_call 内 @1338-1437）

```python
# 主信号：tool_name == "skill_acceleration_exec" 且 result dict 含 __skill_turbo_hitl__ 键
#   且 result["request"] 是 dict → ToolCallInterruptRequest.model_validate(request)
# 兜底信号：get_skill_turbo_hitl_tic() 非 None（且 tool 非 skill_acceleration_exec → stale，warning + drop）
# 改写：ctx.inputs.tool_result = ToolInterruptException(request, tool_call=ctx.inputs.tool_call)
#       ctx.inputs.tool_msg = ToolMessage(content=_SKILL_TURBO_HITL_PLACEHOLDER, tool_call_id=...)
#       return —— 跳过 _emit_tool_result（N8）
# 失败降级：result 仍是 marker dict → result 与 tool_msg 均置 _SKILL_TURBO_HITL_PLACEHOLDER（N9）
```

### skill_turbo ContextVar 复位三处对称

- `after_tool_call` 入口（@1315）：`reset_skill_turbo_context(ctx)` —— token 循环复位，幂等
- `after_invoke`（@986）：同上 + adapter 兜底 `clear_current_skill_turbo_adapter()`
- `on_model_exception`（@1494）：同上
- TIC（ToolInterruptException ContextVar）**不在** token 机制内（工具体直接 set）：session-is-None 早退路径（@1319-1328）单独 `set_skill_turbo_hitl_tic(None)` 清 stale TIC

## 数据结构

```python
# per-session 六字典（__init__ @357-368；sid = conversation_id 或 "default"）
_abort_requested:        dict[str, bool]                  # abort 标志
_pause_events:           dict[str, asyncio.Event]         # 惰性创建，创建即 set（默认畅通）
_conversation_ids:       dict[str, str]                   # sid → 真实 conv_id（"default" 哨兵不入值）
_main_sessions:          dict[str, Session]               # sid → session 对象（身份反查用）
_inflight_tool_calls:    dict[str, dict]                  # tc_id → {tool_call, session, session_id}（跨会话共享）
_cancelled_tool_results: dict[str, list[dict]]            # sid → 占位结果桶（collect→get→clear 序列）

# 实例级（非 per-session）
_checkpointer: Optional[Any]                # 租户域 checkpoint（set_checkpointer @384 注入）
_skill_turbo_adapter / _skill_turbo_request_metadata   # skill_turbo 桥接（set_* @388-396 注入）
_skill_turbo_hitl TIC:  server 运行时 ContextVar（skill_turbo_tools 模块持有）

# extra 键
"_jiuwenswarm_early_checkpoint_done"      # 早 checkpoint 幂等标记（N5）
RESUME_USER_INPUT_KEY（agent-core）       # resume 重放识别（S_20 N9 跳过名单共用）

# 占位文案（@641-645 + legacy 清单 @727-748）
"[Tool interrupted] Tool {name} was interrupted by the user and has no result."     # en 当前
"[工具执行被中断] 工具 {name} 执行过程中被用户打断，没有执行结果。"                    # zh 当前
"[Tool execution interrupted] ..."                                                  # en 旧版（legacy）
_SKILL_TURBO_HITL_PLACEHOLDER / _LEGACY / _LEGACY_V2                                # skill_turbo 三代（legacy，仅 skill_acceleration_exec）
DEEPRESEARCH_INTERACTION_PLACEHOLDER                                                # deepresearch（legacy，仅 deepresearch_execute）
"[Interrupted] Tool execution cancelled by user."                                   # collect_cancelled 产物（不参与占位判定）
```

## 执行路径全景

- **P1 暂停/终止生命周期**：interface 调 `pause(sid)` → event.clear → 下一 checkpoint（`before_model_call` / `before_tool_call`）wait 阻塞 → `resume(sid)` 解除；`abort(sid)` → 标志 + event.set + shell 进程树击杀 → 暂停中任务解除 wait、checkpoint 检查标志 raise `CancelledError` → server 侧进入 S_19 取消结算
- **P2 取消结果转发**：server 收到 cancel → `collect_cancelled_tool_updates(sid)` → 逐 inflight 项生成占位结果（N4 结构）并从 inflight 摘除 → `get_cancelled_tool_results(sid)` 取桶转发前端 → `clear_cancelled_tool_results(sid)` 清空——三步序列 per-session 隔离，防并发会话串桶
- **P3 早 checkpoint**：`before_model_call` 末尾（@1024）→ env / extra / heartbeat / session-agent-engine 四守卫 → `save_contexts` → `post_agent_execute`（tenant checkpointer 优先）→ extra 标记置位；后续 ReAct 迭代因标记跳过
- **P4 call_goal 注入**：`before_model_call`（@1016）→ 逐工具 deepcopy + schema 注入 + `model_copy` → 批量替换 `ctx.inputs.tools` → `_ensure_tool_call_goal_prompt`（@1134，system_prompt_builder 注入中英提示段落，团队工具点名 spawn_member / send_message / build_team 的 call_goal 语义）
- **P5 call_goal 剥离**：`before_tool_call`（@1237-1252）→ `extract_call_goal` → arguments / tool_args 无条件写回 → model_display 流入 `_emit_tool_call` 展示名
- **P6 HITL 改写**：`after_tool_call` → 入口 `reset_skill_turbo_context` → marker 检测（主信号）→ TIC 检测（兜底，非 skill_acceleration_exec 则 stale drop）→ TIE + placeholder ToolMessage 双改写 → return 跳过发射（N8）；任何异常 → 降级占位（N9）
- **P7 上下文修复**：`before_model_call`（@1022，图片剥离后）或 `on_model_exception`（@1497）→ `_fix_incomplete_tool_context` → tools parameters 补全 → call_goal schema 注入（复用）→ malformed JSON 规范化 → 占位/真实分类 → 四计数重建 → `pop_messages` + `add_messages` 回写 → info 日志四计数
- **P8 会话生命周期**：`before_invoke` 登记三件（conversation_ids / main_sessions / `_SID_KEY` extra）→ 任务间 `reset_for_new_task`（保 abort，N2）→ 下一任务入口 `reset_abort` → 会话终局 `cleanup_session` 五字典清空（N4）

## 异常分支与退出路径

| 异常 | 行为 | 后果 |
|---|---|---|
| abort 时 shell 击杀失败 / registry 导入失败 | `logger.debug`（exc_info） | 进程存活但模型链路仍被 checkpoint 终止 |
| 早 checkpoint 落盘异常 | warning + exc_info | 崩溃恢复降级（无该次快照），模型调用继续（N5） |
| `model_copy` 抛异常 | warning，跳过该工具注入 | 该工具无 call_goal schema，模型少填一个展示字段 |
| `ctx.inputs.tools` 批量回写失败 | warning（AttributeError / TypeError） | 注入整批丢弃，无泄漏 |
| `tc.arguments` 写回失败 | warning，继续流程 | 执行侧可能收到含 call_goal 的 args（schema 拒收风险） |
| `ToolCallInterruptRequest.model_validate` 失败 / 改写整体异常 | warning + 降级占位（N9） | HITL 卡片缺失但暂停语义保住 |
| stale TIC（非 skill_acceleration_exec 工具） | warning + set None | 旧 ContextVar 残留不误伤普通工具 |
| `_fix_incomplete_tool_context` 整体异常 | warning 吞 | 模型收到未修复上下文（可能 API 400 由上游重试兜底） |
| `_ensure_json_arguments` 返回非法 | warning + 返回 "{}" | 该 tool_call 参数重置空对象 |
| abort raise `CancelledError` | 冒泡至 agent-core / server | server 侧走 S_19 结算路径 |
| session-is-None 早退时 TIC 清理失败 | debug | stale TIC 残留至下次覆盖 |

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| 暂停中触发 abort | event.set 先解除 wait，标志后 raise | 缺 event.set 会永久挂起 | test_message_handler_stream_cancel.py |
| 断连触发 cancel + abort | 同 P1/P2，team 成员级联 | 漏 collect 会让前端缺工具终态 | test_message_handler_disconnect_cancel.py |
| 权限拒绝中断 | collect + abort + S_15 TIE 组合 | 占位与真实结果方向判错 | test_permission_interrupt_cancel.py |
| team 会话取消 | per-sid 隔离，成员 rail 继承 sid | A 会话取消杀死 B 会话 | test_chat_interrupt_team_cancel.py |
| reset_for_new_task 后旧 abort 残留 | 有意保留，新任务入口 reset_abort | 误清会让取消后的 in-flight 继续跑 | test_deep_adapter_interrupt.py |
| HITL resume 重放 | `RESUME_USER_INPUT_KEY` 跳过发射（S_20 N9）+ TIE 重写 | 不跳双发卡片 | test_skill_turbo_hitl_resume_react.py |
| marker dict 透传模型 | N9 降级占位 | 不降级会被判失败回退 skill_tool | test_skill_turbo_hitl_tie_rewrite_repro.py |
| interrupt 后恢复 | TIE 状态 + 上下文修复四计数 | 占位未被真实替换会误导模型 | test_skill_turbo_interrupt_recovery.py |
| 占位 + resume 完成态同 id 两条 | N10(d) 就地替换占位删 duplicate | 方向反了会保留占位丢真实结果 | test_interrupt_helpers.py / test_interface_interrupts.py |
| 历史会话旧占位文案 | legacy 7 类模板兜底 | 漏登记会当真实结果 | （随修复测试） |
| heartbeat 会话 | 早 checkpoint 跳过 | 不跳会产生无效快照 | （heartbeat 域测试） |
| 合法 JSON arguments | byte-for-byte 原样保留 | 重写会破坏 reasoning 回放 | （随修复测试） |
| 空消息列表 / 无 context | 修复早退 | 无影响 | （随修复测试） |
| 模型异常（API 400） | on_model_exception 修复后重试 | 修复失败由上游重试兜底 | test_skill_turbo_resume_usage.py |

## 修改检查单

- 改 pause / resume / abort / reset 语义或任一字典键控 → 必须连带检查 interface_deep 的 abort 调用点（@15248）与 cleanup 调用点（@3672），跑 cancel 四件（stream_cancel / disconnect_cancel / permission_interrupt_cancel / chat_interrupt_team_cancel）+ test_deep_adapter_interrupt.py
- 改 `reset_for_new_task` 是否清 abort → **先读 N2 注释**（@857-865 docstring 是契约的一部分），误清会复活已取消任务
- 改 `_maybe_early_checkpoint` 落盘点 / 守卫 → 必须确认仍走 `post_agent_execute`（post_run 会关流）；改幂等键名需评估同 invoke 多次落盘的写放大
- 改 call_goal 注入（deepcopy / model_copy 链）→ 必须验证执行侧无 `call_goal` 泄漏（send_file 类 `**kwargs` TypeError 案例，N6 docstring）；改 prompt 段落需连带中英双语文本
- 改 `extract_call_goal` 剥离或 display_name 边界 → 确认 `display_name` 业务字段未被误剥（N7）
- 改 `_fix_incomplete_tool_context` 窗口规则 / 四计数 / 占位替换方向 → 必须跑 HITL 重放与 interrupt 系列（tie_rewrite_repro / interrupt_recovery / interrupt_helpers / interface_interrupts）
- 新增任何工具中断占位文案 → **必须同步登记 legacy 模板清单**（`_is_legacy_tool_interrupt_placeholder_text` @727），否则历史数据判定翻转
- 改 HITL 改写信号源（marker / TIC）或 return 位置 → 必须验证不产生双卡片（N8 注释 @1411-1413）
- 改 collect / get / clear 序列结构 → 必须连带检查 server 侧消费者（S_19 的 pending 收集）与并发会话隔离
- 改 skill_turbo ContextVar 复位点 → 保持三处对称（after_tool_call / after_invoke / on_model_exception）+ TIC 单独清理路径

## 与其它 spec 的关系

- [[S_20]]：同一 rail 的另一半——正向发射通道。两 spec 在 `before_model_call` / `before_tool_call` / `after_tool_call` 三个钩子内职责交错：本 spec 的检查点（wait/abort）在钩子入口，S_20 的发射在钩子后段；sid 解析三序与 per-session 字典隔离的发射面影响归 S_20 N3/N4，状态语义归本 spec
- [[S_15]]：agent-core 中断原语（五状态键 / `ToolInterruptException` / `build_interrupt_state` / 三态决策）——本 spec N8 的 TIE 改写是让 harness 原生 HITL 机制检测到 S_15 中断协议的桥接点；rail 的 pause/abort 独立于 S_15 状态机
- [[S_19]]：server 侧取消结算——本 spec 的 `collect_cancelled_tool_updates` / abort 信号传播是其**上游**；ReAct 消息写入、序号代际、合法 ToolMessage 结算归 S_19
- [[S_02]]：TaskExecutionRail 任务事件族——todo close nudge 的跳过判定（本 rail N9/S_20）与任务间 `reset_for_new_task` 的任务边界语义在两域交界，任务事件本身不经本 rail
- [[S_05]]：todo 代际过滤——修复与恢复路径不触碰 todo 快照；`after_tool_call` 的 todo.updated 发射面归 S_20
- [[S_08]]：`__jiuwenswarm_session_id__` 键语义——本 spec 六字典全部按该键控（N4）；生产者实现与解析三序归 S_20
- [[S_06]] / [[S_09]]：SkillTurbo 引擎与 HITL 作答协议——本 spec 只管 rail 侧改写桥接（N8/N9）与 ContextVar 绑定/复位时机；作答注入、resume answers、加速通道回退归 skill_turbo 域
- jiuwenswarm 其它 AGENTS.md 体系：`server/runtime/agent_adapter/AGENTS.md`（abort / cleanup / checkpointer 注入的集成面）、`server/runtime/skill_turbo/AGENTS.md`（`_SKILL_TURBO_HITL_*` 常量族与 TIC 的实现归属）、`jiuwenswarm/common/AGENTS.md`（tool_display 的 inject/extract 实现）——接缝在此登记，不复制内容
