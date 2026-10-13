# SkillTurbo 事件流协议规约（event-stream-protocol）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_08 / event-stream-protocol |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/`：executor.py（事件构造 / 缓冲 / task 管线）、markdown_stream.py、skill_turbo_tools.py（tool 层转发）、rails/artifact_rail.py、rails/delivery_summary_rail.py；外层 interface_deep.py（HITL 三件套 / invocation_paused） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无（首批 specs） |

## 范围 / 边界

### 本规约管什么

executor / rails 产出的**事件类型全集、发射顺序约束、流式缓冲、tool 层转发协议与前端契约**：

- **两条物理通道**（executor 直接 yield 通道 A / session 流排水通道 B）与汇聚模型。
- **事件产生源两套机制的架构对比**（普通任务 TaskExecutionRail todo 驱动 vs 本模块 executor 节点树驱动；接缝即缺陷面）。
- 事件类型全集表（通道 A / 通道 B / adapter 侧三分表，含 payload 关键字段、产生点、顺序约束、tool 层处理）。
- **task 事件补充机制**五件套：预置任务列表、FIFO + deferred、resume 重放抑制、task.update 带外 OOB、外层 todo 抑制。
- **六条关键顺序约束**（含代码位置与违反后果）。
- 流式缓冲 `_StreamBufferState` 与 markdown 围栏保护三规则。
- **tool 层转发协议 4 步**（skip 集 → 外层 todo 抑制 → 9 项反向映射 → OOB 推送）。
- HITL 三件套事件时序、usage 事件规则、交付总结时序三方契约、artifact.generated、stream_source_id 三层透传、端到端流向图。

### 本规约不管什么

| 子主题 | 归属 spec |
|---|---|
| 执行主循环、双协程生产/消费循环的实现、退出路径全景 E1–E13 | [[S_01]] |
| HITL 中断/恢复状态机、resume_ctx、卡片去重策略 | [[S_06]] |
| AgentWebSocketServer / Gateway / PushRegistry 的 WS 细节与 E2A wire 字段 | [[S_09]] |
| 降级触发决策（为何降级路径不发 chat.error 的决策侧） | [[S_07]] |
| StreamEventRail 的 TIE 改写机制与 ContextVar 绑定 | [[S_09]] / [[S_06]] |

## 事件产生源：与普通任务的两套机制（前置架构背景）

任务列表（task.start / task.complete / task.update）在本仓有**两套独立的事件产生源**——这是理解本 spec 全部约束的前提，也是任务列表类缺陷集中在本模块的结构性原因：

| 维度 | 普通任务（非 SkillTurbo） | SkillTurbo 内部 Stage |
|---|---|---|
| 产生源 | `TaskExecutionRail`（`agents/harness/common/rails/task_execution_rail.py`，外层 DeepAgent rail） | `executor` 的 `_initialize_pending_tasks` / `_before_subplan_execute` / `_after_subplan_execute`（引擎节点树回调） |
| 驱动方式 | **todo 工具驱动**：外层 LLM 显式调 `todo_create` / `todo_modify` / `todo_get` / `todo_list`，rail 检测 todo 状态转移（pending→in_progress→completed）发事件；非 todo 工具经 `_ACTIVE_TASK_ID` ContextVar 归属当前 todo | **引擎驱动**：depth==1 subplan 的编排回调自动发事件，外层 LLM 无参与——外层视角只有一次 `skill_acceleration_exec` 工具调用 |
| task 粒度 | 外层 LLM 自主规划的 todo（主观分期，跨多轮工具调用，天然串行） | 节点树预编排的 Stage（单次工具调用内密集产生，含并发节点） |
| 注入路径 | rail → 外层主循环（2 层） | executor 双协程汇聚 → tool 转发（skip/映射/剥 id）→ 外层主循环 → gateway → 上层前端（5 层） |
| 特有维度 | 无 | 外层 todo 仲裁（抑制+剥 id）、双协程排空排序（deferred）、delta 淹没（task.update OOB 旁路）、**resume 重放事件三态**（跳过/抑制/复用 task_id） |

两套机制**共享底层**：同一条 `parent_session.write_stream → AgentWebSocketServer → Gateway → 上层前端`（taskProgress 槽位 / taskStack segment）消费链、`send_push` OOB 通道、产物检测逻辑（`SkillTurboArtifactRail` 复用 `TaskExecutionRail` 的 `detect_artifact_paths_safe` 等共享 API）。**无法合并产生源**——TaskExecutionRail 只能观察到外层工具调用，看不到 turbo 节点树内部；turbo 的 Stage 事件必须由引擎代理产生。

**两套的接缝 = 缺陷面**。普通任务没有问题而本模块问题集中，不是因为用了劣质实现，而是 turbo 的任务列表天然多出五个普通任务不存在的维度：①注入（事件要从工具执行内部穿透到外层流）、②仲裁（两套 task_id 空间不能同时占列表，外层 todo 活跃时内部事件整体抑制）、③双协程排序（横幅先于 task.start、complete 不抢 start）、④规模（千级 delta 淹没 FIFO → OOB 旁路）、⑤resume 重放（中断恢复后哪些事件重发/抑制/复用 task_id）。修改本 spec 涉及的任何路径时，先对照此表确认改动落在哪个维度、会不会波及其它维度。

