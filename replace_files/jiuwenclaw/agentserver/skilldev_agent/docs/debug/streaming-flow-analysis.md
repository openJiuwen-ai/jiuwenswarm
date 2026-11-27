# SkillDevDeepAdapter 流式显示问题：完整交互流程分析

## 1. 完整数据流路径

### 1.1 请求入站路径

用户通过 Web 前端"对话"页面发送消息，完整路径如下：

```
前端 useWebSocket.ts
  └─ webClient.request('chat.send', {...})
      └─ WebSocket {type: 'req', method: 'chat.send', params: {...}}

WebChannel._handle_raw_message()  [web_channel.py:435]
  ├─ is_stream = False  (因为 chat.send 不在 stream_methods 中)
  ├─ 构造 Message(is_stream=False, req_method=CHAT_SEND)
  ├─ bus.publish_user_messages(message)  → DummyBus, 实际无操作
  └─ _chat_send handler [app_web_handlers.py:1246]
      └─ 立即回复 {type: 'res', ok: true, payload: {accepted: true, session_id}}

_normalize_gateway_message() [app_gateway.py:96-127]
  └─ 强制设置 is_stream = True (因为 method_val == ReqMethod.CHAT_SEND.value)

channel_manager.deliver_to_message_handler(normalized)
  └─ MessageHandler._forward_single() [message_handler.py]
      └─ agent_client.send_request(e2a_envelope)  → WebSocket 转发到 AgentServer
```

### 1.2 AgentServer 侧处理路径

```
AgentWebSocketServer._handle_request()  [agent_ws_server.py:492]
  └─ request.is_stream == True
      └─ _handle_stream(ws, request, send_lock)  [agent_ws_server.py:595]
          └─ async for chunk in agent_manager.process_message_stream(request):
              └─ 逐条 encode_agent_chunk_for_wire(chunk) → E2A JSON → ws.send()
```

### 1.3 interface.py process_message_stream() 分发

```python
# interface.py:778-791 - SKILLDEV_CHAT 路径 (不走此路径, 因为前端发的是 chat.send)
if request.req_method == ReqMethod.SKILLDEV_CHAT:
    async for chunk in adapter.handle_skilldev_chat_stream(request):
        yield chunk
    return

# interface.py:809-978 - 正常 CHAT 路径 (实际走此路径)
adapter = await self._ensure_adapter()
stream_queue = asyncio.Queue()
async def run_stream_task():
    async for chunk in adapter.process_message_stream_impl(request, inputs):
        await stream_queue.put(("chunk", chunk))
    ...
await self._session_manager.submit_task(session_id, run_stream_task)
while not stream_done.is_set() or not stream_queue.empty():
    item = await asyncio.wait_for(stream_queue.get(), timeout=0.1)
    ...
    yield data  # AgentResponseChunk
```

**关键：SkillDevDeepAdapter 通过 `process_message_stream_impl` 被调用，和 JiuWenClawDeepAdapter 走完全相同的 interface.py 代码路径。**

### 1.4 响应回传路径

```
AgentServer → E2A JSON chunk → WebSocket → Gateway

Gateway agent_client._message_receiver_loop()
  └─ MessageHandler._handle_stream_chunk()
      └─ MessageHandler._chunk_to_message(chunk, session_id, metadata)
          └─ Message(type="event", payload=chunk.payload, event_type=EventType(...))
      └─ publish_robot_messages(out)

ChannelManager.dispatch()
  └─ WebChannel.send(msg)  [web_channel.py:298]
      ├─ 确定 event_name (从 msg.event_type 或 msg.payload.event_type)
      ├─ 对于 chat.delta/chat.final/chat.reasoning:
      │   走 else 分支 → payload = {session_id, content}
      ├─ 对于 chat.tool_call/chat.tool_result 等:
      │   走 if 分支 → payload = {**msg.payload} (完整结构)
      └─ _broadcast({type: "event", event: event_name, payload})

前端 webClient.handleIncoming()
  └─ normalizeIncoming() → WsEvent
  └─ dispatchEvent() → 注册的 event handlers
```

### 1.5 前端事件处理

| 事件类型 | Handler | 行为 |
|---------|---------|------|
| `chat.delta` | useWebSocket.ts:651 | `appendStreamContent(content)` — 实时追加文字 |
| `chat.final` | useWebSocket.ts:703 | 完成消息，停止流式 |
| `chat.reasoning` | **无 handler** | **被完全丢弃** |
| `chat.tool_call` | useWebSocket.ts:841 | 添加工具调用卡片 |
| `chat.tool_result` | useWebSocket.ts:862 | 更新工具结果 |

## 2. 两个 Adapter 的关键差异

### 2.1 interface_deep.py (JiuWenClawDeepAdapter)

```python
# llm_reasoning → 只发 chat.reasoning, 不累积
if chunk_type == "llm_reasoning":
    yield AgentResponseChunk(payload={"event_type": "chat.reasoning", "content": content})
    continue
# accumulated_reasoning 从未被 += 赋值，始终为空字符串

# llm_output → 直接发 chat.delta
if chunk_type == "llm_output":
    has_streamed_content = True
    yield AgentResponseChunk(payload={"event_type": "chat.delta", "content": content})
    continue
```

### 2.2 adapter.py (SkillDevDeepAdapter)

