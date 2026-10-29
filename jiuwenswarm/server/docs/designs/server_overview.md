# AgentServer 特性文档

## 概述

AgentServer 是 JiuwenSwarm 的 **Agent 运行时服务进程**，对上通过 WebSocket（主链路）或 HTTP/SSE（可选入口）与 Gateway 通信，对下装配 **openjiuwen DeepAgent / TeamAgent** 运行时。一个 AgentServer 单例进程 = 一套 Agent 运行时 = 零共享状态的独立进程边界。Gateway 启动时自动拉起 AgentServer；AgentServer 启动后按 config.yaml 条件链自动拉起本地 jiuwenbox 沙箱子进程。

本文从概念层面介绍 AgentServer 的功能、原理、特性与能力边界，帮助读者理解其工作原理与能力边界。

---

## 核心概念

### AgentServer 单例进程

AgentServer 是进程级单例（`AgentWebSocketServer.get_instance()`），进程启动时一次性构造，持有：

- `AgentManager`（单租户）/ `TenantAgentPool`（企业多租户）
- `JiuwenBoxRunner`（本地沙箱子进程管理器）
- `PersonalContextHostAPI`（个人上下文 Distill）
- `AcpClientCapabilities`（按连接登记的 ACP 客户端能力）
- 会话级 stream tasks 跟踪、scheduler、model cache

### 双传输入口：WebSocket 主链路 + HTTP/SSE 可选

AgentServer 同时暴露两种传输：

| 入口 | 默认 | 用途 | 端口 |
|---|---|---|---|
| **WebSocket**（主链路） | 开启 | Gateway 与 AgentServer 的双向实时通信 | 默认 18000（CLI --port 覆盖 18092） |
| **HTTP/SSE** | 关闭 | HTTP 客户端 RESTful 调用 + SSE 推送订阅 | 可配置 |

两种入口**共享同一份 HANDLERS 注册表和所有 handler 实现**——业务 handler 不碰 WS/HTTP 对象，统一经 `RequestContext.sink`（三大家族）输出。

### 事件驱动：HANDLERS 注册表 + 默认路径 fallback

每个入站请求由 `ReqMethod`（type 字符串枚举）标识，`dispatch.HANDLERS` 是 `dict[ReqMethod, HandlerSpec]` 注册表（~100+ 条目，进程启动时静态构建）。请求处理链：

```
WS 帧 / HTTP 请求
  → wire_parse 解析 / HTTP routes 构造 AgentRequest
  → pipeline.dispatch_parsed_request（通用流水线）
     → BEFORE_CHAT_REQUEST hook（扩展系统）
     → HANDLERS[req_method] 命中 → 调 handler → return
     → 未命中 → 默认路径 fallback → openjiuwen Runner.invoke（默认只覆盖 chat.send / chat.resume 等少数请求）
```

### HandlerSpec 契约：传输无关自由函数

```python
@dataclass(frozen=True)
class HandlerSpec:
    fn: Any = None            # async def (ctx: RequestContext)
    args: tuple[Any, ...]     # 位置参数（如 schedule.create 的 "create"）
    kwargs: Mapping[str, Any] # 关键字参数（如 SESSION_REWIND_AND_RESTORE 的 restore_files=True）
    stream_fn: Any = None     # is_stream=True 时改用的 fn（目前仅 history.get 用到）
```

Handler 签名统一 `async def (ctx: RequestContext) -> None`，通过 `ctx.sink.send_wire`（ResponseSink 基类）输出。**Handler 不能直接碰 WS/HTTP 对象**——违反 = 同一份 handler 不能同时服务 WS 与 HTTP。

### RequestContext：一次请求的全部上下文

```python
@dataclass(frozen=True)
class RequestContext:
    request: AgentRequest      # 已解析的业务请求（jiuwenswarm.common.schema.agent）
    sink: ResponseSink         # 响应出口（三大家族：WSSink / UnaryHTTPSink / SSESink）
    connection_id: str         # 连接标识（WS=str(id(ws))，HTTP=常量；必须跨请求稳定）
    services: AgentServerServices  # 服务端窄面（只暴露 SERVICE_MEMBERS 登记的能力）
```

### ResponseSink 三大家族

| Sink | 用途 | 输出通道 |
|---|---|---|
| **WSSink** | WebSocket 帧 | Gateway WebSocket |
| **UnaryHTTPSink** | HTTP 单次请求 | HTTP status code + JSON body |
| **SSESink** | HTTP SSE 推送 | `/api/v1/events/stream` 订阅者 |

所有 sink 继承同一个 `ResponseSink` 基类，必须实现 `send_unary / send_chunk / send_error / send_wire` 四方法。

### Pipeline：WS / HTTP 汇合点

`pipeline.dispatch_parsed_request(ctx, request)` 是双传输汇合的通用处理流水线：

