# 出站事件流改写钩子（AgentServer Outbound Event Stream Hook）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature（设计稿，未实现） |
| 日期 | 2026-11-24 |
| 状态 | 已实现（2026-11-25），待评审 |
| 范围 | `jiuwenswarm/server/transports/`、`jiuwenswarm/server/pipeline.py`、`jiuwenswarm/server/agent_http_server.py`、`jiuwenswarm/extensions/sdk/`、`jiuwenswarm/extensions/registry.py` |
| 测试基线 | `tests/transport/test_outbound_stream_hook.py`：28 passed / 2 skipped（pipeline 两条因本地 openjiuwen 版本跳过，CI 覆盖）；示例扩展 `tests/unit_tests/extensions/test_outbound_stream_mapper_example.py`：7 passed |
| Refs | #6967 |
| 关联 spec | `jiuwenswarm/server/docs/specs/S_02_request-pipeline-dispatch.md`（Sink 三大家族契约；实现阶段同步修订） |

## 背景

前端与 JiuwenSwarm 之间不是"一次请求一次响应"，而是持续事件流：`chat.send` 之后，AgentServer
按 token / 工具 / 状态增量产出 `chat.delta`、`chat.tool_call`、`chat.tool_result`、`chat.final`
等，经 `ResponseSink` 编码成 E2A wire 推给上游。

上游集成方（AG-UI 前端、relay-claw 这类本机客户端、或统一事件语义的中间层）需要：

1. 在**响应返回时**介入这条流；
2. **修改**单帧（改字段、合并、丢弃）；
3. **重定义**整条流（换事件语义、补生命周期帧、换 wire 形状），而不只是旁路观察。

现状没有出站事件钩子：`ExtensionRegistry` 的事件（`before_chat_request` 等）在请求前；
`hooks` 配置是命令/提示词形态的生命周期钩子，都不在逐帧事件路径上。

**落点已确认在 AgentServer**（2026-11-24）：实际上游 relay-claw 直连 AgentServer，不经过 Gateway，
挂 Gateway 的钩子对它不可见（依据见 §1）。本文只定 AgentServer 侧的钩子契约与挂点；
AG-UI 适配器由扩展实现，不在本仓库第一阶段交付内。

## 一、谁连 AgentServer：relay-claw 的调用路径

`D:\relay-claw` 拉起的是 **AgentServer**，不是 Gateway：

- 启动：`--desktop-run-agentserver`，或 `-m <agentserver module>`（`jiuwenswarm.server.app_agentserver`），
  注入 `AGENT_PORT` / `WEB_PORT`（`relayclaw-sidecar.ts`）。
- 连接：`ws://127.0.0.1:${agentPort}`（`relayclaw-sidecar.ts` 的 `resolvedUrl`；配置注释示例
  `ws://127.0.0.1:18092`，正是 AgentServer 的 CLI 默认端口），裸 WebSocket（`relayclaw-connection.ts`）。
- 上位请求：**legacy 扁平帧**（`request_id` / `channel_id` / `req_method` / `params` / `is_stream`），
  由 `wire_parse.parse_inbound` 解析；`channel_id` 默认 `officeclaw`。
- 下行响应：**E2A 帧**（`protocol_version` / `response_kind` = `e2a.chunk|e2a.complete|e2a.error` /
  `body.event_type` / `body.delta`），relay-claw 用 `e2aToLegacyFrame` 自己转成
  `RelayClawWsFrame`（`event_type` / `content` / `is_complete`）。

```text
relay-claw(API/Web/Desktop) --WS(legacy req / E2A resp)--> AgentServer
内置 Web 前端 --WS/SSE--> Gateway --E2A wire(WS)--> AgentServer
```

两者在 AgentServer 的同一个汇合点进入：`pipeline.dispatch_parsed_request`。因此钩子挂在这里，
对 relay-claw、Gateway、TUI、ACP 同时生效；这是选 AgentServer 而非 Gateway 的根本原因。

**E2A 是仓库内的统一协议，不等于 AG-UI。** E2A 定义的是 Gateway ↔ AgentServer ↔ relay-claw
之间的信封与事件词汇（`protocol_version: "1.0"`、`response_kind`、`body.event_type = chat.*`），
在仓库内是标准；AG-UI 是另一套面向 UI 的公开事件协议（`RUN_STARTED`、`TEXT_MESSAGE_CONTENT`、
`TOOL_CALL_*` 等），事件名、字段、传输约定都不同。扩展做的就是这两套标准之间的翻译，
不是"把不标准的东西变标准"。

## 二、现状链路（改代码前必须知道的出口）

```
agent_ws_server._handle_message / agent_http_server._dispatch_request
  → wire_parse.parse_inbound
  → RequestContext(request, sink=WSSink(ws, lock) / UnaryHTTPSink() / SSESink(), connection_id, services)
  → pipeline.dispatch_parsed_request(ctx, request)
      → HANDLERS / handlers._default._handle_stream
      → runtime/agent_adapter/interface_deep
      → ctx.sink.send_chunk(AgentResponseChunk) / send_unary(AgentResponse) / send_wire(wire)
          → encode_agent_chunk_for_wire / encode_agent_response_for_wire
          → WSSink: send_wire_payload(ws, wire)        # 连接级 send_lock
          → SSESink: queue → agent_http_server._pump_sse → SSE
          → UnaryHTTPSink: 记录 response / wire，路由层渲染
```

关键事实：

- `RequestContext` 是 **frozen dataclass**；sink 在 dispatch 前构造、按请求唯一。
- **三家族 sink 是唯一业务出口**（`transports/sink.py`）：`WSSink` / `UnaryHTTPSink` / `SSESink`，
  签名 `send_unary / send_chunk / send_error / send_wire`，新增传输必须实现这四个方法。
- `WSSink.send_chunk` 对 `chat.delta` / `chat.reasoning` 有**增量背压**（写缓冲超 32KiB 先让路）；
  `SSESink` 有有界队列与 `finish()` 哨兵；`UnaryHTTPSink` 记录 `response/wire/frames`。
- **不走 `ctx.sink` 的旁路**（第一阶段不覆盖）：
  - `agent_ws_server._handle_message` 的解析错误帧、`sandbox.*` 配置响应；
  - `PersonalContext` 的 `ws_handler` 直接 `send_wire_payload`；
  - 服务端主动推送 `send_push` → `PushRegistry` → `_GatewayWSPushSink`（无请求 ctx）。

## 三、目标与非目标

**目标**

- 进程内、可注册的**出站 E2A 事件流拦截 SPI**，作用于 AgentServer 的所有请求级 sink 出口。
- 扩展可对每帧：放行、改字段、丢弃、一拆多、合成新帧、输出**原始帧**。
- 默认零变化：未配置/未命中时，帧与今天逐字节一致，且不额外编码。
- 扩展故障不影响正常用户：原帧照发（fail-open），单请求错误预算耗尽后停用。

**非目标（第一阶段）**

