# SkillDevDeepAdapter 前后端交互协议文档

## 一、后端 → 前端（AgentResponseChunk 下行事件）

所有下行事件均封装为 `AgentResponseChunk`：

```python
AgentResponseChunk(
    request_id: str,       # 请求 ID
    channel_id: str,       # 通道 ID
    payload: dict | None,  # 事件载荷，具体结构见下文
    is_complete: bool,     # 是否为流终止帧
)
```

### 下行事件速查表

| # | event_type | 分类 | payload 必填字段 | payload 可选字段 | 响应方式 | 产生来源 |
|---|---|---|---|---|---|---|
| 1 | `chat.delta` | 流式内容 | `content: str` | `task_id` | 流式 chunk | `llm_output` / `content_chunk` / `accumulated_text` flush / fallthrough 降级 |
| 2 | `chat.reasoning` | 流式内容 | `content: str` | `task_id` | 流式 chunk | `llm_reasoning`（**前端无 handler，会丢弃**） |
| 3 | `chat.final` | 流式内容 | `content: str` | `task_id` | 流式 chunk | `answer` chunk / 流末尾 `accumulated_text` flush |
| 4 | `chat.error` | 流式内容 | `error: str` | — | 流式 chunk | 异常捕获 / `controller_output(task_failed)` / `error` chunk |
| 5 | `chat.usage_metadata` | 元数据 | `metadata: dict` | `session_id` | 流式 chunk | `llm_usage` |
| 6 | `chat.tool_calls.delta` | 工具调用 | `tool_calls: list` | `source`, `task_id` | 流式 chunk | `tool_calls.delta` |
| 7 | `chat.tool_call` | 工具调用 | `tool_call: dict` | `task_id` | 流式 chunk | `tool_call` |
| 8 | `chat.tool_update` | 工具调用 | *(动态展开)* | `content`, `task_id`, ... | 流式 chunk | `tool_update` |
| 9 | `chat.tool_result` | 工具调用 | `result: str` | `tool_name`, `tool_call_id`, `raw_output`, `task_id` | 流式 chunk | `tool_result` |
| 10 | `chat.ask_user_question` | 用户交互 | `request_id`, `questions`, `expires_at_ms`, `timeout_sec` | `source`, `session_id` | 流式 chunk 或直接推送 | `ask_user_question_tool` / `__interaction__` |
| 11 | `todo.updated` | 任务管理 | `todos: list` | — | 流式 chunk | `todo.updated` |
| 12 | `context.compressed` | 元数据 | `rate: float` | `before_compressed`, `after_compressed` | 流式 chunk | `context.compressed` |
| 13 | `task.start` | 任务管理 | `task_id: str` | `task_content`, `task_index`, `total_tasks`, `parent_request_id`, `timestamp` | 流式 chunk | `task.start` |
| 14 | `task.complete` | 任务管理 | `task_id: str` | `task_content`, `status`, `duration_ms`, `error`, `timestamp` | 流式 chunk | `task.complete` |
| 15 | *(流终止帧)* | 控制 | `payload=None` | — | `is_complete=True` | 流正常结束 |
| 16 | `chat.interrupt_result` | 控制 | `intent`, `success`, `message` | `todos` | 非流式 AgentResponse | `process_interrupt()` |

### 上行请求速查表

| # | 前端方法 | ReqMethod | adapter 方法 | 核心字段 | 响应方式 |
|---|---|---|---|---|---|
| 1 | `chat.send` | `CHAT_SEND` | `process_message_stream_impl` | `session_id`, `query` | 流式 |
| 2 | `skilldev.chat` | `SKILLDEV_CHAT` | `handle_skilldev_chat_stream` | `session_id`, `message`, `task_id?`, `files?`, `skill_packages?`, `tool_spec_files?` | 流式 |
| 3 | `chat.interrupt` | `CHAT_CANCEL` | `process_interrupt` | `session_id`, `intent` | 非流式 |
| 4 | `chat.user_answer` | `CHAT_ANSWER` | `handle_user_answer` | `session_id`, `request_id`, `answers`, `source?` | 非流式 |

---

以下列出每种 `payload` 的完整字段结构。标注 `?` 的字段为可选。

---

### 1. `chat.delta` — 实时文本流

前端收到后逐字追加显示。

```python
{
    "event_type": "chat.delta",
    "content": str,        # 本次增量文本
    "task_id": str,        # ? 当前子任务 ID（TaskExecutionRail 提供）
}
```

**产生路径**：
- `process_message_stream_impl`: `llm_output` / `content_chunk` 类型的 chunk → 直接发出
- `process_message_stream_impl`: `accumulated_text` 缓冲 flush 时发出
- `_parse_stream_chunk`: fallthrough 分支中含 `content`/`output` 的未知 payload → 降级为 delta
- `_parse_stream_chunk`: chunk 为纯 dict 且含 `output` 字段 → 降级为 delta
- `_parse_stream_chunk`: chunk 为其他非 None 类型 → `str(chunk)` 降级为 delta

