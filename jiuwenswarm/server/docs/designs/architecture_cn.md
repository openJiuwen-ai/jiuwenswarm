# AgentServer 架构文档

## 概述

本文档用 mermaid 图与设计决策表描述 `jiuwenswarm.server` 的整体架构、请求处理流、runtime 分层与 sandbox 生命周期。与 `server_overview.md`（特性/功能叙事）互补，本文聚焦**分层关系、数据流方向、不变量**。

---

## 架构总览

```mermaid
graph TD
    subgraph 上游
        Gateway[Gateway<br/>jiuwenswarm/gateway]
        Clients[前端 / TUI / IM 频道客户端]
    end

    subgraph "AgentServer 进程（单例）"
        direction TB
        subgraph "传输层"
            WS[WebSocket 主链路<br/>默认开, 端口 18000/18092]
            HTTP[HTTP/SSE 可选入口<br/>默认关]
        end

        subgraph "协议/分发层"
            WireParse[wire_parse.py<br/>E2AEnvelope → AgentRequest]
            Pipeline[pipeline.py<br/>dispatch_parsed_request 汇合点]
            Dispatch[dispatch.HANDLERS<br/>~100 条目注册表]
            WireTruncate[wire_truncate.py<br/>字节预算常量]
        end

        subgraph "Handlers（域划分）"
            HandlersBootstrap[bootstrap]
            HandlersChat[chat]
            HandlersSession[session]
            HandlersTeam[team]
            HandlersCommands[commands]
            HandlersAgent[agents]
            HandlersOps[ops]
            HandlersSandbox[sandbox]
            HandlersIMHosting[im_hosting]
            HandlersDefault[_default<br/>默认路径 fallback]
        end

        subgraph "Sink / PushRegistry"
            Sink[ResponseSink 三大家族<br/>WSSink / UnaryHTTPSink / SSESink]
            PushRegistry[PushRegistry<br/>WS + SSE 订阅者]
            GatewayPush[gateway_push.wire<br/>E2A chunk wire 标记]
        end

        subgraph "Runtime 管理层"
            AgentManager[AgentManager / TenantAgentPool]
            WarmPool[AgentWarmPool]
            Adapter[agent_adapter 三路<br/>deep / code / flash]
            A2UI[A2UI 协议]
            DebugTrace[debug_trace]
            Session[session/<br/>history / git_diff / kv_cache]
            Skill[skill/<br/>skilldev 11 stages]
            Workspace[workspace/<br/>fs_quota]
        end

        subgraph "Sandbox"
            SandboxRunner[JiuwenBoxRunner 单例]
            SandboxGate[startup_mode=internal<br/>+ enabled + 平台<br/>条件链 auto-start]
            SandboxPolicy[sandbox_policy_render<br/>win32 runtime 副本]
        end

        subgraph "IM 托管"
            ImConnector[im_connector Plugin<br/>钉钉 / 飞书 / WeLink]
            ImHosting[im_hosting.service<br/>poll_due / run_poll_once]
        end
    end

    subgraph "openjiuwen SDK"
        DeepAgent[DeepAgent / TeamAgent]
        Runner[Runner.invoke]
    end

    Gateway -- "E2AEnvelope WS 帧" --> WS
    Gateway -- "HTTP REST / SSE 订阅" --> HTTP
    Clients -- "HTTP" --> HTTP
    WS --> WireParse
    HTTP --> Pipeline
    WireParse --> Pipeline
    Pipeline --> Dispatch
    Dispatch --> HandlersBootstrap & HandlersChat & HandlersSession & HandlersTeam & HandlersCommands & HandlersAgent & HandlersOps & HandlersSandbox & HandlersIMHosting
    Pipeline --> HandlersDefault
    HandlersDefault --> AgentManager
    AgentManager --> Adapter
    Adapter --> Runner
    Runner --> DeepAgent
    HandlersBootstrap & HandlersChat & HandlersSession & HandlersTeam & HandlersCommands & HandlersAgent & HandlersOps & HandlersSandbox & HandlersIMHosting & HandlersDefault --> Sink
    Sink --> PushRegistry
    PushRegistry --> GatewayPush
    GatewayPush --> Gateway
    SandboxGate --> SandboxRunner
    AgentManager --> WarmPool
    ImConnector --> ImHosting
    ImHosting --> Pipeline
```

---

## 请求处理数据流

### WS / HTTP 汇合点

