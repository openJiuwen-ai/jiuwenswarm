# OpenTelemetry 可观测性

JiuwenSwarm 提供基于 OpenTelemetry 的调用链和指标采集能力。当前实现采用“保留
AgentCore 原生 Span，在原 Span 上补充 enterprise 富属性和指标”的融合方式：

- 保留 Code 和 Team 场景原有的 Span 名称及父子关系；
- 新增 Gateway 请求入口 Span 和 Gateway 到 AgentServer 的 Client Span；
- 兼容旧 enterprise dashboard 使用的 `jiuwenclaw.*` 指标、Label 和 Resource；
- 使用同一组 `TracerProvider`、`MeterProvider` 和 W3C Trace Context，避免重复 Span
  和断链。

> `jiuwenclaw.*` 是可观测数据的兼容契约。即使项目包名为 `jiuwenswarm`，也不应将
> 这些指标、Label 或默认 `service.name` 改成 `jiuwenswarm.*`，否则旧 dashboard 的
> 查询和聚合会失效。

## 1. 启用与配置

可观测默认关闭；启用采集后，Trace 和 Metrics 的导出仍可分别控制。

### 1.1 环境变量

开发环境输出到控制台：

```bash
export OTEL_ENABLED=true
export OTEL_TRACES_EXPORTER=console
export OTEL_METRICS_EXPORTER=console
```

通过 OTLP gRPC 导出：

```bash
export OTEL_ENABLED=true
export OTEL_SERVICE_NAME=jiuwenclaw
export OTEL_TRACES_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
```

Trace 和 Metrics 也可使用不同的后端：

```bash
export OTEL_ENABLED=true
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://trace.example.com
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http
export OTEL_EXPORTER_OTLP_TRACES_HEADERS='Authorization=Bearer trace-token'

export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=https://metric.example.com
export OTEL_EXPORTER_OTLP_METRICS_PROTOCOL=http
export OTEL_EXPORTER_OTLP_METRICS_HEADERS='Authorization=Bearer metric-token'
```

### 1.2 `config.yaml`

```yaml
telemetry:
  enabled: true
  service_name: jiuwenclaw
  claw_id: claw-prod-01

  # Trace 和 Metrics 未单独配置时使用的公共回退值
  exporter: otlp
  endpoint: http://localhost:4317
  protocol: grpc
  headers:
    Authorization: Bearer token

  traces:
    exporter: otlp

  metrics:
    exporter: otlp

  sample_rate: 1.0
  max_attributes: 128
  attribute_value_max_length: 10240
  log_messages: true
  redact_prompts: false
  redact_completions: false

  session:
    stuck_threshold_ms: 300000
    stuck_check_interval_s: 30
```

### 1.3 配置项

| 环境变量 | YAML 字段 | 默认值 | 说明 |
|---|---|---:|---|
| `OTEL_ENABLED` | `telemetry.enabled` | `false` | 总开关 |
| `OTEL_EXPORTER_TYPE` | `telemetry.exporter` | `none` | Trace 和 Metrics 的公共导出器回退值：`none`、`console`、`otlp` |
| `OTEL_TRACES_EXPORTER` | `telemetry.traces.exporter` | `none` | Trace 导出器 |
| `OTEL_METRICS_EXPORTER` | `telemetry.metrics.exporter` | `none` | Metrics 导出器 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `telemetry.endpoint` | `http://localhost:4317` | 公共 OTLP 地址 |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `telemetry.protocol` | `grpc` | 公共协议：`grpc` 或 `http` |
| `OTEL_EXPORTER_OTLP_HEADERS` | `telemetry.headers` | 空 | 公共请求头，环境变量格式为 `k=v,k2=v2` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `telemetry.traces.endpoint` | 公共地址 | Trace OTLP 地址 |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | `telemetry.traces.protocol` | 公共协议 | Trace OTLP 协议 |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | `telemetry.traces.headers` | 公共请求头 | Trace 请求头 |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | `telemetry.metrics.endpoint` | 公共地址 | Metrics OTLP 地址 |
| `OTEL_EXPORTER_OTLP_METRICS_PROTOCOL` | `telemetry.metrics.protocol` | 公共协议 | Metrics OTLP 协议 |
| `OTEL_EXPORTER_OTLP_METRICS_HEADERS` | `telemetry.metrics.headers` | 公共请求头 | Metrics 请求头 |
| `OTEL_SERVICE_NAME` | `telemetry.service_name` | `jiuwenclaw` | Resource 中的服务名 |
| `OTEL_CLAW_ID` | `telemetry.claw_id` | 空 | 实例标识 |
| `OTEL_SAMPLE_RATE` | `telemetry.sample_rate` | `1.0` | Trace 采样率，限制在 `0.0` 到 `1.0` |
| `OTEL_MAX_ATTRIBUTES` | `telemetry.max_attributes` | `128` | 单个 Span 最大属性数 |
| `OTEL_ATTRIBUTE_VALUE_MAX_LENGTH` | `telemetry.attribute_value_max_length` | `10240` | 属性值最大长度 |
| `OTEL_LOG_MESSAGES` | `telemetry.log_messages` | `true` | 是否记录消息、工具参数及结果 |
| `OTEL_REDACT_PROMPTS` | `telemetry.redact_prompts` | `false` | 是否脱敏输入和工具定义 |
| `OTEL_REDACT_COMPLETIONS` | `telemetry.redact_completions` | `false` | 是否脱敏输出和工具结果 |
| `OTEL_SESSION_STUCK_THRESHOLD_MS` | `telemetry.session.stuck_threshold_ms` | `300000` | Session 卡住判定阈值 |
| `OTEL_SESSION_STUCK_CHECK_INTERVAL_S` | `telemetry.session.stuck_check_interval_s` | `30` | Session 卡住检查周期 |

