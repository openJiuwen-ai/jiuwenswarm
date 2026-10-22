---
description: jiuwenswarm server 模块说明书——AgentServer 的 HTTP/WebSocket 入口、请求流水线、handlers 注册表、runtime 运行时、sandbox 生命周期与 IM 托管。给修改/检视本模块代码的 AI 助手与工程师。
---

# server — JiuwenSwarm AgentServer

`jiuwenswarm.server` 是 JiuwenSwarm 的 AgentServer 子进程，对上连接 Gateway（WebSocket 主链路 + HTTP/SSE 可选入口），对下装配 **openjiuwen DeepAgent / TeamAgent** 运行时。进程启动顺序、请求分发、handler 注册表、agent 生命周期、sandbox 拉起、IM 频道托管全部落在这里。

本文件是模块入口索引。**修改任何代码前先读对应 spec**（见「设计文档归档与双向同步」）；深入细节跳转：

- `docs/specs/S_01_overview-and-boundaries.md` — 模块总览与包边界（子模块划分 / 依赖方向 / 消费者全景 / 已知遗留）
- `docs/specs/S_02_request-pipeline-dispatch.md` — 请求流水线、HANDLERS 注册表、HandlerSpec 契约、默认路径 fallback、Sink 三大家族
- `docs/specs/S_03_runtime-agent-management.md` — AgentManager（单例/企业版 TenantAgentPool）、AgentWarmPool、agent_adapter 三路接口、a2ui、A2A/A2UI、debug_trace、tenant_context
- `docs/specs/S_04_sandbox-lifecycle.md` — sandbox 启动条件（startup_mode=internal + enabled + 平台）、JiuwenBoxRunner 单例、生命周期、端口分配、企业版 sidecar 例外
- `docs/specs/S_05-im-channel-integration.md` — im_connector Plugin 协议、钉钉/飞书/WeLink 连接器、im_hosting 托管轮询、login/gate/policy

面向**理解系统**（而非改代码）的叙事文档在 `docs/designs/`：

- `docs/designs/server_overview.md` — 特性文档：AgentServer 的功能、原理、能力边界与术语表
- `docs/designs/architecture_cn.md` — 架构文档：请求流 mermaid 图 / runtime 分层图 / sandbox 状态机 / 设计决策表

## 统一术语

| 术语 | 含义 |
|---|---|
| **AgentServer** | 本模块的单例服务进程（`AgentWebSocketServer`），Gateway 的下游 |
| **Gateway** | 上游网关（`jiuwenswarm/gateway`），对前端/IM 频道暴露 |
| **E2A / wire 协议** | Gateway ↔ AgentServer 的消息协议（`jiuwenswarm/common/e2a/`），由 `wire_codec` 编解码 |
| **ReqMethod** | `enum ReqMethod`：每条入站请求的 type 字符串枚举，`dispatch.HANDLERS` 的注册表 key |
| **HandlerSpec** | 一条分发规则（frozen dataclass：`fn + args + kwargs + stream_fn`） |
| **HANDLERS** | `dispatch.py` 里的 `dict[ReqMethod, HandlerSpec]` 注册表（模块加载时静态构建） |
| **默认路径 / fallback** | `HANDLERS` 未命中的请求 → `handlers._default._handle_stream / _handle_unary` → openjiuwen SDK 调 Agent |
| **Sink / ResponseSink** | 响应出口基类，三大家族：`WSSink`（WS 推送）/ `UnaryHTTPSink`（HTTP 单次）/ `SSESink`（HTTP SSE 推送） |
| **RequestContext** | frozen dataclass：一次请求的 `request + sink + connection_id + services` |
| **AgentServerServices** | 服务端窄面门面（`context.py`），只暴露 `SERVICE_MEMBERS` 登记的公有名 |
| **PushRegistry** | 服务端主动推送订阅注册表（`transports/push_registry.py`），WS + SSE 订阅者 |
| **AgentManager** | server 侧 Agent 运行时核心管理器（`runtime/agent_manager.py`）；企业版用 `TenantAgentPool` |
| **AgentWarmPool** | 预热池，按 channel/project/work_mode 维度缓存已创建的 agent 实例 |
| **agent_adapter** | 运行时适配层：`interface_deep`（默认原生 DeepAgent）/ `interface_code` / `interface_flash` |
| **A2UI** | Agentic UI，面向 UI 的结构化 agent 输出协议（`runtime/a2ui/`） |
| **A2A** | Agent-to-Agent，跨 agent 协议（`common/e2a/acp/`） |
| **sandbox / jiuwenbox** | 沙箱：本地 jiuwenbox 子进程（`JiuwenBoxRunner` 单例），由 `AgentWebSocketServer.start()` 按 config.yaml 条件自动拉起 |
| **startup_mode** | sandbox 启动模式：`internal`（AgentServer 拉起子进程）/ `external`（外部拉起，K8s sidecar 模式企业版） |
| **im_connector** | IM 频道连接器抽象（`Plugin` 协议），钉钉/飞书/WeLink 各一份 |
| **im_hosting** | 用户态 IM 托管服务（`im_hosting/service.py`），按策略轮询 IM target |