## 不变量

1. **双通道单向汇聚**：事件只有两个物理入口——通道 A（execute_plan_stream / _execute_node_stream 直接 yield）与通道 B（`session.write_stream`，由 `drain_session_stream` 排水转 A）。LLM 相关事件一律走通道 B；节点 / rails 不得绕过通道汇聚直写前端。
2. **plan.started 必须最先**：execute_plan_stream 在 root 准备前即 yield；前端依赖它创建规划流根节点。
3. **`_make_chunk` 是 AgentResponseChunk 唯一构造出口**：所有 `_make_*` 系列集中经它构造（request_id / channel_id / payload / is_complete），防止字段漂移。
4. **task.start / task.complete 与 chat.\* 保持 FIFO 同序，禁止 OOB**；唯一允许带外推送的是幂等全量快照 `task.update`（经 `send_push`，后到覆盖先到）。
5. **外层有活跃 todo 时内部 task 事件整体抑制**：task.start / task.complete / task.update 三类全 skip + 剥 `task_` 前缀 task_id（防覆盖外层 todo 槽位 / taskStack 嵌套 / segment 分裂）。
6. **暂停态禁止终态帧**：executor 的 AbortError 分支零终态帧（无 plan.finished / 无 complete / 无 chat.error / 无 chat.final），直接 raise；由 adapter 以 `chat.invocation_paused`（is_complete=True）终结。executor 从不构造 chat.final——"谁发终帧"是两层契约。
7. **异常退出统一 flush 生命周期事件**：FallbackContractError / FallbackLimitExceededError / AbortError / 普通异常四分支均先排空 deferred + drain task 事件再 raise / 事件化；唯一例外 CancelledError / KeyboardInterrupt 直接 raise（现状）。
8. **缓冲集合封闭**：仅 `chat.delta` / `chat.reasoning` 可缓冲（`_BUFFERABLE_EVENT_TYPES`）；首个 chunk 立即发（低首字延迟）；非缓冲事件透传前必须先 flush 全部缓冲。
9. **横幅特例**：带 `_bubble_progress` 的 chat.delta 绕过缓冲、不带 task_id、tool 层映射为 `content_chunk`（不延迟气泡进度、不归入 taskRuns 槽位）。
10. **markdown 围栏三规则**：闭合围栏 + 开放围栏粘连时 incoming 前缀 `\n`；`` ``` ``+`json` 流式拆分**不**插换行（否则破坏开放围栏）；`last_emitted` 跨桶 / 跨 flush 存活（前端 streamText 相邻拼接）。
11. **llm_usage 仅成功轮发送**（失败轮泄漏会与后成功轮重复计费）；resume 时 chat.llm_usage 在 session_history 中改写为 usage_metadata。
12. **交付总结骨架单一通道**：只经 `_pending_ppt_delivery_summary` ContextVar → DeliverySummaryRail 在外层 tool_result 之后流出；不进 tool_result 正文、不进中途 chat.delta。
13. **skip 集与 9 项反向映射表是 tool 层唯一过滤 / 转译权威**：映射缺失 = 事件被静默丢弃（无告警）。
14. **stream_source_id 三层透传**：引擎事件注入 → gateway 透传 → 上层前端按桶分组；任一环丢失 → 并发输出逐字交错。
15. **fallback.started / fallback.finished 不在 skip / 映射集**：以原 event_type 转发父会话。
16. **OOB 送达数语义**：`send_push` 返回送达数，0 = 未投递，调用方不得把 warning 当成功。

## 接口契约

### 两条物理通道

- **通道 A（yield 通道）**：execute_plan_stream / _execute_node_stream 直接构造 `AgentResponseChunk` yield 给上层（SkillTurbo.run_stream → adapter → tool 循环）。plan/node 生命周期、task 事件、fallback 事件、complete 帧、节点流输出（chat.delta）走此通道。
- **通道 B（session 流）**：call_llm / stream_llm / StreamEventRail / artifact_rail 经 `session.write_stream(OutputSchema(...))` 写入，由 `_execute_node_stream` 的 `drain_session_stream` 协程排水、`parse_stream_chunk` 归一为带 `chat.` 前缀 event_type 的 payload，汇入通道 A。tool 层再反向映射回 OutputSchema.type（往返一轮，故映射表以 chat.\* 为键）。

### tool 层转发协议（skill_turbo_tools，4 步）

1. **skip 集**（`_SKILL_TURBO_SKIP_EVENT_TYPES`，plan/node 生命周期 4 类：plan.started / plan.finished / node.started / node.finished）→ 丢弃。防御动机：plan.started 的 content 会被外层 `_parse_stream_chunk` fallback 误改写为 chat.delta 泄露给用户。
2. **外层 todo 抑制**：外层有活跃 todo（`get_current_task_id()` 或 rebound ContextVar）→ 内部 `task.*` 三类全 skip + `_without_inner_task_routing` 剥 `task_` 前缀 task_id。
3. **反向映射**：`_prepare_parent_stream_output` 经 `_SKILL_TURBO_EVENT_TYPE_TO_OUTPUT_TYPE`（9 项）把 event_type 映射为 DeepAgent OutputSchema.type → `parent_session.write_stream(OutputSchema)` 转发。9 项：

| event_type（键） | OutputSchema.type |
|---|---|
| chat.delta | llm_output |
| chat.reasoning | llm_reasoning |
| chat.llm_usage | llm_usage |
| chat.usage_metadata | llm_usage |
| chat.tool_call | tool_call |
| chat.tool_result | tool_result |
| chat.tool_update | tool_update |
| chat.tool_calls.delta | tool_calls.delta |
| chat.error | error |

   横幅特判：chat.delta 且带 `_bubble_progress` → `content_chunk`（剥 `_bubble_progress` 与 task_id）。映射表未命中的 chat.\* 事件按原 event_type 直传；tool_call / tool_result / tool_update 类**未进映射即被外层静默丢弃**。

4. **task.update OOB**：`_push_task_event_out_of_band` → `AgentWebSocketServer.send_push`——绕过父会话 FIFO，解决数千页生成 delta 淹没 FIFO 致任务列表滞后；task.start / task.complete 留 FIFO 与 chat.\* 同步推进。OOB 消息结构：

```python
{
    "request_id": …, "channel_id": …, "session_id": …,
    "payload": {…, "event_type": "task.update"},   # 幂等全量快照
    "is_complete": False,
}
# 返回值 = 送达连接数；0 = 未投递，仅告警不得当成功
```

### HITL 三件套事件时序（adapter 侧收口）

```
StreamEventRail.after_tool_call 把 skill_acceleration_exec 的 tool_result 改写为 TIE
（__skill_turbo_hitl__ 返回值标记为主信号 / _skill_turbo_hitl_tic ContextVar 兜底）
  → harness build_interrupt_state
  → chat.tool_call(status=pending_approval) → chat.tool_update → chat.ask_user_question
      （卡片 request_id = 外层 skill_acceleration_exec call id；上层前端按外层 call id 路由作答）
  → chat.invocation_paused（awaiting_user_input=True, is_complete=True 终结帧）
  → gateway_normalize 转为 is_final=False 的 in_progress 帧——客户端不得当任务完成