公共配置按“环境变量、YAML、默认值”解析；信号配置按“信号环境变量、信号 YAML
平铺字段、信号 YAML 嵌套字段、公共配置、默认值”解析。HTTP 公共地址会自动补充
`/v1/traces` 或 `/v1/metrics`；显式指定的信号地址按原值使用。

## 2. 调用链

### 2.1 跨进程传播

Gateway 在 E2A `channel_context` 中注入 W3C `traceparent`/`tracestate`。AgentServer
收到请求后恢复远端 Context、身份和 Session/Channel 指标上下文，后续 Code 或 Team
Span 因而加入同一条 Trace。

```text
[SERVER] channel.request
  └── [CLIENT] jiuwenclaw.gateway.agent.request
        └── AgentServer Code 或 Team 原生调用链
```

`channel.request` 记录外部请求入口；`jiuwenclaw.gateway.agent.request` 只覆盖 Gateway
到 AgentServer 的调用边界，不替代 Agent、LLM 或 Tool Span。

### 2.2 Code 调用链

Code 场景保留 AgentCore 创建的 Agent、LLM 和 Tool Span 名称。典型结构如下：

```text
channel.request
  └── jiuwenclaw.gateway.agent.request
        └── agent.code.normal.<session_id>
              └── agent.<agent_name>.task_iteration
                    ├── llm.call
                    ├── tool.<tool_name>
                    └── llm.call
```

根 Span 根据模式命名为 `agent.<mode>.<session_id>`，例如
`agent.code.normal.session-1`、`agent.agent.plan.session-1`；模式缺失时回退为
`agent.run.<session_id>`。Code 根 Span 的原生模式属性仍为 `jiuwenswarm.mode`。

### 2.3 Team 调用链

Team 场景保留 AgentCore 的团队拓扑：

```text
channel.request
  └── jiuwenclaw.gateway.agent.request
        └── team.<team_name>
              ├── member.<member_name>.<event>
              ├── task.<task_id>
              │     └── task.<task_id>.created
              ├── msg.<sender>-><receiver>
              └── agent.<agent_name>...
                    ├── llm.call
                    └── tool.<tool_name>
```

融合层不会再创建旧 enterprise 方案中的 `jiuwenswarm.agent.invoke`、`gen_ai.chat`
或 `gen_ai.tool.execute:*` 副本，而是在上述原生 Span 上补充属性和指标。因此同一次
调用只有一组 Agent/LLM/Tool Span，且 Code、Team 的分析面板仍可按原名称查询。

### 2.4 Span 属性

属性仅在数据源存在时写入。融合层采用“已有有效值优先”的策略，不覆盖 AgentCore
已经写入的属性。

#### 请求与身份属性