- 不实现 AG-UI 事件映射（扩展做），不新增 AG-UI 专用端点。
- 不覆盖 `send_push` 主动推送、解析错误、PersonalContext、sandbox 配置帧。
- 不改 AgentServer 对 Gateway 的 E2A 语义、不改 Agent 运行时。
- 不支持运行时热加载扩展代码（仍是启动时加载扩展）。

## 四、方案

### 4.1 挂点：`pipeline.dispatch_parsed_request` 包装 `ctx.sink`（D1）

```python
# pipeline.dispatch_parsed_request 开头
sink = ctx.sink
ext = resolve_outbound_stream(request, sink)   # 按 channel 查配置/注册表；无命中返回 None
if ext is not None:
    ext.on_open(StreamMapContext(...))
    ctx = dataclasses.replace(ctx, sink=InterceptingSink(sink, ext))
# ... 原有流程 ...
# finally: ext.on_close(...)
```

选择在流水线而不是 sink 内部：

1. **一处覆盖三家族**：WS / HTTP-SSE / HTTP-unary 的 handler 都拿 `ctx.sink`，包装后全部生效。
2. **保留 sink 原行为**：`InterceptingSink` 对"未改动"的帧走**原方法**（`send_chunk` 原对象、
   `send_unary` 原对象），后端背压、队列、响应记录逻辑不变；只有被改写的帧才走 `send_wire`。
3. **有请求上下文**：选择、`on_open/on_close`、错误预算都按请求，不需要改 sink 构造点
   （`agent_ws_server`、`dispatch._default_context`、`agent_http_server._make_ctx` 都不动）。
4. **不碰 `send_push` 与旁路**：它们没有 `RequestContext`，天然不在覆盖范围，边界清晰。

`InterceptingSink` 的 fast path（默认路径）：

```python
async def send_chunk(self, chunk, *, sequence, response_id=None):
    if ext is None:
        return await inner.send_chunk(chunk, sequence=sequence, response_id=response_id)
    wire = encode_agent_chunk_for_wire(chunk, response_id=..., sequence=sequence)
    out = ext.map_frame(wire, ctx)        # list[dict]；失败 → [wire]
    if len(out) == 1 and out[0] is wire:
        return await inner.send_chunk(chunk, sequence=sequence, response_id=response_id)
    return await self._send_outputs(out)
```

`send_wire` 同构（未改动 → `inner.send_wire(wire)`）；`send_unary` 同理；`send_error` 默认透传。

代价：命中扩展后，`send_chunk` 会先编码一次 wire 给扩展看，原 sink 方法再编码一次（背压语义
优先于省这一次编码）；未命中时没有这项开销，也不改变任何既有行为。

### 4.2 选择与作用域（D2）

按**请求的 `request.channel_id`** 选择，配置在 `config.yaml`：

```yaml
outbound_stream:
  channels:
    my-agui-client: ag-ui        # 该 channel 的请求走扩展；值 = 已注册扩展名
  # 未列出的 channel 一律 native
```

- 解析在 `dispatch_parsed_request` 入口做一次，结果固定在本次请求。
- `<name>` 只在**已注册**的扩展里解析；未知名字 → 一条 warning + 回落 native，不报错。
- 未命中时 `resolve_outbound_stream` 返回 `None`，`ctx.sink` 原样，全链路不创建实例。
- 同一连接上的不同请求可命中不同扩展（e.g. `officeclaw` 与 `web` 并存）。
- 不提供客户端自选（query/header/metadata）开关：选择面只来自服务端配置，避免上游自行切换
  流形状后与自己的解析器不匹配。需要按请求切换时走配置拆分 channel。

### 4.3 扩展接口（D3/D4）

放置：`jiuwenswarm/extensions/sdk/outbound_stream.py`（新文件）。用户按仓库现有扩展机制实现：
`extension.yaml` + `extension.py`，在 `register_extensions(registry)` 里注册；核心通过
`ExtensionRegistry` 查找。**核心只定义接口和调用时机，不内置任何映射。**

```python
@dataclass(frozen=True)
class StreamMapContext:
    transport: str          # "ws" | "http-sse" | "http-unary"
    channel_id: str
    request_id: str
    session_id: str | None
    connection_id: str
    request: Any            # AgentRequest，只读；可读 params["query"] 等


class OutboundStreamExtension(BaseExtension):
    """用户实现：把 jiuwenswarm 的 E2A 事件映射成自己的协议事件。"""

    @abstractmethod
    def map_frame(
        self, frame: dict[str, Any], ctx: StreamMapContext
    ) -> Iterable[dict[str, Any]]:
        """一帧进，0..N 帧出。同步、无 IO、按到达顺序调用。"""

    def on_open(self, ctx: StreamMapContext) -> None:
        """本次请求命中后调用一次；可选，默认无操作。"""

    def on_close(self, ctx: StreamMapContext) -> None:
        """请求结束（含取消）时调用；可选，默认无操作。"""
```

`frame` 就是 relay-claw 看到的那一帧：`{request_id, sequence, is_final, status, response_kind,
body:{event_type, delta|content, ...}, channel, metadata}`。用户据此把 `body.event_type`
映射成自己的事件。

返回值元素三选一：

1. **E2A 帧**：原样返回或改字段后的 wire dict —— 走 `inner.send_wire` / 原方法；
2. **原始帧**：`{"type": "raw", "data": <任意 JSON>}` —— 绕过 E2A 编码：
   - `data` 必须**可 JSON 序列化**、且 `data` 字段必须存在；序列化由核心的 sink 负责，
     因此核心在发送前用 `json.dumps` 探测，不可序列化按"返回非法"降级（见 §4.4），
     不允许把 `TypeError` 漏到传输层；
   - WS：`json.dumps(data)` 文本帧；
   - HTTP/SSE：`data: <json>`（不带 `event:` 行，符合 AG-UI 等只认 `data` 的客户端）；
   - HTTP/unary：不支持，丢弃并 warning；
3. **空列表**：丢弃该帧。

约束：

- `map_frame` **同步**，不 `await`、不阻塞、不抛预期内异常；单帧预算建议 < 1ms。写路径上的异步会
  破坏每连接顺序，也会把扩展延迟传导到背压。重活放在 `initialize`/`on_open` 或后台任务。
- 扩展实例全局一个（`register_extensions` 时创建），`map_frame` 不得持有跨请求可变状态；
  需要按请求保存 run 态时用 `ctx.request_id` 分桶，并在 `on_close` 清理。
- 一帧拆多帧时，输出顺序即发送顺序；核心不重排。
- 入参按只读处理；扩展返回新对象，核心不回收入参修改。
- `sequence` 单调性由原有编码保证；扩展合成帧若不改 `sequence`，下游只按到达顺序消费。

为支持原始帧，`ResponseSink` 协议新增第五个方法（三家族 + `_GatewayWSPushSink` 都实现；
`UnaryHTTPSink` 明确返回 `False`）：

```python
async def send_raw(self, data: Any) -> bool: ...
```

`WSSink.send_raw` 在 `send_lock` 下写 JSON 文本；`SSESink.send_raw` 入队
`{"type": "raw", "data": ...}`，`agent_http_server._pump_sse` 识别后产出
`{"data": json.dumps(data)}`（无 `event` 键）；`UnaryHTTPSink.send_raw` 返回 `False`。

