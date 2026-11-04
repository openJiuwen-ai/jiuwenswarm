---
id: D-20261103-0410-client-tool-adapter
title: 客户端工具调用适配层（Client Tool Adapter）
slug: client-tool-adapter
status: draft
stage: proposals
reviewers: []
created: 2026-11-03
updated: 2026-11-03
version: 1
domain: swarm
modules: [jiuwenswarm/runtime, jiuwenswarm/server, gateway, jiuwenswarm/agents/harness/common/rails]
tags: [client-tool, tool-calling, interrupt, timeout, cancellation, async-tool]
related: []
author: chickeyton
type: D
files: []
author_email: ngton2014@gmail.com
---

# 客户端工具调用适配层（Client Tool Adapter）

## 1. 任务与结论

任务要求：

| # | 要求 |
|---|---|
| R1 | 客户端通过**统一接口**发起/承接工具调用并接收结果 |
| R2 | 工具执行等待期间，客户端**仍可与 Agent 正常交互** |
| R3 | 工具调用超时或被取消后，Agent **不会重新调用**该工具 |
| R4 | Agent **不会处理失败（超时/取消）调用的结果** |

**结论：这是一个真实的、尚未完成的任务。** 2026-11-02 合入的
`c9f2cc072 feat(client-tools): run tools in the connected client and resume with its result`
只搭好了“客户端执行工具”的底层管道（中断 → 请求事件 → 恢复），R1 只完成一部分，R2/R3/R4 与现状**直接冲突**。

## 2. 现状分析

### 2.1 已有链路（基于 agent-core 中断）

```
LLM tool_call(name=X)                     [agent-core c8c621b2]
  └─ AbilityManager: ToolCard(execution="client")
       └─ before_tool_call rails 全部通过 → _pause_client_tool_call
            └─ InterruptKind.CLIENT_TOOL 中断，整个 turn 挂起
JiuwenSwarm
  interface_deep.py   中断 payload → client_tool_request_event()
  stream / gateway    → chat.client_tool_request 事件 (web_connect 全量透传)
客户端执行后
  chat.send {source: "client_tool_interrupt",
             answers: [{interrupt_id, request_id, result}]}
  service.py          _control_ids / deliver_control 按 call id 路由到挂起的 execution
  interface.py        client_tool_answers() → InteractiveInput.answer(interrupt_id, result)
  permission_continuation.validate_client_tool_answers  拒绝错配（CLIENT_TOOL_ANSWER_MISMATCH）
agent-core
  _finish_client_tool_call → default_client_output(result) → ToolMessage → 继续 ReAct 循环
```

已具备的能力：

- `jiuwenswarm/runtime/client_tools.py`：请求事件格式、答复解析、错配异常。
- 并行调用、一次只答复部分调用（未答复的保持挂起，见 core `interrupt/handler.py` `_is_waiting_for_client`）。
- 与权限审批共存：deny 不下发客户端；ask 审批后只下发一次。
- 挂起状态可跨 Agent 空闲驱逐保留；cron 会话遇到该事件立即失败（`gateway/cron/scheduler.py`）。
- `AgentRuntime.pending_interactions()`（`service.py`）可列出挂起中的请求，供客户端重新绑定时补发。

### 2.2 缺口与冲突