| 属性 | 说明 |
|---|---|
| `jiuwenclaw.claw.id` | JiuwenClaw 实例标识 |
| `jiuwenclaw.channel.id` | 请求渠道，如 `web`、`feishu`、`wecom` |
| `jiuwenclaw.session.id` | Session ID |
| `jiuwenclaw.request.id` | 请求 ID |
| `gen_ai.conversation.id` | GenAI 会话 ID，通常与 Session ID 相同 |
| `user.id` / `jiuwenclaw.user.id` | 用户 ID 主属性及 enterprise 兼容别名 |
| `domain.id` / `jiuwenclaw.domain.id` | 域 ID 主属性及 enterprise 兼容别名 |
| `app.id` / `jiuwenclaw.app.id` | 应用 ID 主属性及 enterprise 兼容别名 |
| `jiuwenclaw.req.method` | E2A 请求方法 |
| `jiuwenclaw.stream` | 是否为流式请求 |
| `jiuwenclaw.mode` | 从 AgentServer 请求恢复的模式属性 |
| `jiuwenswarm.mode` | Code 根 Span 保留的原生模式属性 |
| `service.version` | JiuwenSwarm 包版本的 Span 镜像属性 |
| `error.type` | 异常类型 |
| `jiuwenclaw.canceled` | 请求或调用是否被取消 |
| `jiuwenclaw.timeout` | 调用是否超时 |

#### Agent 属性

| 属性 | 说明 |
|---|---|
| `gen_ai.span.type=agent` | Span 类型 |
| `gen_ai.agent.name` / `jiuwenclaw.agent.name` | Agent 名称及兼容别名 |
| `jiuwenclaw.agent.parent` | 父 Session |
| `jiuwenclaw.agent.mode` | Agent 模式 |
| `jiuwenclaw.iteration` | 迭代次数 |
| `gen_ai.input.messages` | 序列化后的 Agent 输入，受消息记录和脱敏配置控制 |

#### LLM 属性

| 属性 | 说明 |
|---|---|
| `gen_ai.span.type=model` | Span 类型 |
| `gen_ai.operation.name=chat` | GenAI 操作类型 |
| `gen_ai.system` | 模型提供方；无法解析时为 `unknown` |
| `gen_ai.request.model` / `gen_ai.response.model` | 请求模型和响应模型 |
| `gen_ai.request.temperature` / `gen_ai.request.top_p` | 模型请求参数 |
| `gen_ai.request.streaming` | 是否为流式模型调用 |
| `gen_ai.response.finish_reason` / `gen_ai.response.finish_reasons` | 完成原因 |
| `gen_ai.usage.input_tokens` | 输入 Token |
| `gen_ai.usage.output_tokens` | 输出 Token |
| `gen_ai.usage.total_tokens` | 总 Token |
| `gen_ai.usage.cache_read.input_tokens` | Cache read Token |
| `gen_ai.usage.cache_creation.input_tokens` | Cache creation Token |
| `gen_ai.usage.reasoning.output_tokens` | Reasoning Token |
| `gen_ai.context.system_prompt` | System Prompt 的估算 Token |
| `gen_ai.context.user_messages` | User 消息的估算 Token |
| `gen_ai.context.assistant_messages` | Assistant 消息的估算 Token |
| `gen_ai.context.tool_results` | Tool 结果的估算 Token |
| `gen_ai.context.skill` | Skill 内容的估算 Token |
| `gen_ai.context.tool_definitions` | Tool 定义的估算 Token |
| `gen_ai.input.messages.count` | 输入消息数量 |
| `gen_ai.input.messages.total_length` | 输入消息字符总长度 |
| `gen_ai.input.messages` / `gen_ai.output.messages` | 序列化输入和输出，受消息记录、脱敏及长度配置控制 |
| `gen_ai.tool.definitions` | Tool 定义，受消息记录、脱敏及长度配置控制 |
| `gen_ai.decision.type` | 模型决策类型 |
| `gen_ai.decision.tool_names` / `gen_ai.decision.tool_count` | 模型选择的 Tool 名称和数量 |
| `gen_ai.streaming.first_token` | 已收到首个流式 Token |

Context Token 使用 AgentCore 的 `TiktokenCounter` 估算；初始化或计数失败时退化为字符
长度估算。它用于分析上下文构成，不应作为模型账单 Token 的严格校验值。

#### Tool 与 Skill 属性

| 属性 | 说明 |
|---|---|
| `gen_ai.span.type=tool` | Span 类型 |
| `gen_ai.tool.name` | Tool 名称 |
| `gen_ai.tool.call.id` | Tool Call ID |
| `gen_ai.tool.arguments` / `gen_ai.tool.result` | Tool 参数与结果，受消息记录和脱敏配置控制 |
| `gen_ai.skill.name` / `gen_ai.skill.id` / `gen_ai.skill.version` | Skill 名称、ID 和版本 |
| `gen_ai.operation.name=load_skill` | `skill_tool` 成功加载 Skill |
| `gen_ai.operation.name=release_skill` | `skill_complete` 释放 Skill |