---

### 2. `chat.reasoning` — LLM 推理过程

> **注意**：前端当前无对应 handler，此事件会被丢弃。

```python
{
    "event_type": "chat.reasoning",
    "content": str,        # 推理内容片段
    "task_id": str,        # ? 当前子任务 ID
}
```

**产生路径**：
- `process_message_stream_impl`: `llm_reasoning` 类型的 chunk
- `_parse_stream_chunk`: `llm_reasoning` 类型的 chunk

---

### 3. `chat.final` — 流式完成帧

标记回复完成，前端停止流式渲染。

```python
{
    "event_type": "chat.final",
    "content": str,        # 最终内容（如已通过 delta 流式发送过则为空字符串 ""）
    "task_id": str,        # ? 当前子任务 ID
}
```

**产生路径**：
- `process_message_stream_impl`: 流结束时若 `accumulated_text` 非空，作为 final 发出
- `_parse_stream_chunk`: `answer` 类型的 chunk
  - 若 `has_streamed_content == True` → `content` 为 `""`
  - 若 `has_streamed_content == False` → `content` 为从 answer payload 中提取的文本

---

### 4. `chat.error` — 错误信息

```python
{
    "event_type": "chat.error",
    "error": str,          # 错误描述文本
}
```

**产生路径**：
- `process_message_stream_impl`: 捕获异常 → `str(exc)`
- `_parse_stream_chunk`: `controller_output` 中 `task_failed`
- `_parse_stream_chunk`: `error` 类型的 chunk
- `_parse_stream_chunk`: chunk 为 dict 且 `result_type == "error"` → `chunk["output"]`

---

### 5. `chat.usage_metadata` — LLM Token 用量

```python
{
    "event_type": "chat.usage_metadata",
    "metadata": dict,      # LLM 使用量数据（prompt_tokens, completion_tokens 等）
    "session_id": str,     # ? 仅 process_message_stream_impl 直接产生时携带
}
```

**产生路径**：
- `process_message_stream_impl`: `llm_usage` 类型的 chunk（带 `session_id`）
- `_parse_stream_chunk`: `llm_usage` 类型的 chunk（不带 `session_id`）

---

### 6. `chat.tool_calls.delta` — 工具调用参数流式增量

```python
{
    "event_type": "chat.tool_calls.delta",
    "tool_calls": list,    # 工具调用增量列表，经 tool_calls_payload_to_json_list() 序列化
                           # 每项格式: {"id": str, "function": {"name": str, "arguments": str}}
    "source": str,         # ? 调用来源标识（仅当原始 payload 含 source 时）
    "task_id": str,        # ? 当前子任务 ID
}
```

---

### 7. `chat.tool_call` — 完整工具调用信息

```python
{
    "event_type": "chat.tool_call",
    "tool_call": dict,     # 完整工具调用对象
                           # 典型结构: {"id": str, "function": {"name": str, "arguments": str}, ...}
    "task_id": str,        # ? 当前子任务 ID
}
```

---

### 8. `chat.tool_update` — 工具执行进度更新

```python
{
    "event_type": "chat.tool_update",
    # 以下字段来自原始 tool_update payload，动态展开:
    "content": str,        # ? 进度文本
    # ... 其余为 payload 中的任意键值
    "task_id": str,        # ? 当前子任务 ID
}
```

---

### 9. `chat.tool_result` — 工具执行结果

```python
{
    "event_type": "chat.tool_result",
    "result": str,         # 工具执行结果（字符串化）
    "tool_name": str,      # ? 工具名称
    "tool_call_id": str,   # ? 对应的 tool_call ID
    "raw_output": Any,     # ? 原始输出（未字符串化）
    "task_id": str,        # ? 当前子任务 ID
}
```

---

### 10. `chat.ask_user_question` — 向用户发起结构化提问

> 有两条产生路径：**ask_user_question_tool 直接推送**（不经过流式 chunk）和 **_parse_stream_chunk 转发**。

#### 路径 A：ask_user_question_tool 直接通过 WebSocket 推送

```python
{
    "event_type": "chat.ask_user_question",
    "request_id": str,         # 关联 ID（格式: "ask_<uuid>"），用于匹配用户回复
    "source": "ask_tool",      # 固定为 "ask_tool"
    "questions": [             # 问题列表（1-4 项）
        {
            "question": str,       # 问题文本
            "options": [           # 选项列表（2-4 项）
                {
                    "label": str,          # 选项标签（必填）
                    "description": str,    # ? 选项描述
                }
            ],
            "multi_select": bool,  # 是否多选
            "header": str,         # ? 问题标题
        }
    ],
    "expires_at_ms": int,      # 超时截止时间戳（毫秒）
    "timeout_sec": float,      # 超时秒数
    "session_id": str,         # ? 会话 ID
}
```