```

对照：`chat.error + is_complete` → gateway 转 E2A final FAILED（response_kind=e2a_error）；正常 complete → E2A SUCCEEDED。卡片去重 / 复用 / 序号策略（同一外层调用第 N 次中断共用 call id、卡片 request_id 改 `{id}#{n}`）归 [[S_06]]。

### usage 事件规则

- `chat.llm_usage` 仅成功轮发送（`_emit_llm_usage`；失败轮丢弃，防泄漏后成功轮重复计费）。
- resume 时 `chat.llm_usage` 在 session_history 中改写为 `chat.usage_metadata`（历史重放口径）。
- usage 多形态序列化统一走 `_serialize_usage_metadata`；事件携带 plan_name（显示名）与 stream_source_id。

### 交付总结时序三方契约

1. **P10 交付节点**（ppt 技能 delivery.py）把纯模板骨架（零 LLM、通道内确定性产出）写入 `artifact_holder["p10_delivery"]["delivery_summary"]`。
2. **tool 层** `_wrap_skill_turbo_result` 成功分支把骨架挂到 `_pending_ppt_delivery_summary` ContextVar——**不进 tool_result 正文、不在流水线中途 chat.delta**：中途无 task_id 会与过程尾同桶，且早于 tool_result 会被上层前端 pptTurboSummary 收集窗口清空。
3. **DeliverySummaryRail**（priority=90，晚于 StreamEventRail 的 80）在外层 tool_result 之后：`take_pending_ppt_delivery_summary()` 取走并清空 → `startswith(DELIVERY_SUMMARY_START)` 校验（标记常量经动态包 `skill_turbo_codes_ppt.ppt.delivery_summary` 引用，ppt 技能不在场时短路）→ `write_stream(OutputSchema(type="llm_output", payload={"content": summary}))`。

防御：HITL / 审批中断（`_is_tool_interrupt`）→ `clear_pending_ppt_delivery_summary()` 不发成功骨架；session None → 清 pending 跳过；emit 异常仅 debug 日志。HITL 续跑无外层工具循环，由 `visible_ppt_turbo_finish_text` 直接返回骨架或中性短句。

### stream_source_id

`_gen_stream_source_id(node_name)` → `skill_turbo:{node}:{hex8}`。`concurrent=True` 的 call_llm / stream_llm 生成并注入 llm_reasoning / llm_output / llm_usage 事件；前端按桶分组并发输出。三层透传链：引擎事件注入 → gateway 透传 → 上层前端分桶——任一环丢失即逐字交错。

### artifact.generated 事件（SkillTurboArtifactRail 检测管线）

产物检测复用 jiuwenswarm `TaskExecutionRail` 共享逻辑（`detect_artifact_paths_safe` / `WorkspaceBaselineState` / `filter_unhooked` / `fire_artifact_hook` / `mark_hooked` / `update_baseline_after_hook`），仅对 `ARTIFACT_DETECTION_TOOL_NAMES` 内的工具生效：