| 要求 | 现状 | 证据 |
|---|---|---|
| R1 统一接口 | **部分**。只有“答复”入口，且混在 `chat.send` 里，靠 `source` 区分；没有工具注册入口，也没有取消入口。仓库中没有任何地方声明 `ToolCard(execution="client")`（只有测试里有），客户端没有途径把自己的工具告诉 Agent。Web 前端、TS/Python SDK、TUI 都没有处理 `chat.client_tool_request`。`pending_interactions()` 只有定义，没有被任何 bind 处理器调用。 | `grep "execution=\"client\""` 只命中注释与测试；`grep client_tool` 在 `channels/`、`sdks/`、`packages/` 下为空 |
| R2 等待期间可交互 | **冲突**。客户端工具基于中断，等待期间整个 turn 处于挂起状态。此时客户端发送的普通 `chat.send` 会被当作“新输入取代挂起”：`discard_permission_continuation` 给每个挂起调用写入 `[INTERRUPTED - Superseded by new user input] … was cancelled` 的 ToolMessage 并清掉中断状态。也就是说，用户只要一说话，工具调用就被取消；无法“工具在跑，人继续聊”。 | `permission_continuation.py` `_superseded_message`；测试 `test_a_new_user_turn_ends_what_the_pause_it_supersedes_waits_on` |
| R3 不重新调用 | **冲突**。core 把 `TIMEOUT / CLIENT_GONE / CANCELLED` 定义为 `RETRYABLE_CLIENT_TOOL_STATUSES`，给模型的文本是“…check the current state before calling it again”，`data.retryable=True`。被取代时 JiuwenSwarm 也复用这段文本。这等于**鼓励**模型重试。没有任何防重入的 rail。 | core `foundation/tool/client.py` `_failure_text`；测试 `test_a_call_whose_client_went_away_ends_with_a_retryable_error` 断言文本含 “call it again” |
| R3 超时 | **缺失**。宿主侧没有任何超时计时器；`TIMEOUT` 状态只能靠客户端自己上报。客户端不回包时 turn 会无限挂起。 | `jiuwenswarm/` 下无 `ClientToolStatus.TIMEOUT` 的产生方 |
| R4 不处理失败结果 | **冲突**。失败结果（包括 content 文本）原样变成 ToolMessage，然后 ReAct 循环**带着它再调一次 LLM**。纯文本恢复也会被 core 转为 `CANCELLED` 加 “The user sent a message instead: …”，模型同样会继续处理。迟到结果在挂起已被取代后会被 `CLIENT_TOOL_ANSWER_MISMATCH` 拒绝，这一点符合要求，但没有显式的“已终结”状态，只是因为挂起已消失而碰巧被拒。 | core `ability_manager.py` `_finish_client_tool_call` |

根因：现有设计把客户端工具当成**同步、阻塞 turn 的工具**，失败也当成普通工具错误交给模型自行决策。任务要求的是**异步、非阻塞的工具**，并且失败是**宿主侧终结**，模型看不到。

## 3. 设计目标与非目标

目标：

1. 客户端只用一套 `client_tool.*` 协议完成注册、接收请求、回传结果、取消、补发挂起请求，无需了解 interrupt / InteractiveInput。
2. 新增 **detached（异步）执行模式**：工具调用立即返回“已受理”，turn 结束，会话保持可交互；结果到达后作为一条新输入投递给 Agent。
3. 每个调用有宿主侧状态机，终结状态唯一（CAS 迁移）：超时、取消、客户端断开都是**终结失败**，其结果永不进入模型上下文，也不触发新的 LLM 轮次。
4. 防重入：Agent 不能再次发起已失败的调用；调用处于挂起状态时也不能重复发起同一调用。
5. 保留现有 blocking（中断）模式，并补上超时与非重试语义，使两种模式对客户端表现为同一接口。

非目标（第一期）：

- Team 模式与 swarm 成员内的客户端工具（与 `SessionMessageService` 一致，只支持 Work/Code Normal 会话）。
- 修改 agent-core 的语义；本设计不要求 core 改动（见 §8 可选的上游改进）。
- 客户端工具的沙箱或安全隔离：工具由客户端执行，安全边界在客户端。

## 4. 总体架构

```
            ┌──────────────── client (web / SDK / TUI / Office task pane) ───────────────┐
            │ client_tool.register   client_tool.result   client_tool.cancel   chat.send │
            └───────▲───────────────────────────┬─────────────────────────────────────┘
   chat.client_tool_request / chat.client_tool_settled        │
            │                                   ▼
 gateway ── 透传 (web_connect 全量事件、message_handler 路由新 ReqMethod)
            │
 agent server ── ClientToolAdapter (jiuwenswarm/runtime/client_tool_adapter.py)  ◀── 新增
            │     ├─ ClientToolRegistry   会话级工具声明 → 生成 Tool / ToolCard
            │     ├─ ClientToolLedger     调用状态机，持久化在会话状态中
            │     ├─ DeadlineScheduler    超时 / 断开宽限计时
            │     └─ ResultDelivery       完成结果 → SessionMessageService 投递新 turn
            │
 agent ── ClientToolGuardRail (rails/client_tool_guard_rail.py)                  ◀── 新增
                  ├─ before_tool_call：拦截重复的挂起调用与已失败调用的重入
                  └─ before_model_call：注入挂起/失败调用摘要（system reminder）
```

## 5. 详细设计

### 5.1 统一线协议

新增 ReqMethod 与 EventType 统一走 `common/schema/message.py` 的 `_ensure_req_method` / `_ensure_protocol_member`，并同步提交到 `openjiuwen-protocol`。

