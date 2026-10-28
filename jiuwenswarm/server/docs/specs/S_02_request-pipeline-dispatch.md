# 请求流水线与分发注册表

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_02 / request-pipeline-dispatch |
| 关联模块 | jiuwenswarm/server/{dispatch.py, pipeline.py, context.py, wire_parse.py, wire_truncate.py, ws_send.py, handlers/, transports/} |
| 最近一次修订日期 | 2026-10-15 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

- HANDLERS 注册表（`dispatch.HANDLERS: dict[ReqMethod, HandlerSpec]`）的条目结构、注册时机、动态注册路径（permissions.*）。
- HandlerSpec frozen dataclass 契约（fn + args + kwargs + stream_fn）与 `resolve_fn(is_stream)` 分支。
- RequestContext frozen dataclass 与 AgentServerServices 门面窄面。
- `pipeline.dispatch_parsed_request` 通用流水线的处理顺序、hook 点、错误处理契约（CancelledError 必须重抛）。
- 默认路径 fallback（`handlers._default._handle_stream / _handle_unary`）：HANDLERS 未命中的请求 → openjiuwen Runner.invoke。
- Sink 三大家族（`ResponseSink` 基类 + `WSSink` / `UnaryHTTPSink` / `SSESink`）契约。
- PushRegistry（`transports/push_registry.py`）的推送订阅管理与异常注销语义（Gateway WS 用 `_GatewayWSPushSink` 例外）。
- wire_truncate 字节预算常量（history / team_history / workflow_snapshot）。

### 本规约**不**管什么

| 子主题 | 归属 spec |
|---|---|
| runtime/agent_manager、AgentWarmPool、agent_adapter 三路 | [[S_03]] runtime-agent-management |
| sandbox auto-start 条件链、JiuwenBoxRunner | [[S_04]] sandbox-lifecycle |
| im_connector Plugin、im_hosting 托管 | [[S_05]] im-channel-integration |

## 不变量

1. **HandlerSpec.fn 必填且可调用**：`__post_init__` 里 `fn is None → ValueError` / `not callable → ValueError`；`stream_fn` 可选但若有必须 callable。
2. **动态注册不覆盖已有**：`_register_permissions_methods()` 重复注册同一个 ReqMethod → RuntimeError（防双注册）。
3. **`resolve_fn(is_stream=True)` 优先 stream_fn**：仅当 `stream_fn is not None` 且 `is_stream=True` 才用 stream_fn；否则用 fn。
4. **schedule.* / issue.* 共用一个 handler**：`_schedule(action)` 工厂返回同一个 `handle_schedule_request`，以 `args=(action,)` 区分。新加 schedule/issue 子命令走这条路径，不要单独写 handler。
5. **connection_id 跨请求稳定**：WS = `str(id(ws))`；HTTP = 常量 `HTTP_CONNECTION_ID`。取每请求唯一值会让 ACP 客户端能力、session.switch 切换锁退化为零互斥。
6. **SERVICE_MEMBERS 是唯一通道**：业务层 handler 只能经 `ctx.services.<公有名>` 访问，AgentWebSocketServer 上的受保护属性名（`_xxx`）不在清单里就不可见不可写；越界 `AttributeError`。
7. **CancelledError 必须重抛**：`dispatch_parsed_request` 的 except 分支里 `asyncio.CancelledError` 不能吞；吞掉会让 `asyncio.gather` 拿不到 CancelledError → `task.cancelled()` 为 False → chat cancel 链断裂。
8. **Sink 基类四方法全实现**：`ResponseSink` 要求 `send_unary / send_chunk / send_error / send_wire`。新增传输类型必须继承它，不要搞平行类。
9. **Gateway WS 推送不注销**：`agent_ws_server._GatewayWSPushSink` 把 WSSink 的异常吃在内部、返回 False，PushRegistry 因此不会注销。**不要把裸 WSSink 塞进 PushRegistry 给 Gateway WS**。
10. **wire_truncate 是字节预算单一真相源**：`wire_truncate.py` 的 `_HISTORY_WIRE_* / _TEAM_HISTORY_* / _WORKFLOW_SNAPSHOT_*` 常量在 handler 里被 import 使用；预算改了先改这里，不要硬编码。
11. **默认路径 fallback 不绕过 HANDLERS**：`dispatch_parsed_request` 里 `dispatch_with_context` 必须先于 fallback 调用；新增 handler 逻辑先登记 HANDLERS。