## 公开入口（public API）

**`server/__init__.py` 当前仅 re-export 少量符号**（`AgentWebSocketServer`、`AgentManager`、`TenantAgentPool` 等），新增公开符号先进对应子模块再同步聚合。`AgentWebSocketServer` 是**进程级单例**——通过 `get_instance(*args, **kwargs)` 获取，第一次调用时构造，后续返回已存实例。

| 域 | 入口 | 说明 |
|---|---|---|
| 启动入口 | `jiuwenswarm.server.app_agentserver.main()` | AgentServer 独立进程 CLI（`jiuwenswarm-agentserver` 命令） |
| 服务实例 | `AgentWebSocketServer.get_instance(host, port, ...)` | 单例，WS 主链路 |
| HTTP 可选 | `AgentHTTPServer(server, host, port)` | 与 WS 共享 handler 的 HTTP/SSE 入口，默认关闭 |
| 运行时管理器 | `AgentManager()` / `TenantAgentPool.get_instance()` | 单租户 / 企业多租户两种形态 |
| 请求流水线 | `dispatch.dispatch_parsed_request(ctx, request)` | WS 和 HTTP 汇合的统一处理流水线 |
| 分发注册表 | `dispatch.HANDLERS: dict[ReqMethod, HandlerSpec]` | 静态构建 |
| 推送注册 | `transports.push_registry.get_push_registry()` | 主动推送统一注册表 |

## 模块地图