**客户端 → 服务端**

| 方法 | 参数 | 说明 |
|---|---|---|
| `client_tool.register` | `session_id`, `client_id`, `tools: [{name, description, input_schema, mode?: "detached"\|"blocking", timeout_s?, idempotent?}]`, `replace?: bool` | 声明本客户端可执行的工具。重复注册时按 `name` 覆盖。也允许通过 `chat.send.params.client_tools` 携带同样的数组（首轮便捷用法）。 |
| `client_tool.result` | `session_id`, `call_id`, `result: ClientToolResult` | 回传结果。`result` 沿用 core 的 `{status, content, is_error, data}` 信封。 |
| `client_tool.cancel` | `session_id`, `call_id`, `reason?` | 客户端或用户主动取消。 |
| `client_tool.list_pending` | `session_id` | 返回本会话所有 `PENDING` 调用（重新绑定时也会自动推送）。 |

**服务端 → 客户端事件**

| 事件 | 关键字段 |
|---|---|
| `chat.client_tool_dispatch`（新增，detached） | `call_id`、`tool_name`、`agent_tool_name`、`arguments`、`mode="detached"`、`deadline`（epoch ms）、`target_client_id` |
| `chat.client_tool_request`（沿用，blocking） | 现有字段不变：`interrupt_id`、`request_id`、`source`，按 `chat.send` 中断恢复方式答复 |
| `chat.client_tool_settled`（新增） | `call_id`、`status`（completed / error / timeout / cancelled / client_gone）、`reason`、`accepted: bool`（宿主是否接受了结果） |
| `chat.client_tool_delivery`（新增） | `call_id`、`delivered: bool`、`reason`：被接受的结果是否真正交给了 Agent |

> 2026-11-03 检视后修订：detached 请求不再复用 `chat.client_tool_request`。已有的 blocking 客户端收到该事件会按中断方式答复，进而被 `CLIENT_TOOL_ANSWER_MISMATCH` 拒绝，所以 detached 改用独立事件名 `chat.client_tool_dispatch`。`accepted` 只表示宿主接受了结果；结果是否交给了 Agent，由 `chat.client_tool_delivery` 单独告知。

**错误码**：`CLIENT_TOOL_BAD_REQUEST`（含非法 `session_id`）、`CLIENT_TOOL_SESSION_NOT_FOUND`（会话不存在或不属于请求者，两种情况不作区分）、`CLIENT_TOOL_UNKNOWN_TOOL`、`CLIENT_TOOL_UNKNOWN_CALL`、`CLIENT_TOOL_CALL_SETTLED`（迟到结果或重复结果）、`CLIENT_TOOL_NOT_OWNER`（非目标客户端回包）、`CLIENT_TOOL_ANSWER_MISMATCH`（沿用）。

**鉴权**：所有 `client_tool.*` 请求先用共享规则（`gateway/common/protocol_ids.py`）校验 `session_id`，再按跨会话消息的归属规则校验请求者是否拥有该会话：带用户的请求只能操作该用户的会话，匿名请求只能操作无主会话。

blocking 模式下，适配层把 `client_tool.result` 内部转换为现有的
`chat.send{source: client_tool_interrupt, answers:[…]}` 并走 `deliver_control`。现有路径保持不变，客户端也不再需要拼这个格式。

### 5.2 工具注册与暴露

- `ClientToolRegistry` 以 `session_id` 为键，保存 `ClientToolSpec`（含 `owner client_id`）。注册即持久化到会话 metadata（`metadata.json` 中 `client_tools` 字段），使会话重载或服务重启后工具定义仍在；`owner` 断开后保留定义，但调用会立即进入 `CLIENT_GONE`（见 5.4）。
- 构建 Agent 工具集时（`interface_deep.py` 中挂载 tools 的位置），按 spec 生成：
  - `mode="blocking"` → `ClientTool(ToolCard(execution="client"))`，即现有中断路径；
  - `mode="detached"`（默认）→ `DetachedClientTool(Tool)`。这是**普通服务端工具**，`execution` 不设为 client，所以 core 不会为它挂起 turn。
- 名称冲突：客户端工具名统一加前缀 `client__<name>`，防止覆盖内置工具。权限规则按带前缀的名字匹配，`before_tool_call` 中的 permission rail 照常生效（现有测试覆盖的 deny/ask 语义保持）。
- cron / 无绑定客户端的会话：不暴露客户端工具。