```
1. bind_log_session（日志上下文绑定）
2. acp_client_capabilities 透传到 metadata（ACP 协议需要）
3. BEFORE_CHAT_REQUEST hook（扩展系统钩子点）
4. dispatch_with_context（HANDLERS 命中 → return）
5. _ensure_auto_team_binding_for_chat（团队绑定兜底）
6. 默认路径 fallback：_handle_stream / _handle_unary（openjiuwen Runner.invoke）
```

### Sandbox（沙箱）：本地 jiuwenbox 子进程

`AgentWebSocketServer.start()` 按 config.yaml **条件链**自动拉起本地 jiuwenbox 沙箱：

```
enterprise=true？          → skip（sidecar 由 K8s 提供）
非 linux/win32？          → skip（jiuwenbox 依赖平台专属能力）
sandbox.runtime.enabled=false（win32）→ skip（opt-in 默认 false）
startup_mode 字段缺失？    → skip（走默认 host 模式）
startup_mode != internal？→ skip（external 由用户拉）
sandbox.type=yuanrong？   → skip（YuanRong 不用本地 box-server）
policy_file 无法解析？    → warning + return
→ runner.ensure_running(timeout=180) → ok → update_sandbox_endpoint
```

**auto-start 是 best-effort**——失败一律 warning，绝不阻断 AgentServer 启动（运维误配 yaml 不能让整个产品起不来）。进程退出时先 DELETE 远端沙箱（box-server 活着才能响应），再停 box-server 子进程。

### Runtime：Agent 运行时管理层

runtime/ 是 Agent 生命周期管理的核心：

