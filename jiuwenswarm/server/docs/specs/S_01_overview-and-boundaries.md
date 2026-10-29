# server 总览与包边界

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_01 / overview-and-boundaries |
| 关联模块 | jiuwenswarm/server/ |
| 最近一次修订日期 | 2026-10-15 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

`jiuwenswarm.server` 子系统的**总体骨架**：

- 子模块划分与职责边界（顶层入口 / pipeline / dispatch / handlers / transports / gateway_push / sandbox / runtime / im / hooks / personal_context）。
- 启动链（`app_agentserver.py` → `AgentWebSocketServer.get_instance()` → `.start()`）的装配顺序约束。
- 请求处理链（WS / HTTP → wire_parse / routes → pipeline → dispatch / 默认路径 → sink）的分层。
- 依赖方向：server 向上只暴露 `AgentWebSocketServer` 单例和有限的 `__init__.py` re-export；向下依赖 `openjiuwen.*` 与 `jiuwenswarm.common.*`。

### 本规约**不**管什么

| 子主题 | 归属 spec |
|---|---|
| HANDLERS 注册表 / HandlerSpec 契约 / 默认路径 fallback / Sink 三大家族 | [[S_02]] request-pipeline-dispatch |
| AgentManager / TenantAgentPool / AgentWarmPool / agent_adapter 三路接口 / a2ui | [[S_03]] runtime-agent-management |
| sandbox auto-start 条件链 / JiuwenBoxRunner 生命周期 / 端口分配 | [[S_04]] sandbox-lifecycle |
| im_connector Plugin 协议 / im_hosting 托管轮询 | [[S_05]] im-channel-integration |

## 不变量

任意时刻，`server/` 子系统必须满足下列断言：

1. **Handler 传输无关**：所有 handler 签名必须是 `async def (ctx: RequestContext) -> None`，不直接碰 WS/HTTP 对象，所有输出经 `ctx.sink.send_wire`（ResponseSink 基类契约）。
2. **HANDLERS 单一真相源**：`dispatch.py.HANDLERS` 是入站分发唯一依据。新增请求类型 → 加 ReqMethod 枚举 → 写 handler → 注册 HANDLERS → 同步 HTTP routes。
3. **Connection ID 跨请求稳定**：`RequestContext.connection_id` 必须在同一 WS / HTTP 连接的所有请求上保持稳定（WS = `str(id(ws))`；HTTP = 常量 `HTTP_CONNECTION_ID`）。
4. **Service 门面窄面**：handler 只能通过 `ctx.services` 访问 `context.SERVICE_MEMBERS` 登记的能力；越界访问当场 AttributeError。
5. **CancelledError 必须重抛**：`pipeline.dispatch_parsed_request` 里 `CancelledError` 不能吞，否则破坏 chat.py 的 cancel 判定链。
6. **Sandbox auto-start best-effort**：`AgentWebSocketServer.start()` 里的 sandbox auto-start **绝对不能阻断进程启动**。
7. **Gateway WS 推送不能因瞬时失败断开**：PushRegistry 对抛异常的订阅者默认当场注销，但 Gateway WS 必须用 `_GatewayWSPushSink` 把异常吞掉、返回 False。
8. **Sink 三大家族基类契约**：`ResponseSink` 要求 `send_unary / send_chunk / send_error / send_wire` 四个方法全实现。
9. **企业版并发写防竞**：`update_config()` 仅在 `not is_enterprise()` 时调用；企业多 Pod 共享 PVC，配置由部署侧/init 写入。
10. **runtime/ 不碰 sandbox 启动**：Agent 装配归 `runtime/agent_manager`；sandbox 启动归 `AgentWebSocketServer.start()`；跨域调用必须显式过门面。
11. **AgentWebSocketServer 单例**：进程内至多一个实例（`get_instance` 类方法），构造参数 host/port/ping_interval/ping_timeout 仅首次生效。
12. **Patch 模块刻意后置**：`app_agentserver.py` 里 `apply_*_patch()` 在 import 顺序上放在 Runner/ExtensionManager 等之后——加新 patch 必须审顺序依赖。

## 接口契约

### AgentWebSocketServer

```python
class AgentWebSocketServer:
    # 单例入口（首次构造 host=127.0.0.1 port=18000）
    @classmethod
    def get_instance(cls, *, host="127.0.0.1", port=18000,
                     ping_interval: float | None = 30.0,
                     ping_timeout: float | None = 300.0) -> "AgentWebSocketServer": ...

    async def start(self, *, listen: bool = True) -> None: ...
    async def stop(self) -> None: ...

    # 服务端主动推送（经 PushRegistry 向所有 WS + SSE 订阅者）
    async def send_push(self, msg: Any) -> None: ...
```