```
server/
├── __init__.py                       # 公开 API 聚合（AgentWebSocketServer / AgentManager 等）
│
├── app_agentserver.py                # AgentServer 独立进程启动序（dotenv → workspace → logging → patch → 扩展 → WS → HTTP → sandbox → signal → finally 清理）
│   # 启动链关键点：
│   # - dotenv_early.parse_dotenv_early() 在模块顶部零依赖执行
│   # - update_config() 非 enterprise 才调（企业版并发写竞态）
│   # - 一批 jiuwenswarm.patch 在 import 顺序上刻意后置
│   # - AgentWebSocketServer.get_instance() 是 WS 单例唯一入口
│   # - LinkMTLSConfig.from_env(role="agentserver") 是 HTTPS 强制开关
│
├── agent_ws_server.py                # WS 服务端主类 AgentWebSocketServer（单例）
│   # 持有：AgentManager / JiuwenBoxRunner / PersonalContextHostAPI / AcpClientCapabilities
│   # start() 行为：before_ws_server_start_hook → legacy_serve → interface_deep 预热两阶段 → sandbox auto-start → event_loop_lag_monitor → im_hosting
│   # send_push(msg)：经 PushRegistry 向所有 WS+SSE 订阅者推送 E2A chunk wire
│   # sandbox auto-start 条件链：enterprise 跳过 → macOS 跳过 → enabled=true → startup_mode=internal → 平台是 linux/win
│   # 构造时默认 host=127.0.0.1 port=18000；命令行 --port 18092 覆盖
│
├── agent_http_server.py              # HTTP/SSE 服务端（可选，默认关）
│   # 与 WS 共享同一个 AgentWebSocketServer 实例 → 复用 handler
│   # SSESink：HTTP 客户端订阅 /api/v1/events/stream 注册推送订阅者
│   # resolve_http_server_settings() 优先级：config.yaml http_server.enabled → AGENT_HTTP_ENABLED env → 强制 mTLS
│
├── agent_http_routes.py              # HTTP 路由表（~60 条 REST 端点）
│   # 覆盖：initialize / sessions / chat / commands / agents / teams / skills / mcp / sandbox / ops
│   # 所有路由最终调 server.invoke_unary() → 走 dispatch_parsed_request
│
├── context.py                        # RequestContext frozen dataclass + AgentServerServices 门面
│   # RequestContext = request + sink + connection_id + services
│   # SERVICE_MEMBERS 是 dict[str, str]：业务层公有名 → AgentWebSocketServer 上的受保护属性名
│   # AgentServerServices.__getattr__ 拦截越界访问 → AttributeError（防止业务代码摸 server._xxx）
│
├── wire_parse.py                     # 入站 wire 解析（WS 帧 JSON → E2AEnvelope → AgentRequest）
├── wire_truncate.py                  # 历史/工作流快照字段截断常量与工具函数（字节预算 + 字符串长度保护）
├── ws_send.py                        # 出站 wire 发送辅助（send_wire_payload 等）
│
├── pipeline.py                       # 请求处理流水线（WS/HTTP 汇合点）
│   # dispatch_parsed_request(ctx, request)：
│   #   1. bind_log_session
│   #   2. acp_client_capabilities 透传到 metadata
│   #   3. BEFORE_CHAT_REQUEST hook
│   #   4. dispatch_with_context（命中 HANDLERS → return）
│   #   5. _ensure_auto_team_binding_for_chat
│   #   6. 默认路径 _handle_stream / _handle_unary（openjiuwen SDK 调 Agent）
│   # CancelledError 必须重抛（吞掉会破坏上层中断判定）
│
├── dispatch.py                       # 请求分发注册表（单一真相源）
│   # HANDLERS: dict[ReqMethod, HandlerSpec]（模块 load 时静态构建N个条目）
│   # HandlerSpec frozen dataclass：fn + args + kwargs + stream_fn
│   # resolve_fn(is_stream)：stream_fn 存在且 is_stream=True 才用 stream_fn
│   # _schedule(action)：schedule.* / issue.* 共用一个 handle_schedule_request，按 action 分派
│   # _register_permissions_methods()：permissions.* 动态注册（防双注册 RuntimeError）
│   # dispatch_to_handler() 与 dispatch_with_context()：两条入口
│
├── sandbox_lifecycle.py              # sandbox 生命周期：shutdown_jiuwenbox_sandboxes（HTTP DELETE box-server）
├── sandbox_config_rpc.py             # sandbox 配置 RPC（同步 handler 在工作线程调 run_coroutine_threadsafe）
├── sandbox_policy_render.py          # Windows sandbox policy runtime 副本（ensure_copy_exists）
├── tool_concurrency.py               # 工具并发门禁（React.concurrency AbilityManager hook）
├── event_loop_monitor.py             # 事件循环 lag 观测（sleep(1) 唤醒延迟 >1s 警告 >5s ERROR）
├── file_transfer_manager.py          # Gateway↔AgentServer 分块文件传输管理
│
├── gateway_push/                     # Gateway 下行推送专用 wire
│   ├── transport.py                  # Gateway WS 传输适配
│   └── wire.py                       # build_server_push_wire：send_push → E2AResponse chunk wire，打 E2A_WIRE_SERVER_PUSH_KEY
│
├── sandbox/                          # 本地 jiuwenbox 子进程
│   ├── jiuwenbox_runner.py           # JiuwenBoxRunner 单例：ensure_running / shutdown / get_stderr_tail
│   └── port_util.py                  # 端口探测与分配
│
├── transports/                       # 传输层：Sink 三大家族 + PushRegistry
│   ├── sink.py                       # ResponseSink 基类 + WSSink + UnaryHTTPSink + SSESink
│   ├── push_registry.py              # PushRegistry：WS + SSE 订阅者管理，按 session/channel 过滤，推送超时自动注销（但 Gateway WS 用 _GatewayWSPushSink 吞异常）
│   └── link_sse_response.py          # SSE 链路响应
│
├── utils/                            # 通用工具
│   ├── diff_service.py               # git diff 差异服务
│   ├── stream_utils.py               # 流式辅助
│   └── utils.py                      # is_team_params 等
│
├── handlers/                         # ~22 个 handler 文件（域划分，各 handler 注册到 dispatch.HANDLERS）
│   ├── __init__.py                   # import dispatch 模块级副作用：触发 handlers 全部导入（注册表填充）
│   ├── _default.py                   # 默认路径 fallback：_handle_stream / _handle_unary（openjiuwen Runner 调 Agent）+ 模式解析 / plan 激活 / team binding
│   ├── _shared.py                    # 进程级共享状态：_sessions_dir_for_request / resolve_agent_request_mode / _CODE_MODE_SYNC_METHODS / _SESSION_PREVIOUS_MODE_KEY / _inject_plan_mode_activation_reminder
│   ├── bootstrap.py                  # INITIALIZE / SESSION_CREATE / SESSION_FORK / ACP_TOOL_RESPONSE：连接引导 + checkpointer 等待
│   ├── chat.py                       # CHAT_SEND / CHAT_RESUME / CHAT_CANCEL + _ensure_auto_team_binding_for_chat（团队绑定兜底）
│   ├── session.py                    # SESSION_* / HISTORY_GET：会话管理 + history 截断（wire_truncate 常量）
│   ├── steering.py                   # CHAT_STEER / CHAT_STEER_STATUS：中途引导
│   ├── team.py                       # TEAM_* 全域：templates / bindings / session_bind / snapshot / mq_publish / members / tasks_dependencies
│   ├── commands.py                   # COMMAND_*：workflows / compact / recap / diff / context / status / model / ...
│   ├── mcp.py / mcp_servers.py       # MCP：command.mcp + mcp_server.add/remove/update/list/get
│   ├── extensions.py                 # EXTENSIONS / HOOKS / HARNESS_PACKAGES
│   ├── permissions.py                # permissions.* 动态注册（get_permissions_config_req_methods）
│   ├── schedule.py                   # schedule.* / issue.* 共用 handle_schedule_request，按 action 分派
│   ├── agents.py                     # AGENTS_*：list/get/create/update/delete/enable/disable/tools_list
│   ├── workspace.py                  # WORKSPACE_*：tree / usage / entries_delete / preview / download
│   ├── sandbox.py                    # command.sandbox + 内部 allocate_internal_jiuwenbox_port / parse_sandbox_host_port
│   ├── file_transfer.py              # FILE_TRANSFER_*：start/chunk/complete 按 event_type 分派
│   ├── im_hosting.py                 # IM_HOSTING_STATUS / im_hosting_list / im_hosting_poll
│   ├── long_horizon.py               # LONG_HORIZON：长程任务
│   └── ops.py                        # PROACTIVE_TICK / BROWSER_RUNTIME_RESTART / CONFIG_CACHE_CLEAR / AGENT_RELOAD_CONFIG / SYNC_AGENTS_CONFIGS / AGENT_PREWARM_SYNC
│
├── hooks/                            # 用户钩子
│   ├── executor.py                   # HookExecutor：钩子执行器
│   └── user_hook_rail.py             # UserHookRail：rail 形态注入 harness
│
├── personal_context/                 # 个人上下文（Distill）
│   ├── host_api.py                   # PersonalContextHostAPI（AgentWebSocketServer 持有单例）
│   ├── profiles_ops.py               # 画像操作
│   ├── ws_handler.py                 # WS 请求处理（PERSONAL_CONTEXT_REQUEST_METHODS 集合）
│   └── distill_ports.py              # Distill 端口分配
│
├── runtime/                           # Agent 运行时管理层——AgentManager 是核心；其余子模块各管一域
│   ├── agent_manager.py              # AgentManager（单租户）/ resolve_adapter（按 req_method/channel 选 interface_deep/code/flash）
│   ├── tenant_agent_pool.py          # TenantAgentPool（企业多租户）：TenantContext → per-tenant AgentManager 池
│   ├── agent_warm_pool.py            # AgentWarmPool：按 WarmKey(channel, project, work_mode) 预热 → sync() 按期望 key 集合重建
│   ├── agent_config_service.py       # 聚合 agent 配置服务
│   ├── prewarm.py                    # 预热三阶段：interface_deep import → checkpointer → 临时 DeepAgent query（mock_model 默认 True）
│   ├── agent_adapter/                # 适配层：interface_deep / interface_code / interface_flash + evolution_* + interrupt_state_machine + steering + team_helpers + workspace_host_init
│   ├── a2ui/                         # A2UI 协议 + runtime（finalizer/formatter/response_finalization）+ validator + stream_guard
│   ├── debug_trace/                  # /debug 模式调试追踪：stream_logger + directives + subagent_capture + task_tool_patch
│   ├── enterprise_config/            # 企业配置 loader + apply_models / apply_mcp / db_queries
│   ├── session/                      # 会话管理：session_manager（任务队列 + 优先级 + 一次性判定）+ session_history + git_diff_watcher + kv_cache 亲和 + permission_response_ledger + work_mode
│   ├── skill/                        # 技能管理：skill_manager + source_registry + artifact_security + enterprise_state_lock + skilldev 自演进流水线（11 stages：init → plan → generate → desc_optimize → test_design → test_run → evaluate → improve → package → validate → stages 编排）
│   ├── skill_turbo/                  # Skill Turbo 执行框架（**已有独立 AGENTS.md + docs/specs/**）：
│   │                                 #     见 jiuwenswarm/server/runtime/skill_turbo/AGENTS.md 与其
│   │                                 #     docs/specs/S_01~S_09；server 层 specs（S_01~S_05）不重复
│   │                                 #     覆盖 Turbo 内部设计，阅读者从 skill/ 或 Turbo 自身文档切入。
│   ├── workspace/                    # 工作区配额：fs_quota_guard + policy_reload + usage_reconciler
│   ├── team_binding_store.py         # 团队绑定存储
│   ├── team_entity_store.py          # 团队实体存储
│   └── team_snapshot_refresh.py      # 团队快照刷新
│
└── im/                               # IM 频道接入（钉钉/飞书/WeLink）
    ├── im_connector/
    │   ├── plugin.py                 # Plugin 协议（connector 必须实现的接口）
    │   ├── registry.py               # 连接器注册
    │   ├── types.py / messages.py / reply.py / learning_source.py / backed.py / cli_runtime.py
    │   └── connectors/<dingtalk|feishu|welink>/  # 每个连接器：cli.py + parser.py + plugin.py
    └── im_hosting/
        ├── service.py                # 托管服务：poll_due() / run_poll_once()
        ├── poller.py                 # 轮询器
        ├── auto_host.py              # 自动托管
        ├── store.py / gate.py / policy.py / login.py
        ├── reply_bridge.py           # 回复桥接（IM 回复 → AgentServer 入站）
        └── connectors.py / cli_resolve.py / paths.py
```