- **before_tool_call**：按 tool_call_id 记录开始时间（供 mtime 校验）；bash 类工具懒建工作区基线（执行前文件快照，供增量 diff）。
- **after_tool_call** 检测管线（顺序固定）：
  1. 线程 + 超时 + 异常兜底执行检测（防 `stat()` 阻塞 event loop）；基线 diff 扫描失败/超限 → `baseline.disabled=True` 本会话禁用基线、降级文本提取（两级检测）；
  2. `filter_unhooked` 幂等去重——文件身份 = 路径 + `mtime_ns` + `size`；
  3. `fire_artifact_hook`（`IMAGE_ARTIFACT_POST_PROCESS` / `ARTIFACT_POST_PROCESS` 扩展 hook，发射事件**前**执行，供扩展原地后处理如加水印），fired 后 `mark_hooked`；
  4. hook 可能改写文件 → `update_baseline_after_hook` 局部刷新基线（含 sha256 读文件，放线程）；
  5. 发射 `artifact.generated` 到 session 流（通道 B）。
- rail 持有 executor 引用（构造注入）；`_hooked_artifacts`（已 hook 文件身份集合）与 `_tool_start_times` 为 rail 实例状态——executor 每请求新建 → 请求隔离。

### task 事件与前端槽位映射

| 事件 | 前端消费位 | 抑制后果 |
|---|---|---|
| task.update | taskProgress 槽位（全量快照覆盖） | 快照丢失 → 任务列表滞后 |
| task.start / task.complete | taskStack segment 归属（chat.\* 事件挂到对应 segment） | 丢 start → 后续 chat.\* 无 segment 可挂 |
| chat.delta（横幅） | 主气泡进度（不进 taskRuns） | 误带 task_id → 被归入 taskRuns 槽位 |
| task.start / complete（外层 todo 激活时） | 整体抑制 + 剥 task_id 后仅 chat.\* 透传 | 不抑制 → 覆盖外层 todo / taskStack 嵌套 |

### 通道 B 归一往返示例

```
call_llm / stream_llm
  → session.write_stream(OutputSchema(type="llm_output", payload={content, …}))
  → drain_session_stream → parse_stream_chunk
  → payload = {event_type: "chat.delta", content, plan_name(显示名), stream_source_id?}
  → 通道 A 汇入（缓冲分桶）
  → tool 层 _prepare_parent_stream_output：chat.delta → llm_output
  → parent_session.write_stream(OutputSchema(type="llm_output"))
```

往返一轮的意义：executor 内部统一以 `chat.*` event_type 处理（缓冲 / skip / task_id 注入），外层 DeepAgent 统一以 OutputSchema.type 消费——两套命名系统的边界就是 tool 层 9 项反向映射表。

## 数据结构

### 事件类型全集 · 通道 A（executor 直接构造）

| event_type | payload 关键字段 | 产生点 | 顺序约束 | tool 层处理 |
|---|---|---|---|---|
| `plan.started` | plan_name="root", content | `_make_plan_started_chunk`（流开头） | 必须最先 | **skip** |
| `plan.finished` | plan_name, final=True, status?, error? | `_make_plan_finished_chunk` | 流末 / 降级收口；后接 complete | **skip**（降级路径唯一终态事件） |
| `node.started` / `node.finished` | plan_name(显示名), content?, task_id? | `_make_node_started/finished_chunk`（produce 协程） | task.start 先于同 stage chat 事件 | **skip** |
| `chat.delta` | content, plan_name, task_id?, data?, `_bubble_progress`? | `_make_node_delta_chunk`（节点流输出；显示名替换、progress/ok 消息补换行） | 缓冲桶合并；横幅绕缓冲 | 映射 `llm_output`；横幅 → `content_chunk`（剥 task_id） |
| `chat.reasoning` | content… | 节点流 / 通道 B 归一 | 可缓冲 | 映射 `llm_reasoning` |
| `chat.error` | error, plan_name?, task_id? | `_make_error_chunk` / `_make_node_error_chunk` | 先 drain task 事件再发 | 映射 `error`（gateway：is_complete+chat.error → E2A final FAILED） |
| `task.start` | task_id, task_content(显示名), task_index, status="in_progress", source="skill_turbo", timestamp | `_build_task_start_event` + `_before_subplan_execute`（仅 depth=1） | 延期至开始横幅之后 | 外层无活跃 todo 转发；有 → skip + 剥 task_id |
| `task.complete` | task_id, status="completed"/"failed", duration_ms, error? | `_build_task_complete_event` + `_after_subplan_execute`（业务失败 `export_status=failed` / `status=error` 也标 failed） | 先 flush deferred start 再入队 | 同上；FIFO 禁 OOB |
| `task.update` | tasks[全量快照(深拷贝,按 index 排序)], total/completed/in_progress/pending/failed_tasks, parent_request_id, timestamp | `_build_task_update_event` / `_emit_task_update_event` | 幂等快照后到覆盖 | 转发 + **OOB 推送** |
| `fallback.started` / `fallback.finished` | node_name, error?, timestamp | fallback_handler `_fallback_stream_impl` | 与 chat 流 FIFO | 原样转发（不在 skip / 映射集） |
| complete 帧 | payload=None, is_complete=True | `_make_complete_chunk` | 每条非 raise 终态路径最后一帧 | 流结束信号 |

### 事件类型全集 · 通道 B（session.write_stream → drain 归一）