### 4.4 失败隔离（D5）

| 情况 | 行为 |
|---|---|
| `on_open` 抛错 | 记 warning，本次请求回落 native，不再调用该扩展 |
| `map_frame` 抛错 / 返回非法，且**尚未成功发出改造帧** | 记 warning，**发送原始帧**（回落原生；客户端此前零字节，不会混协议） |
| `map_frame` 抛错 / 返回非法，且**已成功发出过改造帧** | 记 warning，调用 `on_error(ctx, error)` 取终止帧后停流；不回落原生、不混协议 |
| 扩展返回 `raw` 但 transport 不支持 | 丢弃该帧并 warning |
| 扩展返回 `raw` 但 `data` 不可 JSON 序列化 / 缺 `data` 字段 | 按"返回非法"降级：未 engaged 回落原生；已 engaged 走 `on_error` 终止帧后停流。序列化失败不得外溢到 sink / SSE 泵 |
| `on_close` 抛错 | `InterceptingSink.close()` 内部吞掉并 warning，pipeline 再兜一层；不影响请求收尾 |

判定"已进入扩展模式"的准则是**改造帧成功送达**，而不是 `map_frame` 返回过非原样结果：
扩展返回 `[]`（丢帧/聚合缓冲）或发送失败时客户端仍是零字节，此时后续失败允许回落原生。

不因扩展故障断开连接、不重试、不影响其它请求；与仓库"观测类逻辑 fail-open"口径一致。
`CancelledError` 不吞：`finally` 里 `on_close` 后按原语义传播。

### 4.5 覆盖矩阵

| 路径 | 是否经 `map_frame` | 说明 |
|---|---|---|
| WS `ctx.sink.send_chunk` / `send_wire`（chat 流） | 是 | relay-claw、Gateway、TUI 的主链路 |
| WS `ctx.sink.send_unary` / `send_error` | `send_unary` 是；`send_error` 默认透传 | 一元响应不是事件流，扩展可原样返回 |
| HTTP/SSE `/chat`、`/e2a` 流 | 是 | `SSESink` 唯一出口 |
| HTTP unary `invoke_unary` | 是 | 只支持 E2A 改写；`raw` 不支持 |
| `send_push` / `PushRegistry` 主动推送 | 否 | 无请求 ctx；后续可按订阅者 channel 扩展 |
| 解析错误帧 / `sandbox.*` 配置 / PersonalContext | 否 | 旁路 `send_wire_payload`，无 ctx |
| Gateway 自有的历史回放 / 心跳 | 否 | 不经 AgentServer 事件流 |
| IM / ACP / A2A / TUI | 是（按 channel） | 默认 native；要把它们的 channel 配进映射才生效 |

### 4.6 注册方式

```python
# extension.py
from jiuwenswarm.extensions.sdk.outbound_stream import OutboundStreamExtension


class AgUiStreamExtension(OutboundStreamExtension):
    async def initialize(self, config) -> None: ...   # loader 不调用；保留以满足基类契约
    async def shutdown(self) -> None: ...             # 进程收尾时调用
    def map_frame(self, frame, ctx): ...


async def register_extensions(registry):
    ext = AgUiStreamExtension()
    registry.register_outbound_stream_extension("ag-ui", ext)
    return [ext]
```

```python
# registry
def register_outbound_stream_extension(
    self, name: str, extension: OutboundStreamExtension
) -> None: ...
def get_outbound_stream_extension(self, name: str) -> OutboundStreamExtension | None: ...
```

- 同名重复注册 → `ValueError`（与 `register_skill_source_extension` 一致）。
- `reset_instance()` 清空注册表，避免测试/热重启串实例。
- 当前 `ExtensionManager` 在加载扩展时只调用 `register_extensions`，**不调用 `initialize`**；
  初始化逻辑（读配置、建连接）放在 `register_extensions` 内，或首次 `map_frame` 时惰性执行。
  loader 在 `register_extensions` 返回后才 `set_extension_dir`，不要假设注册时已能读到扩展目录。
- `shutdown` 会在进程收尾（`shutdown_all_extensions`）时调用。
- 扩展包目录：默认搜索 `jiuwenswarm/extensions/<id>/`（每个子目录一个扩展），也可配
  `extensions.extension_dirs`（`;` 分隔；relay-claw 启动 sidecar 时透传的 `EXTENSION_DIRS`
  可用于把用户扩展随客户端分发）。
- 扩展可信性等同现有扩展：启动时加载的进程内代码，不引入新的网络面。

### 4.7 用户扩展示例（最小可用）

```yaml
# extension.yaml
id: ag-ui-stream
name: AG-UI Stream Mapper
version: 0.1.0
description: Map jiuwenswarm E2A events to AG-UI events
min_jiuwenswarm_version: "0.2.3"
dependencies: {}
config_schema:
  type: object
```

```python
# extension.py
from jiuwenswarm.extensions.sdk.outbound_stream import OutboundStreamExtension


class AgUiStreamExtension(OutboundStreamExtension):
    async def initialize(self, config) -> None:
        # loader 当前不调用 initialize；真正的初始化放在 register_extensions 或惰性执行
        pass

    async def shutdown(self) -> None:
        pass

    def map_frame(self, frame, ctx):
        body = frame.get("body") or {}
        event_type = body.get("event_type")

        if event_type == "chat.delta":
            return [{"type": "raw", "data": {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": ctx.request_id,
                "delta": body.get("delta", ""),
            }}]
        if event_type == "chat.final":
            return [{"type": "raw", "data": {
                "type": "RUN_FINISHED",
                "runId": ctx.request_id,
            }}]
        return []


async def register_extensions(registry):
    ext = AgUiStreamExtension()
    registry.register_outbound_stream_extension("ag-ui", ext)
    return [ext]
```

启用（选择一个不经过 relay-claw 的 channel）：

```yaml
outbound_stream:
  channels:
    my-agui-client: ag-ui
```

用户只需要实现 `map_frame`；`on_open` / `on_close` 默认无操作，只有在需要 per-request 状态
（如合成 `RUN_STARTED`、清理未闭合的 text message）时才覆盖。

**完整可运行示例**：`examples/extensions/outbound_stream_mapper/`（AG-UI 映射 + README +
`tests/unit_tests/extensions/test_outbound_stream_mapper_example.py`）。

### 4.8 模块调用流程（启动 / 请求 / 关闭）

