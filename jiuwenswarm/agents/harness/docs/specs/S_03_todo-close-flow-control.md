# S_03 — todo 收口流控：close nudge 与 hold/drop/release 三帧

| | |
|---|---|
| Spec ID | S_03 |
| 状态 | 当前实现的真实描述 |
| 修订日期 | 2026-10-28 |
| 关联模块 | `jiuwenswarm/agents/harness/common/rails/task_execution_rail.py`（与 [[S_02]] 同文件的另一机制面：`_todo_close_nudge_message` / `_attach_todo_close_nudge` / `_arm/_finish/_retry/_mark/_write_todo_close_control` / `_reject_todo_close_nudge` / `_is_last_react_iteration` / `_started_in_progress_tasks`）<br>`jiuwenswarm/server/runtime/agent_adapter/todo_close_text_hold.py`（117 行，adapter 侧缓冲状态机）<br>消费点：`jiuwenswarm/server/runtime/agent_adapter/interface_deep.py` stream() 主循环（实例创建 ~21056、first_chunk 判定 ~21121、decide 分派 ~21206、buffer push ~21298、`_parse_stream_chunk` 剔除 ~22402）<br>配合面（不展开）：`skill_turbo/rails/skill_prompt_rail.py` 的 guide 文本与 `_PPT_DELIVERY_SUMMARY_POST_TOOL_HINT`、`JiuSwarmStreamEventRail` 对 nudge 的静默处理 |
| 装配点 | rail 侧随 [[S_02]]（config `enable_task_execution_rail`，默认 False）一并装配，无独立开关；adapter 侧在每条用户请求的 stream() 循环内新建 `TodoCloseTextHold()` 实例（per-request 生命周期） |

## 1. 范围与边界

本 spec 管 **"纯文本收尾时已开始的 todo 仍未收口的拦截与缓冲"**——为什么与怎么做：

- 约束链：ReAct loop 把"无 tool_calls 的响应"当最终答案 → `task.complete` 只在 todo_modify 后发出 → 纯文本收尾会把停止解释留在前端仍开放的任务段里
- 机制一（rail 侧）：`after_model_call` 把一次纯文本结尾**替换**为内部 close nudge 假 ToolCall，逼模型先 todo_modify 再重写正文
- 机制二（流控）：`before_model_call` 预先发 hold 帧，adapter 把本轮外层正文缓冲；attach 成功发 drop（作废），否则发 release（放行）
- 可靠性：控制帧写失败的重试状态机；commit 帧兜底 flush

**不管**（指向其它 spec）：

- 事件映射 / auto_advance / 绑定 / 产物检测（同文件的其余机制面）→ [[S_02]]
- todo.json 数据层与工具本体 → [[S_01]]
- nudge 文案中引用的"技能协议第 9 条用户打断"规则本体 → 中断交互域（未归档）与 skill_turbo 体系
- skill_turbo prompt 文本"先单独调 todo_modify"的内容维护 → `server/runtime/skill_turbo/AGENTS.md`（本 spec 只锚定其与 nudge 的配合关系）

## 2. 核心不变量

**N1 同一 invoke 至多一次 nudge**
`_todo_close_nudge_used` 挂接成功即置 True；`before_invoke` 重置为 False（跨请求预算刷新）。第二次纯文本收尾直接放行——避免"收口-重写-又被拦"死循环。

**N2 真假 todo_modify 的唯一区分是 tool_call_id 前缀**
nudge 假调用 `name="todo_modify"`（`TODO_CLOSE_NUDGE_TOOL_NAME`，与真工具**同名**），id 形如 `todo-close-nudge-{uuid12}`（`TODO_CLOSE_NUDGE_ID_PREFIX`）。`is_todo_close_nudge` 只认 id 前缀。nudge 的 `before_tool_call` 走 `_reject_todo_close_nudge`：`ctx.extra["_skip_tool"] = True` 拦截执行，tool_result/tool_msg = 中文收口指令（含"上一轮正文视为作废、必须完整重写"）；**不改 todo.json、不触发 auto_advance、`after_tool_call` 直接 return**。流式侧 `JiuSwarmStreamEventRail` 对 nudge 同样不发 tool_call/tool_update/tool_result 帧（用户全程不可见）。