### 5.3 detached 模式调用流程（满足 R2）

```
1. LLM 调用 client__X(args)
2. ClientToolGuardRail.before_tool_call  → 去重 / 防重入检查（5.5）
3. permission rails                       → allow / ask / deny（现有）
4. DetachedClientTool.invoke:
     call_id = tool_call.id
     ledger.open(call_id, tool, args_fingerprint, deadline, owner)   # PENDING
     emit chat.client_tool_request{mode:"detached", deadline, …}
     return ToolOutput(success=True, data={"content":
        "Client tool X accepted (call_id=…). It runs on the user's client; its result
         will arrive later as a separate message. Do not call it again; continue
         with other work or reply to the user."})
5. ReAct 循环拿到这条 ack，turn 正常结束（chat.final），会话回到空闲
6. 用户可以随时发 chat.send，正常开新 turn。GuardRail 每轮注入
   "<pending_client_tools>…</pending_client_tools>" 提示
7a. client_tool.result 到达 → ledger.settle(call_id, COMPLETED|ERROR)   # CAS
      成功 → emit settled{accepted:true}
           → ResultDelivery 向 SessionMessageService 入队一条
             kind=client_tool_result 的内部消息（会话忙则排队到当前 turn 结束后，
             不在 turn 中途注入，见 §9 Q4）
           → Agent 以新 turn 处理："<client_tool_result call_id=… tool=X>…content…</…>"
             图片走 data.multimodal，沿用 default_client_output 的拆分逻辑
7b. 超时 / 取消 / 断开 → ledger.settle(call_id, TIMEOUT|CANCELLED|CLIENT_GONE)
      → emit settled，客户端据此中止执行
      → 不入队、不唤醒 Agent、不调用 LLM（R4）
```

要点：

- LLM 的 tool_call/tool_result 配对在第 4 步就已闭合，所以后续用户输入不会碰到“上一条 assistant 消息有未答复 tool_call”的协议约束。这正是现有中断模式只能“取代”而不能“并行”的原因。
- 结果投递复用 `SessionMessageService` 已有的持久化、去重与异步执行能力（现在用于跨 Session 消息）。新增 `kind=client_tool_result` 并以 `call_id` 作为幂等键，重复投递不会产生两个 turn。
- 多个 detached 调用的结果分别投递；相邻到达的结果可在入队时合并为一个 turn（同一 `SessionMessageService` 批次），减少 LLM 轮次。

### 5.4 调用状态机与超时/取消（满足 R3/R4）

```
            open
             │
             ▼
          PENDING ──result(completed)──▶ COMPLETED   ─┐
             │    ──result(is_error)───▶ ERROR       ─┤ 投递给 Agent（ERROR 作为工具错误投递，见 §9 Q2）
             │    ──deadline──────────▶ TIMED_OUT    ─┐
             │    ──client_tool.cancel▶ CANCELLED    ─┤ 终结失败：不投递、不唤醒、
             │    ──session stop/close▶ CANCELLED    ─┤ 迟到结果返回 CLIENT_TOOL_CALL_SETTLED
             │    ──owner 断开超过宽限期▶ CLIENT_GONE ─┘
```

- **唯一终结**：`ledger.settle()` 以 `(session_id, call_id)` 加锁做 compare-and-set，只允许从 `PENDING` 迁出。先到者胜出，后到的结果/取消一律返回 `CLIENT_TOOL_CALL_SETTLED`，并记 `late_result_dropped` 审计事件。结果内容只写日志摘要，不进入上下文。
- **持久化**：ledger 存在会话级状态中（建议放在 `get_agent_sessions_dir()/<sid>/client_tool_calls.json`，所有路径走 `common/utils.py` 的取值函数），记录 `deadline` 等绝对时间。服务重启后 `DeadlineScheduler` 按 deadline 重新挂计时器，已过期的立即判为 `TIMED_OUT`。
- **超时**：默认 `timeout_s` 取工具声明值，否则取配置项 `client_tools.default_timeout_s`（建议 120s，模板放 `resources/`）。计时器用 `asyncio` 定时任务实现，跨平台。
- **断开**：客户端 WS 断开后进入宽限期（建议 30s，可配置）。宽限期内同一 `client_id` 重连并 bind，会通过 `list_pending` / 自动推送拿回请求继续执行；超时则判 `CLIENT_GONE`。
- **会话 stop / pause / delete**：接入 `RuntimeSessionCoordinator` 的取消钩子，把该会话所有 `PENDING` 调用判为 `CANCELLED` 并推送 settled。
- **history.jsonl**：新增 `client_tool.opened` / `client_tool.settled` 事件，便于按 AGENTS.md 的运行时数据位置排查。