```mermaid
sequenceDiagram
    participant G as Gateway
    participant WS as WebSocketServer
    participant P as Pipeline.dispatch_parsed_request
    participant D as Dispatch.HANDLERS
    participant H as Handler / _default
    participant S as Sink
    participant R as PushRegistry

    G->>WS: E2AEnvelope WS 帧
    WS->>WS: wire_parse.parse_inbound → AgentRequest
    WS->>P: dispatch_parsed_request(ctx, request)

    Note over P: bind_log_session<br/>acp client caps 透传<br/>BEFORE_CHAT_REQUEST hook

    P->>D: dispatch_with_context(ctx, request)
    alt HANDLERS[req_method] 命中
        D->>H: HandlerSpec.resolve_fn(is_stream)(ctx, *args, **kwargs)
    else HANDLERS 未命中（chat.send / chat.resume 等）
        D-->>P: return False
        P->>H: _ensure_auto_team_binding_for_chat<br/>_handle_stream / _handle_unary
        H->>H: AgentWebSocketServer._resolve_adapter<br/>Runner.invoke → DeepAgent
    end

    H->>S: ctx.sink.send_wire(wire)
    S->>R: 推送 WirePushItem（经 PushRegistry.push）
    R->>G: E2A chunk wire / E2A_WIRE_SERVER_PUSH_KEY 标记

    rect rgb(255, 200, 200)
        Note over P,H: CancelledError 必须重抛<br/>否则 chat cancel 链断裂
    end
```

---

## Runtime 分层

```mermaid
graph TB
    subgraph "Handlers（业务层）"
        H1[chat]
        H2[session]
        H3[team]
        H4[commands]
        H5[ops]
        H6[sandbox]
        H7[im_hosting]
    end

    subgraph "agent_adapter（适配层）"
        A1[interface_deep<br/>默认原生 DeepAgent]
        A2[interface_code<br/>编码模式]
        A3[interface_flash<br/>轻量模式]
    end

    subgraph "agent_manager（装配层）"
        M1[AgentManager<br/>单租户]
        M2[TenantAgentPool<br/>企业多租户]
        M3[AgentWarmPool<br/>预热池]
    end

    subgraph "session / skill（状态层）"
        S1[session_manager<br/>任务队列 + 优先级]
        S2[session_history<br/>历史持久化]
        S3[skill_manager<br/>技能管理]
        S4[skilldev<br/>11 stages 自演进]
        S5[workspace<br/>fs_quota]
    end

    subgraph "a2ui（输出协议层）"
        A2UI1[protocol]
        A2UI2[response_finalization]
        A2UI3[stream_guard]
        A2UI4[validator]
    end

    subgraph "openjiuwen SDK"
        O1[DeepAgent / TeamAgent]
        O2[Runner.invoke]
    end

    H1 & H2 & H3 & H4 & H5 & H6 & H7 -- "ctx.services.agent_manager" --> M1
    M1 -- "_resolve_adapter(req_method, channel)" --> A1 & A2 & A3
    A1 & A2 & A3 -- "adapter.invoke / invoke_stream" --> O2
    O2 -- "Runner.create_harness" --> O1
    M1 -- "预热池 sync() / 复用" --> M3
    M1 -- "持有的状态组件" --> S1 & S2 & S3
    S3 -- "触发自演进" --> S4
    O1 -- "输出 → A2UI 运行时链" --> A2UI1 & A2UI2 & A2UI3 & A2UI4
```

**分层方向**：Handler（业务）→ agent_manager（装配）→ agent_adapter（适配）→ openjiuwen SDK（底层）。状态层（session / skill）由 agent_manager 持有，不被 handler 直接触碰（handler 经 `ctx.services.agent_manager` 间接访问）。a2ui 是输出协议层，插在 openjiuwen SDK 的 invoke 输出与 sink 之间。

---

## Sandbox 生命周期

```mermaid
stateDiagram-v2
    [*] --> Skipped: enterprise=true<br/>或 非 linux/win32<br/>或 enabled=false(win32)<br/>或 startup_mode 字段缺失<br/>或 startup_mode≠internal<br/>或 sandbox.type=yuanrong<br/>或 policy_file 无法解析
    Skipped --> [*]

    [*] --> AutoStartAttempt: AgentWebSocketServer.start()<br/>执行 _bootstrap_internal_jiuwenbox
    AutoStartAttempt --> BoxServerRunning: runner.ensure_running(timeout=180) 成功<br/>→ update_sandbox_endpoint<br/>→ update_sandbox_runtime({"enabled": True})
    AutoStartAttempt --> BoxServerFailed: runner.ensure_running 超时/失败<br/>→ logger.warning + return<br/>（不阻断 AgentServer 启动）
    BoxServerFailed --> [*]

    BoxServerRunning --> TUIEnable: 用户 TUI /sandbox enable
    TUIEnable --> BoxServerRunning

    BoxServerRunning --> Shutdown: app_agentserver finally
    Shutdown --> Step1DeleteRemoteSandbox: 非 enterprise<br/>→ asyncio.to_thread(shutdown_jiuwenbox_sandboxes)<br/>HTTP DELETE 到 box-server
    Step1DeleteRemoteSandbox --> Step2StopBoxServer: JiuwenBoxRunner.stop()<br/>停 box-server 子进程
    Step2StopBoxServer --> [*]
    Shutdown --> Step2StopBoxServer: enterprise=true<br/>跳过 step 1
    Step2StopBoxServer --> [*]
```