Skill 不创建独立 Span，而是在 `tool.skill_tool` 和 `tool.skill_complete` 等 AgentCore
原生 Tool Span 上增加 Skill 属性。成功加载和释放分别产生 `skill.loaded`、
`skill.released` 事件。LLM/Tool 消息事件还包括 `gen_ai.<role>.message`、
`tool.arguments` 和 `tool.result`。

## 3. 指标

当前固定目录包含 21 个指标。下表中的“业务 Label”不重复列出所有数据点自动合并的
公共 Label，公共 Label 见 3.2 节。

### 3.1 指标目录

| 指标 | 类型 | 单位 | 业务 Label | 含义 |
|---|---|---|---|---|
| `jiuwenclaw.request.duration` | Histogram | `s` | `jiuwenclaw.channel.id` | Gateway 请求端到端耗时 |
| `jiuwenclaw.request.count` | Counter | `{request}` | `jiuwenclaw.channel.id` | Gateway 请求数 |
| `jiuwenclaw.request.error.count` | Counter | `{request}` | `jiuwenclaw.channel.id` | Gateway 请求错误数 |
| `jiuwenclaw.agent.duration` | Histogram | `s` | `jiuwenclaw.agent.name`, `jiuwenclaw.channel.id` | Agent 处理耗时 |
| `gen_ai.client.operation.duration` | Histogram | `s` | `gen_ai.request.model`, `gen_ai.system`, `jiuwenclaw.channel.id` | LLM 调用耗时 |
| `gen_ai.client.operation.count` | Counter | `{call}` | `gen_ai.request.model`, `status`, `jiuwenclaw.channel.id` | LLM 调用次数 |
| `gen_ai.client.token.usage` | Counter | `{token}` | `gen_ai.request.model`, `gen_ai.system`, `gen_ai.token.type`, `jiuwenclaw.channel.id` | LLM Token 用量 |
| `gen_ai.client.token.first_token_duration` | Histogram | `s` | `gen_ai.request.model`, `gen_ai.system`, `jiuwenclaw.channel.id` | 流式调用首 Token 延迟 |
| `gen_ai.tool.duration` | Histogram | `s` | `gen_ai.tool.name`, `jiuwenclaw.channel.id` | Tool 执行耗时 |
| `gen_ai.tool.call.count` | Counter | `{call}` | `gen_ai.tool.name`, `jiuwenclaw.channel.id` | Tool 调用次数 |
| `gen_ai.tool.error.count` | Counter | `{call}` | `gen_ai.tool.name`, `jiuwenclaw.channel.id` | Tool 错误次数 |
| `gen_ai.tool.token.usage` | Counter | `{token}` | `gen_ai.tool.name`, `gen_ai.request.model`, `jiuwenclaw.channel.id` | 各 Tool 定义占用的上下文 Token |
| `gen_ai.skill.call.count` | Counter | `{call}` | `gen_ai.skill.name`, `gen_ai.skill.version`, `gen_ai.system`, `jiuwenclaw.channel.id` | Skill 激活次数 |
| `gen_ai.skill.duration` | Histogram | `s` | `gen_ai.skill.name`, `gen_ai.skill.version`, `gen_ai.system`, `jiuwenclaw.channel.id` | Skill 从加载到完成的耗时 |
| `gen_ai.skill.error.count` | Counter | `{call}` | `gen_ai.skill.name`, `gen_ai.skill.version`, `gen_ai.system`, `jiuwenclaw.channel.id` | Skill 错误次数 |
| `gen_ai.skill.token.usage` | Counter | `{token}` | `gen_ai.skill.name`, `gen_ai.request.model`, `jiuwenclaw.channel.id` | 各 Skill 内容占用的上下文 Token |
| `jiuwenclaw.session.active` | ObservableGauge | `{session}` | 无 | 当前有真实执行任务的 Session 数 |
| `jiuwenclaw.session.created.count` | Counter | `{session}` | `jiuwenclaw.session.id` | Session Processor 创建次数 |
| `jiuwenclaw.session.state` | Counter | `{transition}` | `jiuwenclaw.session.id`, `jiuwenclaw.session.state`, `jiuwenclaw.session.state.reason` | Session 状态迁移次数 |
| `jiuwenclaw.session.stuck` | Counter | `{occurrence}` | `jiuwenclaw.session.id` | 首次判定为卡住的 Session 数 |
| `jiuwenclaw.session.stuck_age_ms` | Histogram | `ms` | `jiuwenclaw.session.id` | 检查时 Session 已卡住的时长 |

