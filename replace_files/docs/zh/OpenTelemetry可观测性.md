# OpenTelemetry 可观测性

JiuWenClaw 内置了基于 OpenTelemetry 协议的可观测性能力，遵循 [OpenTelemetry GenAI 语义规范](https://opentelemetry.io/docs/specs/semconv/gen-ai/)，支持上报完整的调用链 Trace 和运行指标 Metric。

## 1. 功能概述

通过 OpenTelemetry 集成，可以观测以下调用链路：

| 类型 | 含义 | 记录内容 |
|------|------|----------|
| ENTRY | 请求入口 | 谁发的消息、从哪个渠道来的 |
| AGENT | Agent 调用 | 哪个 Agent 在执行、会话 ID |
| LLM | 大模型调用 | 模型名、Token 消耗（input/output/cache）、系统提示词、输入输出消息完整内容 |
| TOOL | 工具调用 | 工具名、参数、返回结果、是否报错 |

同时记录以下运行指标：
- 请求端到端时延、Agent 处理时延、LLM 调用时延、工具执行时延
- 请求总数、错误数、LLM 调用次数、工具调用次数、工具错误次数
- 当前活跃会话数、累计创建会话数
- Token 消耗（按 input/output/cache 类型统计）
- 模型调用失败数可通过 `gen_ai.client.operation.count{status="error"}` 统计

## 2. 快速启用

### 2.1 通过环境变量启用

```bash
# 启用 telemetry，使用 console 输出（开发调试）
OTEL_ENABLED=true OTEL_EXPORTER_TYPE=console jiuwenclaw-app

# 启用 telemetry，使用 OTLP 导出到 Jaeger/Tempo 等后端（注意本地使用端口4317启动Jaeger/Tempo）
OTEL_ENABLED=true OTEL_EXPORTER_TYPE=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 jiuwenclaw-app
```

### 2.2 通过 config.yaml 启用

在 `config.yaml` 中添加或修改 `telemetry` 段：

```yaml
telemetry:
  enabled: true                     # 设为 true 才会启用 telemetry（默认 false）
  exporter: none                    # 即使启用 telemetry，默认也仍不导出到后端
  endpoint: http://localhost:4317   # OTLP endpoint
  protocol: grpc                    # grpc / http
  log_messages: true                # 是否记录完整消息内容
  service_name: jiuwenclaw          # 服务名称
  claw_id: claw-instance-001        # Claw 实例标识（可选）
```

> 环境变量优先级高于 config.yaml。telemetry 默认关闭；若需要实际导出，请先开启 telemetry，再将 `exporter`、`OTEL_TRACES_EXPORTER` 或 `OTEL_METRICS_EXPORTER` 改为 `console` 或 `otlp`。

## 3. 配置参数

| 环境变量 | config.yaml 字段 | 默认值 | 说明 |
|----------|------------------|--------|------|
| `OTEL_ENABLED` | `telemetry.enabled` | `false` | 总开关 |
| `OTEL_EXPORTER_TYPE` | `telemetry.exporter` | `none` | 公共 exporter fallback：otlp / console / none |
| `OTEL_TRACES_EXPORTER` | `telemetry.traces.exporter` | `none` | trace 导出方式：otlp / console / none |
| `OTEL_METRICS_EXPORTER` | `telemetry.metrics.exporter` | `none` | metrics 导出方式：otlp / console / none |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `telemetry.endpoint` | `http://localhost:4317` | OTLP 后端地址 |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `telemetry.protocol` | `grpc` | OTLP 协议：grpc / http |
| `OTEL_LOG_MESSAGES` | `telemetry.log_messages` | `true` | 是否在 Span Event 中记录完整消息内容 |
| `OTEL_SERVICE_NAME` | `telemetry.service_name` | `jiuwenclaw` | 服务名称 |
| `OTEL_CLAW_ID` | `telemetry.claw_id` | `None` | Claw 实例标识，用于区分同一服务下的不同实例 |

## 4. Trace 调用链结构

一次完整请求的 Trace 结构如下（enterprise_dev / SDK Adapter 架构）：

```
[ENTRY]   channel.request                                 (gateway 进程，渠道入口)
  └── [GATEWAY→AGENT] jiuwenclaw.gateway.agent.request    (gateway→agentserver 的 ws 调用)
        └── [AGENT] jiuwenclaw.agent.invoke               (agentserver 进程，Agent 生命周期)
              ├── [LLM] gen_ai.chat                       ← ReAct 第 1 轮
              │     ├── event: gen_ai.system.message       ← system prompt（log_messages=true 时）
              │     ├── event: gen_ai.user.message         ← 用户输入
              │     └── event: gen_ai.assistant.message    ← 模型输出
              ├── [TOOL] gen_ai.tool.execute: search_web  ← 与 LLM span 同级（均挂在 AGENT 下）
              │     ├── event: tool.arguments
              │     └── event: tool.result
              ├── [LLM] gen_ai.chat                       ← ReAct 第 2 轮
              └── event: gen_ai.assistant.message         ← 最终回答
```

> **层级说明**：
> - `jiuwenclaw.gateway.agent.request` 是 gateway 与 agentserver 之间的 WebSocket 调用 span，用于跨进程 W3C TraceContext 传播。它一定夹在 ENTRY 与 AGENT 之间。
> - **TOOL span 是 AGENT span 的直接子节点，与 LLM span 同级**（而不是 LLM 的子节点）。这是当前实现的既定行为：`TelemetryRail.before_tool_call` 使用当前 OTel context（此时 AGENT span 处于活跃态），并未特意把 TOOL span 嵌到 LLM span 下。

### Span Attributes（遵循 GenAI 语义规范）

**ENTRY span** (`gen_ai.span.type=workflow`):
- `jiuwenclaw.channel.id` — 渠道标识（feishu / web / wecom 等）
- `jiuwenclaw.session.id` — 会话 ID
- `jiuwenclaw.request.id` — 请求 ID
- `gen_ai.span.type` — `"workflow"`

**AGENT span** (`gen_ai.span.type=agent`):
- `jiuwenclaw.agent.name` — Agent 名称
- `jiuwenclaw.session.id` — 会话 ID
- `gen_ai.agent.name` — Agent 名称（GenAI 标准命名空间）
- `gen_ai.conversation.id` — 会话 ID（GenAI 标准命名空间）
- `gen_ai.span.type` — `"agent"`

**LLM span** (`gen_ai.span.type=model`):
- `gen_ai.system` — 模型提供商（openai 等）
- `gen_ai.request.model` — 请求模型名称
- `gen_ai.response.model` — 响应模型名称
- `gen_ai.operation.name` — 操作类型（chat）
- `gen_ai.request.temperature` — 温度参数
- `gen_ai.request.top_p` — top_p 参数
- `gen_ai.usage.input_tokens` — 输入 Token 数
- `gen_ai.usage.output_tokens` — 输出 Token 数
- `gen_ai.usage.total_tokens` — 总 Token 数
- `gen_ai.usage.cache_read_tokens` — 缓存读取 Token 数（有缓存时）
- `gen_ai.response.finish_reasons` — 结束原因（数组形式）
- `gen_ai.response.finish_reason` — 结束原因（字符串形式）
- `gen_ai.span.type` — `"model"`

**TOOL span** (`gen_ai.span.type=tool`):
- `gen_ai.tool.name` — 工具名称
- `gen_ai.tool.call.id` — 调用 ID
- `gen_ai.span.type` — `"tool"`

## 5. Metric 指标

本章基于当前代码实现整理指标清单，而不是仅依据前文摘要说明。当前代码实际定义了 20 个指标，定义位于 `jiuwenclaw/telemetry/metrics.py`，埋点入口位于：

- `jiuwenclaw/telemetry/instrumentors/entry.py`
- `jiuwenclaw/telemetry/instrumentors/agent.py`
- `jiuwenclaw/telemetry/instrumentors/llm.py`
- `jiuwenclaw/telemetry/instrumentors/tool.py`
- `jiuwenclaw/telemetry/instrumentors/session.py`
- `jiuwenclaw/telemetry/instrumentors/queue.py`

### 5.1 指标总览

| 指标名 | 类型 | 单位 | 触发时机 | Labels | 指标来源 | 说明 |
|---|---|---|---|---|---|---|
| `jiuwenclaw.request.duration` | Histogram | `s` | 每次入口请求结束时记录 | `jiuwenclaw.channel.id` | `gateway` | 请求端到端时延 |
| `jiuwenclaw.request.count` | Counter | `{request}` | 每次入口请求开始时累加 | `jiuwenclaw.channel.id` | `gateway` | 请求总数 |
| `jiuwenclaw.request.error.count` | Counter | `{request}` | 入口请求抛异常时累加 | `jiuwenclaw.channel.id` | `gateway` | 请求错误数 |
| `jiuwenclaw.agent.duration` | Histogram | `s` | Agent 处理完成时记录，流式和非流式都会记录 | `jiuwenclaw.agent.name`, `jiuwenclaw.channel.id` | `agentserver` | Agent 处理时延 |
| `gen_ai.client.operation.duration` | Histogram | `s` | 每次 LLM 调用结束时记录 | `gen_ai.request.model`, `gen_ai.system`, `jiuwenclaw.channel.id` | `agentserver` | LLM 调用时延 |
| `gen_ai.client.operation.count` | Counter | `{call}` | 每次 LLM 调用结束时累加 | `gen_ai.request.model`, `status`, `jiuwenclaw.channel.id` | `agentserver` | LLM 调用次数 |
| `gen_ai.client.token.usage` | Counter | `{token}` | LLM 返回 `usage_metadata` 时累加 | `gen_ai.request.model`, `gen_ai.system`, `gen_ai.token.type`, `jiuwenclaw.channel.id` | `agentserver` | Token 消耗 |
| `gen_ai.tool.duration` | Histogram | `s` | 每次工具执行完成时记录 | `gen_ai.tool.name`, `jiuwenclaw.channel.id` | `agentserver` | 工具执行时延 |
| `gen_ai.tool.call.count` | Counter | `{call}` | 每次发起工具调用时累加 | `gen_ai.tool.name`, `jiuwenclaw.channel.id` | `agentserver` | 工具调用次数 |
| `gen_ai.tool.error.count` | Counter | `{call}` | 工具结果被判定为错误时累加 | `gen_ai.tool.name`, `jiuwenclaw.channel.id` | `agentserver` | 工具错误次数 |
| `jiuwenclaw.session.active` | ObservableGauge | `{session}` | 导出时实时采样 | 无 | `agentserver` | 当前活跃会话数 |
| `jiuwenclaw.session.created.count` | Counter | `{session}` | 首次创建 session processor 时累加 | 无 | `agentserver` | 累计创建会话数 |
| `jiuwenclaw.session.state` | Counter | `{transition}` | Session 状态迁移时累加 | `jiuwenclaw.session.id`, `jiuwenclaw.session.state`, `jiuwenclaw.session.state.reason` | `agentserver` | Session 状态迁移次数 |
| `jiuwenclaw.session.stuck` | Counter | `{occurrence}` | Session 首次被判定卡住时累加 | `jiuwenclaw.session.id` | `agentserver` | Session 卡住次数 |
| `jiuwenclaw.session.stuck_age_ms` | Histogram | `ms` | Session 被判定卡住时记录 | `jiuwenclaw.session.id` | `agentserver` | Session 卡住时长 |
| `jiuwenclaw.queue.depth` | ObservableGauge | `{message}` | 每次 metrics 采集时读取 | `queue` | `gateway` | 队列当前深度 |
| `jiuwenclaw.queue.enqueued` | Counter | `{message}` | 消息入队时累加 | `queue`, `jiuwenclaw.channel.id` | `gateway` | 入队消息总数 |
| `jiuwenclaw.queue.dequeued` | Counter | `{message}` | 消息出队时累加 | `queue`, `jiuwenclaw.channel.id` | `gateway` | 出队消息总数 |
| `jiuwenclaw.queue.wait_duration` | Histogram | `ms` | 消息出队时记录 | `queue`, `jiuwenclaw.channel.id` | `gateway` | 队列等待时间 |
| `jiuwenclaw.message.processed` | Counter | `{message}` | 消息出队时累加 | `queue`, `jiuwenclaw.channel.id`, `status` | `gateway` | 处理消息计数 |

### 5.2 Labels 说明

| Label | 所属指标 | 取值说明 |
|---|---|---|
| `jiuwenclaw.channel.id` | `jiuwenclaw.request.duration` `jiuwenclaw.request.count` `jiuwenclaw.request.error.count` `jiuwenclaw.agent.duration` `gen_ai.client.operation.duration` `gen_ai.client.operation.count` `gen_ai.client.token.usage` `gen_ai.tool.duration` `gen_ai.tool.call.count` `gen_ai.tool.error.count` `jiuwenclaw.queue.enqueued` `jiuwenclaw.queue.dequeued` `jiuwenclaw.queue.wait_duration` `jiuwenclaw.message.processed` | 渠道 ID，如 `web`、`feishu`、`wecom`；缺失时为空串 |
| `jiuwenclaw.agent.name` | `jiuwenclaw.agent.duration` | Agent 名称；缺失时为空串 |
| `gen_ai.request.model` | `gen_ai.client.operation.duration` `gen_ai.client.operation.count` `gen_ai.client.token.usage` | LLM 模型名，如配置中的 `model_name` |
| `gen_ai.system` | `gen_ai.client.operation.duration` `gen_ai.client.token.usage` | 模型提供商，由 `model_client_config.client_provider` 推断；推断失败时为 `unknown` |
| `status` | `gen_ai.client.operation.count` `jiuwenclaw.message.processed` | 当前实现只有 `success` 和 `error` |
| `gen_ai.token.type` | `gen_ai.client.token.usage` | 当前实现只有 `input`、`output`、`cache_read` |
| `gen_ai.tool.name` | `gen_ai.tool.duration` `gen_ai.tool.call.count` `gen_ai.tool.error.count` | 工具名；缺失时为空串 |
| `jiuwenclaw.session.id` | `jiuwenclaw.session.state` `jiuwenclaw.session.stuck` `jiuwenclaw.session.stuck_age_ms` | Session ID |
| `jiuwenclaw.session.state` | `jiuwenclaw.session.state` | 当前实现有 `created`、`active`、`idle`、`cancelled`、`destroyed` |
| `jiuwenclaw.session.state.reason` | `jiuwenclaw.session.state` | 当前实现有 `new_request`、`task_started`、`task_completed`、`task_error`、`user_cancel`、`queue_closed` |
| `queue` | `jiuwenclaw.queue.depth` `jiuwenclaw.queue.enqueued` `jiuwenclaw.queue.dequeued` `jiuwenclaw.queue.wait_duration` `jiuwenclaw.message.processed` | 队列名称，取值 `user`（用户消息队列）或 `robot`（机器人消息队列） |

### 5.3 指标判定细节

| 指标名 | 判定逻辑 |
|---|---|
| `gen_ai.tool.error.count` | 当前通过工具结果字符串是否包含 `error`、`exception`、`traceback` 来判定错误 |
| `gen_ai.client.operation.count` | 模型调用失败数通过 `status="error"` 统计，不单独新增失败 metric |
| `gen_ai.client.token.usage` | 仅在 LLM 返回 `usage_metadata` 时上报；没有 usage 时不记录 |
| `jiuwenclaw.session.active` | 当前实现统计所有 agent 实例中仍未结束的 session processor 数量 |
| `jiuwenclaw.session.created.count` | 仅在首次创建 session processor 时累加，同一 session 重用 processor 不重复计数 |
| `jiuwenclaw.session.stuck` | 仅首次检测到卡住时计数一次 |
| `jiuwenclaw.session.stuck_age_ms` | 每次检查到卡住都会记录一次当前卡住时长 |

### 5.4 资源属性

除上表中的 point labels 外，默认 Provider 还会为所有 telemetry 数据附带以下资源属性：

| 属性 | 值来源 | 说明 |
|---|---|---|
| `service.name` | `OTEL_SERVICE_NAME` 或 `telemetry.service_name`，默认 `jiuwenclaw` | 服务标识，用于区分不同部署实例 |
| `service.version` | 当前默认写死为 `0.1.5` | 服务版本号 |
| `jiuwenclaw.claw.id` | `OTEL_CLAW_ID` 或 `telemetry.claw_id` | Claw 实例标识，用于区分同一服务下的不同 Claw 实例 |

> **注入机制**：
> - **Resource 层**：在 `provider.py` 的 `build_default_providers()` 中通过 `Resource.create(resource_attrs)` 设置，所有 Span 和 Metric 自动关联 Resource
> - **Metric Attributes 层**：`jiuwenclaw.claw.id` 会额外注入到每个 metric 数据点的 attributes 中（从 Resource 获取），便于在 Metric 后端按实例聚合查询
> - `service.name` 不注入到 metric attributes，仅通过 Resource 关联

### 5.5 代码来源

| 文件 | 作用 |
|---|---|
| `jiuwenclaw/telemetry/metrics.py` | 指标定义 |
| `jiuwenclaw/telemetry/instrumentors/entry.py` | 请求入口指标 |
| `jiuwenclaw/telemetry/instrumentors/agent.py` | Agent 指标 |
| `jiuwenclaw/telemetry/instrumentors/llm.py` | LLM 指标 |
| `jiuwenclaw/telemetry/instrumentors/tool.py` | 工具指标 |
| `jiuwenclaw/telemetry/instrumentors/session.py` | Session 生命周期与 stuck 指标 |
| `jiuwenclaw/telemetry/instrumentors/queue.py` | 消息队列监控指标 |
| `jiuwenclaw/telemetry/provider.py` | `service.name` / `service.version` 等资源属性 |

### 5.6 Span 属性覆盖

除 metrics labels 外，各 span 还携带以下属性，可在 trace 后端用于过滤和查询：

| Span | 携带的关键属性 |
|---|---|
| Entry (`jiuwenclaw.request`) | `jiuwenclaw.channel.id`, `jiuwenclaw.session.id` |
| Agent (`jiuwenclaw.agent.invoke`) | `jiuwenclaw.agent.name`, `jiuwenclaw.session.id`, `jiuwenclaw.channel.id`, `jiuwenclaw.request.id` |
| LLM (`gen_ai.chat`) | `gen_ai.system`, `gen_ai.request.model`, `jiuwenclaw.session.id` |
| Tool (`gen_ai.tool.execute: *`) | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `jiuwenclaw.session.id` |

## 6. 对接 Jaeger 示例

```bash
# 启动 Jaeger（支持 OTLP gRPC）
docker run -d --name jaeger \
  -p 16686:16686 \
  -p 4317:4317 \
  jaegertracing/all-in-one:latest

# 启动 JiuWenClaw
OTEL_ENABLED=true OTEL_EXPORTER_TYPE=otlp jiuwenclaw-app
```

打开 http://localhost:16686 即可在 Jaeger UI 中查看完整的调用链。

## 7. 注意事项

- 当 `telemetry.enabled` 为 `false` 时，telemetry 模块完全不加载，对性能零影响
- `log_messages` 设为 `true` 会在 Span Event 中记录完整的 system prompt、用户输入和模型输出，数据量较大，生产环境可按需关闭
- 支持跨 WebSocket 的 W3C TraceContext 传播，Gateway 和 AgentServer 分离部署时调用链仍然完整

## 7.1 已知限制与行为说明

本节记录与当前实现相关的已知偏差，便于对照排查：

1. **`gen_ai.system` 在 SDK Adapter 模式下可能为 `unknown`**
   - 现象：LLM span 和 `gen_ai.client.*` 指标上的 `gen_ai.system` label 值为 `unknown`。
   - 根因：`TelemetryRail` 从 `ctx.agent` 直接读取 `model_provider` / `model_client_config`，但 `ReActAgent` 将其存放在 `agent._config` 上。
   - 影响范围：仅 enterprise_dev / SDK Adapter 架构；officeclaw 架构通过 `instrumentors/llm.py` 读取 `agent._config`，不受影响。

2. **`gen_ai.client.token.usage` 在流式请求下可能无数据**
   - 现象：即使正常收到模型回答，token 消耗 metric 也不上报。
   - 根因：OpenAI 兼容协议要求流式请求显式携带 `stream_options={"include_usage": true}` 才会返回 usage chunk；当前 `OpenAIModelClient._build_request_params` 未设置此参数，导致 `chunk.usage` 恒为 `None`。
   - 适用条件：使用 `OpenAI` / 兼容 provider 且走 `stream=True` 链路。非流式（`stream=False`）请求不受影响。

3. **Session 系列指标在 SDK Adapter 模式下不上报**
   - 现象：`jiuwenclaw.session.active` / `session.created.count` / `session.state` / `session.stuck` / `session.stuck_age_ms` 五个指标无数据点。
   - 根因：
     - `instrumentors/__init__.py` 在 SDK Adapter 分支里未调用 `instrument_session()`；
     - `instrumentors/session.py` 仍在 patch `JiuWenClaw._ensure_session_processor` / `_cancel_session_task`，但在 enterprise_dev 架构下这些方法已迁移到 `SessionManager`。
   - 适用条件：仅 enterprise_dev / SDK Adapter 架构；officeclaw 架构不受影响。

## 8. Provider 低侵入扩展与第三方集成

为兼容第三方集成场景，JiuWenClaw 在默认 OpenTelemetry 初始化链路上提供了低侵入扩展能力，支持：

- Trace 和 Metrics 分别上报到不同后端
- `service.name`、`protocol`、`endpoint`、`headers/token` 由用户自定义输入
- 在默认实现不满足时，通过扩展点完全替换 provider 初始化逻辑

### 8.1 设计目标

本能力遵循以下原则：

- `OTEL_ENABLED` 仍然是 telemetry 的唯一总开关
- 不额外引入 trace / metrics 独立采集开关，只支持独立上报配置
- trace 和 metrics 是否真正导出，由各自 exporter 是否为 `none` 决定
- 默认 provider 实现继续可用
- 第三方可通过 provider factory 扩展点完全接管 provider 初始化

### 8.2 上报控制语义

本方案区分“采集”和“上报”：

- 默认 `OTEL_ENABLED=false`，所以在显式开启 telemetry 之前不会进行采集或上报
- `OTEL_ENABLED=true` 时，现有埋点逻辑继续执行
- trace 是否真正上报，由 `OTEL_TRACES_EXPORTER` 控制
- metrics 是否真正上报，由 `OTEL_METRICS_EXPORTER` 控制
- 默认值为 `OTEL_ENABLED=false`、`OTEL_TRACES_EXPORTER=none`、`OTEL_METRICS_EXPORTER=none`

例如：

```bash
OTEL_ENABLED=true
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=none
```

表示：

- trace 继续采集并导出
- metrics 继续记录，但不导出到后端

这样可以在不重构埋点逻辑的前提下，实现 trace 和 metrics 的独立上报控制。

### 8.3 Signal 专属配置

除第 3 章中的公共配置外，还支持 trace / metrics 的独立配置。

公共配置：

```bash
OTEL_SERVICE_NAME
OTEL_EXPORTER_TYPE
OTEL_EXPORTER_OTLP_ENDPOINT
OTEL_EXPORTER_OTLP_PROTOCOL
OTEL_EXPORTER_OTLP_HEADERS
```

Trace 专属配置：

```bash
OTEL_TRACES_EXPORTER
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
OTEL_EXPORTER_OTLP_TRACES_PROTOCOL
OTEL_EXPORTER_OTLP_TRACES_HEADERS
```

Metrics 专属配置：

```bash
OTEL_METRICS_EXPORTER
OTEL_EXPORTER_OTLP_METRICS_ENDPOINT
OTEL_EXPORTER_OTLP_METRICS_PROTOCOL
OTEL_EXPORTER_OTLP_METRICS_HEADERS
```

其中：

- `OTEL_EXPORTER_TYPE` 仍作为公共 exporter fallback，取值 `otlp` / `console` / `none`
- `OTEL_EXPORTER_OTLP_HEADERS` 使用 `k=v,k2=v2` 字符串格式

优先级如下：

1. signal 专属环境变量
2. 公共环境变量
3. `config.yaml` 默认值

例如：

- `OTEL_TRACES_EXPORTER` 未配置时，回退到 `OTEL_EXPORTER_TYPE`
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 未配置时，回退到 `OTEL_EXPORTER_OTLP_ENDPOINT`
- `OTEL_EXPORTER_OTLP_TRACES_HEADERS` 未配置时，回退到 `OTEL_EXPORTER_OTLP_HEADERS`

Metrics 同理。

### 8.4 Provider 扩展点

当默认 provider 逻辑不能满足需求时，可以通过 `TelemetryProviderExtension` 扩展点完全接管 provider 初始化。

#### 扩展点优先级机制

扩展点选择遵循以下优先级：

```
┌──────────────────────────────────────────────────────────────┐
│  优先级 1: ExtensionRegistry 中的 TelemetryProviderExtension  │
│     → 存在则使用扩展的 build_providers() 构建 provider        │
│     → 扩展返回 None 则 fallback 到默认 provider               │
└──────────────────────────────────────────────────────────────┘
                              ↓ fallback
┌──────────────────────────────────────────────────────────────┐
│  优先级 2: 默认 provider                                       │
│     → 读取环境变量 / config.yaml 构建                          │
└──────────────────────────────────────────────────────────────┘
```

#### 扩展目录加载优先级

多个扩展目录的加载顺序由 `extension.yaml` 中的 `priority` 字段控制：

| priority 值 | 加载顺序 | 说明 |
|-------------|----------|------|
| 5 | 先加载 | 高优先级扩展，优先注册 |
| 10 | 后加载 | 默认优先级（未设置时默认为 10） |
| 15 | 最后加载 | 低优先级扩展 |

> **数值越小越先加载**，先加载的扩展先注册到 Registry。

#### 注册 TelemetryProviderExtension

实现自定义扩展：

```python
from jiuwenclaw.extensions.sdk.telemetry_provider import TelemetryProviderExtension
from jiuwenclaw.telemetry.provider import ProviderBundle
from jiuwenclaw.telemetry.config import TelemetryConfig

class MyTelemetryExtension(TelemetryProviderExtension):
    """自定义 telemetry provider 扩展"""
    
    def build_providers(self, cfg: TelemetryConfig) -> ProviderBundle:
        """基于 telemetry 配置构建自定义 provider"""
        # 自定义 TracerProvider 和 MeterProvider 构建逻辑
        tracer_provider = self._build_tracer_provider(cfg)
        meter_provider = self._build_meter_provider(cfg)
        
        return ProviderBundle(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
        )
    
    def _build_tracer_provider(self, cfg):
        # ... 自定义实现
        pass
    
    def _build_meter_provider(self, cfg):
        # ... 自定义实现
        pass
```

在扩展目录的 `extension.py` 中注册：

```python
# extension.py
async def register_extensions(registry):
    from my_extension import MyTelemetryExtension
    
    registry.register_telemetry_provider(MyTelemetryExtension())
    return []
```

#### 扩展目录配置

在 `extension.yaml` 中设置加载优先级：

```yaml
id: my-telemetry-extension
name: My Custom Telemetry
version: 1.0.0
description: 自定义 telemetry provider，上报到私有后端
author: my-team
min_jiuwenclaw_version: 0.1.0
priority: 5  # 高优先级，先于其他扩展加载
dependencies: {}
```

#### 注意事项

- **TelemetryProviderExtension 是单一实例**：后注册的扩展会覆盖之前注册的
- 若需要多个扩展协作，应通过 `priority` 控制加载顺序，让期望的扩展先注册
- 扩展的 `build_providers()` 返回 `None` 时，框架自动 fallback 到默认 provider
- 扩展构建的 provider 仍需遵循 `OTEL_ENABLED` 总开关控制

### 8.5 第三方接入示例

#### 使用默认 provider

直接使用默认 provider，将 trace 和 metrics 分别上报到不同后端：

```bash
export OTEL_ENABLED=true
export OTEL_SERVICE_NAME=my-app

export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://trace.example.com
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http
export OTEL_EXPORTER_OTLP_TRACES_HEADERS='Authorization=Bearer trace-token'

export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=https://metrics.example.com
export OTEL_EXPORTER_OTLP_METRICS_PROTOCOL=http
export OTEL_EXPORTER_OTLP_METRICS_HEADERS='Authorization=Bearer metrics-token'
```

只导出 trace、不导出 metrics：

```bash
export OTEL_ENABLED=true
export OTEL_TRACES_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=none
```

#### 使用自定义 TelemetryProviderExtension

创建扩展目录结构：

```
extensions/
└── my-custom-telemetry/
    ├── extension.yaml
    └── extension.py
```

**extension.yaml**:

```yaml
id: my-custom-telemetry
name: Custom Telemetry Provider
version: 1.0.0
description: 上报到私有观测后端
author: my-team
min_jiuwenclaw_version: 0.1.0
priority: 5
dependencies: {}
```

**extension.py**:

```python
import os

from jiuwenclaw.extensions.sdk.telemetry_provider import TelemetryProviderExtension
from jiuwenclaw.telemetry.provider import ProviderBundle
from jiuwenclaw.telemetry.config import TelemetryConfig

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


class CustomTelemetryExtension(TelemetryProviderExtension):
    """自定义 telemetry provider，上报到私有后端"""
    
    def build_providers(self, cfg: TelemetryConfig) -> ProviderBundle:
        service_name = cfg.service_name
        trace_endpoint = os.getenv("CUSTOM_TRACE_ENDPOINT", "https://trace.example.com")
        metric_endpoint = os.getenv("CUSTOM_METRIC_ENDPOINT", "https://metrics.example.com")
        trace_token = os.getenv("TRACE_BACKEND_TOKEN", "")
        metric_token = os.getenv("METRIC_BACKEND_TOKEN", "")
        
        resource = Resource.create({
            SERVICE_NAME: service_name,
            "service.version": os.getenv("APP_VERSION", "unknown"),
        })
        
        # 构建 TracerProvider
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=f"{trace_endpoint}/v1/traces",
                    headers={"Authorization": f"Bearer {trace_token}"},
                )
            )
        )
        
        # 构建 MeterProvider
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=f"{metric_endpoint}/v1/metrics",
                        headers={"Authorization": f"Bearer {metric_token}"},
                    ),
                    export_interval_millis=15000,
                )
            ],
        )
        
        return ProviderBundle(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
        )


async def register_extensions(registry):
    """扩展入口函数"""
    registry.register_telemetry_provider(CustomTelemetryExtension())
    return []
```