```text
【启动期：扩展加载与注册】

app_agentserver.main()
  └─> extensions.manager.ExtensionManager(registry)
        └─> load_all_extensions()
              └─> extensions.loader.ExtensionLoader
                    ├─ 搜索目录：config extensions.extension_dirs + jiuwenswarm/extensions/
                    ├─ import 扩展目录下的 extension.py
                    └─> extension.register_extensions(registry)
                          └─> registry.register_outbound_stream_extension("ag-ui", ext)
                                └─ extensions.registry.ExtensionRegistry._outbound_stream: {name: ext}


【请求期：一帧的完整路径】

relay-claw / Gateway / TUI
  │  WS(legacy req) 或 HTTP
  v
server.agent_ws_server._handle_message / agent_http_server._dispatch_request
  │  wire_parse.parse_inbound()
  v
server.context.RequestContext(request, sink=WSSink|SSESink|UnaryHTTPSink, ...)
  │
  v
server.pipeline.dispatch_parsed_request(ctx, request)
  │
  ├─ 1. resolve_outbound_stream(request)
  │       ├─ common.config: outbound_stream.channels[request.channel_id] -> "ag-ui"
  │       └─ extensions.registry.get_outbound_stream_extension("ag-ui") -> ext
  │
  ├─ 2. ext.on_open(StreamMapContext(request_id, channel_id, session_id, ...))
  │
  ├─ 3. ctx = dataclasses.replace(ctx, sink=InterceptingSink(ctx.sink, ext))
  │
  ├─ 4. dispatch_with_context / handlers._default._handle_stream
  │       └─> runtime.agent_adapter.interface_deep -> openjiuwen Agent
  │             └─> ctx.sink.send_chunk(AgentResponseChunk)
  │                   │
  │                   v
  │             server.transports.intercept.InterceptingSink.send_chunk
  │                   ├─ common.e2a.wire_codec.encode_agent_chunk_for_wire() -> wire
  │                   ├─ ext.map_frame(wire, ctx)          <── 用户实现的映射
  │                   │     └─ 返回 list[E2A wire | raw | ...]
  │                   └─ 分发：
  │                         E2A wire -> inner.send_wire() / 原 sink 方法
  │                         raw      -> inner.send_raw()
  │                    │
  │                    v
  │             server.transports.sink
  │                   ├─ WSSink.send_wire   -> server.ws_send.send_wire_payload -> WS 客户端
  │                   ├─ WSSink.send_raw    -> ws.send(json.dumps(data))
  │                   ├─ SSESink.send_wire/send_raw -> queue
  │                   │        -> agent_http_server._pump_sse -> HTTP/SSE 客户端
  │                   └─ UnaryHTTPSink      -> response/wire，由路由层渲染（raw 不支持）
  │
  └─ finally: ext.on_close(StreamMapContext(...))   # 含取消/异常路径


【关闭期】

app_agentserver 收尾
  └─> ExtensionManager.shutdown_all_extensions()
        └─> ext.shutdown()
```

依赖方向（不能反向）：

```text
server.pipeline ──> server.transports.intercept ──> extensions.sdk.outbound_stream
      │                      │
      │                      └──> common.e2a.wire_codec
      └──> extensions.registry（只做按名查找，不 import 用户扩展模块）
```

要点：

- **用户扩展代码只被 `map_frame` 调用**，没有对 server/gateway 内部对象的直接依赖；入参是 wire
  dict + 只读 ctx。
- **选择面只有一处**：`outbound_stream.channels`（config）→ `ExtensionRegistry`（注册表）；
  两者都不命中就完全走原链路。
- **无法命中旁路**：`send_push`、解析错误帧、`sandbox.*`、PersonalContext 不经过
  `RequestContext`，因此不会调用 `map_frame`（见 §4.5）。

### 4.9 流程注解（给非开发读者）

先把术语翻译成人话：

| 词 | 人话解释 |
|---|---|
| AgentServer | 真正干活、运行 Agent 的进程（本设计的落点） |
| Gateway | 给内置网页前端用的中转；relay-claw 不经过它 |
| 扩展 | 用户自己写的插件；这次要提供的就是"插件的插槽" |
| 注册表 | "名字 → 插件"的登记簿，启动时登记一次 |
| E2A / wire | 系统与上游约定的"标准信封"；wire 是已经装好信封、准备发送的一帧 |
| sink / 回信通道 | 负责把结果送回客户端的组件（WS、SSE、HTTP 各一种）。**它不是事件名**，而是"发送出口"；事件名是 `chat.delta` 这类内容标签 |
| raw | 不装标准信封，插件想发什么 JSON 就发什么（给 AG-UI 这类客户端用） |

一遍完整对话，按时间顺序发生的事：

| # | 什么时候 | 发生了什么（人话） | 为什么要这样 |
|---|---|---|---|
| 1 | 进程启动 | 有个"扩展管理员"去扩展目录扫描，发现用户写的插件并读进来 | 插件是启动时加载的可信代码，不热加载 |
| 2 | 启动时 | 插件在登记簿上写下自己的名字（如 `ag-ui`），并交出自己的对象 | 之后按名字找插件，不用 import 用户代码 |
| 3 | 用户发消息 | relay-claw / 网页 / TUI 发来一条聊天请求，AgentServer 解析成统一格式 | 所有来源走同一个入口 |
| 4 | 请求进入流水线 | 看这条请求属于哪个渠道（`channel_id`，如 `officeclaw`、`web`） | 不同渠道可以有不同的输出格式 |
| 5 | 查配置 | 配置表里这个渠道配了哪个插件？没配就什么都不做，原样走 | 默认零变化，不影响现有用户 |
| 6 | 命中插件 | 从登记簿拿到插件对象，告诉它"这一轮开始了"（`on_open`） | 给插件机会做本轮初始化 |
| 7 | 包一层 | 在"回信通道"外面套一层拦截器：以后每条往外发的内容都先给插件看 | 只改出口，不碰 Agent 内部，所有渠道都覆盖 |
| 8 | Agent 干活 | Agent 生成一段段回复（文字、工具调用、最终结果），交给回信通道 | 和今天完全一样 |
| 9 | 拦截器加工 | 拦截器先把回复装进标准信封（wire），再调用插件的 `map_frame` | 插件拿到的是上游真正会收到的形状 |
| 10 | 插件决定 | 原样放行 / 改内容 / 丢掉 / 一条拆成几条 / 换成自己的格式（raw） | "映射关系"完全由用户实现 |
| 11 | 真正发送 | 拦截器按插件的结果发送：WS 直接发，SSE 排队发，HTTP 单次返回 | 传输细节仍由系统负责，插件不用管 |
| 12 | 一轮结束 | 不管成功、取消还是报错，都通知插件"这一轮结束了"（`on_close`） | 插件清理本轮状态，避免长连接泄漏 |
| 13 | 进程退出 | 扩展管理员依次通知插件关闭（`shutdown`） | 释放插件自己占用的资源 |

一句话总结：**插件只在第 9 步被调用一次（每帧一次），做"收到标准信封 → 决定发什么"这一件事；
第 7 步的"包一层"保证它不用改 Agent、也不用改每种传输。**

## 五、AG-UI 适配（扩展侧，不在本设计交付内）

核心只提供 §4.3 契约；扩展在 `map_frame` 里读取 `body.event_type` / `body.delta`，用 `raw` 帧输出
AG-UI 事件（事件名/字段以扩展锁定的 AG-UI 版本为准，§4.7 是最小示例）：

