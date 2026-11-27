# SkillDevDeepAdapter 前端显示与日志问题分析（第二版）

> 对比基准：`jiuwenclaw/agentserver/deep_agent/interface_deep.py` (`JiuWenClawDeepAdapter`)
> 问题对象：`jiuwenclaw/agentserver/skilldev_agent/adapter.py` (`SkillDevDeepAdapter`)
> 前端通道：**WebChannel**（`channel_id=web`），不是 VibeSkillChannel
> 日期：2026-05-14

---

## 现象描述

1. **前端内容无法实时展示**：后端日志可以看到大量 `chat.reasoning`、`chat.tool_call`、`chat.tool_result` 等 chunk 已经通过 WebSocket 发出，但前端对话区**没有文本内容出现**。
2. **工具执行可见，但一直在执行**：截图显示工具调用列表可见（todo_create、list_files 等），但部分工具状态卡在执行中。
3. **后端日志量大**：每个 chunk 都被 gateway 层逐条打 INFO 日志。

---

## 前端协议分析（关键前提）

当前走的是普通对话页（WebChannel），前端 `useWebSocket.ts` 只监听以下事件显示文字内容：

| 事件类型 | 前端行为 |
|----------|----------|
| `chat.delta` | 创建/追加流式 assistant 气泡 |
| `chat.final` | 结束流式气泡 / 创建完整 assistant 消息 |
| `chat.tool_call` | 添加工具调用卡片 |
| `chat.tool_result` | 更新工具卡片为完成态 |
| `chat.error` | 显示错误 |

**`chat.reasoning` 在普通 Web 前端没有任何 handler，会被完全忽略。**

---

## 根因分析

### 根因 1（P0，核心问题）：Agent 推理内容全部作为 `chat.reasoning` 发出，前端完全忽略

**这是"前端什么都不显示"的根本原因。**

从终端日志可以看到，后端实际发出了几千个 chunk，但绝大多数是 `chat.reasoning`，几乎没有 `chat.delta`。

原因是 DeepSeek 等模型在 ReAct 模式下，大量文字以 `llm_reasoning` chunk 类型产出（模型的"思考过程"），而实际可见正文 `llm_output` 很少。

`interface_deep.py` 的主循环对此有特殊处理（第 3764-3796 行）：**当收到 `llm_output` 时，会先把 `accumulated_reasoning` 作为 `chat.delta` flush 出去**，而不是作为 `chat.reasoning`。这样即使模型主要产出 reasoning，用户也能在前端看到内容。

```python
# interface_deep.py 第 3764-3796 行
if chunk_type == "llm_output":
    has_streamed_content = True
    if accumulated_reasoning:
        # 关键：reasoning 作为 chat.delta 发出！
        reasoning_payload = {"event_type": "chat.delta", "content": accumulated_reasoning}
        ...
        accumulated_reasoning = ""
    content = chunk.payload.get("content", "")
    delta_payload = {"event_type": "chat.delta", "content": content}
    yield AgentResponseChunk(...)
    continue
```

而且 `interface_deep.py` 在主循环中直接处理 `llm_output`、`llm_reasoning` 等 typed chunk（第 3688-3893 行），**不走 `_parse_stream_chunk`**。只有非 typed chunk 和 fallback 场景才进 `_parse_stream_chunk`。

`adapter.py` 则把**所有 chunk 都统一交给 `_parse_stream_chunk`**，其中 `llm_reasoning` 直接转为 `chat.reasoning`——前端不渲染。

### 根因 2（P0）：终止 chunk 格式错误

`adapter.py` 发出的终止 chunk 是：

```python
yield AgentResponseChunk(
    payload={"event_type": "chat.done"},
    is_complete=True,
)
```

而 `interface_deep.py` 发出的是：

```python
yield AgentResponseChunk(
    payload=None,
    is_complete=True,
)
```