**N3 挂接前置条件（全过才挂）**
`_attach_todo_close_nudge` 七条闸门，任一不过即返回 False：① `_todo_close_nudge_used` 已用；② `ctx.session is None`；③ response 已有 tool_calls（真工具调用照常走）；④ `finish_reason == "length"`（截断重试会持久化无配对 tool_result 的 assistant 消息，不能拦）；⑤ `_is_last_react_iteration`（最后一轮没有空间补一轮回复）；⑥ content 为空；⑦ `_started_in_progress_tasks()` 为空。

**N4 挂接即抹除原响应**
attach 成功时 `response.content = ""` 且 `response.tool_calls = [nudge]`——历史里留下原文会让下一轮误以为"已送达"（适配器此刻还没下发挂起的流式块）。

**N5 hold 布防先于流式输出，条件与 nudge 判定同源**
`before_model_call` → `_arm_todo_close_text_hold`：先重试未送达帧（N7），再按"未用 nudge + 非最后迭代 + 有 started in_progress"发 hold 帧。`finish_reason=length` 在调用返回前不可知，截断轮同样被 hold、事后 release——这是设计允许的（多缓冲一轮，正确放行）。armed 期间外层 `llm_output` 全部入缓冲（`should_buffer`）。

**N6 drop 永不重写为 release**
`_finish_todo_close_text_hold`：`want_drop = dropped or _todo_close_undelivered == "drop"`。drop 写失败记 `undelivered="drop"`，重试仍发 drop——若改发 release，前端会看到本应作废的草稿正文。

**N7 未送达帧在下一次 before_model_call 首先重试**
`_retry_undelivered_todo_close` 在 arm 之前执行：`_todo_close_undelivered ∈ {"drop","release"}` 时重发对应帧，成功才清账；推断规则——`undelivered is None and hold_pending` 视为 release（hold 已发出但 finish 未达或失败，语义是放行）。

**N8 foreign 子流不缓冲**
`should_buffer(foreign=True)` 恒 False：子代理 / 子 LLM 输出（`_is_foreign_stream_source` 判定）正常流式，不受外层收口流控影响。

**N9 commit 帧兜底 flush**
`TODO_CLOSE_COMMIT_CHUNK_TYPES = {"tool_call", "answer", "task.complete"}`：release 帧丢失时，这三类"本轮输出已定案"的帧先 flush 缓冲再正常处理（`flush_pass`）。补刀作废走 drop，不会落到这里。

**N10 二次 hold 丢弃未 release 的旧缓冲**
`on_control("todo_close_hold")` → `_replace_hold()`：清空旧 payloads 并 warning——上一轮 hold 未 release 说明那轮已异常结束，旧正文不可信。

**N11 控制帧对用户侧三重不可见**
① 不计 first_chunk 统计（`is_todo_close_control` 排除，流式日志的首 token 时间不含内部控制帧）；② `_parse_stream_chunk` 对控制帧返回 None（非 (type,payload) 形态的 chunk 不下发）；③ decide 返回 skip（(type,payload) 形态的也不下发）。

**N12 `_started_in_progress_tasks` 双路径取数**
优先"本轮已 start"（`_active_tasks` ∩ `_todo_started` 且 status=in_progress，HITL resume 会清空这两个内存集合而前端段仍开放）；为空回退磁盘 `_todo_map` 中 stamped in_progress（覆盖 text-only resume 场景）；`generation_token` 缺失的 legacy 行**跳过**（非本轮工作，不追收口）。

## 3. 接口契约

### 3.1 rail 侧（task_execution_rail.py，全部私有方法 + 两个模块级公开）

