# S_15 中断原语与三态决策协议

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_15 / interrupt-primitives-and-tri-state-decision |
| 关联模块（agent-core 仓） | `openjiuwen/core/single_agent/interrupt/`（`state.py` 五状态键与状态模型、`response.py` `InterruptRequest`/`ToolCallInterruptRequest`、`exception.py` `ToolInterruptException`、`handler.py` `ToolInterruptHandler` 588 行、`resume_guard.py` 恢复分类四问）、`openjiuwen/harness/rails/interrupt/`（`interrupt_base.py` `BaseInterruptRail`+三态决策、`confirm_rail.py` `ConfirmInterruptRail`、`ask_user_rail.py` `AskUserRail`） |
| 关联模块（jiuwenswarm 仓） | 无本体文件——本层为纯 agent-core 原语层；jiuwenswarm 侧的权限 rail（[[S_16]]）、宿主装配（[[S_17]]）、相位守卫（[[S_18]]）、取消结算（[[S_19]]）均为其上层消费者，经 `openjiuwen` 包依赖引用 |
| 最近一次修订日期 | 2026-10-26 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **HITL（Human-in-the-Loop）中断交互的原语层与三态决策协议**——agent-core 为一切中断类 rail 与宿主交互提供的地基：

- **五状态键**（`state.py`）：INTERRUPTION_KEY / RESUME_USER_INPUT_KEY / INTERRUPT_AUTO_CONFIRM_KEY / RESUME_START_ITERATION_KEY / RESUME_BATCH_ALLOW_KEYS 的键名、存放位置与生命周期语义
- **状态模型**：`BaseInterruptionState` → `ToolInterruptionState`（interrupted_tools / auto_confirm_mapping）与 `ToolInterruptEntry`（含 sub-agent 中断的内外层 id 映射结构）
- **中断信号载体**：`ToolInterruptException`（`AgentInterrupt` 子类）与 `InterruptRequest`/`ToolCallInterruptRequest`（子类扩展字段经 `model_dump` 保留）
- **三态决策协议**（`interrupt_base.py`）：`InterruptDecision` 封闭三态（Approve / Reject / Interrupt）与 `_apply_decision` 的三种落地行为
- **两个基类子类**：`ConfirmInterruptRail`（ConfirmPayload 判定序）与 `AskUserRail`（问答收口语义）
- **中断处理器**（`handler.py`）：build → commit（含流式下发）→ resume（注入-重放-弹出）→ abandon（残留卡关闭）
- **恢复分类**（`resume_guard.py`）：四问函数族（pending 集合 / confirm 判定 / 纯权限判定 / 匹配判定 / 残留关闭判定）

不管：

- `PermissionInterruptRail` 本体（权限决策序、auto_confirm/batch_allow 双 key、persist_allow 磁盘授权记忆）→ [[S_16]]（其中 `compute_batch_allow_key` 的指纹算法契约归 S_16，本 spec 只管调用侧注入与弹出时机）
- jiuwenswarm 宿主侧装配（配置来源、三级 rail 选择、`_permission_scene_hook`、交互三源转换）→ [[S_17]]
- 中断相位机 / 权限响应台账 / 续跑注入守卫 → [[S_18]]
- 取消结算与收尾（jiuwenswarm 侧 `settle_cancelled_confirm_interrupt`、`{id}#{n}` 序号代际）→ [[S_19]]
- runner 框架层对 `AbortError` 的捕获与 unwind 机制本身——本 spec 只管 cause 链契约（rail 抛 `AbortError(cause=ToolInterruptException)`）
- SkillTurbo 的 HITL 卡片注册表与 `hitl_dedup` 序号代际 → skill_turbo 域 [[S_06]] / [[S_09]] 与 agent_adapter/AGENTS.md 铁律 2
- agent_teams 侧中断转发 / relay 去重 → agent_teams 域

## 不变量