---

## IM 托管生命周期

```mermaid
sequenceDiagram
    participant S as AgentWebSocketServer.start()
    participant H as HostingService
    participant P as Poller
    participant C as im_connector (Plugin)
    participant R as registry
    participant Pl as policy

    S->>S: enterprise=true？跳过<br/>非 enterprise：继续
    S->>H: get_hosting_service().start()
    H->>H: set_agent_manager(TenantAgentPool)
    H->>Pl: poll_interval / 窗口
    loop 周期轮询（按 policy）
        H->>P: poll_due()
        P->>C: registry.get(name).poll_once()
        C->>C: IM WS / CLI pull
        C-->>P: list[IMMessage]
        P->>P: parser.py → AgentRequest
        P->>H: dispatch_parsed_request(ctx, request)
        H->>R: reply_bridge.reply()
        R->>C: connector.send_reply()
    end
```

---

## 进程启动序（AgentServer.main → _run）

```mermaid
sequenceDiagram
    participant CLI as app_agentserver.main()
    participant D as dotenv_early
    participant WS as AgentWebSocketServer
    participant Ext as ExtensionManager
    participant HTTP as AgentHTTPServer（可选）
    participant Sandbox as _bootstrap_internal_jiuwenbox
    participant IM as im_hosting（非 enterprise）
    participant Signal as signal

    CLI->>D: parse_dotenv_early("jiuwenswarm-agentserver")<br/># 零依赖
    D-->>CLI: env 就绪
    CLI->>CLI: workspace 初始化<br/>update_config (enterprise 跳过)
    CLI->>CLI: 一批 apply_*_patch()<br/># 顺序后置
    CLI->>Ext: ExtensionManager.load_all_extensions
    Ext-->>CLI: extensions loaded
    CLI->>WS: AgentWebSocketServer.get_instance()<br/># 单例构造
    WS->>WS: AgentManager / JiuwenBoxRunner / PersonalContextHostAPI 持有
    CLI->>WS: server.start()
    WS->>WS: legacy_serve 开 WS 端口<br/># listen 先于后续
    WS->>WS: interface_deep 预热<br/># 阶段 1+2 独立 task
    WS->>Sandbox: _bootstrap_internal_jiuwenbox<br/># best-effort
    Sandbox-->>WS: ok / warning（不阻断）
    WS->>WS: event_loop_lag_monitor 启动
    alt 非 enterprise
        WS->>IM: hosting.start()
    end
    CLI->>HTTP: AgentHTTPServer.start()（可选）
    HTTP-->>CLI: ok / False（失败不阻断 WS）
    CLI->>Signal: SIGINT / SIGTERM → stop_event.set()
    Note over CLI: await stop_event.wait()<br/># 主循环直至信号
```

---

## 设计决策表