| event_type（归一后） | payload 关键字段 | 产生点 | tool 层处理 |
|---|---|---|---|
| `chat.delta`（LLM 正文） | content, plan_name(显示名), stream_source_id? | call_llm / stream_llm → session 流 | 映射 `llm_output` |
| `chat.reasoning` | content… | call_llm / stream_llm（reasoning 实时刷；失败轮脏前缀由重试覆盖） | 映射 `llm_reasoning` |
| `chat.llm_usage` / `chat.usage_metadata` | usage | `_emit_llm_usage`（仅成功轮） | 映射 `llm_usage` |
| `chat.tool_call` | name, arguments, tool_call_id | StreamEventRail.before_tool_call 补发（turbo 内部工具） | 映射 `tool_call` |
| `chat.tool_result` | tool_call_id, content | rail.after_tool_call（中断态跳过） | 映射 `tool_result` |
| `chat.tool_update` | tool_call_id, status/content | rail.after_tool_call | 映射 `tool_update` |
| `chat.tool_calls.delta` | 增量参数 | rail | 映射 `tool_calls.delta` |
| `artifact.generated` | artifacts[{path,name,extension,size,exists}], tool_name, task_id, timestamp, count | SkillTurboArtifactRail（session.write_stream，OutputSchema type 直接用） | 经 `_make_session_event_chunk` 透传 |

### adapter / 外层侧事件

| event_type | 产生点 | 语义 |
|---|---|---|
| `chat.tool_call(pending_approval)` + `chat.tool_update` + `chat.ask_user_question` | harness `__interaction__` 转换（外层 tool_call_id 对齐） | HITL 三件套前两帧 |
| `chat.invocation_paused` | adapter 流末 `hitl_pending_stream`（is_complete=True） | 暂停终结帧；gateway 转 is_final=False in_progress |
| `llm_output`（交付总结） | DeliverySummaryRail（priority=90，外层 tool_result 之后） | 交付骨架唯一出口 |

### gateway_normalize 帧语义对照

| 输入特征 | gateway 输出 | 前端语义 |
|---|---|---|
| chat.error + is_complete | E2A final FAILED（response_kind=e2a_error） | 本轮以系统异常终结 |
| awaiting_user_input=True（invocation_paused） | is_final=False in_progress 帧 | 流保持开启等待作答，不得当任务完成 |
| 正常 complete 帧 | E2A_COMPLETE SUCCEEDED | 本轮成功终结 |

### 流式缓冲（`_StreamBufferState` / `_StreamBufferBucket`）

- 分桶键 `(stream_source_id, event_type)`：并发节点按 source_id 隔离，串行共享 None 桶；桶间 FIFO。
- 可缓冲类型：`chat.delta` / `chat.reasoning`。flush 时机五个：

| # | 时机 | 实现点 |
|---|---|---|
| 1 | 首 chunk 立即发（低首字延迟） | 桶 `first_chunk_sent` 标记 |
| 2 | 3s 到期（`_SKILL_TURBO_STREAM_FLUSH_INTERVAL_SECONDS`） | 消费循环到期检查 |
| 3 | 非缓冲事件透传前 | 先 flush 全部桶再透传 |
| 4 | 事件类型切换时 | 防 delta 与 reasoning 交错 |
| 5 | 流末 | `_flush_all_buffer_chunks` 清底 |

- `last_emitted` 存活于桶 pop / flush 之后，跨桶保持围栏分隔。

### task_states 单条记录

```python
{
  "task_id": "task_{uuid8}",      # 首跑生成；resume 按 content/index 对齐复用
  "task_content": display_name,   # resume 对齐键之一
  "task_index": int,              # resume 对齐键之二
  "source": "skill_turbo",
  "status": "pending|in_progress|completed|failed",
  # in_progress: start_time；终态: end_time / duration_ms / error?
}
```

## 执行路径全景

### 典型请求事件时序（单 stage 视角）

```
plan.started                                    ← 流第一帧
task.update（全量 pending 快照）                 ← 预置任务列表
  ── stage N 开始 ──
chat.delta(_bubble_progress 横幅)               ← 绕缓冲、不带 task_id
task.start(stage N)                             ← deferred flush，晚于横幅
task.update（in_progress 快照）
node.started / chat.delta / chat.reasoning …    ← 缓冲桶合并输出
chat.tool_call / chat.tool_result …             ← 通道 B 排水
task.complete(stage N)                          ← 先 flush deferred start 再入队
task.update（completed 快照）
  ── …后续 stage 重复… ──
plan.finished(final=True)                       ← 流末
complete(is_complete=True)                      ← 非 raise 终态路径最后一帧
（外层）tool_result → DeliverySummaryRail llm_output（交付骨架，成功分支）
```

异常变体：HITL 中断在任意点触发 → 零终态帧 + 三件套 + invocation_paused；fallback 终止 → task flush + plan.finished(failed) + complete（无 chat.error，[[S_07]]）。

### P1 事件产生与双协程汇聚

节点回调（chat 输出 / 工具事件）与 session 流（LLM 事件）→ `_execute_node_stream` 双生产者（`produce_node_output` + `drain_session_stream`，持 `_task_event_drain_lock` 串行化排空）→ `asyncio.Queue`（异常对象与 None 哨兵也走队列）→ 消费循环分型转发（异常分型归 [[S_01]]）。enqueue 顺序：先 flush deferred task 事件 → drain task 队列 → 横幅 / 普通事件 chunk。

