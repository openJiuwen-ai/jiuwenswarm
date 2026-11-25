# Outbound Stream Mapper（AG-UI 示例）

这是 `OutboundStreamExtension` 的**外部扩展示例**：把 AgentServer 出站事件流里的 E2A 事件
翻译成 AG-UI 事件。它演示的是"用户自己实现映射关系"，不是核心内置能力。

设计文档：`jiuwenswarm/server/docs/features/F_01_outbound-event-stream-hook.md`

## 映射关系

| E2A `body.event_type` | 输出（AG-UI，raw 帧） |
|---|---|
| 本轮首帧 | `RUN_STARTED`（`threadId=session_id`，`runId=request_id`） |
| `chat.delta` | `TEXT_MESSAGE_START`（首次）→ `TEXT_MESSAGE_CONTENT` |
| `chat.reasoning` | `THINKING_START`（首次）→ `THINKING_TEXT_MESSAGE_CONTENT` |
| `chat.tool_call` | `TOOL_CALL_START` + `TOOL_CALL_ARGS` |
| `chat.tool_result` | `TOOL_CALL_END` + `TOOL_CALL_RESULT` |
| `chat.ask_user_question` | `CUSTOM`（`name="jiuwen.ask_user"`；AG-UI 无 HITL 原语） |
| `chat.final` | `TEXT_MESSAGE_END` / `THINKING_END`（如未闭合）+ `RUN_FINISHED` |
| `chat.error` / `status=failed` | `RUN_ERROR` |
| 其它（processing_status / todo / usage…） | 丢弃 |

AG-UI 事件名和字段会随协议版本变化，**以你实际接入的 AG-UI 版本为准**，改的是本文件
`map_frame` 里的分支，不需要动 jiuwenswarm 核心。

## 目录结构

```text
outbound_stream_mapper/
├── extension.yaml    # 扩展清单（id/name/version/依赖）
├── extension.py      # 映射实现：map_frame + on_open/on_error/on_close
└── README.md
```

## 启用

1. 把**父目录**（包含 `outbound_stream_mapper/` 的那一层）加入扩展搜索路径：

```yaml
# ~/.jiuwenswarm/config/config.yaml
extensions:
  extension_dirs: "D:/path/to/examples/extensions"
```

2. 把某个渠道映射到本扩展（渠道 = 请求里的 `channel_id`）：

```yaml
outbound_stream:
  channels:
    my-agui-client: agui      # agui 是 register_extensions 里登记的名字
```

3. 重启 AgentServer（配置当前不热更新）。

## 用哪个渠道？

**不要**把 `officeclaw` / `web` 配成本扩展：

- `officeclaw` 是 relay-claw 的渠道，它自己用 `e2aToLegacyFrame` 解析 E2A；本扩展输出的是
  raw JSON，relay-claw 会解析失败/丢帧。
- `web` 是内置 Web 前端，同样按 E2A 前端帧解析。

AG-UI 客户端应使用**独立的新渠道值**（例如 `my-agui-client`），由客户端请求时携带。
外部客户端仍需按 AgentServer 现有 WS/HTTP 请求格式发送 `chat.send`（本扩展只负责出口）。

## 行为边界（核心约定）

- **只有流式 `chat.*` 请求**会进入本扩展；`session.create` / `history.get` 等一元响应原样透传。
- `map_frame` 必须同步、快速返回（建议 < 1ms）；一帧可以返回 0..N 帧。
- **终帧由扩展负责**：本示例在 `chat.final` 发 `RUN_FINISHED`，在异常时由 `on_error` 发
  `RUN_ERROR`。`on_close` 只做状态清理，不能再发帧。
- 扩展开启后如果 `map_frame` 失败：
  - 还没成功发出过改造帧 → 核心回落原生 E2A（客户端此前零字节，不会混协议）；
  - 已经发出过改造帧 → 核心调用 `on_error` 取终止帧后停流，不会混入 E2A。
- 扩展实例全局一个：`_runs` 按 `request_id` 分桶，`on_close` 必须清理，避免长连接泄漏。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest tests/unit_tests/extensions/test_outbound_stream_mapper_example.py -o addopts="--asyncio-mode=auto" -q
```

测试只加载 `extension.py` 并直接调用 `map_frame`，不启动 AgentServer。

## 自定义

- 换协议：改 `map_frame` 的分支与 `_raw(...)` 里的字段；返回 E2A 帧则保持原信封。
- 加状态（例如流式工具参数分片聚合）：在 `on_open` 初始化、`map_frame` 更新、`on_close` 清理。
- 需要读 jiuwenswarm 请求参数（如用户 prompt）：`ctx.request.params` / `ctx.params`。