| # | 决策 | 方案 | 拒绝的方案 | 理由 |
|---|---|---|---|---|
| D1 | AgentServer 进程定位 | Gateway 的下游 Agent 运行时，独立进程 | 与 Gateway 同进程合并 | **保持进程级隔离**：AgentServer 崩溃不拖垮 Gateway；Gateway 可独立扩展为多 AgentServer 后端（负载均衡 / 冷热备）。 |
| D2 | HANDLERS 注册表静态构建 | `dispatch.py` import 时一次性构建 ~100 条目 | 动态 registry + decorator 注册 | **确定性**：进程启动时就能 grep 全量注册表，IDE 跳转直接可见；动态注册会让 HANDLERS 条目分散全仓，重构时漏改。 |
| D3 | Handler 传输无关（不碰 WS/HTTP） | 统一 `async def (ctx: RequestContext) -> None` | handler 直接拿到 ws / http request 对象 | **同一份 handler 同时服务 WS 与 HTTP**：违反 = 新增传输时重写 handler。 |
| D4 | ResponseSink 三大家族 | WSSink / UnaryHTTPSink / SSESink 继承同一个基类 | 平行实现（各 sink 独写一套） | **统一契约 + 统一测试**。新增传输只需新增一个 sink 子类。 |
| D5 | Gateway WS 推送异常吞噬 | `_GatewayWSPushSink` 包装 WSSink，异常吃内返回 False | 让 PushRegistry 按默认语义（异常即注销） | Gateway WS 是**控制面**：瞬时 send 失败不应让整个 Gateway 掉线等重连。普通订阅者（用户 SSE / 普通 WS）默认注销语义正确（坏连接尽早踢掉）。 |
| D6 | Connection ID 跨请求稳定 | WS = `str(id(ws))`，HTTP = 常量 | 每请求唯一 uuid | **ACP 客户端能力、session.switch 切换锁靠分槽**；取每请求唯一值会让它们退化成零互斥（静默错，不报错）。 |
| D7 | Sandbox auto-start best-effort | 失败一律 warning，绝不阻断 AgentServer 启动 | 失败即 SystemExit | **运维误配不能让整个产品起不来**。`/sandbox enable` 是显性入口，比启动时隐式拉起更可控。 |
| D8 | startup_mode 必须显式配置 | `get_sandbox_startup_mode_explicit()` 字段缺失返回 None，**不走默认回落** | `get_sandbox_startup_mode()` 字段缺失回落 internal | **避免开箱即拉起 jiuwenbox**：用户升级后突然多出 box-server 进程是破坏性行为。 opt-in。 |
| D9 | shutdown 两步顺序固定 | 先 DELETE 远端沙箱，再停 box-server 子进程 | 反过来停 box-server 再 DELETE | DELETE 需要 box-server 活着才能响应；反过来 DELETE 必败、沙箱没清。box-server 进程退出时 FastAPI lifespan shutdown 兜底调 shutdown_all_sandboxes。 |
| D10 | enterprise 并发写防竞 | AgentServer 进程内 `update_config()` 仅 `not is_enterprise()` 调用 | 企业版也在 AgentServer 启动时 merge 写 config.yaml | K8s 多 Pod 共享 PVC：并发 Pod 同时写 config.yaml 会导致写竞态。配置由部署侧/init 写入 PVC，运行时经 Gateway reload_config 热更新。 |
| D11 | prewarm 阶段 3 独立 fire-and-forget | 阶段 3（临时 DeepAgent + query）不被阶段 1/2 的兜底 await 阻塞 | 阶段 3 也放进兜底 await | 避免首请求被 LLM 真实调用（最长 120s）阻塞；mock_model 默认 True 零成本预热。 |
| D12 | AgentWarmPool 排除 acp/a2a | WarmKey.channel ∈ {acp, a2a} 不走预热池 | 全通道统一预热 | acp/a2a 是外部 agent control plane，自身管理生命周期；预热池为 jiuwenswarm 内部通道设计。 |
| D13 | enterprise 冷加载序列 | 每个 `reload_*_from_gateway_db` 调用失败只记 warning | 冷加载失败即 SystemExit | PVC 缺表或缺字段时 AgentServer 仍能起来，不阻断 Gateway 侧正常启动；字段补齐后经 reload_config 热更新。 |
| D14 | Patch 模块刻意后置 | `app_agentserver.py` 里 `apply_*_patch()` 放在 Runner/ExtensionManager import 之后 | 随手插到文件开头 | Runner 是被 patch 覆盖的目标之一；patch 必须在 import Runner 之后、Runner 首次使用之前执行。顺序错 = patch 不生效。 |
| D15 | sandbox runner Python 必须是标准 CPython | `_is_std_cpython()` 判定 `python.exe` 父目录有 `python3*.dll` 且不在 Scripts/ 子目录 | 接受 uv trampoline / venv launcher | jbx-sandbox 跑不了 uv trampoline/venv launcher（WinError 5）；runner 必须是标准 CPython 根目录。 |
| D16 | wire_truncate 常量集中定义 | 所有 history/workflow_snapshot 字节预算集中在 wire_truncate.py | handler 里各写各的常量 | **字节预算单一真相源**。改预算先改这里，不要硬编码。 |

---

## 与其它模块的关系

| 模块 | 关系 |
|---|---|
| `openjiuwen.core` / `openjiuwen.harness` | AgentServer 的底层运行时：`Runner.invoke` → DeepAgent |
| `openjiuwen.agent_teams` | AgentServer 通过 `handlers/team.py` 暴露 team API，不实现 team 逻辑 |
| `jiuwenswarm.common.e2a` | E2A wire 协议（E2AEnvelope / wire_codec），**Gateway ↔ AgentServer 的消息编码统一来源** |
| `jiuwenswarm.common.schema` | AgentRequest / AgentResponse / AgentResponseChunk / ReqMethod 枚举 |
| `jiuwenswarm.gateway` | 上游网关（AgentServer 的 Gateway 客户端部署在 Gateway 进程内） |
| `jiuwenswarm.instance_manager` | 多实例启动编排；AgentServer 进程启动由 `start_services.py` 编排 |
| `jiuwenswarm.server.runtime.skill_turbo` | Server 子树内部的 Turbo 执行框架（有自己的 AGENTS.md 与 docs/specs/），AgentServer 装配它但不侵入 |

---

## 能力边界

**已实现的** / **暂不支持的** 完整列表见 `server_overview.md` 「能力边界」节。