| E2A `body.event_type` | AG-UI 事件（示例） | 备注 |
|---|---|---|
| 某 `request_id` 的首帧 | `RUN_STARTED {threadId, runId}` | 扩展合成；`runId=request_id`，`threadId=session_id` |
| `chat.delta` | `TEXT_MESSAGE_START` → `TEXT_MESSAGE_CONTENT` → `TEXT_MESSAGE_END` | start 只发一次，end 在 final |
| `chat.reasoning` | `THINKING_START` / `THINKING_TEXT_MESSAGE_CONTENT` / `THINKING_END` | |
| `chat.tool_call` | `TOOL_CALL_START` + `TOOL_CALL_ARGS` | 工具名/参数在 `body` |
| `chat.tool_result` | `TOOL_CALL_RESULT` | |
| `chat.ask_user_question` | `CUSTOM`（如 `jiuwen.ask_user`） | AG-UI 无 HITL 原语 |
| `chat.final` | `TEXT_MESSAGE_END` + `RUN_FINISHED` | 清理 run 态 |
| `chat.error` / `status=failed` | `RUN_ERROR` | |
| `chat.processing_status` / `todo.*` / usage | 丢弃或 `CUSTOM` | 与 AG-UI 无语义对应 |

**兼容性警告（必须写进扩展文档）**：relay-claw 的 `e2aToLegacyFrame` 只认 E2A 帧。把
`officeclaw` channel 配成输出 `raw` 的 AG-UI 扩展，会让 relay-claw 的解析器失败/丢帧。
AG-UI 模式应配给**不经过 relay-claw 的 channel**（如新增专用 channel），或 relay-claw 侧同步加
AG-UI 分支。二者只能选一个，不能默认套在 `officeclaw` 上。

## 六、拒绝的方案

| 方案 | 拒绝理由 |
|---|---|
| **Gateway 网页出站层挂钩**（前一版设计） | relay-claw 直连 AgentServer，不经过 Gateway，钩子对它不可见；且 Gateway 只覆盖内置 Web 前端，覆盖面比 AgentServer 窄。 |
| **在每个 sink 类内部加钩子** | 三个 sink 各写一遍选择/失败/raw 逻辑，`send_chunk` 的背压分支还得复制；sink 契约被污染。包装层只依赖 `ResponseSink` 四方法 + 新增的 `send_raw`。 |
| **直接改 `send_wire_payload`** | 拿不到 `RequestContext`（channel/session/request_id），无法按请求选择；还会误伤旁路帧。 |
| **Agent 输出层（`interface_deep` chunk）** | 那里要自己再编码一次，且会改变 Agent 运行时的产出语义；wire 层信息等价、契约更稳。 |
| **config.yaml `command` 钩子（进程/脚本）** | 逐帧起进程延迟不可接受；同连接顺序无法保证。若上游是独立进程（relay-claw 自己实现），需要另设跨进程扩展形态，见未决问题。 |
| **直接把 AgentServer 对外事件流改成 AG-UI** | 破坏 relay-claw 与 Gateway 的 E2A 契约；应作为可选扩展而非默认。 |

## 七、兼容性、安全与可观测性

- **默认零变化**：未命中时 `ctx.sink` 原对象、不编码第二次；命中但扩展原样返回时走原 sink 方法。
- **顺序**：WS 由连接级 `send_lock` 串行；SSE 由队列/单生成器串行；`map_frame` 同步，不插队。
- **取消**：`on_close` 在 `finally` 执行；不吞 `CancelledError`（pipeline 既有铁律）。
- **持久化**：历史落盘/会话索引不经过帧，扩展丢帧不改已落库内容。
- **安全**：选择只来自服务端配置；不跳过任何鉴权；`raw` 帧由扩展自负责内容（核心不做脱敏，
  AgentServer 侧本就没有 Web 的敏感信息脱敏链路）。
- **观测**：命中时一条 info（channel → 扩展名）；首次 `map_frame` 失败 warning；停用 error；
  不做逐帧日志。

## 八、验证计划（设计阶段定义，开发阶段执行）

| # | 场景 | 通过条件 |
|---|---|---|
| 1 | 未配置 `outbound_stream` | 现有 `tests/unit_tests/server/` 全绿；WS/SSE 帧与基线字节一致 |
| 2 | channel 配 `demo`，扩展原样返回 | 帧内容不变，且 `WSSink.send_chunk` 背压分支仍生效 |
| 3 | 扩展改字段 / 丢帧 / 一拆多 | 客户端收到序列等于扩展返回序列，顺序稳定 |
| 4 | 扩展发 `raw` | WS 收到 JSON 文本；SSE 只有 `data:` 无 `event:`；unary 丢弃并 warning |
| 5 | 扩展 `map_frame` 抛错（未发出改造帧） | 原始帧仍送达；warning 带 channel/request_id |
| 6 | 扩展先丢帧/发送失败，再抛错 | 仍回落原生（客户端此前零字节），不静默空响应 |
| 7 | 已发出 raw/改造帧后抛错 | 发 `on_error` 终止帧并停流；不混入 E2A 原帧 |
| 8 | 纯 raw 流正常结束 | `close()` 不误报"缺终帧" |
| 9 | 同连接两个请求，不同 channel | 各自命中各自扩展，互不串状态 |
| 10 | 流式请求被 `chat.interrupt` 取消 | `on_close` 被调用，无任务泄漏，取消语义不变 |
| 11 | relay-claw channel 保持 native | E2A 帧与基线一致，`e2aToLegacyFrame` 正常 |
| 12 | `send_push` / 解析错误 / sandbox 帧 | 不进 `map_frame`，行为不变 |
| 13 | 扩展返回 raw 但 `data` 不可 JSON 序列化（或缺失 `data`） | 未 engaged 回落原生；已 engaged 发 `on_error` 终止帧后停流；WS/SSE 都不中断、无异常外溢 |

实现阶段至少覆盖 5~10 为单测；11/12 用既有测试兜底。

## 九、实施步骤（实现阶段，文件级）

1. `jiuwenswarm/extensions/sdk/outbound_stream.py`：`StreamMapContext` +
   `OutboundStreamExtension` 基类（`map_frame` 抽象 + `on_open/on_close` 默认空实现）。
2. `jiuwenswarm/extensions/registry.py`：注册/查找/清空；同名拒绝。
3. `jiuwenswarm/server/transports/intercept.py`（新）：`InterceptingSink`、
   `resolve_outbound_stream(request, sink)`、fail-open/错误预算、`raw` 分发。
4. `jiuwenswarm/server/transports/sink.py`：`ResponseSink` 增第五方法 `send_raw`；`WSSink` /
   `SSESink` 实现，`UnaryHTTPSink` 返回 `False`。
5. `jiuwenswarm/server/agent_ws_server.py`：`_GatewayWSPushSink.send_raw` 透传（保持协议完整）。
6. `jiuwenswarm/server/agent_http_server.py`：`_pump_sse` 识别 `{"type": "raw"}` 并只发 `data:`。
7. `jiuwenswarm/server/pipeline.py`：入口解析 + `dataclasses.replace(ctx, sink=...)`；
   `finally` 中 `on_close()`（含取消路径）。
8. `jiuwenswarm/common/config.py`：`outbound_stream.channels` 读取（含缓存失效口径）。
9. 测试：`tests/unit_tests/server/test_outbound_stream_hook.py`（按 §8）；fake sink/ws。
10. 文档：修订 `jiuwenswarm/server/docs/specs/S_02_request-pipeline-dispatch.md`（sink 契约 +
   流水线挂点）；按模块约定拆 `feat(server)` / `test(server)` / `docs(server)` 三连提交。

