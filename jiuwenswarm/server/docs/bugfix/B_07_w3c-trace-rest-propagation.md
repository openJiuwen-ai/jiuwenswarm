# gateway→agentserver REST 边界丢弃 W3C trace 上下文，跨服务链路无法合并

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-28 |
| 严重程度 | 中（可观测性链路缺陷：一轮对话在 trace 存储中恒为两条独立 trace，跨服务排障需人工按 request_id 对账；不影响业务功能与身份链路） |
| 范围 | `jiuwenswarm/gateway/routing/agent_rest_map.py`、`jiuwenswarm/gateway/cron/scheduler.py`、`jiuwenswarm/server/agent_http_server.py`、`jiuwenswarm/server/agent_http_routes.py`、`tests/unit_tests/gateway/test_agent_rest_map_w3c.py`、`tests/unit_tests/server/test_agent_http_server_w3c.py` |
| 测试基线 | 新增 5 例（gateway 2 + server 3，含 1 例 request_context→build_agent_request 集成链）；同域既有回归 routes params + gateway client + runtime routed client 全绿；关联登记 issue #7365、修复 MR !11198 |
| 关联 spec | `S_02`（E2A 边界：本修复不改变信封契约，W3C 键经由既有 channel_context→metadata 通路）与 `S_13`（请求上下文：新增「REST 入口捕获 W3C 头并入 channel_context」一层，路由身份四字段契约不变） |

## 现象

同一轮会话在 trace 存储（Jaeger / collector file / manager 检索）中恒为两条独立 trace：gateway 侧 `channel.request` + `jiuwenclaw.gateway.agent.request` 一条，agentserver 侧 SDK 全树（`agent.*.run` / `llm.call` / `tool.*`）另一条，trace_id 不同，仅能靠 request_id 人工对账。cron 触发的会话还有第二处缺口：gateway client span 缺 `user.id` 属性。

## 根因

失效链条：

1. gateway 的 client span（`telemetry/gateway_client.py` `_open_client_span`）在统一遥测 active 时把 `traceparent`/`tracestate` 注入 `envelope.channel_context`——注入源正常；
2. REST 南向组装 `identity_headers`（`gateway/routing/agent_rest_map.py`）只产出 X-* 身份头与内部 ext 头，body 保持纯 params，**channel_context 中的 W3C 键在 REST 边界被丢弃**；
3. agentserver `build_agent_request → _build_channel_context`（`server/agent_http_server.py`）仅由路由身份头（`apply_routing_metadata`）与 ext 头重建 channel_context，**不读 HTTP 头里的 W3C 键**；
4. `e2a_to_agent_request` 把 channel_context 原样落成 `AgentRequest.metadata`，`bind_incoming_request`（`telemetry/request_context.py`）的 `_W3C_TRACE_CONTEXT.extract(metadata)` 从 metadata 顶层读 W3C 键——上游送不到则 extract 恒空，SDK run span 无父上下文。
5. cron 缺 user_id 同理：`gateway/cron/scheduler.py` 构建 envelope 的 metadata 未带 user_id，client span 的 `user.id` 读 `channel_context["user_id"]` 故缺失。chat.send 走默认路径、默认路径本就执行 `bind_incoming_request`，因此缺陷只在「上游送达」一段，agentserver 绑定逻辑无需改动。

## 决策

- **「HTTP 头透传 + 入口捕获」而非整体透传 channel_context**：REST body 保持纯 params 是既有边界契约（信封语义在 WS 主链路，REST 是身份头重建形态）；整体透传 channel_context 会把用户可控面整体引入 REST 边界（扩展键/内部键），与「channel_context 由 agentserver 按白名单子集重建」的既有设计冲突。仅透传 W3C 两键是最小面，且与既有 X-* 身份头同模式。
- **ContextVar 捕获而非改 `build_agent_request` 签名**：`invoke_unary` / `iter_stream` 与全部声明式路由共享 `request_context` 一个入口，ContextVar 让捕获与消费各自贴在既有函数边界内，不改任何调用方签名；uvicorn 每请求独立 task，ContextVar 请求级隔离，无跨请求泄漏。`request_context ↔ agent_http_server` 本就存在相互导入，routes 侧对 `capture_w3c_headers` 的延迟导入沿用既有循环规避模式。
- **default 用 None 而非共享可变 dict**：ContextVar 的 default 会被所有 task 读到，可变共享对象被原地修改会全局污染（Python ContextVar 已知反模式）；读取方统一 `or {}` 判空。
- **捕获/透传 best-effort**：链路追踪不得弄坏业务请求（观测 fail-open），捕获异常仅 `logger.debug` 降级。

## 关闭态无副作用（遥测未开启时）

- traceparent 仅在统一遥测 active 时注入 channel_context；未注入时 `identity_headers` 不新增任何头（输出与改前逐字节等价）；
- cron `metadata.user_id` 的唯一消费者是 client span 属性；agentserver 侧 `metadata.user_id` 本就经 X-User-Id 头存在，身份链路不变；
- 入口捕获在请求无 W3C 头时 ContextVar 为空，`_build_channel_context` 零合并；外部调用方自带 traceparent 时 metadata 仅多两个透传键，无业务消费者（`bind_incoming_request` 附加的远程 context 在无 span 时惰性）。

以上语义由行为测试钉死：`tests/unit_tests/gateway/test_agent_rest_map_w3c.py`（透传/关闭态无新增头）、`tests/unit_tests/server/test_agent_http_server_w3c.py`（合并/空捕获重置/集成链）。

## 验证

- 新增 5 例测试全绿；同域既有回归（routes params / gateway client / runtime routed client）全绿；telemetry 域定向（gateway_client 14 passed）。
- 113 环境实测（0.0.50s 基线 + collector 0.114.0）：同一轮会话的 gateway client span 与 agentserver SDK 全树合并为一条 trace，两个 pod 名同框，cron 与 web 链路均验证；落地通道：gateway/manager 走 Deployment hostPath 单文件挂载，agentserver 裸 pod（重建者会回滚 spec 漂移）走镜像 OCI 加层手术——正式发版随代码仓合入自然带入。
- 完整实测记录见交付文档《Trace埋点OTEL上报对接方案》§8 与《Trace埋点OTEL接入部署手册》。