`interface.py` 的 `process_message_stream` 通过检查 `data.is_complete` 来判断流式结束（第 943 行）。当 `is_complete=True` 但 payload 不为 None 时，流程层可能仍然尝试处理 payload dict，而 `chat.done` 不是前端认识的事件类型，会被忽略。更重要的是，**`interface.py` 有兜底逻辑**（第 1038-1044 行）：如果 adapter 没有发出过 `is_complete=True` 的 chunk，它会补发一个。现在 adapter 发了 `is_complete=True` 的 `chat.done`，导致兜底逻辑被跳过，但 `chat.done` 本身不是前端可理解的终止信号。

### 根因 3（P1）：主循环过于简化，缺少 `interface_deep.py` 的关键 flush 策略

`interface_deep.py` 的主循环（第 3684-3893 行）做了大量工作：

1. **`llm_reasoning` 单独处理**：直接 yield `chat.reasoning`（但也 accumulate 用于 flush）
2. **`llm_output` 触发 accumulated_reasoning flush**：把累积的 reasoning 作为 `chat.delta` 发出
3. **`answer` 触发 accumulated_text + accumulated_reasoning flush**：确保所有积攒的文本在 answer 前发出
4. **每个非 llm_output/llm_reasoning/answer 的 typed chunk 前都做 flush**：确保文本不积攒

`adapter.py` 完全没有这些策略——所有 chunk 都直接 parse 后 yield，没有 accumulate-and-flush 机制。

### 根因 4（P1，之前已修复）：`tool_result` 缺少 `tool_name`/`tool_call_id`

上一轮已修复。前端用这些字段将工具结果与调用配对。

### 根因 5（P2）：日志频率过高

这主要是因为 gateway 的 `agent_client.py` 对每个收到的 chunk 都打 INFO 日志（第 378 行和 381 行）。这是全局行为，`interface_deep.py` 也存在同样的日志量——只不过 `interface_deep.py` 产出的 chunk 数通常更少（因为有 accumulate 策略，多个小 delta 攒成一个大 delta 再发）。`adapter.py` 每个小 delta 都即时 yield，自然日志量更大。

---

## 问题总结

| 编号 | 问题 | 影响 | 优先级 |
|------|------|------|--------|
| 1 | `llm_reasoning` → `chat.reasoning`，前端不渲染 | **前端完全没有文本内容** | P0 |
| 2 | 终止 chunk 用 `chat.done` 而非 `payload=None` | 流式结束信号不规范 | P0 |
| 3 | 主循环缺少 accumulate-and-flush 策略 | 内容不实时 + 日志量大 | P1 |
| 4 | ~~`tool_result` 缺少元数据~~ | ~~工具卡住~~ | ~~已修复~~ |
| 5 | 日志频率（gateway 层全局行为） | 干扰排查 | P2 |

---

## 修复结果（2026-05-14）

已按 `interface_deep.py` 的流式主循环方式重写 `SkillDevDeepAdapter.process_message_stream_impl`：

| 修复项 | 状态 |
|--------|------|
| 将“统一 `_parse_stream_chunk`”改为按 typed chunk 分层处理 | 已完成 |
| `llm_reasoning` 即时发 `chat.reasoning`，并累积到 `accumulated_reasoning` | 已完成 |
| `llm_output` / `content_chunk` 到来时，先将 `accumulated_reasoning` 作为 `chat.delta` flush，再发送正文 `chat.delta` | 已完成 |
| `answer` 前 flush `accumulated_text` / `accumulated_reasoning`，再走 `_parse_stream_chunk` 生成 `chat.final` | 已完成 |
| 其他 typed chunk 前先 flush 缓冲，再转发工具、todo、context 等结构化事件 | 已完成 |
| 终止帧由 `{"event_type": "chat.done"}` 改为 `payload=None, is_complete=True` | 已完成 |
| 主循环增加 `asyncio.CancelledError` 与普通异常保护，异常时发 `chat.error` | 已完成 |

本次修复的目标是让 SkillDev Agent 与 `JiuWenClawDeepAdapter` 在 WebChannel 普通对话页上的流式语义保持一致，使前端能重新通过 `chat.delta` / `chat.final` 显示内容。