### 5.5 防重入与上下文提示（满足 R3）

`ClientToolGuardRail`（`jiuwenswarm/agents/harness/common/rails/client_tool_guard_rail.py`）：

- 指纹 = `tool_name + sha256(canonical_json(arguments))`。
- `before_tool_call`：
  - 同指纹存在 `PENDING` 调用 → 拒绝本次调用，返回 `"Already running as call_id=…; wait for its result."`，不再下发客户端。
  - 同指纹存在 `TIMED_OUT/CANCELLED/CLIENT_GONE` 调用，且此后**没有新的用户发起输入** → 拒绝，返回固定文案 `"This client tool call was <status> and will not be retried automatically."`。文案里不带任何客户端内容。
  - 用户之后的新输入（`chat.send` 普通 query，不含结果投递这类内部消息）会重置“允许重试”窗口：用户明确再次要求时可以重新调用。不采用更严格的范围（见 §9 Q3）。
- `before_model_call`：注入简短的 `pending_client_tools` 摘要（挂起中的调用；最近失败且不得自动重试的调用），只带工具名、call_id 和状态，不带结果内容。

### 5.6 blocking 模式的补齐（与现有中断路径兼容）

现有中断路径保留，供必须同步拿到结果的工具使用（如“读当前选区再改写”）。补齐以下几点，使它同样满足 R3/R4：

1. **宿主超时**：中断挂起时由 `ClientToolAdapter` 开账（同一 ledger，`mode=blocking`）。到期后**不走** `InteractiveInput` 恢复（那样会把失败结果交给模型再调 LLM），而是复用 `discard_permission_continuation` 的机制：为挂起调用写一条固定的终结 ToolMessage（满足配对，内容为 `[CLIENT TOOL <status> — not retried]`），清除 `INTERRUPTION_KEY`，释放 execution，并发出 `chat.client_tool_settled` 和 turn 结束事件。**不触发 LLM**。
2. **取代文案**：`permission_continuation._superseded_message` 里客户端工具的取消文本改为不可重试的版本（不再拼 core 的 “check the current state before calling it again”），并在 ledger 中标为 `CANCELLED`，以便 GuardRail 生效。
3. **客户端上报的失败状态**：`client_tool.result` 携带 `status ∈ {timeout, cancelled, client_gone}` 时，按第 1 点的终结处理，不调用 `InteractiveInput.answer`。
4. **bind 补发**：在会话 bind 处理器中调用已有的 `AgentRuntime.pending_interactions()`，补发 blocking 请求；detached 请求由 ledger 补发。两者合并后统一经 `list_pending` 返回。

blocking 模式下 R2 仍受限：等待期间用户发送消息会取代该调用。这是模式本身的语义，文档与工具声明中需要写明，推荐默认使用 detached。

### 5.7 多客户端与路由

- 请求只推给 `owner client_id`（注册该工具的客户端），事件带 `target_client_id`，其他绑定同一会话的客户端可以展示进度但不能回包（`CLIENT_TOOL_NOT_OWNER`）。
- `owner` 掉线并超过宽限期，同会话另一个注册了同名工具的客户端**不会**自动接管已发出的调用（避免重复执行副作用），只接管之后的新调用。

## 6. 代码落点