#### 路径 B：_parse_stream_chunk 中 chunk_type == "chat.ask_user_question"

```python
{
    "event_type": "chat.ask_user_question",
    # ... 原始 payload 中的所有字段（结构同路径 A）
}
```

#### 路径 C：_parse_stream_chunk 中 chunk_type == "__interaction__"

通过 `convert_interactions_to_ask_user_question()` 转换，输出结构与路径 A 类似。

---

### 11. `todo.updated` — Todo 列表更新

```python
{
    "event_type": "todo.updated",
    "todos": [             # 完整 todo 列表
        {
            "id": str,         # todo ID
            "content": str,    # todo 内容
            "activeForm": str, # 当前显示文本
            "status": str,     # "pending" | "in_progress" | "completed"
        }
    ],
}
```

---

### 12. `context.compressed` — 上下文压缩通知

```python
{
    "event_type": "context.compressed",
    "rate": float,                 # 压缩率
    "before_compressed": int|None, # ? 压缩前 token 数
    "after_compressed": int|None,  # ? 压缩后 token 数
}
```

---

### 13. `task.start` — 子任务开始

```python
{
    "event_type": "task.start",
    "task_id": str,                # 子任务 ID
    "task_content": str,           # ? 任务描述
    "task_index": int,             # ? 任务序号（从 0 开始）
    "total_tasks": int,            # ? 总任务数
    "parent_request_id": str,      # ? 父请求 ID
    "timestamp": float,            # ? 时间戳
}
```

---

### 14. `task.complete` — 子任务完成

```python
{
    "event_type": "task.complete",
    "task_id": str,                # 子任务 ID
    "task_content": str,           # ? 任务描述
    "status": str,                 # ? 完成状态（如 "success" / "failed"）
    "duration_ms": int,            # ? 执行耗时（毫秒）
    "error": str,                  # ? 错误信息（失败时）
    "timestamp": float,            # ? 时间戳
}
```

---

### 15. 流终止帧

标志整个流结束，payload 为 None。

```python
AgentResponseChunk(
    request_id=rid,
    channel_id=cid,
    payload=None,          # 无载荷
    is_complete=True,      # 标记流结束
)
```

---

### 16. `chat.interrupt_result` — 中断响应（非流式 AgentResponse）

```python
{
    "event_type": "chat.interrupt_result",
    "intent": str,         # "pause" | "resume" | "cancel" | "supplement"
    "success": True,       # 固定 True
    "message": str,        # "任务已暂停" | "任务已恢复" | "任务已取消" | "任务已切换"
    "todos": list,         # ? 仅 cancel 时且有待办项时携带，格式同 todo.updated 中的 todos
}
```

---

## 二、前端 → 后端（AgentRequest 上行请求）

前端通过 WebSocket 发送请求，经 Gateway 转发为 `AgentRequest`，由 `interface.py` 路由到 adapter 的不同方法。

### 请求路由表

| 前端请求方法 | ReqMethod | adapter 方法 | 响应方式 |
|---|---|---|---|
| `chat.send` | `CHAT_SEND` | `process_message_stream_impl` | 流式（AgentResponseChunk） |
| `skilldev.chat` | `SKILLDEV_CHAT` | `handle_skilldev_chat_stream` | 流式（AgentResponseChunk） |
| `chat.interrupt` | `CHAT_CANCEL` | `process_interrupt` | 非流式（AgentResponse） |
| `chat.user_answer` | `CHAT_ANSWER` | `handle_user_answer` | 非流式（AgentResponse） |

---

### 1. `chat.send` — 用户发消息

```typescript
{
  session_id: string,      // 会话 ID
  query: string,           // 用户输入文本
}
```

**后端响应**：流式 AgentResponseChunk（上述所有下行事件类型）

---

### 2. `skilldev.chat` — SkillDev 专用入口

```typescript
{
  session_id: string,
  task_id?: string,                                            // 任务 ID（不传则自动生成）
  message: string,                                             // 用户输入（也可用 query 字段）
  files?: [{filename: string, base64Data: string}],            // 参考文件（任意类型）
  skill_packages?: [{filename: string, base64Data: string}],   // 参考 Skill 包（.zip/.skill）
  tool_spec_files?: [{filename: string, base64Data: string}],  // 可用工具说明
}
```

**后端处理**：
1. 根据 `task_id` 创建独立工作区 `{base_workspace}/skilldev/{task_id}/`
2. 初始化子目录：`skill/`、`resources/ref-files/`、`resources/ref-skills/`、`resources/available-tools/`、`evals/`、`output/`
3. 将上传资源 base64 解码写入对应目录（zip/skill 文件自动解压）
4. 组装 `inputs` 并委托给 `process_message_stream_impl`