## 十、需要确认的问题

### P0（不定会返工）

1. **映射范围：该 channel 的所有帧，还是只映射流式 chat 事件？**
   - 现状风险：`session.create` / `history.get` / `permissions.*` 等一元响应也走 `send_unary`；
     示例映射对不认识的事件返回 `[]`，会把这些响应直接丢掉，渠道不可用。
   - 建议：核心只把 `response_kind in (e2a.chunk, e2a.complete)` 且请求 `is_stream=True`
     （或 `body.event_type` 以 `chat.` 开头）的帧交给扩展，其余原样透传。
   - 备选：契约要求扩展"不认识的帧必须原样返回"，但靠扩展自觉，容易踩坑。

2. **中途失败的协议一致性**：一旦扩展已把前几帧改成 raw（AG-UI），后续某帧失败时**不能**回落成
   E2A 原帧（客户端会收到两种协议混杂）。
   - 建议：本次请求进入"扩展模式"后锁定；`map_frame` 失败 → 终止本轮，发一个约定的终止帧
     （扩展的 `RUN_ERROR` 或核心发的 raw 终止帧），不回落 native；只有"尚未发出任何帧"时才允许回落。

3. **入站协议由谁负责**：AG-UI 只定义"事件流长什么样"，没定义客户端怎么发 `chat.send`。客户端目前
   仍要说 AgentServer 的 WS/HTTP 请求格式（legacy 扁平帧或 E2A 信封）。
   - A. AG-UI 客户端自己适配入站（本期只做出口映射）；
   - B. 另做 AG-UI 入站端点（`POST /agent` 收 `RunAgentInput`），本期不做，需要另立需求。

4. **channel 身份怎么定**：配置按 `channel_id` 选扩展，但 AG-UI 客户端得先有自己独立的 channel 值。
   - `relay-claw` 固定 `officeclaw`，`web` 是内置前端；raw 不能配给这两个（会破坏解析）。
   - 需要确认：新客户端用什么 channel 值（如 `agui`）、由谁下发；是否允许客户端自带 `channel_id`
     （有伪造/串格式风险，建议只认服务端配置或握手白名单）。

### P1（影响体验，需拍板）

5. **终帧兜底**：扩展若丢掉/改坏 `chat.final`（或 `is_final=true` 的帧），客户端可能永远等不到结束。
   建议核心在 `on_close` 时检查"本轮是否发过终帧"，没发则补一个约定的终止帧（或至少记 error）。
   终止帧长什么样需要和扩展约定。

6. **`sequence` / `is_final`**：扩展一拆多、合成帧后 `sequence` 是否由核心重编？建议核心不改，
   扩展负责；但 `is_final` 必须恰好一帧，否则客户端状态机坏。

7. **一元响应与 raw**：`UnaryHTTPSink.send_raw` 不支持；如果 AG-UI 客户端用 HTTP 单次请求拿不到流，
   是否接受只支持 WS / SSE 两种流式入口。

8. **主动推送**：`send_push`（cron、通知、权限卡片）不经过 `ctx.sink`，AG-UI 客户端收不到这些消息。
   本期是否接受？（建议接受并写进限制。）

9. **配置生效方式**：`outbound_stream.channels` 改动需要重启，还是走现有 config reload 热更新？
   建议第一期重启生效，写清楚。

### P2（实现细节，可后定）

10. **扩展分发与版本**：用户扩展放 `jiuwenswarm/extensions/<id>/` 或 `extensions.extension_dirs`；
    relay-claw 经 `EXTENSION_DIRS` 分发。需要确认是否随产品打包示例扩展、`min_jiuwenswarm_version`
    如何校验。
11. **多租户/企业版**：进程级配置会被所有租户共用；企业版是否需要按租户选择不同扩展？本期建议不做。
12. **性能**：命中扩展后每帧多一次编码（`send_chunk` 先编码给扩展看，原 sink 再编码一次）。
    不接受的话，需要改成"把 `AgentResponseChunk` 直接给扩展"，接口会变复杂。
13. **安全**：raw 帧内容由扩展负责，核心不做脱敏；channel 选择只来自服务端配置。是否需要审计
    raw 帧数量/大小。
14. **仓库内示例扩展**：已提供 `examples/extensions/outbound_stream_mapper/`（AG-UI 映射示例，
    含 README 与单测），作为用户自定义映射的起点。

## 与现有资料的关系

- 事件语义以 `jiuwenswarm/server/runtime/skill_turbo/docs/specs/S_08_event-stream-protocol.md`
  为准（工具/HITL 事件的前端契约）；本设计只包裹"写出"，不改事件语义。
- Sink 三大家族契约归 `jiuwenswarm/server/docs/specs/S_02_request-pipeline-dispatch.md`；
  新增 `send_raw` 必须同步该 spec。
- 扩展注册面沿用 `jiuwenswarm/extensions/registry.py`；不新增独立扩展加载器。

## 十一、实现记录（2026-11-25）

按"默认口径"落地，实际改动文件：

| 文件 | 改动 |
|---|---|
| `extensions/sdk/outbound_stream.py` | 新增 `StreamMapContext` / `OutboundStreamExtension`（`map_frame` 抽象 + `on_open`/`on_error`/`on_close` 默认实现） |
| `extensions/sdk/__init__.py` | 导出两个新符号 |
| `extensions/registry.py` | `register/get/list_outbound_stream_extension`；`reset_instance` 清空 |
| `server/transports/intercept.py` | 新增 `InterceptingSink` / `resolve_outbound_stream` / `detect_transport` |
| `server/transports/sink.py` | `ResponseSink` 增 `send_raw`；`WSSink`/`SSESink` 实现，`UnaryHTTPSink` 返回 `False` |
| `server/transports/__init__.py` | 未改（避免包级导入 `common.config`） |
| `server/agent_ws_server.py` | `_GatewayWSPushSink.send_raw` 透传 |
| `server/agent_http_server.py` | `_pump_sse` 识别 raw 帧，只发 `data:` |
| `server/pipeline.py` | 入口解析 + `dataclasses.replace(ctx, sink=...)`；`finally` 调 `close()` |
| `tests/transport/test_outbound_stream_hook.py` | 单测：注册 / 选择 / 改写 / raw / 失败降级 / 取消 / 接线 |
| `examples/extensions/outbound_stream_mapper/` | 外部扩展示例（AG-UI 映射）+ README |
| `tests/unit_tests/extensions/test_outbound_stream_mapper_example.py` | 示例扩展单测（7 例） |

实现与设计稿的差异（均有测试覆盖）：

1. **新增 `on_error`**：设计稿只写了"发终止帧"，实现把这部分交给扩展——
   `on_error(ctx, error) -> frames`，默认返回空（静默停流）。这样核心不需要猜扩展的协议。
2. **`send_error` 在扩展模式下抑制**：尚未进入扩展模式时原样发；已进入后原生错误帧会混协议，
   直接丢弃并记 warning。