| 文件 | 变更 |
|---|---|
| `jiuwenswarm/runtime/client_tools.py` | 扩展事件构造（`mode`/`deadline`/`call_id`），新增 settled 事件构造与错误码 |
| `jiuwenswarm/runtime/client_tool_adapter.py`（新） | `ClientToolAdapter`、`ClientToolRegistry`、`ClientToolLedger`、`DeadlineScheduler`、`ResultDelivery` |
| `jiuwenswarm/runtime/client_tool_tools.py`（新） | `DetachedClientTool`，以及按 spec 生成 blocking `ClientTool` |
| `jiuwenswarm/agents/harness/common/rails/client_tool_guard_rail.py`（新） | 去重、防重入与上下文提示 |
| `jiuwenswarm/common/schema/message.py` | 注册 `client_tool.*` ReqMethod 与 `chat.client_tool_settled` EventType |
| `jiuwenswarm/server/agent_ws_server.py` / `server/dispatch.py` | 新方法的处理器、bind 时补发、断开时启动宽限计时 |
| `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py` | 构建工具集时挂载客户端工具与 GuardRail |
| `jiuwenswarm/server/runtime/agent_adapter/permission_continuation.py` | 取代文案改为不可重试；blocking 超时终结复用 discard 逻辑 |
| `jiuwenswarm/server/runtime/session/session_message_service.py` | 新增 `kind=client_tool_result` 并按 call_id 幂等 |
| `gateway/channel_manager/web/web_connect.py`、`gateway/message_handler/message_handler.py`、`app_web_handlers.py` | 透传新事件、路由新方法 |
| `jiuwenswarm/resources/` 配置模板 | `client_tools.default_timeout_s`、`client_tools.disconnect_grace_s` |
| `sdks/`（Python/TS） | `ClientToolHost` 帮助类：register / onRequest / respond / cancel，内置 deadline 自取消 |
| `.agents/agent-docs/specs/` | 落地后新增 S 文档，记录协议契约与状态机不变量 |

## 7. 测试计划

单元测试（pytest，纯函数风格，放在 `tests/unit_tests/runtime/` 与 `tests/unit_tests/agentserver/`）：

- ledger：CAS 只允许一次终结；结果与超时竞态时先到者胜；迟到结果返回 `CLIENT_TOOL_CALL_SETTLED` 且不入队；重启后按 deadline 恢复计时。
- detached：调用立即返回 ack 并结束 turn；等待期间普通 `chat.send` 正常完成一个 turn，挂起调用不受影响（R2）；完成结果恰好产生一个新 turn（幂等）；超时/取消/断开后**零次** LLM 调用（R4，用 fake model 计数）。
- GuardRail：挂起中同指纹被拒；失败后同指纹被拒（R3）；新的用户输入之后允许再次调用；拒绝文案不含客户端内容。
- blocking：宿主超时结束 turn 且不调用 LLM；取代文案不再含 “call it again”；客户端上报 `status=timeout` 按终结处理。需要同步更新现有断言 `test_a_call_whose_client_went_away_ends_with_a_retryable_error`。
- 协议：`client_tool.result` 在 blocking 模式下等价于现有 `chat.send{source: client_tool_interrupt}`；非 owner 回包被拒；bind 时同时补发 blocking 与 detached 请求。
- 回归：现有 `test_client_tool_*`、`test_session_message_pause.py` 全部通过；cron 会话不暴露客户端工具。

集成测试：用 WS 客户端模拟器跑完整链路（register → request → 用户插话 → result → 新 turn），覆盖超时与断线重连两条路径。

## 8. 分期

1. **P1**：协议与 ledger、detached 模式、超时/取消/断开、GuardRail、`SessionMessageService` 投递；Python SDK。
2. **P2**：blocking 模式补齐（宿主超时终结、非重试文案、bind 补发、`client_tool.result` 统一入口）；TS SDK 与 Web 前端接入。
3. **P3（可选）**：向 agent-core 提议 `ClientToolStatus` 的重试策略可由宿主覆盖（如 `ClientTool.retry_policy`），或提供“终结而不继续”的恢复选项，以便删除 JiuwenSwarm 侧的 discard 绕行；Team 模式支持。

## 9. 已确认决策（2026-11-03）

| # | 问题 | 决策 |
|---|---|---|
| Q1 | 任务解读 | **确认**：任务指“Agent 发起、由客户端执行的工具”（与 `c9f2cc072` 方向一致）。“客户端直接调用服务端工具”（类似 `mcp_app.call_tool`）不在本设计范围内。 |
| Q2 | 客户端报告的执行错误（`is_error=true` / `status=error`）是否投递 | **确认投递**：`ERROR` 是客户端真实执行后的结果，作为工具错误投递给模型。只有 `TIMED_OUT / CANCELLED / CLIENT_GONE` 属于终结失败，不投递。 |
| Q3 | 是否需要更严格的防重入范围 | **不需要**：保持 §5.5 的“工具名 + 参数”指纹规则，用户发出新输入后放开。 |
| Q4 | 会话正在运行另一 turn 时结果何时投递 | **排队到该 turn 结束后**再作为新 turn 投递；不在 turn 中途注入。 |