```python
# llm_reasoning → 发 chat.reasoning + 累积到 accumulated_reasoning
if chunk_type == "llm_reasoning":
    accumulated_reasoning += content          # ← 额外的累积操作！
    yield _make_chunk(_add_task_id({"event_type": "chat.reasoning", "content": content}))
    continue

# llm_output → flush accumulated_reasoning 为 chat.delta + 发 chat.delta
if chunk_type in {"llm_output", "content_chunk"}:
    has_streamed_content = True
    if accumulated_reasoning:                 # ← 大量累积的 reasoning 一次性 flush
        yield _make_chunk(_add_task_id({"event_type": "chat.delta", "content": accumulated_reasoning}))
        accumulated_reasoning = ""
    yield _make_chunk(_add_task_id({"event_type": "chat.delta", "content": content}))
    continue

# answer → flush 剩余缓冲
# fallthrough → flush accumulated_reasoning 为 chat.reasoning (前端不渲染!)
```

## 3. 问题根因分析

### 3.1 核心问题

DeepSeek 模型在 ReAct 模式下的输出模式：

```
[大量 llm_reasoning chunks]  →  [tool_call]  →  [tool_result]  →  [大量 llm_reasoning]  →  [answer]
```

`llm_output` chunk 非常少（DeepSeek 主要用 reasoning 输出内容，而非 output）。

### 3.2 interface_deep.py 的行为

- reasoning 阶段：前端收到大量 `chat.reasoning` → **前端无 handler，全部丢弃** → 前端不显示
- `llm_output` 到来：前端收到 `chat.delta` → 前端渲染
- `answer` 到来：前端收到 `chat.final` → 前端渲染

**结论：interface_deep.py 在 reasoning 阶段也不显示内容，但 `llm_output` / `answer` 足够频繁，用户体感尚可。**

### 3.3 SkillDevDeepAdapter 的行为

- reasoning 阶段：前端收到大量 `chat.reasoning` → **前端无 handler，全部丢弃**
- 同时 `accumulated_reasoning` 持续增长
- 当 `llm_output` 到来：`accumulated_reasoning`（可能含数千字符）一次性作为 `chat.delta` flush → 前端突然显示大量内容
- 当 `tool_call` / `tool_result` 等到来（fallthrough 分支）：`accumulated_reasoning` 作为 `chat.reasoning` flush → **前端再次丢弃**
- 当 `answer` 到来：`accumulated_reasoning` 作为 `chat.reasoning` flush → **前端丢弃**

**结论：SkillDevDeepAdapter 的 accumulate-and-flush 策略导致了两个问题：**
1. **内容延迟显示**：大量 reasoning 被累积，只有等到 `llm_output` 才作为 `chat.delta` 一次性 flush
2. **内容丢失**：在 `answer` 和 fallthrough 分支中，accumulated_reasoning 被作为 `chat.reasoning` flush，**前端不渲染**

### 3.4 为什么 interface_deep.py "能正常显示"

interface_deep.py 没有 `accumulated_reasoning += content` 操作，所以：
1. 不存在内容延迟累积的问题
2. 每个 `llm_output` / `answer` 的内容即时作为 `chat.delta` / `chat.final` 发出
3. 虽然 reasoning 阶段也不显示，但 DeepSeek 在常规对话中 `llm_output` 更频繁

## 4. 修复方案

### 方案 A：去除 accumulated_reasoning，与 interface_deep.py 完全对齐

- 删除 `accumulated_reasoning` 变量
- `llm_reasoning` 只发 `chat.reasoning` 然后 continue（和 interface_deep.py 一致）
- `llm_output` 直接发 `chat.delta`
- 不做任何累积

**优点**：行为与 interface_deep.py 100% 一致，确保同样的前端表现。
**缺点**：reasoning 阶段仍然不显示内容（和 interface_deep.py 一样）。

### 方案 B：将 llm_reasoning 直接作为 chat.delta 发出

- `llm_reasoning` 直接作为 `chat.delta` 发出，不累积
- 前端实时渲染每个 reasoning token

**优点**：用户能实时看到 AI 思考过程。
**缺点**：与 interface_deep.py 行为不一致；reasoning 内容（通常含 `<think>` 标签等内部推理）可能不适合直接展示。

### 方案 C（推荐）：先对齐 interface_deep.py，再按需优化

1. **第一步**：完全对齐 interface_deep.py 的 streaming 行为，确保基线正确
2. **第二步**：如果 reasoning 阶段的延迟不可接受，再针对性地优化（如方案 B）

## 5. 具体修复清单

### 5.1 adapter.py process_message_stream_impl 修改

| 修改项 | 说明 |
|--------|------|
| 删除 `accumulated_reasoning` 变量 | 和 interface_deep.py 对齐 |
| `llm_reasoning` 分支：只发 `chat.reasoning` 然后 continue | 不再累积 |
| `llm_output` 分支：直接发 `chat.delta` | 不再 flush accumulated_reasoning |
| `answer` 分支：flush `accumulated_text`，然后走 `_parse_stream_chunk` | 和 interface_deep.py 对齐 |
| fallthrough 分支：flush `accumulated_text`，然后走 `_parse_stream_chunk` | 不再处理 accumulated_reasoning |

### 5.2 保留的正确部分

| 已有实现 | 状态 |
|----------|------|
| `_add_task_id` helper | 正确，保留 |
| `_make_chunk` helper | 正确，保留 |
| `_parse_stream_chunk` 方法 | 正确，保留 |
| try/except 保护 | 正确，保留 |
| 终止帧 `payload=None, is_complete=True` | 正确，保留 |