| 入口 | 签名要点 | 行为 |
|---|---|---|
| `before_model_call` | — | 先 `_bind_context_to_in_progress_task`（[[S_02]]）再 `_arm_todo_close_text_hold` |
| `after_model_call` | — | `dropped = _attach_todo_close_nudge(ctx)`，finally `_finish_todo_close_text_hold(ctx, dropped=dropped)`——attach 抛错也必须结账 |
| `_attach_todo_close_nudge(ctx)` | → `bool` | 七闸门（N3）→ 抹除原响应（N4）→ 置 `_todo_close_nudge_used` |
| `_arm_todo_close_text_hold(ctx)` | — | 先重试（N7）→ 四闸门（undelivered 在账 / hold_pending / nudge 已用 / 最后迭代 / 无开放任务，任中即不布防）→ 发 hold 帧 |
| `_finish_todo_close_text_hold(ctx, *, dropped)` | — | session None 直接清三态；want_drop 走 drop 帧否则 release 帧；未布防且无对应 undelivered 则不发（省帧） |
| `_retry_undelivered_todo_close(ctx)` | — | N7 重试 |
| `_write_todo_close_control(session, stream_type)` | → `bool` | `session.write_stream(OutputSchema(type, index=0, payload={"result_type": "control"}))`；异常 warning + False |
| `_started_in_progress_tasks()` | → `list[(raw_id, task)]` | N12 双路径；空列表 = 无需收口 |
| `_is_last_react_iteration(ctx)` | → `bool` | `react_iteration >= _react_agent._config.max_iterations`（两者皆有效正整数才判定） |
| `_reject_todo_close_nudge(ctx)` | — | N2 拒绝路径 |
| `_todo_close_nudge_message(open_tasks)` | → `str` | 中文收口指令：判定/收口规则四条/作废重写条款/逐项 `id=... content=...` 清单 |
| `is_todo_close_nudge(tool_call)`（模块级） | → `bool` | id 前缀判定（N2） |

### 3.2 adapter 侧（todo_close_text_hold.py）

| 入口 | 返回 | 语义 |
|---|---|---|
| `decide(chunk_type, *, foreign=False)` | `skip` / `flush_skip` / `flush_pass` / `buffer` / `pass` | 每帧唯一决策入口：控制帧 skip（release 为 flush_skip）；commit 帧兜底 flush_pass；armed 且外层 llm_output 为 buffer；其余 pass |
| `on_control(chunk_type)` | `arm` / `drop` / `release` / `""` | 应用控制帧副作用；二次 hold 触发 `_replace_hold`（N10） |
| `should_buffer(foreign)` | `bool` | `not foreign and armed`（N8） |
| `push(payload)` / `take()` | — | 缓冲追加 / 取走并清空 |
| `should_flush_for(chunk_type)` | `bool` | commit 帧且缓冲非空（N9） |
| `is_todo_close_control(chunk_type)`（模块级） | `bool` | 三帧类型判定（供 first_chunk 排除与 parse 剔除） |

### 3.3 消费点（interface_deep.py，adapter 主循环内）

`todo_close_hold = TodoCloseTextHold()` 每请求新建 → 每帧先 `decide` 分派（skip 直接 continue；flush_* 先 `_take_todo_close_chunks()` 下发缓冲再按需 continue）→ `llm_output` 且动作 buffer 时 `push(delta_payload)` 而不下发。`_take_todo_close_chunks` 下发前补插 `_begin_visible_chat_content` 气泡边界帧。

## 4. 数据结构

**rail 实例三态**（`__init__` 建初值，`before_invoke` 重置；与 [[S_02]] 其余状态同生命周期）：

| 字段 | 值域 | 含义 |
|---|---|---|
| `_todo_close_nudge_used` | `bool` | 本 invoke 的 nudge 预算（N1） |
| `_todo_close_hold_pending` | `bool` | 已发 hold 未确认 drop/release |
| `_todo_close_undelivered` | `None` / `"drop"` / `"release"` | 写失败待重试的帧类型（N6/N7） |

**控制帧**：`OutputSchema(type ∈ {"todo_close_hold","todo_close_drop","todo_close_release"}, index=0, payload={"result_type": "control"})`——三常量 `TODO_CLOSE_HOLD/DROP/RELEASE_STREAM_TYPE` 定义在 todo_close_text_hold.py，rail 侧 import 使用（单一真相源）。

**hold 缓冲**：`armed: bool` + `_payloads: list[dict]`（chat.delta payload 原样暂存）。实例 per-request，无跨请求状态。

**nudge 假 ToolCall**：`ToolCall(id="todo-close-nudge-{hex12}", type="function", name="todo_modify", arguments="{}")`。

## 5. 执行路径全景

**P1 正常放行（release 路径，多数轮次）**：before_model_call 无开放任务/未布防 → 无控制帧 → 正文正常流式。布防了的正常轮：hold 帧 → llm_output 入缓冲（用户侧零输出）→ after_model_call attach 返回 False（有 tool_calls 或判定不过）→ release 帧 → adapter flush 缓冲下发（含气泡边界帧）。