## 接口契约

### dispatch.HANDLERS（模块加载时静态构建）

约 100+ 条目，分域：
- bootstrap ：INITIALIZE / SESSION_CREATE / SESSION_FORK / ACP_TOOL_RESPONSE
- session ：SESSION_LIST / SESSION_RENAME / SESSION_SWITCH / SESSION_DELETE / SESSION_REWIND / SESSION_REWIND_AND_RESTORE（同 fn + kwargs restore_files=True）/ SESSION_REWIND_COMPACT（同 fn + kwargs compact=True）/ SESSION_REWIND_CONTEXT / HISTORY_GET（fn + stream_fn 分支）
- chat ：CHAT_CANCEL / CHAT_STEER / CHAT_STEER_STATUS
- file_transfer（3）：按 event_type 分派
- team ：TEAM_* 全域
- commands ：COMMAND_* + COMMAND_MCP / COMMAND_SANDBOX（外借）
- mcp_servers ：MCP_SERVER_ADD / REMOVE / UPDATE / LIST / GET
- agents ：AGENTS_*
- extensions ：EXTENSIONS_* + HOOKS_LIST + HARNESS_PACKAGES_*
- schedule ：SCHEDULE_CHECK_CONFIG / UPDATE_CONFIG / CREATE / RUN / LIST / STATUS / LOGS / CANCEL / DELETE + ISSUE_WATCH_ONCE / ISSUE_STATE_LIST / ISSUE_DELETE / ISSUE_MATRIX（共用 handle_schedule_request）
- ops ：PROACTIVE_TICK / LONG_HORIZON / BROWSER_RUNTIME_RESTART / CONFIG_CACHE_CLEAR / AGENT_RELOAD_CONFIG / SYNC_AGENTS_CONFIGS / AGENT_PREWARM_SYNC
- workspace ：WORKSPACE_*（共用 handle_workspace）
- im_hosting（若干）：IM_HOSTING_STATUS / im_hosting_list / im_hosting_poll
- permissions（动态注册）

### pipeline.dispatch_parsed_request

```python
async def dispatch_parsed_request(
    ctx: RequestContext,
    request: AgentRequest,
    *,
    peer: Any = None,
) -> None: ...
```

处理顺序：bind_log_session → acp_client_capabilities 透传 → BEFORE_CHAT_REQUEST hook → dispatch_with_context（命中 HANDLERS return）→ _ensure_auto_team_binding_for_chat → 默认路径 _handle_stream / _handle_unary。

### context.RequestContext（frozen dataclass）

```python
@dataclass(frozen=True)
class RequestContext:
    request: AgentRequest
    sink: ResponseSink
    connection_id: str
    services: AgentServerServices
    # properties: params (安全访问 request.params) / request_id / channel_id / session_id
```

### context.AgentServerServices（门面窄面）

```python
SERVICE_MEMBERS: dict[str, str] = {
    # 共享状态
    "agent_manager": "_agent_manager",
    "session_stream_tasks": "_session_stream_tasks",
    "scheduler_service": "_scheduler_service",
    "scheduler_agent": "_scheduler_agent",
    "jiuwenbox_runner": "_jiuwenbox_runner",
    "proactive_engine": "_proactive_engine",
    "model_cache": "_model_cache",
    "default_model": "_default_model",
    # 跨域行为
    "prepare_session_switch_owner": "_prepare_session_switch_owner",
    "dispatch_session_switch_kvc": "_dispatch_session_switch_kvc",
    "ensure_persistent_checkpointer_response": "_ensure_persistent_checkpointer_response",
    "resolve_adapter": "_resolve_adapter",
    "build_model_cache": "_build_model_cache",
    "tenant_pool": "_tenant_pool",
    # 沙箱端口
    "is_tcp_port_bindable": "_is_tcp_port_bindable",
    "pick_free_tcp_port": "_pick_free_tcp_port",
    # 其他
    "stateless_fallback_agents": "_stateless_fallback_agents",
    "get_acp_client_capabilities": "_get_acp_client_capabilities",
    # 公有名直穿透（不是受保护成员）
    "send_push": "send_push",
    "set_acp_client_capabilities": "_set_acp_client_capabilities",
}
```