## 架构铁律

1. **Handler 传输无关**：所有 handler 签名必须是 `async def (ctx: RequestContext) -> None`，不直接碰 WS/HTTP 对象，所有输出经 `ctx.sink.send_wire`（Sink 三大家族基类契约）。违反 = 同一份 handler 不能同时服务 WS 与 HTTP。
2. **HANDLERS 注册表单一真相源**：`dispatch.py` 的 `HANDLERS` 是入站分发的唯一依据。新增/改动请求类型先加 ReqMethod 枚举值 → 写 handler → 注册到 HANDLERS → 同步 HTTP routes（如有 RESTful 入口）。不要绕过 HANDLERS 把逻辑直接塞进 pipeline。
3. **Connection ID 跨请求稳定**：`RequestContext.connection_id` 必须在同一 WS / HTTP 连接的所有请求上保持稳定（WS = `str(id(ws))`，HTTP = 常量 `HTTP_CONNECTION_ID`）。ACP 客户端能力、session.switch 切换锁都靠它分槽——取每请求唯一值会让它们静默退化成零互斥。
4. **Service 门面窄面**：handler 只能通过 `ctx.services` 访问 `SERVICE_MEMBERS` 登记能力；越界访问 `AttributeError`。AgentWebSocketServer 的受保护属性 `_xxx` 名不变，业务层看不到。新增能力必须同步在 `context.py` 的 `SERVICE_MEMBERS` 登记一行。
5. **CancelledError 必须重抛**：`pipeline.dispatch_parsed_request` 的 except 分支吞 `CancelledError` = 破坏上层中断判定（`asyncio.gather` 拿不到 CancelledError → `task.cancelled()` 为 False）。chat.py 的 cancel 分支正是依赖这一信号。
6. **Sandbox auto-start best-effort**：`AgentWebSocketServer.start()` 里的 sandbox auto-start **绝对不能阻断进程启动**——用户手改 config.yaml 误配会让整个产品起不来；失败一律记 warning，留给 TUI `/sandbox enable` 重试。企业版 enterprise 由 sidecar 提供，跳过本地拉起。
7. **Gateway WS 推送不能因瞬时失败断开**：PushRegistry 对抛异常的订阅者默认当场注销，但 Gateway WS 要特殊保留——`agent_ws_server._GatewayWSPushSink` 把异常吃在内部、返回 False，注册表因此不会注销。**不要把裸 WSSink 塞进 PushRegistry 给 Gateway WS**。
8. **Sink 三大家族契约**：`ResponseSink` 基类要求 `send_unary / send_chunk / send_error / send_wire` 四个方法全实现。新增传输类型（如未来的 NATS）必须继承 ResponseSink 实现这四个方法，不要在 transports 下搞平行类。
9. **A2UI / A2A / ACP 三条路径正交**：A2UI（面向 UI 的结构化 agent 输出）、A2A（agent-to-agent 协议）、ACP（外部 agent control plane）是三条独立的出站通道，互不替代。写新的输出协议先确认归哪一条，不要让三条路径互相重叠。
10. **Patch 模块刻意后置**：`app_agentserver.py` 里的一批 `apply_*_patch()` 在 import 顺序上刻意放在 `Runner`、`ExtensionManager` 等之后（参见模块顶部注释的理由）。往 import 序列里加新 patch 必须先审顺序依赖，**不要随手插到文件开头**。
11. **企业版并发写防竞**：企业多 Pod 共享 PVC，`update_config()` 由部署侧/init 写入 PVC，AgentServer 不再 merge 写 `config.yaml`；配置变更经 Gateway reload_config 热更新。app_agentserver 里 `if not is_enterprise(): update_config()` 不是装饰，是为了防止企业版并发 Pod 的 config 写竞态。
12. **Session namespace 不与 Agent namespace 混**：session/ 只管会话生命周期、历史、git diff watcher、kv_cache 亲和、permission ledger——**不碰 agent 装配**。Agent 装配归 runtime/agent_manager；skilldev 自演进归 runtime/skill/skilldev；sandbox 启动归 AgentWebSocketServer.start()。跨域调用必须显式过门面，不要 session/ 直接 import sandbox。