**P2 nudge 拦截闭环（drop 路径）**：hold 帧 → 正文入缓冲 → 纯文本收尾且七闸门全过 → attach 抹除原响应挂 nudge（N4）→ drop 帧（缓冲正文作废，N6）→ ReAct 因 tool_calls 非空继续 → before_tool_call 拦截 nudge 返回收口指令（N2）→ 模型调真 todo_modify → [[S_02]] P3 闭环发 task.complete → 下一轮纯文本收尾（N1 已用，不再拦；hold 亦不再布防）→ 正文直达用户。

**P3 失败重试**：finish 阶段 drop/release 帧写失败 → `undelivered` 记账 → 下一次 before_model_call 的 `_retry_undelivered_todo_close` 首先重发 → 成功清账后本轮才继续 arm 判定。极端情况下 release 永远没到 → adapter 侧 commit 帧兜底（N9）。

**P4 adapter 分派表**（decide 输出 → 主循环动作）：

| 动作 | 触发 | 主循环行为 |
|---|---|---|
| skip | hold / drop 帧 | continue，不下发 |
| flush_skip | release 帧 | 下发缓冲 + 不下发 release 帧本身 |
| flush_pass | commit 帧且缓冲非空 | 下发缓冲 + 继续处理本帧 |
| buffer | armed 外层 llm_output | push 后 continue |
| pass | 其余（含 foreign llm_output、llm_usage、llm_reasoning……） | 正常下发 |

**P5 不达路径**：`finish_reason=length` 截断轮（不挂 nudge，但 hold 已布防 → release 放行）；最后 ReAct 迭代（不挂不布防，正文照常流式）；force_finish 路径（deepresearch 直显等）不进 after_model_call，维持各自原有收尾行为；无 started in_progress（三态全部静默）。

## 6. 异常分支与退出路径

| 分支 | 行为 | 违反后果 |
|---|---|---|
| `ctx.session is None` | attach 返回 False；finish 直接清三态不发帧 | 无流可写，机制自然退化为不拦 |
| `_write_todo_close_control` 抛错 | warning + False → undelivered 记账（N7） | 本轮帧丢失；drop 丢失由 commit 兜底可能露出草稿（可接受的最差情况），release 丢失由 N9 兜底 |
| attach 自身抛错 | finally 仍执行 finish（dropped=False → release） | 正文放行，收口失败留给下一轮 |
| 二次 hold（上轮未 release） | `_replace_hold` 丢旧缓冲 + warning | 旧正文本来就是异常轮的，丢弃正确 |
| 控制帧混入旧格式 chunk 流 | `_parse_stream_chunk` 对控制帧返回 None | 不下发，兼容 |

## 7. 边界 case 清单

| Case | 期望 | 测试锚点 |
|---|---|---|
| 纯文本收尾挂一次 nudge | 挂接且仅一次 | `test_text_only_ending_attaches_one_todo_close_nudge` |
| 截断（finish_reason=length）不挂 | 不挂 | `test_truncated_text_does_not_attach_nudge` |
| 最后 ReAct 迭代不挂 | 不挂 | `test_last_react_iteration_does_not_attach_nudge` |
| 有真 tool_calls 时不改响应 | 保留原调用 | `test_text_only_ending_keeps_real_tool_calls` |
| 无开放任务不挂 | 不挂 | `test_text_only_ending_skipped_without_open_task` |
| nudge 被拦截且不推进 todo | `_skip_tool` + 无 auto_advance + `_todo_map` 不变 | `test_nudge_tool_is_skipped_and_does_not_advance_todos` |
| release 失败在下次 hold 前重试 | 重发 release | `test_failed_release_is_retried_before_the_next_hold` |
| drop 失败重试仍发 drop（不发 release） | N6 | `test_failed_drop_is_retried_and_not_released` |
| before_invoke 重置 nudge 预算 | 三态清零 | `test_before_invoke_resets_nudge_budget` |
| text-only resume 对磁盘 stamped in_progress 补 nudge | N12 回退路径 | `test_text_only_resume_nudges_stamped_disk_in_progress` |
| unstamped legacy 行不触发 nudge | 跳过 | `test_unstamped_legacy_in_progress_does_not_nudge` |
| skill_turbo 指南要求先收口再总结（cn/en + PPT hint） | prompt 配合面 | `test_skill_turbo_success_hint_closes_todo_before_summary` |
| 技能协议允许 final 前一轮独立 todo_modify（含打断场景） | prompt 配合面 | `test_skill_protocol_allows_standalone_todo_modify_before_final` |
| 流式 rail 对 nudge 不发三帧 | emitted == [] | `test_stream_rail_does_not_emit_todo_close_nudge` |
| hold+drop 后 release 归还正文 | 缓冲语义 | `test_hold_drops_text_and_release_returns_it` |
| commit 帧在 release 丢失时兜底 flush | N9 | `test_commit_frame_flushes_when_release_was_missed` |
| 二次 hold 丢弃未 release 缓冲 | N10 | `test_second_hold_discards_unreleased_text` |
| drop 序列不转发正文 | N6 | `test_drop_sequence_does_not_forward_text` |
| release 序列只转发一次且 answer 去重 | flush_skip 语义 | `test_release_sequence_forwards_text_once_and_answer_is_deduped` |
| foreign llm_output 不缓冲 | N8 | `test_foreign_llm_output_is_not_buffered` |
| 旧格式 chunk 路径剔除控制帧 | N11② | `test_parse_stream_chunk_drops_todo_close_control` |