### transports.sink.ResponseSink

```python
class ResponseSink:
    async def send_unary(self, resp: Any, *, response_id: str | None = None) -> bool: ...
    async def send_chunk(self, chunk: Any, *, sequence: int, response_id: str | None = None) -> bool: ...
    async def send_error(self, request_id: str, message: str, *, code: str, channel_id: str = "") -> bool: ...
    async def send_wire(self, wire: dict[str, Any]) -> bool: ...
```

三大家族：WSSink（WebSocket 二进制推送，最终调 send_wire_payload）/ UnaryHTTPSink（HTTP 单次，HTTP status code 回写）/ SSESink（HTTP SSE 推送，按 session 过滤，有 budget 截断）。

## 数据结构

| 结构 | 位置 | 谁写 | 谁读 | 生命周期 |
|---|---|---|---|---|
| HANDLERS dict | dispatch.py 模块级 import 时 | handlers/__init__.py 的 `from . import xxx` 副作用 + _register_permissions_methods() | pipeline.dispatch_with_context / dispatch_to_handler | 进程级 |
| HandlerSpec frozen dataclass | dispatch.py 模块级静态构建 | 进程启动一次性 | resolve_fn / 调用点参数展开 | 进程级 |
| RequestContext frozen dataclass | context.py | WS _connection_handler / HTTP _make_ctx | 所有 handler | 单请求 |
| SERVICE_MEMBERS dict | context.py 模块级 | 一次性手工维护 | AgentServerServices.__getattr__ | 进程级 |
| PushRegistry | transports/push_registry.py 单例 | AgentWebSocketServer 连接注册 / SSE endpoint 注册 | send_push / gateway_push.build_server_push_wire | 进程级 |
| wire_truncate 常量 | wire_truncate.py 模块级 | 一次性手工维护 | session_history / workflow_snapshot handler | 进程级 |

## 执行路径 / 装配关系

### P1：HANDLERS 填充时机

```
server/__init__.py import
  → handlers/__init__.py 的 import dispatch 模块
  → dispatch.py 模块级 HANDLERS = {...}（静态构建 100 条目）
  → _register_permissions_methods()（permissions.* 动态注册）
  → 双重注册 → RuntimeError
```

### P2：单次请求处理（WS / HTTP 汇合）

```
[WS] _connection_handler → wire_parse.parse_inbound(envelope) → AgentRequest
[HTTP] routes 路由 → server.invoke_unary() → _make_ctx() → pipeline.dispatch_parsed_request

  1. bind_log_session(session_id)
  2. acp_client_capabilities → metadata（非 initialize 的 acp 信封）
  3. BEFORE_CHAT_REQUEST hook（ExtensionRegistry.trigger）
  4. dispatch_with_context(ctx, request) → HANDLERS[req_method].resolve_fn(is_stream)(ctx, *args, **kwargs) → True
     ↓ False
  5. _ensure_auto_team_binding_for_chat(ctx, request)
  6. 默认路径: _handle_stream(ctx, request) 或 _handle_unary(ctx, request)
     ↓ openjiuwen Runner.invoke
     ↓ 响应生成
  7. ctx.sink.send_wire(wire) → transports.push_registry / gateway_push.wire

CancelledError → 必须重抛（chat cancel 链依赖）
WebSocketConnectionClosed → 记 info 并放弃回包
Exception → 编码 error_resp + sink.send_wire(error_wire)
```

