# AgentServer WebSocket 接口

其他项目直连 AgentServer 时，使用 WebSocket，帧内容为 E2A。信封公共字段以 [E2A-protocol.md](E2A-protocol.md) 为准。本文按功能写每个 `method` 的用途、`params` 和 `body.result`。字段从 handler 源码整理。冲突时以源码为准，并回头修正本文。

除特别声明外，接口都是一元调用：`is_stream` 为 `false`，成功时读 `e2a.complete` 的 `body.result`，失败时读 `e2a.error` 的 `body`。

| 章 | 内容 |
|----|------|
| 1–2 | 连接，以及任意一帧怎么读 |
| 3 | 对话。`chat.send` 的请求和事件在同一节；向用户提问与中断单独成节 |
| 4 | 配置更新 |
| 5 | 会话与历史 |
| 6 | 命令 |
| 7 | 智能体 |
| 8 | 技能 |
| 9 | 团队 |
| 10 | 权限 |
| 11 | 调度 |
| 12 | MCP、沙箱、扩展、文件传输 |
| 13 | 插件、运维、长程任务 |
| 14 | HTTP 入口 |

后文章节里，每个方法都是先写请求 `params`，紧接着写这一次调用的响应。流式接口的响应是事件表，一元接口的响应是 `body.result` 字段表。

`config.get`、`config.set`、`channel.*`、`heartbeat.*`、`updater.*` 在 Gateway 本地处理，不进入 AgentServer 的分发表。直连 AgentServer 时不要发这些 `method`。配置变更的主流程是 `sync_agents_configs`，见第 4 章。`agent.reload_config` 只给 JiuwenSwarm 个人版热更新用，后续可能弃用。

实现入口：`jiuwenswarm/server/agent_ws_server.py`、`jiuwenswarm/server/dispatch.py`、`jiuwenswarm/server/handlers/`。

---

## 1. 连接

| 项 | 值 |
|----|----|
| 地址 | `ws://{host}:{port}`，无路径 |
| 默认 | `ws://127.0.0.1:18092` |
| 主机 | `AGENT_SERVER_HOST`，缺省 `127.0.0.1` |
| 端口 | 启动参数 `--port`，否则 `AGENT_SERVER_PORT` 或 `AGENT_PORT`，缺省 `18092` |
| 保活 | 服务端 WebSocket ping 间隔 30 秒，超时 300 秒 |
| 入站单帧上限 | 8 MiB |
| 出站单帧预算 | 6 MiB；超出时改为错误帧，`code` 为 `response_too_large` |

握手默认不校验 `Origin`。仅当 `JIUWENSWARM_ENABLE_ORIGIN_CHECK=1` 时校验；此时无 `Origin` 的客户端只有在 `JIUWENSWARM_WS_ALLOWED_ORIGIN_HOSTS` 包含 `none` 时才能连上。企业版打开该开关后仍放行。

连接建立后，服务端发送的第一帧不是 E2A：

```json
{"type": "event", "event": "connection.ack", "payload": {"status": "ready"}}
```

收到该帧后再发业务请求。同一条连接上可以并行多个请求，用帧里的 `request_id` 归并响应。同一个 `request_id` 在该请求结束前不要复用。中断、续答都是新的 `request_id`，挂在同一个 `session_id` 上。

---

## 2. 帧