### P2 缓冲层与 markdown 围栏保护

execute_plan_stream 消费循环对 chat.delta / chat.reasoning 按 (source_id, event_type) 分桶；五个 flush 时机见数据结构节。围栏保护三规则（markdown_stream.py）：

1. **粘连防护**：连续 chat.delta 无分隔符拼接时，闭合围栏 ```` ``` ```` 与下个开放围栏 ```` ```json ```` 会粘成一行六反引号——CommonMark 不视为 closer，其后执行日志全被吞进一个 `<pre>`。`markdown_stream_incoming(previous, incoming)` 检测到粘连（incoming 首行是围栏行 / previous 末行是围栏行且 incoming 不是语言标签续行）时给 incoming 前缀 `\n`。
2. **拆分不插**：流式常把 ```` ```json ```` 拆成 ```` ``` ```` + `json` 两块，此情形**不**插换行（否则破坏开放围栏）；`_FENCE_INFO_CONTINUATION` 限定 info 必须是单一语言标记（无空格），防英文进度文本被误判为围栏续行。
3. **流末收口**：`terminate_dangling_markdown_fence` 对以完整围栏行结尾的内容补 `\n`；marker-only 围栏（无 info）不动。`last_emitted` 记录跨桶 / flush 存活，保证前端相邻 streamText 拼接时围栏分隔不丢。

### P3 task 事件管线（五件套）

1. **预置任务列表**：`_initialize_pending_tasks` 为每个 depth=1 subplan 分配 `task_{uuid8}`（pending）并立即发全量快照（首个 node 输出前让前端看到全量待办）；resume 时 `_restore_task_states_from_snapshot` 按 task_content / task_index 双键对齐**复用原 task_id**（防前端叠第二套 Stage）。
2. **FIFO + deferred**：task 事件入 `_task_events_queue_var`，`_drain_task_event_chunks` 排空；`_deferred_task_lifecycle_events` 把 task.start / task.update 先暂存，等阶段开始横幅入队后再 `_flush_deferred_task_lifecycle_events`（横幅先行约束）。
3. **resume 重放抑制**：completed stage 跳过 start / complete；in_progress stage 跳过重复 start + 横幅抑制；`_park_later_in_progress_tasks` 保证同时只有一个 running。
4. **task.update OOB**：唯一允许绕过 FIFO 的 task 事件（幂等全量快照经 `send_push` 旁路直达，防 delta 淹没致任务列表滞后）。
5. **外层 todo 抑制**：见接口契约 tool 层第 2 步。

### P4 终态收口

正常路径：flush 全桶 → plan.finished(final) → complete。各异常分支的帧组合与 flush 义务见"六条关键顺序约束"第 5/6 条与 [[S_01]] E 表；降级路径（E10/E11）不发 chat.error 的决策侧归 [[S_07]]。帧组合速查：

| 退出路径 | chat.error | plan.finished | complete | raise |
|---|---|---|---|---|
| 正常完成（E1） | — | final | ✓ | — |
| 节点错误事件化（E3/E4） | node_error / 透传 | failed | ✓ | —（流继续/收口） |
| 校验/加载失败（E5/E6） | ✓ | **无** | ✓ | — |
| 超时（E7） | ✓ | failed | ✓ | — |
| HITL 中断（E8） | **无任何终态帧** | 无 | 无 | ✓ |
| fallback 终止（E10/E11） | **不发** | failed | ✓ | ✓ |
| 未知异常（E12） | ✓ | failed | ✓ | —（吞） |
| 用户取消（E9） | 无 | 无 | 无 | ✓（不 flush） |

### P5 tool 层转发

skill_turbo 工具体消费 executor 流 → 4 步协议（skip / 抑制 / 映射 / OOB）→ `parent_session.write_stream`。

### P6 外层链路（端到端流向图）

```
PlanNode 回调(rails/session stream)
  → executor._execute_node_stream 双协程汇聚(produce_node_output + drain_session_stream
     → asyncio.Queue)
  → execute_plan_stream(缓冲/flush/过滤) → AgentResponseChunk
  → skill_turbo tool 循环(skip/映射/剥id) → parent_session.write_stream(OutputSchema)
  → 外层 DeepAgent 主循环(_parse_stream_chunk) → AgentResponseChunk
  → AgentWebSocketServer(E2A wire, ws://127.0.0.1:{agentPort})
  → Gateway(PushRegistry / E2A normalize)
  → 上层前端(气泡 / taskProgress 槽位 / taskStack segment)
  （task.update 另有 send_push OOB 直推旁路）
```

### P7 HITL 暂停时序

executor AbortError 分支零终态帧直接 raise → adapter `_emit_skill_turbo_hitl_chunks` 发三件套（chat.tool_call(pending_approval) → chat.tool_update → chat.ask_user_question）→ `chat.invocation_paused` 终结帧 → gateway 转 is_final=False in_progress 帧，前端流保持开启等待作答。HITL 检测到 ask_user 后 adapter 跳过后续所有 chunk（chat.final / chat.delta / chat.reasoning）。

### P8 交付总结时序