### P3：send_push（服务端主动推送）

```
AgentWebSocketServer.send_push(msg)
  → gateway_push.wire.build_server_push_wire(msg)  # E2A chunk wire + E2A_WIRE_SERVER_PUSH_KEY
  → PushRegistry.push(wire)
     → 遍历 WS + SSE 订阅者
     → Gateway WS 用 _GatewayWSPushSink.send_wire → 内部 WSSink.send_wire + 异常吞噬
     → 普通订阅者异常 → PushRegistry 当场注销（超时自动移除）
```

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| HANDLERS 里 ReqMethod 未匹配 | dispatch_with_context 返回 False → 走默认路径 | handler 漏注册让 chat.send 走 runner，但 session.list 也会走 runner（语义错） | dispatch 注册表完整性 |
| history.get is_stream=True | stream_fn 分支走 handle_history_get_stream | stream_fn 忘记注册 → fn 被调，流式被截断 | dispatch.stream_fn 分支 |
| connection_id 取每请求唯一值 | ACP 能力、session.switch 切换锁零互斥 | 静默错，不报错 | WS 连接跨请求稳定性 |
| Wire 消息超大（history/workflow） | wire_truncate 常量截断 | 截太短 → 用户看不到完整历史；截太长 → WS 单帧超限 | wire_truncate 字节预算 |
| Gateway WS send_push 瞬时失败 | PushRegistry 抛异常 → 普通订阅者被注销 | Gateway 被踢出推送名单 → 后续推送丢失 | _GatewayWSPushSink 异常吞噬 |
| WS / HTTP sink 不一致 | 同一个 handler 输出在两种传输下不同 | 用户体验差 + 难复现 | handler 双路径一致性 |
| permissions.* 动态注册 + 重复 | _register_permissions_methods RuntimeError | 进程启动崩溃 | 防双注册 |
| CancelledError 被 except Exception 吞 | task.cancelled()=False → chat cancel 链断裂 | 用户取消不了任务 | dispatch.CancelledError 重抛 |

## 修改检查单

- **加 HANDLERS 条目** → 加 ReqMethod 枚举值 → 写 handler（async def ctx → None）→ 注册 HANDLERS（fn / args / kwargs / 可选 stream_fn）→ 如果有 RESTful 入口同步 agent_http_routes.py → tests/unit_tests/server/test_dispatch.py → 对应 handler 测试。
- **改 HandlerSpec.fn/args/kwargs** → 同步 HANDLERS 条目 → 确认没有重复注册 → 跑 dispatch 测试。
- **改 wire_truncate 常量** → 所有 import 了 `_HISTORY_WIRE_* / _TEAM_HISTORY_* / _WORKFLOW_SNAPSHOT_*` 的 handler 重审截断逻辑 → 跑 history.get / team.history.get / team.snapshot 大 payload 测试。
- **改 sink 基类** → 同步 WSSink / UnaryHTTPSink / SSESink 三实现 → 所有 send_wire 调用点不摸具体 sink 类型 → 跑 handler 双路径一致性测试。
- **改 PushRegistry 注销语义** → 确认 Gateway WS 的 _GatewayWSPushSink 异常吞噬仍在 → 跑 server 重启推送测试。
- **改 RequestContext 字段 / AgentServerServices 门面** → SERVICE_MEMBERS 同步 → 所有 handler 的 `ctx.services.xxx` 调用是否越界 → 加新能力不要绕门面直摸 server._xxx。
- **改 pipeline.dispatch_parsed_request 顺序** → hook 点位置确认 → CancelledError 分支不被意外覆盖 → 跑 chat.send cancel 测试。

## 与其它 spec 的关系

- [[S_01]] 提供子模块划分与启动链总览
- [[S_03]] runtime/agent_manager 与默认路径 fallback 的 agent 装配链
- [[S_04]] sandbox auto-start 条件链（agent_ws_server.start() 的最后一步）