## 生命周期 / 配置消费

### 进程启动序（`app_agentserver.main()` → `_run()`）

```
1. parse_dotenv_early("jiuwenswarm-agentserver") —— 零依赖早期解析 --dotenv
2. workspace 初始化：migrate_legacy_user_config_if_needed → prepare_workspace / update_config（enterprise 跳过）
3. bootstrap_openjiuwen_logging —— pin openjiuwen log dir
4. 一批 jiuwenswarm.patch 按顺序应用（GLM XML / MaaS span_id / auth header / evolution rail / thinking / perf / debug trace）
5. Runner.callback_framework → ExtensionManager.load_all_extensions
6. enterprise 版冷加载 task_memory / memory / logging_levels / audit_log / permissions / workspace_quota
7. AgentWebSocketServer.get_instance() —— 单例构造（AgentManager + JiuwenBoxRunner + PersonalContextHostAPI + AcpClientCapabilities）
8. server.start() —— legacy_serve 开 WS 端口 → interface_deep 预热两阶段 → sandbox auto-start → event_loop_lag_monitor → im_hosting.start（enterprise 跳过）
9. AgentHTTPServer（可选，默认关，link_mtls.enforced 时强制开启）
10. init_proactive_engine —— 主动推荐引擎
11. run_teammate_bootstrap_daemon —— 分布式 teammate bootstrap 守护
12. start_mcp_registry_runtime
13. mem_reclaimer.setup() —— GC 周期回收（默认关）
14. signal.SIGINT/SIGTERM → stop_event.set()
15. await stop_event.wait() —— 主循环
16. finally 清理：usage_reconciler.stop → MCP registry stop → HTTP stop → server.stop() → jiuwenbox DELETE 远端沙箱 → box-server 子进程 stop → perf flush → observability shutdown → history.flush
```