客户端每发一帧，正文是一个 `E2AEnvelope`。字段见 [E2A-protocol.md §4](E2A-protocol.md#4-e2aenvelope-字段表)。对接至少带上 `request_id`、`channel`、`method`、`params`、`is_stream`；已有会话再带 `session_id`。

服务端除首帧 `connection.ack` 外，每帧都是 `E2AResponse`。公共字段见 [E2A-protocol.md §12](E2A-protocol.md#12-e2a-响应协议e2aresponse)。业务内容在 `body` 里，不在帧顶的 `type` / `event`。`chat.send` 推回哪些事件，和请求写在第 3 章。

### 2.1 一元响应

`is_stream` 为 `false` 时回一帧。

| `response_kind` | `status` | `is_final` | `body` |
|-----------------|----------|------------|--------|
| `e2a.complete` | `succeeded` | `true` | `{ "result": <业务对象> }` |
| `e2a.error` | `failed` | `true` | `{ "code", "message", "details" }` |

`chat.interrupt`、`chat.user_answer`、`session.create` 都走这一帧。成功时读 `body.result`。

### 2.2 流式响应

`is_stream` 为 `true` 时，同一 `request_id` 下 `sequence` 从 0 递增。中间帧与结束帧种类不同。

| 情形 | `response_kind` | `status` | `is_final` | 怎么读 |
|------|-----------------|----------|------------|--------|
| 文本增量 `chat.delta` | `e2a.chunk` | `in_progress` | `false` | `body.delta` 是字符串；`body.delta_kind` 为 `text` 或 `reasoning` |
| 其他事件 | `e2a.chunk` | `in_progress` | `false` | `body.delta` 是事件对象，`body.event_type` 与对象里的 `event_type` 相同 |
| 正常结束 | `e2a.complete` | `succeeded` | `true` | `body.result` 常为 `{}`。`chat.final` 在这之前的中间帧里 |
| 流内失败 | `e2a.error` | `failed` | `true` | `body.details` 为原错误对象，其中 `event_type` 为 `chat.error` |
| 等待用户 | `e2a.chunk` | `in_progress` | `false` | `body.event_type` 为 `chat.invocation_paused`，`body.awaiting_user_input` 为 `true` |

等待用户的那一帧 `is_final` 仍是 `false`，但本次 `chat.send` 的流在这里结束，后面不会再有 `e2a.complete`。调用方应把它当作本请求的流结束，并且任务未完成。续跑要另发一条新的 `chat.send`。请求、事件和续答都在第 3 章。

`chat.delta` 的推理增量：`body.delta_kind` 为 `reasoning`（对应载荷里的 `source_chunk_type = llm_reasoning`）。普通文本为 `text`。团队成员增量可能另带 `body.role`、`body.member_name`、`body.stream_source_id`。

### 2.3 从帧里取出事件

```text
metadata._jiuwenswarm_server_push == true  → 服务端推送，不归入在途请求
response_kind == e2a.chunk 且 event_type == chat.delta
    → 文本 = body.delta
response_kind == e2a.chunk 且 event_type 为其他
    → 事件对象 = body.delta（dict）
response_kind == e2a.complete
    → 事件对象 = body.result
response_kind == e2a.error
    → 事件对象 = body.details
```

推送帧同样是 `E2AResponse`，`metadata._jiuwenswarm_server_push` 为 `true`（常量 `E2A_WIRE_SERVER_PUSH_KEY`）。`response_kind` 为 `acp.output_request` 的反向调用只发给握手时带了 `X-Jiuwen-Push-Consumer: gateway` 的连接。

入站不是合法 JSON 时，服务端回一帧失败的 `E2AResponse`，不关闭连接。

---

## 3. 对话

一次用户消息对应一条 `chat.send`。这一节同时写清两件事：客户端发出去的请求，以及这条请求推回来的事件。事件不是另一套接口。

中断、把答案交回去、在执行中追加一句，都还是这条对话上的后续请求，写在本节后半。

### 3.1 请求：`chat.send`

`is_stream` 置 `true`。同一条连接上用信封的 `request_id` 把后面的事件收成一轮。这个 `request_id` 在本轮结束前不要复用。

```json
{
  "protocol_version": "1.0",
  "request_id": "req_1",
  "session_id": "sess_1",
  "channel": "web",
  "method": "chat.send",
  "is_stream": true,
  "params": {
    "query": "你好",
    "mode": "agent"
  }
}
```

字段来自 `jiuwenswarm/common/schema/chat_send.py` 的 `ChatSendParams`，以及 `JiuWenSwarmAgentServer._build_inputs` 实际读取的键。除下表标明必填的情况外，都可以省略。

**正文与模式**

| 字段 | 说明 |
|------|------|
| `query` | 用户文本。非空时优先于 `content`。`query` 缺失或为空串时改读 `content` |
| `content` | 用户文本。`query` 为空时使用。斜杠技能标记保留在正文里 |
| `mode` | `agent`、`code.normal`、`code.plan`、`code.team`、`team`、`team.plan`。历史值 `plan`、`fast`、`agent.plan`、`agent.fast` 归一为 `agent` |
| `team` | 布尔。为真时按团队处理，不从正文剥离 `/debug` |
| `skills` | 字符串数组。非空时作为本轮指定技能，正文不再按 `/skills use` 解析技能名 |
| `session_id` | 也可只放在信封顶层。进入执行的会话 id 取信封上的 `session_id` |
| `system_prompt` | 非空字符串时作为本请求的系统提示，只作用于这一轮 |
| `interactive_ask` | 布尔，也接受 `interactiveAsk`。显式打开引导模式。缺省为关，不从 `supports_user_interaction` 推断 |
| `supports_user_interaction` | 布尔。缺省为真。显式 `false` 时关闭向用户提问 |
| `supplementary_info` | 非空字符串。写入用户消息 JSON 的补充说明，不替代正文 |

**模型、目录**

| 字段 | 说明 |
|------|------|
| `model_ref` | 模型身份。有值时按它解析模型，并要求解析结果的模型名与 `model_name` 一致，否则失败，信息为 `model_name does not match model_ref` |
| `model_name` | 无 `model_ref` 时按名称或 `{model_name}#{index}` 选模型。对不上则用默认模型 |
| `project_dir` | 项目根目录。也可放在请求 `metadata.project_dir`，`params` 优先 |
| `cwd` | 工作目录。也可放在 `metadata.cwd`，`params` 优先 |
| `workspace_dir` | 本请求的工作根。会展开、创建目录，成功后同时覆盖 `cwd` 和工具沙箱的工作区。创建失败则退回 `cwd` |
| `trusted_dirs` | 字符串数组。显式可信目录，会进入用户消息。未传且工作区读权限为 allow 时，服务端只把 `project_dir` 注入权限轨，不写入用户消息 |
| `plan_entry_source` | `slash_command` 或 `e2a` 时视为显式进入计划模式 |

**附件**

| 字段 | 说明 |
|------|------|
| `files` | 对象。传统的用户文件更新，放进用户消息的 `files_updated_by_user`。其中 `uploaded_images` 为数组时，和 `media_items` 一起作为多模态图片 |
| `attachments` | 数组。`@file` 等附件。结构由调用方传入，Agent 侧契约保留该字段 |
| `media_items` | 数组。每项为对象，至少有 `path` 或 `url`。可选 `type`、`filename` 或 `name`、`mime_type` 或 `mimeType`、大小。写入历史，并参与多模态图片 |

图片对象需要本地 `path` 才会送进模型。同一 `path` 只保留一次。

**续答**

普通发消息不要带这些字段。字段含义和 `answers` 元素见 3.4.2。

| 字段 | 说明 |
|------|------|
| `request_id` | 提问卡片上的工具调用 id，不是信封 `request_id` |
| `source` | 原样回传卡片上的 `source` |
| `answers` | 答案数组。与「`source` 为 `ask_user_interrupt` 且带了 `status`」二者有其一即按续答处理 |
| `status` | `ask_user_interrupt` 的显式状态。没有 `answers` 时，带了它仍算一次回答 |
| `original_request` | 仅 `ask_user_interrupt` 时读取，回传原问题 |
| `plan_approval_kind` | `team.plan` 且 `source` 为 `confirm_interrupt` 时必须和 `plan_content`、`plan_language` 一起成套，否则整请求失败 |
| `plan_content` | 计划正文 |
| `plan_language` | `cn` 或 `en` |
| `approval_schema` | 演进审批的 schema |
| `evolution_meta` | 对象。演进审批元数据。`approval_transport` 为 `interrupt` 时按中断续答处理 |
| `activate_response` | 对象 `{interaction_id, action, feedback}`。有则走 Harness 激活恢复，不再按普通对话调用模型 |
| `is_supplement` | 为真时，写入历史的用户文本改用 `supplement_input` |
| `supplement_input` | 补充请求的原始用户输入 |
| `_plan_reminder_original_query` | 进入计划模式那一轮的用户原文，用于历史展示，避免把拼进正文的系统提醒记成用户提问 |

**运行上下文与历史**

| 字段 | 说明 |
|------|------|
| `run` | 对象。已有运行上下文时原样传入。之后若有 `cron` 会被覆盖 |
| `cron` | 对象。有则把 `run` 设为 `{kind: "cron", context: {extra: {cron}}}` |
| `extension_config` | 仅企业版。也可放在 `metadata`。写入 `run.context.extra`，供 Rails 使用 |
| `log_as_user` | 显式 `false` 时本轮不记入用户历史 |
| `attach_goal` | 显式 `true` 时视为目标挂载，不记入用户历史 |
| `context_size`、`context_window_size`、`context_window`、`max_context`、`max_context_size`、`max_context_message_num`、`max_input_tokens`、`max_prompt_tokens`、`n_ctx`、`ctx_len` | 有值则原样透传到本轮 inputs，作上下文长度提示 |

### 3.2 这一轮怎么结束

同一 `request_id` 下 `sequence` 从 0 递增。中间帧是 `e2a.chunk`，结束方式只有下面三种。

| 结局 | 最后一帧 | 调用方怎么处理 |
|------|----------|----------------|
| 正常完成 | 先收到 `chat.final`（`is_final` 为 `false`），再收到 `e2a.complete`，`is_final` 为 `true`，`body.result` 多为 `{}` | 以 `is_final: true` 停止收流。展示正文用此前的 `chat.delta`，`chat.final` 的 `content` 经常是空串 |
| 执行失败 | `e2a.error`，`body.details` 是 `chat.error` 对象，`body.message` 来自该对象的 `error` | 本轮结束，展示错误 |
| 等人 | `e2a.chunk`，`body.event_type` 为 `chat.invocation_paused`，`body.awaiting_user_input` 为 `true`。`is_final` 仍是 `false`，后面不会再有 `e2a.complete` | 本轮流结束，任务未完成。用新的 `chat.send` 把答案送回去，见 3.4.2 |

### 3.3 `chat.send` 会推回的事件

除 `chat.delta` 外，事件对象在 `e2a.chunk` 的 `body.delta` 里，`body.event_type` 与对象里的 `event_type` 相同，`body.delta_kind` 为 `custom`。Deep 主路径把 `chat.final` 也放在这种中间帧上，`is_final` 为 `false`。请求结束是其后单独的 `e2a.complete`。

`chat.delta` 特殊：文本在 `body.delta` 字符串里，不在事件对象的 `content` 里。

| `event_type` | 帧 | 含义 | 字段 |
|--------------|----|------|------|
| `chat.delta` | 中间帧 `e2a.chunk` | 模型输出增量 | `body.delta` 为文本。`body.delta_kind` 为 `text`，或在 `source_chunk_type` 为 `llm_reasoning` 时为 `reasoning`。团队或并发子流可带 `role`、`member_name`、`stream_source_id`。`goal_intermediate` 为 `true` 时只是目标模式的中间收束 |
| `chat.reasoning` | 中间帧 | 推理文本 | 事件对象的 `content` |
| `chat.final` | 中间帧，`is_final` 为 `false` | 当前这段可见回复收束，请求流还可以继续 | `content`。正文已由 `chat.delta` 送过时这里是空串。用户轮切到目标轮时也会插一帧空的 `chat.final`，用来拆开展示气泡。目标仍在执行时，这一帧会被改成 `chat.delta` 且 `goal_intermediate: true` |
| `chat.error` | 结束帧 `e2a.error` 的 `body.details` | 本轮失败 | `error`。可有 `code`、`recoverable` |
| `chat.processing_status` | 中间帧 | 仍在处理，用于保活 | `is_processing`、`current_task`（思考中为 `thinking`） |
| `chat.tool_call` | 中间帧 | 发起工具调用 | `tool_call` |
| `chat.tool_calls.delta` | 中间帧 | 流式拼装工具调用参数 | 事件对象原样放在 `body.delta` |
| `chat.tool_update` | 中间帧 | 工具状态变化 | 更新对象展开到事件上，常见 `tool_call_id`、`status` |
| `chat.tool_result` | 中间帧 | 工具返回 | `tool_name`、`tool_call_id`、`result`。有则带 `raw_output`、`status`、`success`、`is_error`、`error`、`summary` |
| `task.start` | 中间帧 | 子任务开始 | `task_id`、`task_content`、`task_index`、`total_tasks`、`parent_request_id`、`timestamp` |
| `task.update` | 中间帧 | 子任务进度 | `tasks`、`total_tasks`、`completed_tasks`、`in_progress_tasks`、`pending_tasks`、`parent_request_id`、`timestamp` |
| `task.complete` | 中间帧 | 子任务结束 | `task_id`、`task_content`、`status`、`duration_ms`、`error`、`timestamp` |
| `todo.updated` | 中间帧 | 待办列表变化 | `todos` |
| `context.usage` | 中间帧 | 上下文占用 | `rate`、`context_max`、`tokens_used`。可有 `role`、`member_name` |
| `context.compression_state` | 中间帧 | 上下文压缩过程 | `status`、`phase`、`processor`、`summary`、`operation_id` |
| `chat.usage_summary` | 中间帧 | 本轮 token 汇总。`total_tokens` 不大于 0 时不发 | `session_id`、`model`、`usage`（`input_tokens`、`output_tokens`、`total_tokens`，有缓存或费用时再带 `cache_tokens`、`cache_hit_rate`、`input_cost`、`output_cost`、`total_cost`）。可有 `usage_percent`、`context_window_tokens` |
| `chat.usage_metadata` | 中间帧 | 用量元数据 | `metadata` |
| `chat.retract` | 中间帧 | 撤回已展示内容 | 载荷原样展开 |
| `chat.file` | 中间帧或推送 | 交给用户的文件 | `files[]`：`path`、`name`、`mime_type` |
| `chat.media` | 中间帧 | 媒体 | 与 `chat.file` 走同一条展示路径，事件对象在 `body.delta` |
| `chat.ask_user_question` | 中间帧，出现在暂停之前 | 向用户提问或要确认 | 见 3.4.1 |
| `chat.invocation_paused` | 本轮最后一帧，流在此结束 | 任务停在等人 | `body.awaiting_user_input` 为 `true`。`is_final` 为 `false` |
| `chat.subtask_update` | 中间帧 | 子任务状态 | `task_id`、`description`、`status`（`starting` / `tool_call` / `tool_result` / `completed` / `error`）、`index`、`total` |
| `goal.snapshot` / `goal.updated` | 中间帧 | 目标状态 | 事件对象在 `body.delta` |
| `team.*` | 中间帧 | 团队运行。以 `team.` 开头的类型原样透传 | 常见 `type`、`member_id`、`new_status`、`content`、`tool_call`、`tool_result` |
| `chat.evolution_status` | 中间帧 | 技能演进阶段 | `status` 为 `start` 或 `end` |
| `chat.evolution_generated` / `chat.evolution_published` | 中间帧 | 演进生成、发布 | 事件对象在 `body.delta` |
| `chat.symphony_status` | 中间帧 | Symphony 状态 | 事件对象在 `body.delta` |
| `workflow.updated` | 中间帧 | 工作流更新 | 事件对象在 `body.delta` |
| `runtime.accepted` | 中间帧 | 运行时已接单 | 事件对象在 `body.delta` |
| `execution.error` | 中间帧 | 执行错误 | 事件对象在 `body.delta` |
| `harness.activate_interaction` | 中间帧 | Harness 激活确认 | `interaction_type`（`activate_confirm`）、`interaction_id`、`extension_name`、`runtime_path`、`options`（缺省 `accept` / `reject`） |
| `plan.approval_required` | 中间帧 | 计划待批准 | 计划确认也可以是 `source` 为 `confirm_interrupt` 的提问卡 |

不认识的 `event_type` 应跳过，不要因此断开连接。

`chat.interrupt_result` 不是这条流里的增量，它是 `chat.interrupt` 的一元结果，见 3.4.3。`history.message` 属于历史接口。`proactive_recommendation`、`heartbeat.relay` 是服务端推送，`metadata._jiuwenswarm_server_push` 为 `true`，不要归进这条 `request_id`。

### 3.4 向用户提问与中断

这一节是对话进行中要人介入、或要停掉正在跑的任务。提问卡从 `chat.send` 的流里推回来；答案仍用一条新的 `chat.send` 交回。中断和审批应答是另外的一元请求。

#### 3.4.1 提问卡 `chat.ask_user_question`

出现在暂停帧之前。`body.delta` 示例：

```json
{
  "event_type": "chat.ask_user_question",
  "request_id": "call_abc",
  "source": "ask_user_interrupt",
  "questions": [
    {
      "question": "用哪种格式？",
      "header": "Question",
      "multi_select": false,
      "options": [
        {"label": "Markdown", "description": ""},
        {"label": "Other", "description": "Custom input"}
      ]
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `request_id` | 这次中断的工具调用 id。续答时放进 `params.request_id`。形如 `call_xxx#2` 的序号后缀由服务端剥掉后再对齐原调用 |
| `source` | 中断来源。续答时原样回传，并决定用哪条方法续 |
| `questions` | 问题列表 |
| `questions[].question` | 题干 |
| `questions[].header` | 短标题，缺省 `Question` |
| `questions[].options` | `{label, description, value?}`。已有选项时服务端追加 `{label: "Other", description: "Custom input"}`。没有选项表示自由输入 |
| `questions[].multi_select` | 是否多选 |
| `questions[].preview` | 可选预览，`{text, title?, outline_ref?, format?}` |
| `agent_scope_id` | 子代理范围，可无 |
| `skill_approval_card` | Skill 审批卡，可无 |
| `plan_content` / `plan_language` / `plan_approval_kind` | `source` 为 `confirm_interrupt` 且是退出计划模式时才有。`plan_language` 为 `cn` 或 `en` |
| `expires_at_ms` | 过期时间，可无 |
| `evolution_meta` | 演进审批附带的元数据，可无 |

| `source` | 含义 | 用户提交后发什么 |
|----------|------|------------------|
| `ask_user_interrupt` | 向用户提问 | 新的 `chat.send` |
| `permission_interrupt` | 工具权限 | 新的 `chat.send` |
| `confirm_interrupt` | 控制类确认，含计划批准 | 新的 `chat.send` |
| `evolution_interrupt` | 需要恢复被暂停的演进 | 新的 `chat.send` |
| 其余（技能演进审批、子代理委托审批等） | 不恢复主循环 | `chat.user_answer`，见 3.4.4 |

权限卡的默认选项标签是「本次允许」「本会话内允许」「拒绝」。非企业版还有「永久记住」。`selected_options` 使用这些标签。

#### 3.4.2 把答案交回：仍是一条 `chat.send`

信封使用新的 `request_id`，`is_stream` 仍为 `true`，`session_id` 不变。服务端从中断点继续，响应仍是 3.3 的事件流，可能直接到 `chat.final`，也可能再弹出下一张卡。

```json
{
  "request_id": "req_2",
  "session_id": "sess_1",
  "channel": "web",
  "method": "chat.send",
  "is_stream": true,
  "params": {
    "query": "",
    "mode": "agent",
    "request_id": "call_abc",
    "source": "ask_user_interrupt",
    "answers": [
      {"selected_options": ["Markdown"]}
    ]
  }
}
```

自由输入或选了 Other：

```json
"answers": [
  {"selected_options": ["Other"], "custom_input": "用表格"}
]
```

| `answers[]` 字段 | 说明 |
|------------------|------|
| `selected_options` | 选项 `label` 或 `value` 的字符串数组。多选就多项 |
| `custom_input` | 自由文本 |
| `action` | 部分审批卡使用，如 `allow_once`、`allow_always`、`reject` |

`ask_user_interrupt` 还可以带 `status` 和 `original_request`。没有 `answers` 时，只要 `source` 为 `ask_user_interrupt` 且带了 `status`，服务端仍视为一次显式回答。

用户还没答就取消：发 3.4.3 的 `chat.interrupt`，`intent` 为 `cancel`。结果里 `invalidate_pending_cards` 为 `true` 时，卡片作废，再提交旧的卡片 `request_id` 会被拒绝。

#### 3.4.3 `chat.interrupt`

打断当前会话上正在跑的任务。这是一条新的一元请求，`is_stream` 为 `false`，信封 `request_id` 与正在流式输出的那条 `chat.send` 不同。`session_id` 必须是被打断的会话。

```json
{
  "request_id": "req_cancel_1",
  "session_id": "sess_1",
  "channel": "web",
  "method": "chat.interrupt",
  "is_stream": false,
  "params": { "intent": "cancel" }
}
```

| `params` 字段 | 必填 | 说明 |
|---------------|------|------|
| `intent` | 否 | 缺省 `cancel`。取值 `pause`、`resume`、`cancel`、`supplement` |
| `new_input` | 否 | `supplement` 或 `cancel` 时附带的新输入 |
| `mode` | 否 | 有则按该模式查找正在跑的 agent |

| `intent` | 行为 |
|----------|------|
| `cancel` | 终止当前任务，清理未完成 todo，作废该会话上未应答的提问卡，并取消该 `session_id` 上仍在跑的流 |
| `supplement` | 停掉当前执行，保留 todo，作废未应答卡片，并取消流。用于停掉当前、接着办新的 |
| `pause` | 在下一个模型调用或工具调用检查点阻塞。不取消流 |
| `resume` | 解除 `pause`。不取消流。团队模式下不在这里继续生成，应再发一条用户消息 |

成功时一帧 `e2a.complete`，`body.result`：

| 字段 | 说明 |
|------|------|
| `event_type` | 固定 `chat.interrupt_result` |
| `intent` | 回显 |
| `success` | 是否作用到一个正在跑的任务。没有在跑的任务时，`cancel` 仍可能返回 `success: true`，表示当前没有可取消的任务 |
| `message` | 如「任务已取消」「任务已暂停」「任务已恢复」「任务已切换」 |
| `new_input` | 请求里带了才有 |
| `invalidate_pending_cards` | `cancel` 与 `supplement` 为 `true` |
| `todos` | `cancel` 后更新过的待办，可无 |
| `cancelled_tools` | 被中断的工具调用，可无 |

团队模式走团队运行时：`pause`、`cancel` 作用于团队会话；`resume` 只返回「直接发送下一条消息即可继续」，不在本请求里恢复生成。

`chat.resume` 是另一条方法，用于恢复已暂停任务。暂停后的继续也可以发本方法且 `intent` 为 `resume`。

#### 3.4.4 `chat.user_answer`

不恢复主执行循环。用于技能演进审批、子代理委托审批。`is_stream` 为 `false`。

```json
{
  "request_id": "req_ans_1",
  "session_id": "sess_1",
  "channel": "web",
  "method": "chat.user_answer",
  "is_stream": false,
  "params": {
    "request_id": "skill_evolve_1",
    "answers": [{ "selected_options": ["accept"] }]
  }
}
```

成功时 `body.result` 为 `{ "accepted": true, "resolved": true }` 或 `resolved` 为 `false`。`resolved` 表示服务端是否认领了这张卡片。`request_id` 前缀为 `team_skill_evolve_`、`evolve_simplify_`、`skill_evolve_`，或 `source`、审批结构能对上演进与子代理审批时，才会解析为已处理。

### 3.5 `chat.swarmflow_reply`

团队工作流（swarmflow）跑到 `human` 或 `human_session` 节点时会停住，等真人把这段话说完。这句话用本方法送回去，工作流才从该节点继续。

普通对话发 `chat.send`。提问卡、权限卡、计划确认发新的 `chat.send` 或 `chat.user_answer`。这三种都不会唤醒 swarmflow 的人工节点。没有这种节点的团队，不需要接本方法。

`is_stream` 为 `false`。它不新开一轮对话，也不取消该会话上正在跑的流。服务端按 `session_id` 找到团队运行时，把 `answer` 交给 `TeamManager.interact`。投递目标是 `swarmflow:<run_id>:<correlation_id>`；没有 `run_id` 时退化为 `swarmflow:<correlation_id>`。

| `params` 字段 | 必填 | 说明 |
|---------------|------|------|
| `session_id` | 是 | 团队会话 id，用来定位团队运行时。也可放在信封顶层 |
| `correlation_id` | 是 | 这一轮人工节点的关联号，形如 `{phase}:{label}:{turn}`，跨恢复保持不变。对不上则投递失败 |
| `answer` | 是 | 真人回复正文。空串视为缺参 |
| `run_id` | 否 | 这次 workflow run 的 id。同时有多次 run 时用来避免回复串到另一次 |
| `team_name` | 否 | 契约里有。handler 不读，团队名由运行时按 `session_id` 解析 |

缺 `session_id`、`correlation_id` 或 `answer` 时失败，`body.result` 为 `{ "ok": false, "error": "missing session_id/correlation_id/answer" }`。投递成功为 `{ "ok": true }`。运行时拒绝时 `ok` 为 `false`，`error` 为拒绝原因。

### 3.6 执行中追加：`chat.steer`、`chat.steer.status`

`chat.steer` 往一轮正在跑的 `chat.send` 里追加文本，不新开一轮。`chat.steer.status` 只查询这条追加是否还在。两条都必须 `is_stream: false`，流式会被直接拒绝。信封上的 `session_id` 如果有值，必须和 `params.session_id` 相同。

| 字段 | `chat.steer` | `chat.steer.status` | 说明 |
|------|--------------|---------------------|------|
| `session_id` | 必填 | 必填 | 非空，长度 ≤ 512 |
| `invocation_id` | 必填 | 必填 | 这一轮调用的 id，长度 ≤ 512 |
| `active_request_id` | 必填 | 必填 | 正在跑的那条 `chat.send` 的信封 `request_id`，长度 ≤ 512 |
| `input_id` | 必填 | 可无 | 客户端给这条追加分配的 id，长度 ≤ 512 |
| `client_message_id` | 必填 | 不传 | 长度 ≤ 512 |
| `content` | 必填 | 不传 | 追加文本。非空，长度 ≤ 32000 |

`params` 里出现 `attachments`、`files`、`images`、`media_items`、`documents`，或出现上表以外的键，请求失败。

成功时 `body.result`：

| 字段 | 说明 |
|------|------|
| `input_id` | 回显 |
| `invocation_id` | 回显 |
| `active_request_id` | 回显。不会改写成团队内部的轮次 id |
| `supported` | 仅状态查询、且没有 `input_id`、当前又没有在跑的调用时出现，值为 `false` |
| `status` | 状态查询命中不了调用时为 `unknown` |
| `reason` | 拒绝原因 |

| `reason` | 含义 |
|----------|------|
| `INVALID_REQUEST` | 缺字段、超长、信封 `session_id` 与 params 不一致，或误开了流式 |
| `CONTENT_UNSUPPORTED` | `content` 为空或超长，或带了附件类字段 |
| `RUN_NOT_ACTIVE` | 没有 agent 认领这一轮 |

没有在跑的调用时，服务端不创建 agent、不占会话。

---


## 4. 配置更新

配置变更的主流程是 `sync_agents_configs`：按修订号整份替换一个服务下的智能体目录。`agent.reload_config` 只被 JiuwenSwarm 个人版 Gateway 用来热更新当前进程，企业版在外置 Runtime 托管配置时不发，后续可能弃用。

| 方法 | 用途 |
|------|------|
| `sync_agents_configs` | **配置变更主流程。** 按修订号整份替换一个 `service_id` 下的智能体目录 |
| `agent.reload_config` | 仅 JiuwenSwarm 个人版在用。热更新当前进程里已有的 agent，不改目录。后续可能弃用 |
| `config.cache_clear` | 清掉进程内配置缓存和 embed 配置缓存，不改配置内容 |
| `agent.prewarm.sync` | 按当前启用的通道重新对齐后台预热，配置变更后让预热实例跟上 |

### 4.1 `sync_agents_configs`

配置变更的主流程。一次调用描述某个服务此刻应存在的全部智能体。目录里有、这次没带来的会被删掉。`is_stream` 为 `false`。

| 字段 | 必填 | 说明 |
|------|------|------|
| `revision` | 是 | 非空字符串。与该 `service_id` 上次成功应用的修订号相同则整单跳过，已有智能体都返回 `action: unchanged` |
| `service_id` | 是 | 非空字符串，做命名空间规范化。非法时 `error` 为 `invalid service_id: ...` |
| `agents` | 是 | 数组，可以是空数组（表示该服务下不再保留任何智能体）。同一 `agent_id` 不能重复 |
| `shared_env` | 否 | 对象。只校验形态，不写入进程环境。不在 spawn 表里的键会被忽略并记日志 |

`agents[]` 每一项：

| 字段 | 必填 | 说明 |
|------|------|------|
| `agent_id` | 是 | 非空字符串，同样做命名空间规范化 |
| `config` | 是 | 对象。不是对象时失败，信息为 `agent config must be an object` |
| `env` | 是 | 对象。必须带齐下面的键，多出来的键会收下。值为 `null` 表示从生效环境里删掉该键，空串会保留 |
| `runtime` | 是 | 对象。协议层不校验内部字段，但参与内容哈希。不是对象时失败 |

`env` 里 `JIUWENCLAW_DISABLED_SKILLS`、`JIUWENCLAW_SHARED_SKILLS_DIRS` 与对应的 `JIUWENSWARM_*` 互为别名，带其中一种即可。缺任何必填键时失败，`error` 为 `agent '<id>': env missing required keys: ...`。

必填键：

| 分组 | 键 |
|------|----|
| 模型 | `API_KEY`、`API_BASE`、`MODEL_NAME`、`MODEL_PROVIDER`、`MODEL_CONTEXT_WINDOW`、`LLM_API_KEY`、`default_headers` |
| 记忆与演进 | `MEMORY_ENGINE`、`EVOLUTION_ENABLED`、`TTSE_ENABLED`、`EMBED_API_KEY`、`EMBED_API_BASE`、`EMBED_MODEL` |
| 技能 | `ENABLED_SKILLS`、`DISABLED_SKILLS`，以及 `JIUWENCLAW_DISABLED_SKILLS` / `JIUWENSWARM_DISABLED_SKILLS`、`JIUWENCLAW_SHARED_SKILLS_DIRS` / `JIUWENSWARM_SHARED_SKILLS_DIRS`（别名对里各出现一个即可） |
| 工具调用守卫 | `TOOL_CALLING_GUARD_ENABLED`、`TOOL_CALLING_GUARD_DISABLE`、`TOOL_CALLING_GUARD_STRIP_REASON` |
| 视觉、图像、音频、视频 | `VISION_API_KEY`、`VISION_API_BASE`、`VISION_PROVIDER`、`VISION_MODEL_NAME`、`VISION_DEFAULT_HEADERS`；`IMAGE_GEN_API_KEY`、`IMAGE_GEN_API_BASE`、`IMAGE_GEN_PROVIDER`、`IMAGE_GEN_MODEL_NAME`、`IMAGE_GEN_DEFAULT_HEADERS`；`AUDIO_API_KEY`、`AUDIO_API_BASE`、`AUDIO_PROVIDER`、`AUDIO_MODEL_NAME`；`VIDEO_API_KEY`、`VIDEO_API_BASE`、`VIDEO_PROVIDER`、`VIDEO_MODEL_NAME` |
| 搜索与外部凭证 | `BOCHA_API_KEY`、`JINA_API_KEY`、`PERPLEXITY_API_KEY`、`SERPER_API_KEY`、`PETAL_SEARCH_URL`、`PETAL_SEARCH_HEADERS`、`TAVILY_API_KEY`、`WEB_SEARCH_API_KEY`、`FREE_SEARCH_DDG_ENABLED`、`FREE_SEARCH_BING_ENABLED`、`EMAIL_TOKEN`、`GITHUB_TOKEN`、`ACR_ACCESS_KEY`、`ACR_ACCESS_SECRET`、`ACR_BASE_URL` |

写入时以 `env` 为准，覆盖 `config` 里的同名开关：

| `env` | 写到 |
|-------|------|
| `MEMORY_ENGINE` | `config.memory.engine`。合法值 `builtin`、`external`、`both`、`none`。空或非法时用 `config` 里已有的合法值，再没有则 `builtin` |
| `EVOLUTION_ENABLED` | `config.react.evolution.enabled`。顶层 `config.evolution` 会并进 `react.evolution`；两边都有时以 `react.evolution` 为准 |
| `TTSE_ENABLED` | `config.react.ttse.enabled`。`env` 和 `config` 都没给时，这个键不写入，沿用磁盘上的默认 |

内容哈希按合成后的 `config`、`env`、`runtime` 计算。哈希与目录里现有的相同则 `action` 为 `unchanged`，不重载。

成功或逐项失败时，`body.result` 都带 `event_type: sync_agents_configs.result`，以及 `revision`、`service_id`、`agents`。外层 `ok` 为 true 仅当每一项 `ok` 都为 true。校验不通过（缺字段、类型不对）时外层失败，`error` 为 `ValueError` 的文本，同样带这个 `event_type`。

`agents[]` 出参：

| 字段 | 说明 |
|------|------|
| `agent_id` | 规范化之后的 id |
| `action` | `added`、`updated`、`removed`、`unchanged` |
| `ok` | 该项是否成功。重载有失败会话时为 false，`error` 为 `reload failed for one or more sessions` |
| `error` | 失败原因，成功时为 null |
| `warmup` | `{ok, error, skipped}`。跳过预热时 `skipped` 为 true |
| `reload` | 有重载时为 `{applied, deferred, failed}`，否则为 null |

### 4.2 `agent.reload_config`

仅 JiuwenSwarm 个人版 Gateway 在保存配置时调用，用来热更新已经在跑的 agent，不替换目录。企业版在外置 Runtime 托管配置时会跳过这条。后续可能弃用，新对接用 `sync_agents_configs`。

| 字段 | 必填 | 说明 |
|------|------|------|
| `config` | 否 | 新配置对象 |
| `env` | 否 | 环境覆盖 |
| `target_channel_id` | 否 | 非空才下发 |
| `target_session_id` | 否 | 非空才下发 |
| `reload_scopes` | 否 | 字符串数组。空或未传表示全部。会触发 agent 重载的取值是 `model`、`team`、`permissions`、`agent_runtime`。主动推荐还会看 `proactive` |

`channel_id` 为 `officeclaw`，或信封带了非空 `agent_id` 时，走租户池 `reload_tenant_config`。否则走 `reload_agents_config`。成功出参 `{ "reloaded": true }`。

### 4.3 `config.cache_clear`

无入参。成功出参 `{ "cleared": true }`。

### 4.4 `agent.prewarm.sync`

| 字段 | 必填 | 说明 |
|------|------|------|
| `enabled_channels` | 是 | 字符串数组。不是数组时失败，`code` 为 `BAD_REQUEST`，`error` 为 `enabled_channels must be a list` |
| `config` | 否 | 预热用配置 |
| `env` | 否 | 预热用环境 |

出参为计数：`target`、`ready`、`warming`、`failed`、`stale`。预热池关闭时五项都是 0。

## 5. 会话

`is_stream` 均为 `false`。会话 id 放在 `params.session_id`；已连接会话也可以放在信封顶层 `session_id`，各方法下文会写清谁优先。

### 5.1 `session.create`

创建会话。服务端分配 `session_id`。`params.create_token` 必填，同一 token 重复调用会认领同一次预热，不要在 `params` 里自造 `session_id`。

**入参**（`params`）

| 字段 | 必填 | 说明 |
|------|------|------|
| `create_token` | 是 | 非空字符串。缺失直接失败 |
| `mode` | 否 | 缺省按 `agent` 解析。`team` / `team.plan` / `code.team` 视为 swarm |
| `title` | 否 | 标题，缺省空串 |
| `user_id` | 否 | |
| `project_id` / `project_dir` / `work_mode` | 否 | 项目绑定。非法绑定失败，`code` 来自绑定校验 |
| `previous_session_id` | 否 | 创建后切换上下文时的上一个会话 |
| `cron_id` | 否 | |
| `is_swarm` | 否 | 为真时按团队会话创建 |

**出参**

| 字段 | 说明 |
|------|------|
| `session_id` | 服务端分配的会话 id。同时有 `sessionId`，值相同 |
| `projectId` | 解析后的项目 id |
| `projectDir` | 解析后的项目目录 |
| `workMode` | 解析后的工作模式 |
| `prewarm_hit` | 是否命中预热会话 |
| `prewarm_status` | 预热认领状态 |

目录已存在时失败，`code` 为 `ALREADY_EXISTS`。

### 5.2 `session.list`

**入参**（`params`）

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `limit` | 否 | 20 | 夹在 1 到 200 |
| `offset` | 否 | 0 | 小于 0 时按 0 |

**出参**

| 字段 | 说明 |
|------|------|
| `sessions` | 当前页。按 `last_message_at` 倒序。元素是会话 metadata |
| `total` | 总数（不含 `heartbeat` 前缀会话） |
| `limit` | 本次生效的 limit |
| `offset` | 本次生效的 offset |

`sessions[]` 至少包含：`session_id`、`channel_id`、`user_id`、`created_at`、`last_message_at`、`title`、`message_count`、`mode`、`project_id`、`project_dir`、`work_mode`、`cron_id`。没有 `metadata.json` 的旧目录用上述字段的空值或 `mode: "unknown"` 补齐。

### 5.3 `session.rename`

不传 `title` 只查询，不改标题。

**入参**（`params`）

| 字段 | 必填 | 说明 |
|------|------|------|
| `session_id` | 是 | 也可使用信封上的 `session_id`。两者都空则失败，`code` 为 `BAD_REQUEST` |
| `title` | 否 | 不传：只读当前标题。空串：清除标题。非空：设置，去掉首尾空白后最长 200 字符 |

**出参**

| 字段 | 说明 |
|------|------|
| `session_id` | |
| `title` | 当前标题 |
| `previous_title` | 修改前的标题。只查询时与 `title` 相同 |

### 5.4 `session.switch`

**入参**（`params`）

| 字段 | 必填 | 说明 |
|------|------|------|
| `session_id` | 是 | 目标会话。也可放在信封 `session_id` |
| `previous_session_id` | 否 | 切出的会话 |

**出参**

| 字段 | 说明 |
|------|------|
| `session_id` | 目标会话 |
| `mode` | 解析后的模式 |
| `switched` | `true` |

### 5.5 `session.delete`

**入参**（`params`）

| 字段 | 必填 | 说明 |
|------|------|------|
| `session_id` | 是 | 只认 `params.session_id`，不回落到信封 |

**出参**：`session_id`，表示已删除的会话。

失败：`session_id` 为空或非法为 `BAD_REQUEST`；目录不存在为 `NOT_FOUND`；运行时清理失败为 `DELETE_FAILED`。

### 5.6 `session.fork`

**入参**（`params`）

| 字段 | 必填 | 说明 |
|------|------|------|
| `source_session_id` | 是 | 被复制的会话 |
| `target_session_id` | 是 | 新会话 id |
| `title` | 否 | 新会话标题 |

**出参**：fork 实现返回的结果对象。失败时 `body` 带 `code`。

### 5.7 `session.rewind` / `session.rewind_and_restore` / `session.rewind_compact` / `session.rewind_context`

把会话历史截到指定用户轮次。`turn_index` 从 1 开始，按历史里 `role=user` 的记录计数。

| 方法 | 与 `session.rewind` 的差别 |
|------|--------------------------|
| `session.rewind` | 只截历史 |
| `session.rewind_and_restore` | 截历史并恢复文件 |
| `session.rewind_compact` | 截断处写入压缩摘要 |
| `session.rewind_context` | 截历史并标记上下文是否回退成功 |

**入参**（`params`）

| 字段 | 必填 | 说明 |
|------|------|------|
| `session_id` | 是 | 也可放在信封 `session_id` |
| `turn_index` | 是 | 整数，≥ 1，且不超过用户轮次数 |
| `compact_summary` | 仅 `rewind_compact` | 摘要正文 |
| `direction` | 仅 `rewind_compact` | 缺省 `from` |
| `summarized_count` | 仅 `rewind_compact` | 缺省 0 |

**出参**（`session.rewind`）

| 字段 | 说明 |
|------|------|
| `session_id` | |
| `turn_index` | |
| `content` | 被截掉的那条用户消息正文 |
| `content_preview` | `content` 的前 80 字符 |
| `remaining_records` | 截断后剩余记录数 |
| `removed_records` | 被移除的记录数 |
| `rewind_context` | 上下文是否回退成功 |

`turn_index` 非法、没有历史、没有用户消息时失败，`code` 为 `BAD_REQUEST`。

### 5.8 `history.get`

`is_stream` 为 `false` 时一帧返回本页。为 `true` 时按条推 `history.message`，最后一帧 `status: done` 且 `is_final: true`。

**入参**（`params`）

| 字段 | 必填 | 说明 |
|------|------|------|
| `session_id` | 是 | 非空。没有历史文件则失败 |
| `page_idx` | 是 | 从 1 开始。≤ 0 或超过总页数则失败 |

**出参**（非流式 `body.result`）

| 字段 | 说明 |
|------|------|
| `messages` | 本页记录，新的在前。每条是历史记录，含 `role`、`content`、`event_type`、`session_id` 等 |
| `total_pages` | 总页数，至少为 1 |
| `page_idx` | 本次页码 |

失败时一元响应 `ok` 为 false，`payload.error` 为 `invalid page_idx or session history not found`。流式时这句错误在 `chat.error` 事件里，并且该帧即为结束帧。

流式每一条：

| 字段 | 说明 |
|------|------|
| `event_type` | `history.message` |
| `message` | 单条历史记录。结束帧没有此字段 |
| `session_id` | |
| `total_pages` | |
| `page_idx` | |
| `status` | 仅结束帧，值为 `done` |

### 5.9 `initialize`

`is_stream` 为 `false`。

**入参**（`params`）

| 字段 | 必填 | 说明 |
|------|------|------|
| `clientCapabilities` | 否 | 客户端能力对象，缺省 `{}` |
| `protocolVersion` | 否 | 缺省 `0.1.0` |

**出参**：服务端能力对象，其中回显 `protocol_version`。异常时失败，`error` 为异常文本。

### 5.10 `acp.tool_response`

把 ACP 工具的 JSON-RPC 结果交回正在等的调用。`is_stream` 为 `false`。

**入参**（`params`）

| 字段 | 必填 | 说明 |
|------|------|------|
| `jsonrpc_id` | 是 | 与待完成调用的 id 对应 |
| `response` | 否 | 对象。不是对象时按 `{}` |

**出参**：认领成功为 `{ "accepted": true }`。没有匹配的等待调用则失败。

---

## 6. 命令

这些方法对应斜杠命令，都是一元调用。需要会话上下文时把 `session_id` 放在信封上。

### 6.1 `command.compact`

压缩当前会话上下文。成功后服务端还会推一帧 `metadata._jiuwenswarm_server_push = true` 的压缩状态，供界面刷新用量。

**入参**：`mode`，缺省 `agent`。会话取信封 `session_id`，空则 `default`。

**出参**

| 字段 | 说明 |
|------|------|
| `result` | 压缩执行结果 |
| `stats` | 压缩统计 |
| `summary` | 有摘要时才有 |
| `compact_summary` | 与 `summary` 相同 |

推送载荷 `event` 侧字段包括 `phase`、`processor`、`before.tokens`、`after.tokens`、`saved.tokens`、`saved.percent`、`summary`。

### 6.2 `command.compact_partial`

按用户轮次做部分压缩。

| 字段 | 必填 | 说明 |
|------|------|------|
| `turn_index` | 是 | 整数，缺省按 0，不合法会失败 |
| `direction` | 否 | 缺省 `from` |
| `mode` | 否 | 缺省 `agent` |

**出参**与压缩实现的返回对象一致，失败时 `error` 为异常文本。

### 6.3 `command.add_dir`

把目录加入信任目录。

| 字段 | 必填 | 说明 |
|------|------|------|
| `path` | 是 | 空串视为缺失 |
| `remember` | 否 | 缺省 `false`，原样回显 |

**出参**：`path`、`remember`、`persist`。`persist.ok` 为 false 时本请求也失败。`path` 为空时 `persist` 为 `{ "ok": false, "error": "path is required" }`。

### 6.4 `command.model`

用 `action` 区分。

| `action` | 其他字段 | 出参 |
|----------|----------|------|
| `add_model` | `target`：模型名 | `{ "type": "model_added", "name" }` |
| `switch_model` | `model`；`env_updates` 对象，必填且不能为空。`API_BASE` 不能是占位域名 | 切换结果对象 |
| 其他或缺省 | 无 | `{ "current", "available" }`。`current` 来自环境变量 `MODEL_NAME`，缺省 `unknown` |

### 6.5 `command.status`

| 字段 | 必填 | 说明 |
|------|------|------|
| `action` | 否 | `overview`（缺省）、`usage`、`config` |
| `cwd` | 否 | 仅 `overview`。记忆诊断用的工作目录，缺省进程当前目录 |
| `trusted_dirs` | 否 | 仅 `overview`。非空数组时用第一项作为记忆诊断目录 |

`action=usage` 最多扫描 500 条会话。出参：

| 字段 | 说明 |
|------|------|
| `sessions_total` | 会话总数 |
| `messages_total` | `message_count` 之和 |
| `models_used` | `{name, count}` 数组，按 count 降序。`name` 取的是会话 `mode`，不是模型名 |
| `active_days` | 有 `created_at` 的不同 UTC 日期数 |
| `longest_session_hours` | `last_message_at - created_at` 的最大小时数，保留 1 位小数 |

`action=config` 出参：`config_path`、`settings_sources`。后者在设置了 `JIUWENSWARM_CONFIG_DIR` 时先放该环境变量，再放配置文件路径。

`action=overview` 出参：`version`、`session_id`、`cwd`、`model`、`provider`、`api_base`、`connection_status`（固定 `connected`）、`mcp_servers`（`name`、`enabled`、`transport`）、`config_path`、`settings_sources`、`memory_warnings`。

### 6.6 `command.workflows`

| 字段 | 必填 | 说明 |
|------|------|------|
| `action` | 否 | 缺省 `list` |
| `workflow_id` 或 `workflow_run_id` | `get`、`get_human_prompt` 时必填 | |
| `agent_id` 或 `correlation_id` | `get_human_prompt` 时至少其一 | |

`get` 找不到工作流时失败，`error` 为 `workflow not found: ...`。

### 6.7 `command.btw`

旁路提问，不写入主对话。

| 字段 | 必填 | 说明 |
|------|------|------|
| `question` | 是 | 去掉空白后为空则出参 `{ "status": "failed", "error": "Question is required" }`，请求本身仍可能 `ok: true` |
| `mode` | 否 | 缺省 `agent` |

成功出参含模型回答。

### 6.8 `command.simplify`

| 字段 | 必填 | 说明 |
|------|------|------|
| `target` | 视动作 | 要简化的目标 |

成功出参为 `{ "prompt": ... }`。

### 6.9 `command.session` / `command.resume` / `command.context` / `command.recap` / `command.diff` / `command.chrome`

这些命令把 `mode`（缺省 `agent`）和信封上的会话交给对应 agent 能力。`command.session` 另读 `action`，缺省 `overview`，以及 `cwd`、`trusted_dirs`。失败时出参 `error` 为异常文本。

### 6.10 `command.goal`

非流式目标控制。`params.action` 缺省 `get`，常用 `set`、`pause`、`resume`、`clear`。`set` 时 `objective` 会作为用户轮次写入历史。出参里 `result_type` 为 `goal_error` 或 `goal_confirm_required` 时，该请求视为失败。

---

## 7. 智能体

配置工作区里的子 agent。`workspace_dir` 决定读写哪份配置，可空则用默认工作区。除特别说明外为一元调用。

### 7.1 `agents.list`

**入参**：`workspace_dir`，可无。

**出参**：`agents`，元素为 agent 配置对象。

### 7.2 `agents.get`

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | |
| `workspace_dir` | 否 | |

**出参**：`agent`。不存在时失败，`error` 为 `Agent 不存在: {name}`。

### 7.3 `agents.create`

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | |
| `description` | 是 | |
| `prompt` | 否 | `generate` 成功时会被模型生成的提示词覆盖 |
| `location` | 是 | 配置来源 |
| `generate` | 否 | 缺省 `true`。为真且 `name`、`description` 都有时，用模型生成 `when_to_use` 和 `prompt` |
| `workspace_dir` | 否 | 不进入 agent 对象 |
| `model` / `tools` / `color` / `permission_mode` / `memory_scope` / `disallowed_tools` / `when_to_use` / `max_iterations` / `skills` | 否 | 与 `CreateAgentParams` 一致。不在该结构里的键会被丢掉 |

创建成功后会把该 agent 写入配置并热加载。热加载失败不影响创建成功。

**出参**

| 字段 | 说明 |
|------|------|
| `agent` | 创建后的配置对象 |
| `generated` | 是否用了模型生成文案 |
| `applied` | 是否已写进配置并完成热加载 |
| `reload_error` | 热加载失败时的文本，成功为 `null` |

### 7.4 `agents.update`

`name` 必填，用来定位。其余字段与更新结构一致，只提交要改的项。`workspace_dir` 可选。

**出参**：`ok`、`applied`、`reload_error`。`applied` 表示配置热加载是否成功。

### 7.5 `agents.delete` / `agents.enable` / `agents.disable`

`name` 必填，`workspace_dir` 可选。启用和禁用只改开关并热加载。删除失败时 `error` 为异常文本。

### 7.6 `agents.tools_list`

**入参**：`workspace_dir`，可无。

**出参**：该工作区 agent 可用工具列表。读取失败时 `error` 为异常文本。

---

## 8. 技能

技能方法由 `SkillManager` 处理，返回值原样放进 `body.result`。多数失败不走异常，而是 `success: false` 加 `detail`，此时外层 `ok` 仍可能为 `true`。抛 `ValueError` 的方法（如 `skills.get` 缺少 `name`）才会变成失败帧。

企业版只允许白名单内的 `skills.*`。直连时白名单外的技能写操作返回拒绝。

### 8.1 `skills.list`

列出本地技能、内置技能，以及 marketplace 里尚未安装的技能。企业版不扫描 marketplace，也不返回内置技能。

| 字段 | 必填 | 说明 |
|------|------|------|
| `refresh_marketplaces` | 否 | 缺省 `false`。个人版为 `true` 时先对已配置 marketplace 做 clone/pull 再扫描。企业版忽略 |
| `with_installed` | 否 | 缺省 `false`。为 `true` 时同一响应附带 `plugins`，内容与 `skills.installed` 的 `plugins` 相同 |

**出参**：`skills` 数组。`with_installed` 为真时另有 `plugins`。

### 8.2 `skills.installed`

无必填参数。

**出参**

| 字段 | 说明 |
|------|------|
| `plugins` | marketplace 安装记录。每项含 `plugin_name`、`marketplace`、`spec`（`name@marketplace`）、`version`、`installed_at`、`git_commit`、`enabled`、`skills` |
| `skills` | `source_type` 为 `prebuilt`、`user`、`builtin` 的安装记录 |

`source_type` 为这三者的记录不会出现在 `plugins` 里。

### 8.3 `skills.get`

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 缺少时抛错，失败信息为 `缺少参数: name` |
| `origin` | 否 | 有重名时按来源精确匹配 |

**出参**：技能详情。源码把正文从 `body` 映到 `content`，把路径从 `path` 映到 `file_path`。找不到时失败，信息为 `未找到 skill: {name}`。

### 8.4 `skills.install`

| 字段 | 必填 | 说明 |
|------|------|------|
| `spec` | 是 | `plugin_name@marketplace_name`。没有 `@` 时，若名称能对上内置技能目录，则按内置技能安装 |
| `force` | 否 | 缺省 `false`。已存在且不为真时不覆盖 |

**出参**：成功 `{ "success": true }`。格式不对或名称为空时 `{ "success": false, "detail" }`。企业版安装内置技能时 `error_code` 为 `builtin_not_available`。

内置安装路径还接受 `name` 与 `force`，由 `skills.install` 在 spec 无 `@` 时转入。

### 8.5 `skills.toggle`

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | |
| `enabled` | 是 | 必须是布尔。缺省或其他类型时 `detail` 为 `缺少参数: enabled (bool)` |
| `origin` | 否 | 重名时定位具体安装记录 |

**出参**：`success` 为 true 或 false，失败带 `detail`。

### 8.6 `skills.uninstall`

`name` 必填。缺少时抛错 `缺少参数: name`。

### 8.7 `skills.evolution.status`

`name` 必填。

**出参**：`name`、`exists`。`exists` 表示该技能目录下是否有 `evolutions.json`。

### 8.8 `skills.enterprise.list`

与 `skills.installed` 相同的列表，并给每条技能补上 `skill_name`、`skill_source`、`skill_version`、`service_id`、`agent_id`。`service_id`、`agent_id` 优先取 params，否则取当前租户。响应顶层也回显这两个 id。

### 8.9 检索与来源搜索

| 方法 | 必填 | 失败形态 |
|------|------|----------|
| `skills.online_search.search` | `q` | `{ "success": false, "partial": false, "items": [], "sources": [], "detail": "缺少参数: q" }` |
| `skills.skillnet.search` | `q` | `{ "success": false, "detail": "缺少参数: q" }` |
| `skills.skillnet.install` | `url` | `detail` 为 `缺少参数: url` |
| `skills.skillnet.install_status` | `install_id` | `detail` 为 `缺少参数: install_id` |
| `skills.clawhub.search` | `q` | `detail` 为 `缺少参数: q` |
| `skills.clawhub.download` | `slug` | `detail` 为 `缺少参数: slug` |
| `skills.marketplace.add` | `url` | `detail` 为 `缺少参数: url` |
| `skills.retrieval.tree` | 无 | `language` 缺省 `cn` |

`skills.marketplace.list` 的出参为 `marketplaces`，每项含 `name`、`url`、`enabled`、`install_location`、`last_updated`。

---

## 9. 团队

### 9.1 `team.templates.list`

无必填参数。**出参**：`templates`。

### 9.2 `team.bindings.list`

无必填参数。**出参**：`teams`，元素为带展示字段的绑定。

### 9.3 `team.binding.create`

| 字段 | 必填 | 说明 |
|------|------|------|
| `team_name` | 是 | |
| `template_id` | 是 | 去掉空白后不能空 |

**出参**：`team`，为绑定对象。校验失败时 `code` 来自存储异常，缺省 `BAD_REQUEST`。

### 9.4 `team.binding.generate`

| 字段 | 必填 | 说明 |
|------|------|------|
| `description` 或 `prompt` | 是 | 用这段文字生成团队。两者都读，`description` 优先 |

**出参**：`team`，以及默认模板信息。生成失败时 `code` 为 `GENERATION_FAILED`。

### 9.5 `team.session.bind`

把会话绑到已有团队。

| 字段 | 必填 | 说明 |
|------|------|------|
| `session_id` | 是 | 也可放在信封上 |
| `team_name` | 是 | |
| `mode` | 否 | 缺省 `team` |

**出参**含绑定后的会话与团队标识。失败 `code` 缺省 `BAD_REQUEST`。

### 9.6 `team.delete`

`team_name` 必填，空则 `code` 为 `BAD_REQUEST`。不存在为 `NOT_FOUND`。删除过程失败时 `code` 缺省 `DELETE_FAILED`。

### 9.7 `team.snapshot`

`session_id` 取 params 或信封。没有团队时出参为 `{ "members": [], "tasks": [], "team_id": null }`。有团队时为该会话的成员、任务和团队 id。

### 9.8 `team.history.get`

| 字段 | 必填 | 说明 |
|------|------|------|
| `session_id` | 是 | 缺则失败，`error` 为 `session_id is required` |
| `member_name` | 否 | 只看该成员 |
| `cursor` 或 `offset` | 否 | 起始下标，缺省 0，夹在 `0..total` |
| `limit` | 否 | 条数上限，有默认值和上限 |
| `max_bytes` | 否 | 本页字节预算，有默认值和上下限 |

**出参**

| 字段 | 说明 |
|------|------|
| `records` | 本页记录 |
| `session_id` | |
| `cursor` | 本次起点 |
| `next_cursor` | 下一页起点 |
| `has_more` | `next_cursor < total` |
| `total` | 过滤后的总条数 |

单条记录超过字节预算时会被压缩后再放入 `records`，避免一页卡死。

### 9.9 `team.members.get`

给加入流程查人类席位。`session_id` 取 params 或信封，另可带 `team_name`。

**出参**：`ok` 与 `members`。查不到或异常时请求失败，`payload` 里不带对外文案。

### 9.10 `team.mq.publish`

`payload` 为要发布的消息体。成功出参 `{ "published": true }`，失败出参 `{ "error": reason }`。

### 9.11 `team.session.reset` / `team.runtime.dissolve`

按 `team_name` 与 `session_id` 结束团队运行时。`reset` 清会话侧团队状态，`dissolve` 解散正在跑的运行时。两者都要求能定位到团队和会话。

---

## 10. 权限

`permissions.*` 进同一个分发函数。读方法不会触发配置热加载，写方法成功后会在后台重载，让权限轨不用等到下一次工具调用才看见新配置。

| 方法 | 入参 | 出参 |
|------|------|------|
| `permissions.enabled.get` | 无 | `{ "enabled": bool }`，缺省按 true |
| `permissions.enabled.set` | `enabled` 必须是布尔 | `{ "enabled": 写入值 }` |
| `permissions.file_guard.workspace.rw_enabled.get` | 无 | `{ "rw_enabled": bool }` |
| `permissions.file_guard.workspace.rw_enabled.set` | `rw_enabled` 必须是布尔 | `{ "rw_enabled": 写入值 }` |
| `permissions.file_guard.workspace.access.get` | 无 | 工作区 `read` / `write` / `exec` |
| `permissions.file_guard.workspace.access.set` | `access` 必须是对象，含要改的 `read` / `write` / `exec` | 更新后的访问视图 |
| `permissions.tools.set` | `tools` 为「工具名 → 级别」对象 | 写入后的工具权限 |
| `permissions.tools.update` | `tool` 或 `name`，以及 `level` | 更新后的该项 |
| `permissions.tools.delete` | `tool` 或 `name` | 删除结果 |
| `permissions.rules.create` | `rule` 对象 | 新建的规则 |
| `permissions.rules.update` | `id`，`patch` 对象 | 更新后的规则 |
| `permissions.rules.delete` | `id` | 删除结果 |
| `permissions.approval_overrides.delete` | `id` | 删除结果 |

`enabled` 不是布尔、`rw_enabled` 不是布尔、`access` 不是对象时，失败信息分别为 `enabled must be boolean`、`rw_enabled must be boolean`、`access must be object with read/write/exec`。

`permissions.tools.get`、`permissions.tools.list`、`permissions.rules.get`、`permissions.approval_overrides.get` 无必填参数，出参为当前配置视图。工具列表会合并运行中 agent 的工具目录。

---

## 11. 调度

`schedule.*` 与 `issue.*` 共用一个处理函数，由方法名决定动作。`create`、`run`、`cancel`、`delete`、`issue.watch_once` 会先取一个 agent；取不到则失败，信息为 `Failed to get agent for schedule request`。

| 方法 | 入参 | 说明 |
|------|------|------|
| `schedule.check_config` | 无 | 出参为调度配置检查结果 |
| `schedule.update_config` | `fields` 对象，缺省 `{}` | 局部更新调度配置 |
| `schedule.create` | `query` 缺省空串；`interval_hours` 缺省 4；`run_immediately` 缺省 false；`model_name`、`pipeline` 可无 | 创建定时任务 |
| `schedule.run` | `query`、`model_name`、`pipeline` | 立即跑一次 |
| `schedule.list` | 无 | 出参 `{ "tasks": [...] }` |
| `schedule.status` | `task_id` | 查单任务 |
| `schedule.logs` | `task_id`；`log_type` 缺省 `current`；`history_index` 缺省 -1；`offset` 缺省 0；`limit` 缺省 500 | 读任务日志 |
| `schedule.cancel` / `schedule.delete` | `task_id` | 取消或删除 |
| `issue.watch_once` | `model_name` 可无 | 对议题做一次巡检 |
| `issue.state.list` / `issue.delete` / `issue.matrix` | 按议题存储读取对应 id 或筛选条件 | 未知动作时 `error` 为 `未知的调度操作: {action}` |

---

## 12. MCP、沙箱、扩展与文件

### 12.1 `command.mcp`

会话内的 MCP 命令。`action` 缺省 `list`。`add`、`remove`、`update`、`get` 需要 `name`。

企业版禁止在这里改服务器定义。

### 12.2 `mcp.server.add` / `remove` / `update` / `list` / `get`

管理 MCP 服务器注册表。企业版一律拒绝，出参 `code` 为 `MCP_FORBIDDEN`，并带回 `action`。

| 方法 | 入参 | 出参 |
|------|------|------|
| `mcp.server.add` | `servers` 必须是数组 | `{ "results" }`。不是数组时 `error` 为 `servers must be a list` |
| `mcp.server.remove` | `names` 必须是数组 | 删除结果 |
| `mcp.server.update` | `servers` 必须是数组 | 更新结果 |
| `mcp.server.list` | 无 | 服务器列表 |
| `mcp.server.get` | `name` | 单个服务器 |

### 12.3 `command.sandbox`

只在 Linux 上可用。`params.sub` 缺省 `status`。

| `sub` | 作用 |
|-------|------|
| `status` | 出参 `{ "runtime": <当前沙箱运行时> }`。`sandbox.type=yuanrong` 时改为只读配置视图，且不允许其他 sub |
| `enable` / `disable` | 重建 agent 的系统操作类型 |
| `exclude.add` / `exclude.remove` / `exclude.list` | 排除项。add/remove 读 `pattern` |
| `files.allow` / `files.deny` / `files.list` | 文件策略。写操作读 `path` |

除 `status` 外的写操作走运行时热更新，不重建整个 agent。`enable` / `disable` 会重建 agent。参数不合法时 `code` 为 `SANDBOX_BAD_REQUEST`。

`sandbox.enabled.get`、`sandbox.enabled.set`、`sandbox.startup_mode.*`、`sandbox.files.*`、`sandbox.network.*` 是另一组配置 RPC，直接读写沙箱开关、启动方式和文件/网络策略，不经过 `command.sandbox` 的 `sub`。

### 12.4 扩展与 Harness 包

| 方法 | 入参 | 出参 |
|------|------|------|
| `extensions.list` | 无 | 已安装 Rail 扩展列表 |
| `extensions.import` | `folder_path` 必填，且必须是已存在的目录 | 导入后的扩展对象。缺少路径时失败信息为 `缺少 folder_path 参数` |
| `extensions.delete` | `name` | 删除结果 |
| `extensions.toggle` | `name`；`enabled` 缺省 `false` | 切换结果 |
| `hooks.list` | 无 | Hook 列表 |
| `harness.packages.get` | 无 | 已安装包 |
| `harness.packages.scan` | 无 | 扫描结果。失败 `error` 为异常文本 |
| `harness.packages.activate` / `deactivate` / `delete` | `package_id` 必填 | 缺少时 `code` 为 `BAD_REQUEST`，`error` 为 `missing package_id` |

### 12.5 文件分块传输

`file.transfer.start`、`file.transfer.chunk`、`file.transfer.complete` 只在分布式文件传输开启时可用。未开启时失败，`error` 为 `file transfer not enabled (distributed mode required)`，并带回 `event_type`。

| 方法 | 入参 |
|------|------|
| 三条共用 | `transfer_id`。`event_type` 可省略，省略时用方法名本身 |
| `file.transfer.start` | `filename` 缺省 `unnamed`；`file_size` 缺省 0；`sha256`；`total_chunks` 缺省 0；`chunk_size` 缺省 65536；`mime_type` |
| `file.transfer.chunk` | `chunk_index`；`base64_data` |
| `file.transfer.complete` | `sha256`，用于整文件校验 |

按顺序调用：先 `start`，再按 `chunk_index` 送 `chunk`，最后 `complete`。任一块失败就不要发 `complete`。

---

## 13. 插件、运维与长程任务

这些方法都是一元调用。插件方法的业务结果在 `body.result` 里，失败常以 `success: false` 和 `detail` 返回，外层帧仍可能是成功。运维方法失败时 `ok` 为 false，错误在 `body` 或 `body.result.error`。

### 13.1 插件

实现：`SkillManager.handle_plugins_*`。

| 方法 | 入参 | 出参 |
|------|------|------|
| `plugins.list` | 无 | `{ plugins }`。每项：`plugin_name`、`marketplace`、`spec`（有 marketplace 时为 `name@marketplace`）、`version`、`installed_at`、`git_commit`、`skills`、`enabled` |
| `plugins.install` | `spec` 必填。本地路径、`http(s)` URL，或 marketplace 规格。`force` 仅本地/URL 时传给导入 | 缺少 `spec` 时 `{ success: false, detail: "缺少参数: spec" }`。本地/URL 走技能本地导入的返回；marketplace 走 `skills.install` 的返回，成功后再把插件标为启用 |
| `plugins.uninstall` | `name` | 与 `skills.uninstall` 相同。卸载前若存在 `_disabled_plugins/<name>` 会先删掉该缓存目录 |
| `plugins.enable` / `plugins.disable` | `name` 必填 | 缺少 name：`detail` 为 `缺少参数: name`。找不到：`未找到插件: <name>`。成功 `detail` 提示还需执行插件重载才生效 |
| `plugins.reload` | 无 | `{ success, plugins_count, disabled_count, skills_count, detail }`。按启用状态在技能目录和 `_disabled_plugins` 之间移动目录 |

### 13.2 其他运维

`proactive.tick` 由调度触发一次主动推荐。入参 `target_channel` 可选。引擎未初始化时失败，`error` 为 `ProactiveEngine not initialized`。成功出参 `success`（是否真的产生推荐）和 `status`：`tick_executed` 或 `no_recommendation`，若已有上次 tick 时间会附上 `last_tick_at`。

`browser.runtime_restart` 无入参。成功出参 `result`（本地浏览器运行时重启结果）和 `reset_runtimes`（已重置的运行时数量；当前 SDK 没有重置接口时为 0）。

### 13.3 `long_horizon`

一元。`params.action` 必填，大小写不敏感。`mark_due` 直接标记阶段到期，其余交给 `LongHorizonActions.handle`。工作区来自当前请求；`service_id`、`agent_id` 来自租户标识，缺省 `default`。异常时外层 `ok` 为 false，`body.result` 为 `{ success: false, error }`。`success` 为 false 时外层同样失败。

| `action` | 入参 | 出参要点 |
|----------|------|----------|
| `list` | 无额外必填 | `{ success, tasks, count }` |
| `inbox` | 无额外必填 | `{ success, inbox }` |
| `mark_due` | `task_id`、`stage_id`、`job_id` 可空字符串 | `mark_stage_due` 的返回 |
| `draft` / `confirm` / `update` / `delete` / `stage_action` | 见长程任务参数对象。`stage_action` 的阶段动作含 `mute_year`、`done`、`skip`、`snooze`、`defer`、`start` | 各动作自己的结果对象 |

未知 `action` 时 `error` 为 `Unknown action. Valid: draft, confirm, update, list, inbox, delete, stage_action`。`mark_due` 不在这句里，它在进入该校验之前就被单独处理。

---

## 14. HTTP

同一套 `method` 另有 HTTP/SSE 入口，默认关闭。打开方式是 `config.yaml` 的 `http_server.enabled`，或环境变量 `AGENT_HTTP_ENABLED`。默认地址 `http://127.0.0.1:8766/api/v1`。link mTLS 强制开启时，本进程不监听 WebSocket 端口，并打开该 HTTP 入口。

HTTP 上的业务对象与本文相同。流式时每一帧 E2A 放在 SSE 的 `data` 里。资源路径只是把 `method` 拆成 URL，不另定义一套字段。