- **N1（五状态键语义各自固定）**：键名与语义不得改动——`INTERRUPTION_KEY = "__react_agent_interruption__"`（存 `ToolInterruptionState`，session 持久）、`RESUME_USER_INPUT_KEY = "_resume_user_input"`（replay 期 ctx.extra）、`INTERRUPT_AUTO_CONFIRM_KEY = "__interrupt_auto_confirm__"`（auto_confirm 记忆，session 持久）、`RESUME_START_ITERATION_KEY = "_resume_start_iteration"`（续跑起点，ctx.extra）、`RESUME_BATCH_ALLOW_KEYS = "_resume_batch_allow_keys"`（批次级授权，ctx.extra）。session 持久键与 ctx.extra 键**不得互换存放位置**。
- **N2（batch allow 生命周期 = replay 本身）**（state.py @17-25）：`RESUME_BATCH_ALLOW_KEYS` 只在重放前注入 `ctx.extra`、在**同一 `finally`** 中与 resume keys 一并弹出、**永不写 session state**——后续迭代的全新批次必须重新询问。key 值复用 auto_confirm key 语义（工具名，或简单 shell 命令的 `tool:subcommand`）。
- **N3（中断信号唯一载体与 cause 链）**：工具执行被中断的唯一表达 = `ToolInterruptException`（request + tool_call 两字段，`AgentInterrupt` 子类）；rail 侧只能经 `_raise_interrupt` 抛 `AbortError(reason=..., cause=ToolInterruptException(...))`——runner 捕获 AbortError 后从 cause 中还原中断语义，**不得另造异常类型**。
- **N4（三态决策封闭）**：`InterruptDecision` 只有 `ApproveResult`（可带 `new_args` 改写 `ctx.inputs.tool_args` 后继续执行）、`RejectResult`（`_skip_tool`：置 `ctx.extra["_skip_tool"]=True` + `ctx.inputs.tool_result` + `ctx.inputs.tool_msg`，工具体跳过但结果二元组完整）、`InterruptResult`（触发 N3 中断）三态。`_apply_decision` 对未知子类静默放行——新增决策类型必须同时改 `_apply_decision`。
- **N5（ConfirmPayload schema 判定式）**（resume_guard.py @97-103）：confirm/权限卡的判定 = payload_schema 的 properties **含 `"approved"` 且不含 `"answers"`**。该判定式是纯权限关闭路径（S_19）与恢复路由的分类依据，两侧实现必须保持一致（jiuwenswarm 侧因 CI pin 旧版而本地复制实现，修改任一侧须同步另一侧）。
- **N6（恢复分类四问语义）**（resume_guard.py）：
  1. `pending_tool_resume_ids` = 外层 tool_call id 与内层 interrupt request id 的**并集**（结构化回复可命中任一层）
  2. `is_confirm_payload_interrupt`：任一 pending 是 confirm 卡即真
  3. `is_pure_permission_confirm_interrupt`：**全部** pending 均为 confirm 卡且无 `ask_user` / `skill_acceleration_exec`（`_SKIP_CONFIRM_TOOL_NAMES`）——混合中断留在既有 resume 路径，防残留 confirm 吞掉 ask_user 答案
  4. `is_matching_tool_resume`：InteractiveInput 的 `user_inputs` 键集**非空且 issubset** pending 集合
  5. `should_abandon_unmatched_confirm_resume`：纯 confirm + 不匹配 + 用户输入**非结构化**（纯文本如「继续」不是批准）→ 关闭当新 query；结构化输入（含空 / 错 id）**必须留在 handle_resume** 让卡片可重试