**后端响应**：流式 AgentResponseChunk

---

### 3. `chat.interrupt` — 中断操作

```typescript
{
  session_id: string,
  intent: 'pause' | 'resume' | 'cancel' | 'supplement',
}
```

不同 intent 的后端行为：

| intent | 行为 |
|---|---|
| `pause` | 暂停 StreamEventRail |
| `resume` | 恢复 StreamEventRail |
| `cancel` | 终止 Agent + 取消 AskUserQuestion 等待 + 取消未完成 Todos |
| `supplement` | 终止 Agent + 取消 AskUserQuestion 等待（不取消 Todos） |

**后端响应**：非流式 `chat.interrupt_result`（见上文 §16）

---

### 4. `chat.user_answer` — 回答结构化提问

```typescript
{
  session_id: string,
  request_id: string,              // 与下发问题的 ask_id 对应
  answers: UserAnswer[],           // 用户选择结果
  source?: string,                 // "ask_tool" 等
}

// UserAnswer 结构：
interface UserAnswer {
  selected_options: string[];      // 选中的选项 label 列表
}
```

**后端处理**：
1. 判断 `source == "ask_tool"` 或 `request_id` 以 `"ask_"` 开头
2. 调用 `AskUserQuestionRegistry.resolve(request_id, answers)` 解除 Future 阻塞
3. `ask_user_question_tool` 中的 `wait_for_answer` 返回，Agent 继续执行

**后端响应**：

```python
AgentResponse(
    ok=True,
    payload={
        "accepted": True,      # 固定 True
        "resolved": bool,      # 是否成功匹配到等待中的 Future
    }
)
```

---

## 三、交互流程图

```
前端                          Gateway                    SkillDevDeepAdapter
 │                              │                              │
 │── chat.send / skilldev.chat ─→│── AgentRequest ────────────→│
 │                              │                              │
 │                              │  ←── chat.delta ─────────────│  实时文本
 │                              │  ←── chat.reasoning ─────────│  推理过程（前端丢弃）
 │                              │  ←── chat.tool_calls.delta ──│  工具参数增量
 │                              │  ←── chat.tool_call ─────────│  工具调用
 │                              │  ←── chat.tool_update ───────│  工具进度
 │                              │  ←── chat.tool_result ───────│  工具结果
 │                              │  ←── todo.updated ───────────│  待办更新
 │                              │  ←── task.start ─────────────│  子任务开始
 │                              │  ←── task.complete ──────────│  子任务完成
 │                              │  ←── chat.usage_metadata ────│  Token 用量
 │                              │  ←── context.compressed ─────│  上下文压缩
 │                              │                              │
 │                              │  ←── chat.ask_user_question ─│  ask_tool 发起提问
 │←─ 渲染问题卡片 ──────────────│                              │
 │                              │                              │  (阻塞等待回答)
 │── chat.user_answer ─────────→│── AgentRequest(CHAT_ANSWER)→│
 │                              │                              │  (Registry.resolve → 继续)
 │                              │  ←── chat.delta ─────────────│  继续输出
 │                              │  ←── chat.final ─────────────│  完成帧
 │                              │  ←── {payload=None, done} ───│  流终止
 │                              │                              │
 │── chat.interrupt ───────────→│── AgentRequest(CHAT_CANCEL)→│
 │←── chat.interrupt_result ────│  ←── AgentResponse ──────────│
```

---

## 四、关键代码文件索引

| 文件 | 职责 |
|---|---|
| `agentserver/skilldev_agent/adapter.py` | SkillDevDeepAdapter 主体，流式处理、中断、用户回答 |
| `agentserver/tools/ask_user_question_tool.py` | ask_user_question 工具实现，推送问题并阻塞等待 |
| `agentserver/deep_agent/ask_user_question_registry.py` | 关联 ask_id → Future 的注册表 |
| `agentserver/interface.py` | Facade 层，路由 AgentRequest 到 adapter |
| `agentserver/agent_adapters.py` | AgentAdapter 协议定义 |
| `agentserver/stream_utils.py` | `tool_calls_payload_to_json_list()` 等工具函数 |
| `schema/message.py` | ReqMethod 枚举定义（CHAT_SEND / CHAT_ANSWER 等） |
| `web/src/hooks/useWebSocket.ts` | 前端事件监听与请求发送 |
| `web/src/services/webClient.ts` | 前端事件名映射 |
| `web/src/components/ChatPanel/InlineQuestionCard.tsx` | 问题卡片 UI 组件 |
| `web/src/stores/chatStore.ts` | pendingQuestion 状态管理 |