3. **映射范围收敛**：`resolve_outbound_stream` 只对 `is_stream=True` 且
   `req_method ∈ {chat.send, chat.resume, chat.answer}` 的请求生效；`session.create` /
   `history.get` 等一元响应完全不经过扩展。
4. **raw 不套 send budget**：raw 是扩展自定义协议，核心只做 JSON 序列化；
   大小/内容由扩展负责（设计稿已列为待定项，实现按"扩展负责"）。
5. **扩展解析位置**：放在 `dispatch_parsed_request` 最前（早于 `_consume_runtime_approval_reply`），
   `on_close` 在外层 `finally`，取消路径也会执行。

验证结果：

- `tests/transport/test_outbound_stream_hook.py`：21 passed。
- `tests/transport`：1059 passed / 29 skipped，2 failed；两个失败在 `git stash` 基线同样失败
  （`test_table_covers_expected_method_set`、`test_query_params_reach_params`），与本次改动无关。
- `tests/unit_tests/extensions`：154 passed。
- `tests/unit_tests/server`（忽略环境缺模块的 `test_runtime_guard_config.py`）+
  `tests/unit_tests/agentserver/test_ws_send.py`：941 passed / 2 skipped，1 failed；
  失败项在基线同样失败（`test_adapter_history_read_uses_session_owned_tool_path_and_backend`）。

已知遗留（对应 §10）：`send_push` 与解析错误/PersonalContext 旁路不覆盖；扩展拆帧后
`sequence` 不重编；配置改动需重启。示例扩展见 `examples/extensions/outbound_stream_mapper/`。

评审修复（第二轮）：

- 「已进入扩展模式」改为**改造帧成功送达**后才置位；扩展返回 `[]`（丢帧/聚合缓冲）或发送
  失败时客户端仍是零字节，后续失败仍按契约回落原生。
- 删除不可达的「连续失败 ≥ 10 次」预算，统一为「首次失败即终止」（未 engaged）/
  「`on_error` 终止帧」（已 engaged），F_01 §4.4 与 §八 用例同步。
- WS 增量背压下沉到 `WSSink.send_wire`（扩展改写后的 `chat.delta` 也过闸门）；
  `WSSink.send_raw` 无条件过闸门。
- pipeline 的扩展接入整体 `try/except` 降级为原生；`on_close` 移到汇总/解绑之后并兜底，
  避免第三方异常掩盖 `CancelledError`。
- `StreamMapContext.channel_id` 与选路共用 `resolve_channel_id`（空渠道归一化为 `web`）。
- 纯 raw 流正常收尾不再误报「缺终帧」。

评审修复（第三轮）：

- `raw` 帧的 `data` 在 `_validate` 阶段做 JSON 可序列化探测（并校验 `data` 字段存在）。
  此前不可序列化的 `data` 会一路放到 `WSSink.send_raw` 的 `json.dumps` / SSE 泵里抛错，
  位于失败隔离之外：WS 请求直接异常收尾，SSE 流无声中断，与「扩展故障只降级」冲突。
  现在按「返回非法」走同一条 `_handle_failure` 路径（未 engaged 回落原生 / 已 engaged 走
  `on_error` 终止），F_01 §4.3 §4.4 §八 用例 13 同步。
- 探测即一次目标序列化，成本随 raw 帧大小线性增加（每帧多一次 `json.dumps`）；raw 帧
  通常是小事件，换取的是失败隔离对扩展契约违规的完整覆盖。

## 附录 A：设计稿代码（带注释）

> 以下是**设计稿**，不是已实现的代码。命名、文件位置以实现阶段为准；注释面向
> "看得懂流程、但不一定写代码"的读者，所以每段都先用一句话说"这段在干嘛"。
> A.3（核心拦截器）代码随评审演进较多，已改为指向实现文件，避免文档与代码漂移。

### A.1 扩展接口（`jiuwenswarm/extensions/sdk/outbound_stream.py`，新文件）

```python
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable

from jiuwenswarm.extensions.sdk.base import BaseExtension


@dataclass(frozen=True)
class StreamMapContext:
    """一次请求的"只读信息卡"，扩展按需取用，不能改。"""

    transport: str          # 这条流从哪种通道出去："ws" / "http-sse" / "http-unary"
    channel_id: str         # 请求来自哪个渠道，例如 "officeclaw"、"web"、"acp"
    request_id: str         # 本轮请求的唯一编号；扩展按它区分不同轮次
    session_id: str | None  # 会话编号；同一会话的多轮会复用它
    connection_id: str      # 连接编号；WS 用连接对象 id，HTTP 是固定常量
    request: Any            # 原始请求对象（只读）；可读 params["query"] 拿用户问题


class OutboundStreamExtension(BaseExtension):
    """用户实现的扩展基类：把 jiuwenswarm 的事件映射成自己的协议。

    用户只需要实现 map_frame；on_open / on_close 不写就用默认的空实现。
    initialize / shutdown 是 BaseExtension 要求的，保留为空方法即可。
    """

    @abstractmethod
    def map_frame(
        self, frame: dict[str, Any], ctx: StreamMapContext
    ) -> Iterable[dict[str, Any]]:
        """每收到一帧事件调用一次。必须同步、快速返回（建议 < 1ms）。

        frame 是系统原本要发出去的那一帧（标准信封），大致长这样：
            {
              "request_id": "...",
              "sequence": 3,                    # 第几帧，递增
              "is_final": False,                # 是否本轮最后一帧
              "response_kind": "e2a.chunk",     # chunk / complete / error
              "body": {"event_type": "chat.delta", "delta": "你好"},
            }

        返回值就是"这一帧你想怎么发"：
            []                              -> 丢掉，不发给客户端
            [frame]                         -> 原样发送（返回同一个对象时性能最好）
            [改过的 frame, ...]              -> 用改过的内容发送
            [{"type": "raw", "data": ...}]  -> 不装标准信封，直接发 data（AG-UI 用这个）
        """
        raise NotImplementedError

    def on_open(self, ctx: StreamMapContext) -> None:
        """一轮对话开始时调用一次（可选；默认什么都不做）。"""
        return None

    def on_close(self, ctx: StreamMapContext) -> None:
        """一轮对话结束时调用一次（成功、取消、报错都会调用；可选）。"""
        return None
```

### A.2 注册表（`jiuwenswarm/extensions/registry.py`，改现有文件）

```python
# 在 ExtensionRegistry.__init__ 里加一张表：名字 -> 扩展对象
self._outbound_stream_extensions: dict[str, Any] = {}


def register_outbound_stream_extension(self, name: str, extension: Any) -> None:
    """扩展启动时调用：把自己的名字登记进来。"""
    n = (name or "").strip()
    if not n:
        raise ValueError("outbound stream extension name must be non-empty")
    if n in self._outbound_stream_extensions:      # 同名不许重复登记
        raise ValueError(f"duplicate outbound stream extension: {n}")
    self._outbound_stream_extensions[n] = extension


def get_outbound_stream_extension(self, name: str) -> Any | None:
    """系统按名字取扩展；取不到就返回 None（外部会回落到原流程）。"""
    return self._outbound_stream_extensions.get((name or "").strip())
```