- **N7（commit 时即发交互流）**（handler.py @277-306）：`commit_interrupt` 在提交时即逐条 `session.write_stream` 交互 chunk 并置 `_interaction_emitted=True`（防 close_stream 先跑导致丢卡）；写流失败仅 warning，回落由 `write_interrupt_to_stream` 补发；后者见 `_interaction_emitted` 标志即跳过（防重复）。
- **N8（resume 注入逐键 + 回落通用键）**（handler.py @422-439 / @38）：按各 pending request `metadata["resume_user_input_key"]` 逐键把 user_input 注入 `ctx.extra`；任一 request 缺失该 metadata 或键集为空 → 回落注入通用 `RESUME_USER_INPUT_KEY`。**重放结束后 finally 必须弹出全部注入键**（含通用键与 N2 的批次键）——注入键泄漏到下一轮会让 rail 误判已获用户授权。
- **N9（batch allow 收集 fail-closed）**（handler.py @441-489）：仅 `user_inputs` 中 **approved** 的兄弟调用贡献 key；rejected / 未答的继续问；任何结构不匹配返回空集（不扩权）。`compute_batch_allow_key` 经 **deferred import** 引入（harness rail 模块依赖 core interrupt 模块，模块级导入会循环依赖）；指纹计算异常时该调用 key 记 `""` 丢弃。
- **N10（auto_confirm 记忆写入条件）**（handler.py @491-514）：仅当 user_input 是 InteractiveInput 且某项 `auto_confirm=True` 且 `state.auto_confirm_mapping` 命中该 inner_id 的 key 时，才写 `session` 的 `INTERRUPT_AUTO_CONFIRM_KEY`（session 无则跳过）。mapping 未命中不得写入——防把授权记忆挂到空 key 上放行一切。
- **N11（AskUserRail 空 answers 必收口）**（ask_user_rail.py @93-102）：answers 为空（用户跳过 / 未选 / 超时）必须 `reject` 返回显式跳过文案（中英文双语：「用户未提供回答……请不要重复提问」），**不得 re-pend**——重问帧复用同一 request_id，会被 team sidecar 去重吞掉，leader pending 到 relay watchdog kill run。非空 answers 以 Claude Code 风格 `"question"="answer"` 串作为 tool_result 返回。
- **N12（重放语义）**（handler.py @351-420）：`handle_resume` 按 **outer_id**（每工具/子代理一次，非每 inner_id）执行：deepcopy 各 tool_call（sub-agent 的把 user_input 塞进 `args.query`）→ 经注入的 `execute_tool_call` 回调重放（避免直接访问 protected member）→ 重放后仍有中断则 commit 新状态并返回 interrupt dict；否则置 `RESUME_START_ITERATION_KEY = state.iteration + 1` 返回 None 续跑。
- **N13（残留卡关闭写合法 ToolMessage）**（handler.py @327-349 / @530-588）：`abandon_unmatched_confirm_interrupt` 对 pending 调用写 cancelled ToolMessage（`[Tool execution interrupted] ...`，与既有 tool_call_id **去重**后追加），写失败仅 warning（仍清理）；最后清 `INTERRUPTION_KEY` 放行新 query。**不得重写原 tool_call_id 的 ASK**——RelayClaw 去重会吞掉。
- **N14（用户输入匹配三分支）**（interrupt_base.py @269-306）：`_get_user_input`——InteractiveInput 按 `user_inputs[tool_call_id]` 精确命中（未命中返回 None）；dict 输入按键命中否则整体返回；其它类型原样透传。子代理内部 rail 的 tool_call_id 即 inner_id，直接匹配，无需别名映射。

## 接口契约

### 状态与模型（`core/single_agent/interrupt/state.py`）

| 符号 | 契约 |
|---|---|
| 五个键常量 | N1 键名与语义；RESUME_BATCH_ALLOW_KEYS 注释即 N2 契约文本 |
| `BaseInterruptionState` | ai_message / iteration / original_query |
| `ToolInterruptEntry` | tool_call + `interrupt_requests: Dict[inner_id, InterruptRequest]` + `is_sub_agent` |
| `ToolInterruptionState` | 加 `interrupted_tools: Dict[outer_id, ToolInterruptEntry]` 与 `auto_confirm_mapping: Dict[inner_id, auto_confirm_key]` |

### 中断信号（`exception.py` / `response.py`）

| 符号 | 契约 |
|---|---|
| `ToolInterruptException(request, tool_call=None)` | N3 唯一信号载体；message 取 request.message |
| `InterruptRequest` | message / payload_schema / auto_confirm_key / ui_options / metadata 五字段 |
| `ToolCallInterruptRequest` | `model_config = {"extra": "allow"}`；`from_tool_call(request, tool_call)` 经 `model_dump` 保留子类扩展字段（如 `AskUserRequest.questions`），补 tool_name / tool_call_id / tool_args / index |

### 恢复分类（`resume_guard.py`）

| 函数 | 契约 |
|---|---|
| `pending_tool_resume_ids(state) -> frozenset` | N6.1 外内并集 |
| `is_confirm_payload_interrupt(state) -> bool` | N6.2 任一 confirm |
| `is_pure_permission_confirm_interrupt(state) -> bool` | N6.3 全 confirm 且无 `_SKIP_CONFIRM_TOOL_NAMES` |
| `is_matching_tool_resume(user_input, state) -> bool` | N6.4 非空 issubset |
| `should_abandon_unmatched_confirm_resume(user_input, state) -> bool` | N6.5 纯 confirm + 不匹配 + 非结构化 |

### `ToolInterruptHandler`（handler.py，ReActAgent 持有）