（前 14 项位于 `tests/unit_tests/agentserver/rails/test_task_execution_todo_close_nudge.py`，后 7 项位于上一级 `tests/unit_tests/agentserver/test_todo_close_text_hold.py`。）

## 8. 修改检查单

1. 改 nudge 拦截条件（N3 七闸门）→ 逐条评估"拦不住"（正文吞进任务段）与"拦错"（死循环/截断轮被拦）两侧后果，跑 `test_task_execution_todo_close_nudge.py` 全量
2. 改三帧类型名或 payload → rail 侧 `import` 单一真相源（todo_close_text_hold.py）+ interface_deep 消费点 + N11 三处不可见保证同步
3. 改 drop/release 语义 → **绝不可**把 drop 失败重写成 release（N6 是产品语义不是实现细节）
4. 改 commit 帧集合 → 评估"定案帧"完备性：漏一种 = 该路径正文卡死到请求结束
5. 改 `_started_in_progress_tasks` → 双路径与 unstamped 跳过语义（N12）别丢；与 [[S_02]] N7 代际过滤共用 token 判定
6. 改 hold 布防时机 → 记住 hold 必须先于首个 llm_output 帧（before_model_call），晚一帧就漏缓冲
7. 改 `_todo_close_nudge_message` 文案 → 测试断言了"没有送达用户""完整重写"关键词（`test_nudge_tool_is_skipped_and_does_not_advance_todos`），同步改
8. 在 adapter 主循环加新 chunk 类型 → 先过 `decide` 分派（或明确 pass），别绕过缓冲状态机

## 9. 与其它 spec 的关系

- **[[S_02]]**：同文件按机制面切分——`_todo_close_*` / `_attach_todo_close_nudge` / `_arm/_finish/_retry` 方法族与本 spec 的三态归 S_03，其余归 S_02；`_started_in_progress_tasks` 是共享判定入口（S_02 §3.1 表中标注指向本 spec）；`before_invoke` 同一处重置两域状态
- **[[S_01]]**：nudge 迫使模型调用的 todo_modify 是 S_01 工具链的普通消费者；`generation_token` 语义（N12 的 stamped 判定）以 S_01 N5 为准
- **[[S_04]]**（计划审批）/ **[[S_06]]**（deepresearch 落盘）：审批流与 deepresearch 的收尾不走 close nudge（force_finish 路径，见 P5）
- **[[S_05]]**（profile 变体）：flash/code 的 todo 工具卡变体不改变收口流控协议
- `server/runtime/skill_turbo/AGENTS.md`：guide 文本与 PPT 交付 hint 的"先单独 todo_modify"是 prompt 层配合，减少 nudge 触发概率；机制本体在本 spec
- `server/runtime/agent_adapter/AGENTS.md`：`TodoCloseTextHold` 的消费循环、first_chunk 排除、parse 剔除落在 adapter 侧（接缝登记处）