三方契约见接口契约节；HITL / 审批中断分支不发成功骨架；HITL 续走 `visible_ppt_turbo_finish_text`。

### P9 resume 重放事件行为

task_states 快照恢复（复用 task_id）→ 恢复首段发 task.update 快照 → completed stage 静默跳过（无 start / complete / 横幅）→ in_progress stage 完整重入但抑制重复 start → 恢复首段不广播"全流程开始"（防假进度）。对照表：

| stage 快照状态 | task.start | task.complete | 开始横幅 |
|---|---|---|---|
| completed | 跳过 | 跳过 | 跳过 |
| in_progress | 跳过（防重复） | 正常发 | 抑制 |
| pending | 正常发 | 正常发 | 正常发 |

### 六条关键顺序约束

| # | 约束 | 代码位置 | 违反后果 |
|---|---|---|---|
| 1 | plan.started 必须最先 | execute_plan_stream 流开头 | 前端无根节点，后续 chunk 无处挂载 |
| 2 | task.start 先于同 stage chat 事件 | drain 协程持锁先排 task 事件 + deferred 机制 | 开始横幅前出现孤儿进度 chunk；双排空时 start 反超横幅 |
| 3 | task.complete 不得抢在 task.start 前 | `_after_subplan_execute` 先 `_flush_deferred_task_lifecycle_events` 再入队 complete（子节点零 chunk 时 enqueue 不触发 drain 兜底） | 前端 taskStack segment 归属错乱、start/complete 计数不配对 |
| 4 | 横幅 `_bubble_progress` 绕缓冲、不带 task_id | 消费循环横幅分支 + `_make_node_delta_chunk` | 横幅延迟 3s 或被归入 taskRuns 槽位 |
| 5 | 暂停态禁止终态帧 | executor AbortError 分支零帧 raise；adapter invocation_paused 收尾（两层"谁发终帧"契约） | 前端把暂停当完成/失败终结本轮，resume 无从续接 |
| 6 | 异常退出统一 flush 生命周期事件 | 消费循环四分支（FallbackContract / FallbackLimit / AbortError / 普通异常）先排空 deferred + drain 再 raise / 事件化；唯一例外 CancelledError / KeyboardInterrupt | 前端任务面板停滞 in_progress |

## 异常分支与边界 case