| 方法 | 契约 |
|---|---|
| `build_interrupt_state(results, tool_calls, ai_message, iteration, original_query)` | 收集 `ToolInterruptException`（直接中断：inner=outer）与 sub-agent 中断 dict（`result_type=="interrupt"` + `interrupt_ids`：逐内层 request）；无中断返回 `(None, [])` |
| `commit_interrupt(state, context, session, invoke_inputs, sub_agent_outputs)` | 先 `save_contexts` 再 `save` state；N7 commit 即发；置 `invoke_inputs.result` |
| `write_interrupt_to_stream(result, session)` | 见 `_interaction_emitted` 即跳过 |
| `handle_resume(resume_ctx: ResumeContext)` | N12 重放语义；返回 interrupt dict（继续等待）或 None（续跑） |
| `abandon_unmatched_confirm_interrupt(state, session, context)` | N13 残留卡关闭 |
| `save` / `load` / `clear` | INTERRUPTION_KEY 的 session 读写清 |
| `ResumeContext` dataclass | state / user_input / ctx / context / session / invoke_inputs / `execute_tool_call` 回调（注入以避免访问 protected member） |

### `BaseInterruptRail`（interrupt_base.py，`priority = 90`）

| 成员 | 契约 |
|---|---|
| `before_tool_call(ctx)` | tool_name ∈ `_tool_names` 过滤 → `_resolve_tool_call_id` → N14 `_get_user_input` → 读 session `INTERRUPT_AUTO_CONFIRM_KEY` → `resolve_interrupt` → ask_user 专用 `_record_ask_user_event`（轨迹事件 requested / resolved，异常只 debug，**不得破坏中断路径**）→ `_apply_decision` |
| `resolve_interrupt(...)` | 子类必须实现；返回三态之一 |
| `approve(new_args=None)` / `reject(tool_result=None)` / `interrupt(request)` | 三态工厂 |
| `_raise_interrupt` | N3 AbortError cause 链 |
| `_skip_tool` | N4 RejectResult 落地（`_skip_tool` / tool_result / tool_msg 三件套） |
| `add_tool` / `add_tools` / `get_tools` | 注册拦截工具名；`add_policy` 为废弃别名（policy 忽略） |

### `ConfirmInterruptRail`（confirm_rail.py）

| 成员 | 契约 |
|---|---|
| `ConfirmPayload` | approved / feedback / auto_confirm / **persist_allow**（`auto_confirm=True + persist_allow=False` = 仅本会话记忆——语义边界归 [[S_16]]） |
| `resolve_interrupt` | user_input None → auto_confirm 命中 approve 否则 interrupt（带 ConfirmPayload schema）；解析失败 → **再 interrupt**（重问）；approved → approve；否则 reject(feedback) |
| `_get_auto_confirm_key(tool_call)` | 默认返回 `tool_call.name`（子类可覆写——S_16 覆写为 AST 解析） |

### `AskUserRail`（ask_user_rail.py）

| 成员 | 契约 |
|---|---|
| `init(agent)` / `uninit(agent)` | 按语言注册 / 注销 `AskUserTool` 到 ability_manager |
| `AskUserRequest` | `questions` 扩展字段（靠 N3 的 extra-allow 序列化保留） |
| `resolve_interrupt` | None → interrupt；dict 含 answers / 单问 answer / 非空 str → payload；N11 空 answers 收口 reject |
| `_build_skipped_tool_result()` | N11 双语跳过文案 |

## 数据结构

- 中断结果 dict：`{"result_type": "interrupt", "state": [OutputSchema(INTERACTION, InteractionOutput(id=inner_id, value=payload))], "interrupt_ids": [inner_id], "_interaction_emitted": bool}`——sub-agent 透传时 state 为原 OutputSchema 列表
- `auto_confirm_mapping: Dict[inner_id, auto_confirm_key]`——inner_id 到可记忆 key 的映射（N10 写入前提）
- `ToolInterruptEntry.is_sub_agent=True` 标记外层调用是子代理（resume 时走 `_build_sub_agent_resume_tool_call` 把 user_input 塞 `args.query`）
- resume 注入键集合：`_resume_user_input_keys_for_state` 返回 `{metadata 键集} ∪ {RESUME_USER_INPUT_KEY（有任一缺失时）}`

## 执行路径

1. **P1 首次中断**：rail `before_tool_call` → `resolve_interrupt` 返回 `InterruptResult` → `_raise_interrupt` 抛 `AbortError(cause=ToolInterruptException)` → runner 捕获 unwind → ReActAgent 从结果中收集（`_collect_interrupts` 区分直接异常与 sub-agent dict）→ `build_interrupt_state` → `commit_interrupt`（save contexts → save state → 发交互流 → 置 result）→ invoke 返回 interrupt dict
2. **P2 用户恢复**：新请求带 InteractiveInput 到达 → `should_abandon_unmatched_confirm_resume` 判定（N6.5：非结构化且不匹配 → 走 P3 关闭；其余进 resume）→ N10 存 auto_confirm → N8 逐键注入 + N9 批次键注入 → deepcopy 重放（sub-agent 塞 query）→ `finally` 弹出全部注入键 → 重放仍有中断 → 递归 commit（回 P1 语义）；无中断 → 置起点 iteration 返回 None 续跑
3. **P3 rail 侧决策**：重放中每工具经 `before_tool_call` → N14 取用户输入 → resolve → Approve（可改 args）/ Reject（N4 skip 三件套，不执行工具体但结果完整）/ Interrupt（再抛）
4. **P4 残留卡关闭**：`abandon_unmatched_confirm_interrupt` → 写 cancelled ToolMessage（去重）→ 清 INTERRUPTION_KEY → 新 query 被当普通输入放行