| 域 | 职责                                                                                                                                                         |
|---|------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **AgentManager**（单租户）/ **TenantAgentPool**（企业多租户） | agent 实例装配                                                                                                                                                 |
| **AgentWarmPool** | 预热池：按 `WarmKey(channel, project, work_mode)` 缓存已创建的 agent 实例（排除 acp/a2a）                                                                                   |
| **prewarm.py 三阶段** | 阶段 1+2（interface_deep import + checkpointer）→ 阶段 3（临时 DeepAgent query，mock 默认 True，独立 fire-and-forget）                                                     |
| **agent_adapter 三路** | interface_deep（默认原生 DeepAgent）/ interface_code / interface_flash                                                                                           |
| **A2UI 协议** | Agentic UI：面向 UI 的结构化 agent 输出（protocol + response_finalization + stream_guard + validator）                                                                |
| **debug_trace** | /debug 模式调试追踪（stream_logger + directives + task_tool_patch）                                                                                                |
| **session/** | 会话管理（session_manager / history / metadata / project_store / git_diff_watcher / kv_cache 亲和 / permission ledger / work_mode）                                |
| **skill/** + **skilldev/** | 技能管理（skill_manager / source_registry）+ 技能自演进流水线（init → plan → generate → test_design → test_run → evaluate → improve → package → validate → desc_optimize） |
| **workspace/** | 工作区配额（fs_quota_guard / policy_reload / usage_reconciler）                                                                                                   |

### IM 频道接入

AgentServer 启动末尾（非 enterprise）按 config.yaml 条件自动托管外部 IM 频道（钉钉 / 飞书 / WeLink）：

- **im_connector Plugin 协议**：每个连接器必须实现 connect / disconnect / poll_once / send_reply / login_via_cli
- **im_hosting 托管服务**：按 policy 轮询 IM target，parser.py 把 IM 消息转换成 AgentRequest，送进同一条 pipeline 分发链；reply_bridge 把 AgentServer 回复桥接回 IM

### Hook 与 PersonalContext

| 域 | 职责 |
|---|---|
| **hooks/** | executor（钩子执行器）+ user_hook_rail（rail 形态注入 harness） |
| **personal_context/** | Distill：host_api（AgentWebSocketServer 持有单例）+ profiles_ops + ws_handler（PERSONAL_CONTEXT_REQUEST_METHODS 集合）+ distill_ports |

### Wire 协议与字节预算

| 文件 | 职责 |
|---|---|
| **wire_parse.py** | WS 帧 JSON → E2AEnvelope → AgentRequest |
| **wire_truncate.py** | history / team_history / workflow_snapshot 字节预算（`_HISTORY_WIRE_* / _TEAM_HISTORY_* / _WORKFLOW_SNAPSHOT_*`）——**单一真相源** |
| **ws_send.py** | send_wire_payload 等出站辅助 |
| **gateway_push/wire.py** | build_server_push_wire：AgentServer send_push → E2A chunk wire + `E2A_WIRE_SERVER_PUSH_KEY` 标记 |

### PushRegistry：服务端主动推送订阅注册表

`transports/push_registry.py` 管理 WS + SSE 订阅者，按 session/channel 过滤。**Gateway WS 用特殊的 `_GatewayWSPushSink`** 把异常吞在内部（PushRegistry 对抛异常的订阅者默认注销，Gateway WS 不能因一次瞬时失败被踢出）。

---

## 数据流

### 请求处理端到端

```
Gateway WS 帧                      HTTP/SSE 请求
  ↓                                ↓
wire_parse.parse_inbound           agent_http_routes 路由 → invoke_unary
  ↓                                ↓
AgentRequest                       AgentRequest（同形）
  ↓                                ↓
dispatch_parsed_request ────────────┘
  ├─ BEFORE_CHAT_REQUEST hook
  ├─ HANDLERS[req_method] 命中 → handler → ctx.sink.send_wire
  ├─ 未命中 → _handle_stream / _handle_unary
  │             ↓ openjiuwen Runner.invoke
  │             ↓ A2UI runtime 链（response_finalization → stream_guard → validator）
  │             ↓ ctx.sink.send_wire
  ↓
transports.push_registry / gateway_push.wire
  ↓
Gateway WS / HTTP SSE 订阅者
```

### send_push（主动推送）

```
任何 handler 内 / runtime 触发
  ↓
AgentWebSocketServer.send_push(msg)
  ↓
gateway_push.wire.build_server_push_wire → E2A chunk wire + E2A_WIRE_SERVER_PUSH_KEY
  ↓
PushRegistry.push(wire)
  ├─ Gateway WS → _GatewayWSPushSink（异常吞噬，不因瞬时失败注销）
  ├─ 普通 WS → WSSink.send_wire
  └─ HTTP SSE 订阅者 → SSESink.send_wire
```

---

## 能力边界

### 当前支持

- WebSocket 主链路（Gateway ↔ AgentServer）+ HTTP/SSE 可选入口（RESTful + SSE 推送订阅）
- 声明式 HANDLERS 注册表驱动的请求分发（~100 条目）
- Handler 传输无关（WS / HTTP / SSE 同一份代码）
- 默认路径 fallback → openjiuwen DeepAgent / TeamAgent 运行时
- AgentWarmPool 预热池（排除 acp / a2a 通道）
- prewarm 三阶段启动加速
- sandbox auto-start best-effort + JiuwenBoxRunner 生命周期管理
- IM 频道托管（钉钉 / 飞书 / WeLink，Plugin 协议扩展）
- PersonalContext（Distill）个人上下文
- A2UI 结构化 agent 输出
- /debug 模式调试追踪
- agent_adapter 三路接口（interface_deep / code / flash）
- 企业版多租户（TenantAgentPool + PVC 并发写防竞）
- 企业版冷加载（task_memory / memory / logging / audit_log / permissions / workspace_quota）
- 字节预算保护（wire_truncate history / workflow_snapshot 截断）

### 暂不支持

- 不提供除 WS + HTTP 之外的其它传输（如 NATS 或自定义协议）
- 不实现新的 IM 频道以外的事件源（需要新 Plugin 协议）
- sandbox 只支持 linux + win32（macOS 跳过）
- sandbox 不支持本地 box-server 以外的 provider（yuanrong 走 YR_* env，算特例）
- agent_adapter 接口目前只有三路，第四路（interface_extra）需要同步 AgentWebSocketServer._resolve_adapter

---

## 术语表

| 术语 | 含义 |
|---|---|
| AgentServer | 本模块的单例服务进程（AgentWebSocketServer） |
| Gateway | 上游网关（jiuwenswarm/gateway） |
| E2A / wire 协议 | Gateway ↔ AgentServer 的消息协议（jiuwenswarm/common/e2a/） |
| ReqMethod | enum ReqMethod：入站请求的 type 字符串枚举 |
| HandlerSpec | frozen dataclass：一条分发规则 |
| HANDLERS | dispatch.py 里的 dict[ReqMethod, HandlerSpec] 注册表 |
| 默认路径 / fallback | HANDLERS 未命中的请求 → openjiuwen Runner.invoke |
| Sink / ResponseSink | 响应出口基类，三大家族 |
| RequestContext | frozen dataclass：一次请求的完整上下文 |
| AgentServerServices | 服务端窄面门面（SERVICE_MEMBERS 16 个公有名） |
| PushRegistry | 服务端主动推送订阅注册表 |
| AgentManager | 单租户 Agent 管理器 |
| TenantAgentPool | 企业多租户 Agent 管理器池 |
| AgentWarmPool | 预热池，按 WarmKey 维度缓存 agent 实例 |
| A2UI | Agentic UI，结构化 agent 输出 |
| sandbox / jiuwenbox | 本地沙箱子进程 |
| startup_mode | internal（AgentServer 拉起）/ external（用户拉起 / K8s sidecar） |
| im_connector Plugin | IM 频道连接器协议 |
| im_hosting | 用户态 IM 托管服务 |
| wire_truncate | 历史/工作流快照字节预算单一真相源 |