### dispatch.HANDLERS

```python
HANDLERS: dict[ReqMethod, HandlerSpec] = {
    ReqMethod.SESSION_LIST: HandlerSpec(fn=session_handlers.handle_session_list),
    ReqMethod.HISTORY_GET: HandlerSpec(
        fn=session_handlers.handle_history_get,
        stream_fn=session_handlers.handle_history_get_stream,  # is_stream=True 时用
    ),
    ReqMethod.SCHEDULE_CREATE: _schedule("create"),  # 共用 handler 按 action 分派
    # ... ~100 条目
}
```

### dispatch.HandlerSpec（frozen dataclass）

```python
@dataclass(frozen=True)
class HandlerSpec:
    fn: Any = None                 # async def (ctx, *args, **kwargs)
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    stream_fn: Any = None          # is_stream=True 且非 None 时改用
```

## 执行路径 / 装配关系

### P1：AgentServer 进程启动（app_agentserver.py）

```
parse_dotenv_early                # 零依赖早期
  → workspace 初始化               # prepare_workspace / update_config(enterprise 跳过)
  → bootstrap_openjiuwen_logging   # pin log dir
  → 一批 apply_*_patch()           # GLM XML / MaaS span_id / auth / evolution / thinking / perf / debug trace
  → ExtensionManager.load_all_extensions
  → 企业版冷加载                   # task_memory / memory / logging / audit_log / permissions / workspace_quota
  → AgentWebSocketServer.get_instance()  # 单例构造
  → server.start()                   # WS 端口开放 + interface_deep 预热两阶段 + sandbox auto-start + event_loop_lag_monitor + im_hosting
  → AgentHTTPServer.start()          # 可选，默认关
  → init_proactive_engine
  → run_teammate_bootstrap_daemon    # 分布式 teammate bootstrap 守护
  → start_mcp_registry_runtime
  → mem_reclaimer.setup()
  → signal.SIGINT/SIGTERM
  → await stop_event.wait()
  → finally: cleanup                # usage_reconciler.stop → MCP registry stop → HTTP stop → server.stop() → sandbox DELETE + runner.stop → perf flush → observability shutdown → history.flush
```

### P2：请求处理流水线（WS / HTTP 汇合）

```
WS:
  _process_request → wire_parse.parse_inbound → AgentRequest
  → pipeline.dispatch_parsed_request
HTTP:
  agent_http_routes 路由 → AgentHTTPServer._make_ctx
  → server.invoke_unary → pipeline.dispatch_parsed_request
    ↓
dispatch_parsed_request(ctx, request):
  1. bind_log_session
  2. acp_client_capabilities 透传到 metadata
  3. BEFORE_CHAT_REQUEST hook（ExtensionRegistry.trigger）
  4. dispatch_with_context → HANDLERS[req_method] 命中 → return
  5. _ensure_auto_team_binding_for_chat（团队绑定兜底）
  6. 默认路径: _handle_stream / _handle_unary（openjiuwen Runner.invoke）
    ↓
响应 → ctx.sink.send_wire → transports.push_registry / gateway_push.wire
推送 → server.send_push → gateway_push.build_server_push_wire
```

## 修改检查单

- 改 `dispatch.HANDLERS` 注册表 → 同步：HTTP routes（如有 RESTful 入口）→ `handlers/` 对应文件 → `tests/unit_tests/server/test_dispatch.py` 覆盖 → 跑 `tests/unit_tests/server/handlers/`。
- 改 `RequestContext` 字段 → 同步：`context.py` 的 SERVICE_MEMBERS → 所有 handler 的 `ctx.services.xxx` 调用是否越界 → `AgentServerServices.__getattr__` 新增条目。
- 改 `AgentWebSocketServer.start()` 条件链 → 同步：[[S_04]] sandbox auto-start 条件 → 检查是否打破 N6（best-effort）→ 跑 `tests/unit_tests/server/test_agent_ws_server.py`。
- 改 `app_agentserver.py` import 顺序 → 同步：N12（patch 后置约束）→ 确认 Runner / ExtensionManager 在 patch 之前 import → 跑启动测试。
- 新增 sink 实现 → 继承 ResponseSink → 实现 send_unary / send_chunk / send_error / send_wire 四方法 → 不要在 transports 下搞平行类。

## 与其它 spec 的关系

- [[S_02]] 细化 HANDLERS 注册表、HandlerSpec 契约、Sink 三大家族、默认路径
- [[S_03]] 细化 runtime/agent_manager、AgentWarmPool、agent_adapter 三路
- [[S_04]] 细化 sandbox auto-start 条件链、JiuwenBoxRunner 生命周期
- [[S_05]] 细化 im_connector Plugin 协议、im_hosting 托管