```python
# reset_instance() 里同步清空，避免测试/热重启时串到上一次的实例
cls._instance._outbound_stream_extensions.clear()
```

### A.3 核心拦截器（`jiuwenswarm/server/transports/intercept.py`，新文件）

> 设计稿代码已由实现取代（`resolve_channel_id`、`_validate_raw`、`_engaged` 置位时机、
> `_raw_seen` 诊断等均随评审演进），这里不再重复贴代码：实现见
> `jiuwenswarm/server/transports/intercept.py`，契约见 [[S_02]] 不变量 9 与 §4.4。

### A.4 流水线接线（`jiuwenswarm/server/pipeline.py`，改现有文件）

```python
async def dispatch_parsed_request(ctx, request, *, peer=None):
    """所有请求（WS / HTTP）都会经过这个汇合点。"""

    outbound_sink = None
    try:
        # 第 1-3 步整体兜底：配置读取 / sink 识别 / 扩展 on_open 的任何异常都降级为原生，
        # 不允许逃出流水线（否则会跳过错误回包与监控状态统计）。
        try:
            ext = resolve_outbound_stream(request)      # 返回 None = 保持原样
            if ext is not None:
                map_ctx = StreamMapContext(
                    transport=detect_transport(ctx.sink),       # ws / http-sse / http-unary
                    channel_id=resolve_channel_id(request),     # 与选路同一归一化口径
                    request_id=request.request_id,
                    session_id=request.session_id,
                    connection_id=ctx.connection_id,
                    request=request,
                )
                ext.on_open(map_ctx)                            # 告诉扩展：这一轮开始了
                outbound_sink = InterceptingSink(ctx.sink, ext, map_ctx)
                # RequestContext 是 frozen 的，所以用 replace 生成一个新 ctx
                ctx = dataclasses.replace(ctx, sink=outbound_sink)
        except Exception:
            logger.warning("[OutboundStream] setup failed; fallback to native", exc_info=True)
            outbound_sink = None

        # ── 以下是原来的流程，一行都没改 ──
        ...原有 dispatch_with_context / _handle_stream / _handle_unary...
    finally:
        # 汇总/解绑先做；扩展收尾放最后并自行兜底，异常不得掩盖 CancelledError。
        _emit_monitoring_summary(...)
        unbind_log_session(*log_bind)
        if outbound_sink is not None:
            try:
                outbound_sink.close()               # 内部会调 ext.on_close 并吞异常
            except Exception:
                logger.warning("[OutboundStream] on_close failed", exc_info=True)
```

```python
def resolve_channel_id(request):
    """选路与 StreamMapContext 共用的渠道归一化口径。"""
    return str(getattr(request, "channel_id", "") or "web").strip() or "web"


def resolve_outbound_stream(request):
    """查配置 + 查注册表。任何一步不确定都返回 None（= 完全保持原样）。"""
    channel = resolve_channel_id(request)

    cfg = get_config()                                  # 读 config.yaml
    section = cfg.get("outbound_stream") if isinstance(cfg, dict) else {}
    channels = section.get("channels") if isinstance(section, dict) else {}
    name = str((channels or {}).get(channel) or "").strip()
    if not name:
        return None                                     # 这个渠道没配扩展

    ext = ExtensionRegistry.get_instance().get_outbound_stream_extension(name)
    if ext is None:
        logger.warning("[OutboundStream] unknown extension %r for channel %s", name, channel)
        return None                                     # 配了名字但没人注册 → 原样走

    return ext
```

### A.5 给 sink 增加 raw 发送能力（`jiuwenswarm/server/transports/sink.py`，改现有文件）

```python
class WSSink:
    async def send_wire(self, wire):
        # 增量背压下沉到写出路径：扩展改写后的 chat.delta 也经过这里，不能绕过闸门
        if _wire_event_type(wire) in _DELTA_EVENT_TYPES:
            await _wait_for_delta_room(self._ws)
        async with self._send_lock:
            return await send_wire_payload(self._ws, wire)

    async def send_raw(self, data):
        """不装信封：直接把 data 变成 JSON 文本发出去（AG-UI 这类客户端用）。"""
        # raw 无法判别事件语义，无条件先过一次背压闸门
        await _wait_for_delta_room(self._ws)
        async with self._send_lock:          # 和普通发送共用同一把锁，保证顺序
            await self._ws.send(json.dumps(data, ensure_ascii=False))
        return True


class SSESink:
    async def send_raw(self, data):
        """SSE：先入队，再由 HTTP 生成器写成 `data: {...}`（没有 event: 行）。"""
        await self.queue.put({"type": "raw", "data": data})
        return True


class UnaryHTTPSink:
    async def send_raw(self, data):
        """单次 HTTP 没有"流"，发不了 raw：返回失败，由上层记录并丢弃。"""
        logger.warning("[UnaryHTTPSink] raw frame is not supported on unary HTTP")
        return False
```

```python
# agent_http_server._pump_sse 里，把队列内容变成 SSE 时多一个分支：
if item.get("type") == "raw":
    # raw：只写 data 行，不带 event 名，符合 AG-UI 的读法
    yield {"data": json.dumps(item.get("data"), ensure_ascii=False)}
    continue
```

### A.6 用户扩展示例（带逐行注释）

```python
# extension.py —— 用户自己写的文件，放在扩展目录下
from jiuwenswarm.extensions.sdk.outbound_stream import OutboundStreamExtension


class AgUiStreamExtension(OutboundStreamExtension):
    """把 jiuwenswarm 的 chat.* 事件翻译成 AG-UI 的事件。"""

    async def initialize(self, config):
        # 当前 loader 不调用 initialize；保留空实现满足基类要求
        pass

    async def shutdown(self):
        # 进程退出时调用；本例没有需要释放的资源
        pass

    def map_frame(self, frame, ctx):
        # 从标准信封里取"事件类型"和"内容"
        body = frame.get("body") or {}
        event_type = body.get("event_type")

        if event_type == "chat.delta":
            # 模型正在输出文字：翻译成 AG-UI 的"文本增量"事件
            return [{"type": "raw", "data": {
                "type": "TEXT_MESSAGE_CONTENT",
                "messageId": ctx.request_id,   # 用本轮请求 id 当消息 id
                "delta": body.get("delta", ""),
            }}]

        if event_type == "chat.final":
            # 本轮结束：翻译成 AG-UI 的"运行结束"事件
            return [{"type": "raw", "data": {
                "type": "RUN_FINISHED",
                "runId": ctx.request_id,
            }}]

        # 其它事件（工具、思考、状态）本例先丢掉；要支持就继续加分支
        return []


async def register_extensions(registry):
    # 启动时把扩展登记到注册表；名字要和 config.yaml 里配的一致
    ext = AgUiStreamExtension()
    registry.register_outbound_stream_extension("ag-ui", ext)
    return [ext]
```

```yaml
# config.yaml —— 启用：把某个渠道的请求交给 ag-ui 这个扩展
outbound_stream:
  channels:
    my-agui-client: ag-ui
```