## 异常分支与退出路径

- commit 时写流失败：warning 吞掉，回落 `write_interrupt_to_stream` 补发（N7）
- ConfirmPayload 解析失败：**再 interrupt**（重新弹卡），不静默放行（fail-closed）
- ask_user 轨迹事件记录失败：debug 吞掉（不得破坏中断路径）
- batch allow 指纹计算异常：该 key 丢弃（N9 fail-closed）
- abandon 时写 cancelled ToolMessage 失败：warning + 仍清理 INTERRUPTION_KEY（N13）
- `_build_sub_agent_resume_tool_call` 参数解析失败：args 置 `{}` 后塞 query

## 边界 case

| 场景 | 行为 | 依据 |
|---|---|---|
| 纯文本「继续」打到纯 confirm 卡上 | 关闭残留卡当新 query | N6.5 / N13 |
| 结构化回复但 id 全错 | 留在 handle_resume，卡可重试 | N6.5 |
| 混合中断（confirm + ask_user 同时 pending） | 不走纯权限关闭，走既有 resume | N6.3 |
| 批次内 3 个同参调用，用户批准 1 个 | 重放时另 2 个凭批次键跳过询问 | N2 / N9 |
| 用户 auto_confirm + persist_allow | session 记忆与磁盘记忆分流（磁盘侧归 S_16） | N10 |
| sub-agent 中断恢复 | user_input 塞进重放 tool_call 的 args.query | N12 |
| 重放产生新中断 | 递归 commit，多轮卡继续 | P2 |
| 用户跳过 ask_user（空 answers） | reject 显式跳过文案，不 re-pend | N11 |
| close_stream 先于 agent 返回运行 | 交互卡已在 commit 时发出，不丢失 | N7 |
| rail 收到 dict 型非结构化 resume 输入 | 按键命中或整体透传 | N14 |

## 修改检查单

改 state.py / resume_guard.py / handler.py / interrupt 基类三件前：

- [ ] 五状态键的键名与存放位置未被互换（N1）？
- [ ] RESUME_BATCH_ALLOW_KEYS 仍在 replay 的同一 finally 弹出、未写入 session（N2）？
- [ ] 中断信号仍唯一走 ToolInterruptException + AbortError cause 链（N3）？
- [ ] 新增 InterruptDecision 子类时 `_apply_decision` 已同步（N4）？
- [ ] `_is_confirm_payload_schema` 判定式与 jiuwenswarm 侧本地副本同步（N5）？
- [ ] `_SKIP_CONFIRM_TOOL_NAMES` 增删时纯 confirm 判定语义复核（N6.3）？
- [ ] commit 即发 + `_interaction_emitted` 防重未被破坏（N7）？
- [ ] resume 注入键 finally 弹出无泄漏（N8）？
- [ ] batch allow 收集仍 fail-closed、deferred import 未提级（N9）？
- [ ] auto_confirm 写入仍要求 mapping 命中（N10）？
- [ ] ask_user 空 answers 仍收口不 re-pend（N11）？
- [ ] 残留卡关闭未改为重写原 ASK（N13）？

## spec 关系

- 上游：agent-core runner 的 AbortError 捕获框架（框架层，无 spec）
- 下游：[[S_16]]（PermissionInterruptRail 继承 ConfirmInterruptRail 并覆写 `_get_auto_confirm_key` / resolve 决策序）、[[S_17]]（宿主把 interrupt dict 转换为 ask_user_question 卡）、[[S_18]]（相位机与台账包住 resume 周期）、[[S_19]]（jiuwenswarm 侧残留卡结算复用 N5 判定式与 N13 行为）
- 跨域：skill_turbo [[S_06]] / [[S_09]] 的 HITL 卡片注册表消费本层 request_id 语义；agent_teams 域消费 sub-agent 中断 dict 透传格式；harness 域 [[S_08]] 错误检测器与本域 `[Tool execution interrupted]` 前缀互不消费