---

## 修复建议

### 建议 1（P0）：重写 `process_message_stream_impl` 主循环

**不能用"全部交给 `_parse_stream_chunk`"的简化模式。** 需要在主循环中像 `interface_deep.py` 一样对 typed chunk 分门别类处理：

```python
async def process_message_stream_impl(self, request, inputs):
    ...
    has_streamed_content = False
    accumulated_text = ""
    accumulated_reasoning = ""

    async for chunk in Runner.run_agent_streaming(self._instance, inputs):
        if not (hasattr(chunk, "type") and hasattr(chunk, "payload")):
            # 非 typed chunk → 走 _parse_stream_chunk
            parsed = self._parse_stream_chunk(chunk)
            if parsed is not None:
                # flush accumulated 内容
                yield from _flush_accumulated(...)
                yield AgentResponseChunk(payload=parsed, ...)
            continue

        chunk_type = chunk.type

        if chunk_type == "llm_reasoning":
            content = ...  # 提取 content
            yield AgentResponseChunk(payload={"event_type": "chat.reasoning", "content": content}, ...)
            accumulated_reasoning += content
            continue

        if chunk_type == "llm_output":
            has_streamed_content = True
            # 关键：把 accumulated_reasoning 作为 chat.delta flush
            if accumulated_reasoning:
                yield AgentResponseChunk(payload={"event_type": "chat.delta", "content": accumulated_reasoning}, ...)
                accumulated_reasoning = ""
            content = ...  # 提取 content
            yield AgentResponseChunk(payload={"event_type": "chat.delta", "content": content}, ...)
            continue

        if chunk_type == "answer":
            # flush all accumulated
            if accumulated_text:
                yield AgentResponseChunk(payload={"event_type": "chat.delta", "content": accumulated_text}, ...)
                accumulated_text = ""
            if accumulated_reasoning:
                yield AgentResponseChunk(payload={"event_type": "chat.reasoning", "content": accumulated_reasoning}, ...)
                accumulated_reasoning = ""
            parsed = self._parse_stream_chunk(chunk, has_streamed_content=has_streamed_content)
            if parsed:
                yield AgentResponseChunk(payload=parsed, ...)
            continue

        # 其他 typed chunk（tool_call, tool_result, etc.）
        # 先 flush accumulated
        if accumulated_text:
            yield AgentResponseChunk(payload={"event_type": "chat.delta", "content": accumulated_text}, ...)
            accumulated_text = ""
        if accumulated_reasoning:
            yield AgentResponseChunk(payload={"event_type": "chat.reasoning", "content": accumulated_reasoning}, ...)
            accumulated_reasoning = ""
        parsed = self._parse_stream_chunk(chunk, has_streamed_content=has_streamed_content)
        if parsed:
            yield AgentResponseChunk(payload=parsed, ...)

    # 流结束后 flush 剩余 accumulated
    if accumulated_text:
        yield AgentResponseChunk(payload={"event_type": "chat.final", "content": accumulated_text}, ...)
    if accumulated_reasoning:
        yield AgentResponseChunk(payload={"event_type": "chat.reasoning", "content": accumulated_reasoning}, ...)
```

核心要点：
- `llm_reasoning` 同时发 `chat.reasoning` 和 accumulate
- `llm_output` 到来时把 accumulated_reasoning 作为 **`chat.delta`** flush（这是让前端看到内容的关键）
- `answer` 前 flush 所有缓冲
- 其他 typed chunk（tool_call 等）前也 flush

### 建议 2（P0）：终止 chunk 改为 `payload=None, is_complete=True`

```python
yield AgentResponseChunk(
    request_id=request.request_id,
    channel_id=request.channel_id,
    payload=None,
    is_complete=True,
)
```

### 建议 3（P1）：异常处理

参照 `interface_deep.py`，用 try/except 包裹主循环，异常时 yield `chat.error`。