### 请求处理流水线（WS / HTTP 共享）

```
WS: agent_ws_server._process_request → wire_parse.parse_inbound → pipeline.dispatch_parsed_request
HTTP: agent_http_routes 路由 → agent_http_server._make_ctx → server.invoke_unary → pipeline.dispatch_parsed_request
  ↓
1. bind_log_session
2. acp_client_capabilities 透传到 metadata
3. BEFORE_CHAT_REQUEST hook
4. dispatch_with_context: HANDLERS[req_method] 命中 → return
5. _ensure_auto_team_binding_for_chat（团队绑定兜底）
6. 默认路径 _handle_stream / _handle_unary → openjiuwen Runner.invoke
  ↓
响应 / push → ctx.sink.send_wire → transports.push_registry / gateway_push.wire
```

### 环境变量

| 变量 | 用途 | 默认 |
|---|---|---|
| `AGENT_SERVER_HOST` | WS 监听 host | `127.0.0.1` |
| `AGENT_SERVER_PORT` / `AGENT_PORT` | WS 监听 port（CLI --port 覆盖） | `18092`（CLI 默认）；WS 构造默认 `18000` |
| `AGENT_HTTP_ENABLED` | HTTP 入口开关 | 未设 → 走 config.yaml http_server.enabled |
| `JIUWENSWARM_<TYPE>_PORT` | sandbox / web 等基础端口覆盖 | - |
| `JIUWENSWARM_DATA_DIR` | 实例数据根 | 默认 workspace |