| # | 边界 case | 现行为 / 风险 | 测试覆盖 |
|---|---|---|---|
| 1 | E5/E6 校验/加载失败 | yield chat.error + complete 后 return，**无 plan.finished**（不对称）；前端靠 complete 兜底收口 | test_exit_path_frames（E5/E6 帧序列） |
| 2 | E9 用户取消 | CancelledError / KeyboardInterrupt 不 flush task 事件直接 raise；前端面板可能滞留 in_progress，依赖上层终止流（现状最薄弱路径） | test_exit_path_frames（E9 用例） |
| 3 | 双排空乱序 | produce / drain 双协程排空时 task.start 反超开始横幅 → deferred 机制保证横幅先行 | 部分：test_resume_skip_completed（回调级 deferred 用例；双协程竞态级无） |
| 4 | markdown 围栏粘连 | 闭合 ```` ``` ```` + 开放 ```` ```json ```` 粘成六反引号，CommonMark 不视为 closer，其后日志全吞进一个 `<pre>` | test_markdown_stream_fence |
| 5 | ```` ``` ````+`json` 拆分 | 流式拆块场景误插换行会破坏开放围栏 → `_is_fence_info_continuation` 不插 | test_markdown_stream_fence |
| 6 | OOB 送达数为 0 | send_push 返回 0 = 未投递；当作成功则任务快照静默丢失 | 部分：test_skill_turbo_tools（OOB 旁路）；0 送达判定无用例 |
| 7 | 外层 todo 抑制失效 | taskProgress 槽位被内部步骤覆盖 / taskStack 嵌套 / segment 分裂 | test_skill_turbo_tools |
| 8 | 缓冲 flush 时机错配 | 类型切换或非缓冲事件前未 flush → 相邻 delta 交错、围栏状态错乱 | 部分：test_resume_skip_completed（横幅绕缓冲） |
| 9 | usage 失败轮泄漏 | 失败轮发送 llm_usage 会与后成功轮重复计费 | test_llm_retry_rail_mount（failed attempt 不发 usage） |
| 10 | 交付骨架校验失败 | `startswith(DELIVERY_SUMMARY_START)` 不过 → 静默跳过；ppt 动态包不在场返回空串短路走各自兜底文案 | test_ppt_delivery_summary |
| 11 | task_id 未剥 | 内部 task_id 泄漏进外层事件 → segment 分裂 / 槽位抢占 | test_skill_turbo_tools |
| 12 | fallback 事件透传 | 不在 skip / 映射集，以原 event_type 转发——新增 skip 项时勿误收 | test_fallback_context_writeback |
| 13 | chat.error 抢跑 | 前端 useWebSocket 收 chat.error 清 activeRequestIdRef 抢先终结本轮——降级路径因此禁发 chat.error（决策侧见 [[S_07]] P9） | test_exit_path_frames（E10/E11 禁发断言） |
| 14 | invocation_paused 语义 | gateway 转 is_final=False in_progress 帧，客户端不得当任务完成；对照 chat.error+is_complete → E2A final FAILED | 部分：test_exit_path_frames（executor 侧零帧已锁定）；adapter 终结帧无用例 |
| 15 | plan.started content 泄露 | 不 skip 会被外层 `_parse_stream_chunk` fallback 误改写为 chat.delta 泄露给用户 | 无（tool 层 skip 集无用例） |
| 16 | task.update delta 淹没 | 数千页生成 delta 淹没 FIFO，任务列表滞后（Stage 卡 in_progress 直至交付后）→ OOB 旁路解决 | test_skill_turbo_tools（OOB 用例） |
| 17 | parent_session 为 None | task 事件 drop 并告警（任务列表到不了前端）；resume 侧 session_id 缺失仅告警继续 | 无专项用例 |
| 18 | 中断态 tool_result 抑制 | StreamEventRail 中断态跳过 `_emit_tool_result`（卡片由 harness `__interaction__` 转换统一发出）——否则工具结果凭空出现 | 部分：test_skill_turbo_hitl_tie_rewrite_repro（TIE 改写侧击） |
| 19 | task.update 快照一致性 | 全量快照必须深拷贝 + 按 task_index 排序；否则后到快照与前端列表顺序抖动、并发修改穿透 | 无直接断言 |
| 20 | 横幅与 task.start 的时序耦合 | 横幅依赖 deferred 机制保持在 task.start 之前；技能侧横幅不带 `_bubble_progress` 标记则时序保证失效（魔法键协商是纯约定，[[S_04]]） | test_resume_skip_completed（deferred 用例组） |

## 修改检查单

| 改动 | 必查 |
|---|---|
| 新增事件类型 | tool 层反向映射表（9 项）+ skip 集（4 类）+ 前端 handler 三处同步——映射错 = 静默丢弃，无告警 |
| 改终态帧 | 核对"暂停态禁 final"两层谁发终帧契约：executor 零帧 raise、adapter invocation_paused 收尾；complete 帧覆盖所有非 raise 终态路径 |
| 改异常分支 | 对照四分支 flush 义务（FallbackContract / FallbackLimit / AbortError / 普通异常）+ 唯一例外 CancelledError / KeyboardInterrupt 的去留 |
| 改缓冲 | 围栏保护三规则（粘连 / 拆分 / last_emitted）+ 首 chunk 立即发 + 五个 flush 时机 |
| 改 task 事件 | FIFO / OOB 归属（仅 task.update 可 OOB）+ 外层 todo 抑制 + deferred 顺序（横幅先行、complete 后置）+ resume 复用 task_id |
| 改交付时序 | 三方契约（P10 写 holder / tool 挂 ContextVar / rail priority=90 晚于 tool_result）+ HITL / 审批中断不发骨架分支 |
| 改 stream_source_id | 三层透传核对（引擎注入 / gateway / 上层前端分桶），任一环丢失即并发交错 |
| 改 OOB 推送 | send_push 送达数语义（0 ≠ 成功）+ 仅幂等全量快照可 OOB 的边界 |
| 改 usage 事件 | 失败轮不发送 + resume 改写 usage_metadata 口径 |
| 改 skip 集 / 映射表 | 逐项核对通道 A / B 全集表——skip 集只收 plan/node 生命周期 4 类；映射表键必须带 `chat.` 前缀 |
| 改 HITL 三件套 | 卡片 request_id 必须用外层 skill_acceleration_exec call id；invocation_paused 的 is_complete=True + awaiting_user_input=True 双字段不可拆 |

## 测试资产

| 测试文件 | 锁定行为 |
|---|---|
| `tests/unit_tests/server/runtime/skill_turbo/test_ppt_delivery_summary.py` | 交付总结三方时序（ContextVar / rail / startswith 校验） |
| `tests/unit_tests/agentserver/test_skill_turbo_tools.py` | tool 层转发 4 步：skip 集 / 映射表 / 剥 task_id / OOB |
| `tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py` | 暂停帧语义与恢复 |
| `tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py` | HITL 三件套时序与 resume 重放事件抑制 |

改事件流协议的任何路径，上述测试是最低回归集；新增事件类型 / 改顺序约束必须补对应用例后再动实现。

## 与其它 spec 的关系

| Spec | 关系 |
|---|---|
| [[S_01]] | 执行循环与退出路径全景 E1–E13 是本 spec 帧协议的宿主；本 spec 定义每条退出路径"发什么帧"，E 编号语义归 S_01 |
| [[S_06]] | HITL 状态机、resume_ctx、ask_user 卡片去重 / 序号策略；本 spec 只管三件套帧时序与暂停帧语义 |
| [[S_07]] | 降级决策（4 层决策、FallbackContractError 传播）；本 spec 承接其帧层机制——不发 chat.error、task flush、fallback 事件透传 |
| [[S_09]] | WS / gateway / ContextVar 外层集成细节：AgentWebSocketServer.send_push、gateway_normalize 的 E2A 帧转换、StreamEventRail 的 TIE 改写 |
| [[S_02]] | PromptRail"暂停不是失败"提示词——本 spec P7 暂停时序的行为侧配套 |
| [[S_05]] | 动态包与门面：DELIVERY_SUMMARY_START 标记常量经动态包引用，ppt 技能不在场时的短路行为 |