### 3.2 Label 说明

| Label | 值域或来源 |
|---|---|
| `jiuwenclaw.channel.id` | 请求渠道；缺失时不写入 |
| `jiuwenclaw.agent.name` | Agent 名称；缺失时不写入 |
| `gen_ai.request.model` | 请求模型；缺失时不写入 |
| `gen_ai.system` | LLM Provider；Skill 指标固定为 `jiuwenclaw` |
| `status` | `success` 或 `error` |
| `gen_ai.token.type` | `input`、`output`、`cache_read`、`cache_creation`、`reasoning` |
| `gen_ai.tool.name` | Tool 名称；缺失时不写入 |
| `gen_ai.skill.name` / `gen_ai.skill.version` | Skill 名称和版本；缺失值不写入 |
| `jiuwenclaw.session.id` | 当前 Session ID |
| `jiuwenclaw.session.state` | `created`、`active`、`idle` 或 `cancelled` |
| `jiuwenclaw.session.state.reason` | `new_processor`、`task_started`、`task_completed`、`task_error`、`task_cancelled`、`user_cancel` 或 `session_closed` |

每个指标数据点还会按上下文自动合并以下公共 Label：

| 公共 Label | 来源 | 说明 |
|---|---|---|
| `jiuwenclaw.claw.id` | MeterProvider Resource | 配置后写入所有指标数据点 |
| `jiuwenclaw.session.id` | 当前请求的 ContextVar | 请求范围内自动写入 |
| `user_id` | `IdentityStore` | 当前用户 ID |
| `domain_id` | `IdentityStore` | 当前域 ID |
| `app_id` | `IdentityStore` | 当前应用 ID |

合并优先级为“调用点 Label < Resource/请求上下文 < IdentityStore”，空字符串和
`None` 不写入。`service.name` 和 `service.version` 通过 Resource 关联，不重复写入每个
指标数据点。

### 3.3 指标语义说明

- `gen_ai.client.operation.count{status="error"}` 表示 LLM 失败，没有独立的 LLM
  error 指标。
- `gen_ai.client.token.usage` 仅在模型返回对应 Usage 时记录；每种 Token 类型形成独立
  数据点。
- `gen_ai.client.token.first_token_duration` 仅在观测到流式首 Token 时记录。
- Tool 指标保留物理 Tool 调用；Skill 指标额外描述 `skill_tool` 到
  `skill_complete` 的逻辑生命周期，两者不会互相替代。
- `gen_ai.skill.duration` 只有找到匹配的 Skill 激活记录并正常完成时才记录；缺失、
  过期或重复完成不会伪造耗时。
- `jiuwenclaw.session.active` 统计存在真实 in-flight Task 的去重 Session 数。
- `jiuwenclaw.session.stuck` 对同一轮 in-flight Session 只记录首次发现；
  `jiuwenclaw.session.stuck_age_ms` 在每次检查发现卡住时记录当前时长。

## 4. Resource

默认 Trace 和 Metrics Provider 共享同一份 Resource：

| Resource 属性 | 来源 | 默认值或说明 |
|---|---|---|
| `service.name` | `OTEL_SERVICE_NAME` / `telemetry.service_name` | `jiuwenclaw`，兼容旧 enterprise dashboard |
| `service.version` | 已安装的 `jiuwenswarm` 包版本 | 源码环境无法读取包元数据时回退为当前内置版本 |
| `jiuwenclaw.claw.id` | `OTEL_CLAW_ID` / `telemetry.claw_id` | 可选实例标识 |

OpenTelemetry SDK 还会补充标准的 `telemetry.sdk.language`、`telemetry.sdk.name` 和
`telemetry.sdk.version`。自定义 Provider 时，TracerProvider、MeterProvider 和显式
`ProviderBundle.resource` 必须使用相等的 Resource，避免 Trace 与 Metrics 被聚合到
不同服务。

## 5. 扩展点

### 5.1 自定义 Provider（公开扩展点）

第三方可实现 `TelemetryProviderExtension`，并通过
`ExtensionRegistry.register_telemetry_provider()` 注册。扩展的
`build_providers(cfg)` 返回 `ProviderBundle`，或返回 `None` 使用内置 Provider。