### 文件系统落点

| 路径 | 用途 |
|---|---|
| `~/.jiuwenswarm/config/config.yaml` | 主配置 |
| `~/.jiuwenswarm/config/.env` | 环境变量覆盖 |
| `~/.jiuwenswarm/agent/jiuwenclaw_workspace/` | 默认 agent 工作空间 |
| `~/.jiuwenswarm/agent/sessions/` | session 历史 |
| `~/.jiuwenswarm/.personal_context/` | PersonalContext 数据 |
| `~/.jiuwenswarm/instances.yaml` | 多实例注册表（具名实例） |

## 测试

server 模块测试路径分散：

```
tests/unit_tests/server/               # 顶层 pipeline / dispatch / context / sink 单测
tests/unit_tests/server/handlers/      # handlers/* 域测试
tests/unit_tests/server/runtime/       # runtime/* 域测试
tests/unit_tests/server/im/            # im_connector / im_hosting 测试
tests/integration/test_agent_server.py # 进程级集成
```

pytest.ini 的 addopts 含全仓 coverage，日常跑请清掉：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit_tests/server/ -o addopts="" -x
```

- **改 HANDLERS 注册表或 HandlerSpec 契约时**，先跑 `tests/unit_tests/server/test_dispatch.py`（注册表完整性、HandlerSpec 构造、stream_fn 分支）。
- **改 RequestContext / AgentServerServices 门面时**，必须确认 HANDLERS 里没有 handler 直接访问 server 受保护属性——如果有，同步进 SERVICE_MEMBERS 并走门面。
- **改 sandbox auto-start 条件链时**，对照 `docs/specs/S_04_sandbox-lifecycle.md` 的边界 case 清单（enterprise 跳过 / Linux+Windows 支持 / macOS 跳过 / enabled=false 跳过 / startup_mode != internal 跳过 / 非 yuanrong 跳过 / policy_file 缺失 / box-server spawn 失败 / port 冲突回退）。
- **改 PushRegistry 或 sink 实现时**，确认 Gateway WS 路径的异常吞噬仍在（`_GatewayWSPushSink` 类），别让一次瞬时 send 失败把 Gateway 踢出订阅名单。

## 代码风格

- **类型注解**：PEP 585 内置泛型 + PEP 604 联合类型（`X | None`）；存量 `Optional[X]` / `Dict[K,V]` 写法维持现状，不强改（本模块面向 3.10+ 兼容形态）。
- **注释 / docstring 英文为主**，关键机制处注释写"为什么"而非"是什么"（pipeline.py 的 CancelledError 重抛注释、wire_truncate.py 的字节预算注释、agent_ws_server.py 的 sandbox auto-start 条件链注释都是契约的一部分，清理时勿顺手删除）。
- **日志禁止 `print`**；业务层日志走 `server_logger.info/exception`，不直接写 `print`；用户可见错误经 `sink.send_error` 走 wire 协议，**不得用 traceback 糊给用户**。
- **常量 / 预算集中定义**：`wire_truncate.py` 的 `_HISTORY_WIRE_*` / `_TEAM_HISTORY_*` / `_WORKFLOW_SNAPSHOT_*` 是字节预算单一真相源，改预算先改这里，不要在 handler 里硬编码。

## 设计文档归档与双向同步（本模块强制）

本模块设计文档**全部**落在 `docs/` 下，命名与结构规约见 [`docs/AGENTS.md`](docs/AGENTS.md)。把 **`server/` 代码 + `docs/specs/` + `docs/designs/` + 本 AGENTS.md** 当作一个一致性单元维护——本模块的 HANDLERS 注册表、sandbox auto-start 条件链、RequestContext 契约、Service 门面窄面是多个域协作的公共面，"改了注册表不改文档"会让新 handler 漏掉 wire truncate、让 sandbox 误拉起进程、让业务层绕过服务门面。

**前置铁律——文档先是设计的输入，才是提交的产出。** 动手改任何代码**之前**，先读相关 `docs/specs/S_NN_*`（现有契约 / 不变量 / 修改检查单），拿它们校准方案；方案定了**当场**同步修订对应 spec。先码后档、拖到提交前才补文档是把流程做反了。

三条强制约束，提交时必查：

1. **特性更新归档 feature 且三连续提交**：`docs/features/` 新增 `F_NN_<slug>.md`；特性代码（`feat(server)`）、测试（`test(server)`）、文档（`docs(server)`）拆成三个紧邻提交、同一 PR 落地。bugfix 触及 spec 描述的路径/不变量时同样三连；纯缺陷修复归档 `docs/bugfix/B_NN_<slug>.md` 时合并为**一个** `fix(server)` 提交。
2. **规约变动必更新 specs**：HANDLERS 注册表条目增删 / RequestContext 字段 / SERVICE_MEMBERS 窄面成员 / sandbox auto-start 条件链 / AgentWarmPool key 结构 / sink 契约变化时，同步修订对应 `S_NN`（元信息"最近一次修订日期"填当天）；新规约主题 = 新 spec 文件。
3. **读到不一致当场修文档**：本文件 / specs 里与代码不符的接口名、常量数、路径、HandlerSpec 条目数，先 grep 代码、以代码为准刷新文档，同一次改动落地；不把过时表述当新约束执行。点名数量的句子（"SERVICE_MEMBERS"、"三大家族 sink"、"sandbox auto-start 七条件"）都是契约的一部分。

## 提交约定

本模块改动的 commit message scope 固定用 `server`（如 `feat(server): add chat.answer handler`）。

footer 用 `Refs: #<issue>` 格式关联 issue。issue 号若无法从当前上下文明确，必须先询问用户，不要臆造或留空。

涉及 `docs/features/F_*` / `docs/specs/S_*` 文档更新的特性改动，**特性代码、测试代码、文档拆成三个连续提交**（提交 1 特性代码 `feat(server)`，提交 2 单测 `test(server)`，提交 3 文档 `docs(server)`）；归档 `docs/bugfix/B_*` 的缺陷修复例外，代码、测试、文档合并为一个 `fix(server)` 提交。细则见上文「设计文档归档与双向同步」与 [`docs/AGENTS.md`](docs/AGENTS.md)。