```python
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from jiuwenswarm.extensions.sdk.telemetry_provider import TelemetryProviderExtension
from jiuwenswarm.telemetry.provider import ProviderBundle


class MyTelemetryProvider(TelemetryProviderExtension):
    async def initialize(self, config):
        self.config = config

    def build_providers(self, cfg):
        resource = Resource.create({"service.name": cfg.service_name})
        return ProviderBundle(
            tracer_provider=TracerProvider(resource=resource),
            meter_provider=MeterProvider(resource=resource),
            resource=resource,
            owns_tracer=True,
            owns_meter=True,
        )
```

`owns_tracer` 和 `owns_meter` 决定 JiuwenSwarm 停止时是否关闭对应 Provider。若资源由
扩展或外部进程生命周期管理，应设置为 `False`，并在扩展自己的 `shutdown()` 中完成
必要清理。

### 5.2 回调扩展

扩展可以使用 `ExtensionRegistry.register(event, handler, priority=...)` 订阅 AgentCore
回调事件，补充业务事件或自定义指标。回调必须满足以下约束：

- 不创建第二套全局 TracerProvider 或 MeterProvider；
- 不重新创建或重命名 AgentCore 的 Agent、LLM、Tool、Team Span；
- 使用当前 Span 写入属性或事件，并保持业务返回值不变；
- 为异步并发状态使用请求级 ContextVar 或带容量/TTL 的注册表，并在成功、错误、取消
  和流式提前结束时清理；
- 通过回调优先级保证“AgentCore 先创建 Span，富化回调再写入 Span”。

### 5.3 内部融合边界

以下组件用于内置融合，不是稳定的第三方 SDK：

| 组件 | 职责 |
|---|---|
| `TelemetryRuntime` | Provider 安装、AgentCore 初始化、回调和 Session 生命周期 |
| `RichTelemetryCallbacks` | 在原生 Agent/LLM/Tool Span 上补充属性并记录指标 |
| `SpanRegistryProcessor` | 跨异步任务查找同一 Trace 中仍活动的 Span |
| `TraceBindingRegistry` | 绑定 Gateway 请求与 Agent 长生命周期任务 |
| `TelemetryMetrics` / `METRIC_SPECS` | 固定 enterprise 兼容指标目录和记录入口 |
| `GatewayTelemetryAgentClient` | Gateway Client Span 和 W3C 下游传播 |

新增或修改指标、Label、Resource、Span 名称属于 dashboard 契约变更，应同步更新本文档
及 `tests/unit_tests/telemetry`、`tests/integration/telemetry` 中的契约测试；第三方不应
直接依赖上述内部类的私有状态。

## 6. 实现索引

| 文件 | 职责 |
|---|---|
| `jiuwenswarm/telemetry/config.py` | 配置加载和优先级 |
| `jiuwenswarm/telemetry/provider.py` | 默认/扩展 Provider 与 Resource |
| `jiuwenswarm/telemetry/runtime.py` | 统一运行时生命周期 |
| `jiuwenswarm/telemetry/gateway.py` | Gateway SERVER Span、请求指标和上游 W3C 注入 |
| `jiuwenswarm/telemetry/gateway_client.py` | Gateway CLIENT Span 和下游 W3C 注入 |
| `jiuwenswarm/telemetry/request_context.py` | AgentServer Context、身份和请求属性恢复 |
| `jiuwenswarm/telemetry/metrics.py` | 21 个指标及公共 Label |
| `jiuwenswarm/telemetry/session.py` | Session 生命周期和卡住指标 |
| `jiuwenswarm/telemetry/enrichment/callbacks.py` | Agent/LLM/Tool/Skill 富化和指标 |
| `jiuwenswarm/extensions/sdk/telemetry_provider.py` | Provider 扩展 SDK |

## 7. 使用注意事项

- `OTEL_ENABLED=true` 只启用采集；要发送到后端，还需将对应信号的 exporter 配置为
  `console` 或 `otlp`。
- 完整消息、Tool 参数和结果可能包含敏感信息。生产环境应结合
  `OTEL_LOG_MESSAGES`、`OTEL_REDACT_PROMPTS`、`OTEL_REDACT_COMPLETIONS` 和后端访问
  控制进行配置。
- `OTEL_SAMPLE_RATE=0` 时不产生可导出的 Span，但基于回调和真实 Session 生命周期的
  Metrics 仍可记录。
- FaaS 入口不在当前统一运行时的启用范围内；本文描述的是 Gateway 和 AgentServer
  请求链路。
- 排查旧 dashboard 无数据时，优先检查 `service.name=jiuwenclaw`、
  `jiuwenclaw.claw.id`、指标前缀和 Label 是否被采集端或后端重写。
