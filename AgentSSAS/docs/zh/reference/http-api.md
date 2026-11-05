# HTTP API 参考

HTTP 服务模式（`mode: http`）下，AgentSSASCore 以独立 FastAPI 服务运行，AgentSSASSecurityRail 通过 `AgentSSASRemoteBackend` 将事件转发到 `POST /api/v1/events`，获取风险评估结果。本文说明服务启动方式、接口 schema 与认证机制。

相关文档：

- [配置参考](./configuration.md)：`http_*` 系列配置字段
- [事件格式参考](./event-format.md)：`raw_event` 三层结构
- [运行测试](../how-to/run-tests.md)：HTTP 相关测试的依赖说明

## 服务启动

### 方式一：模块入口（默认配置）

```bash
python -m agent_ssas.core.modes.http.server
```

使用 `AgentSSASConfig()` 默认配置，监听 `0.0.0.0:8443`。可通过环境变量覆盖监听地址、端口与存储根目录：

```bash
SSAS_HTTP_HOST=0.0.0.0
SSAS_HTTP_PORT=8443
SSAS_HOME=<存储根目录>
```

启动该入口需要安装 `http` 扩展依赖（fastapi、uvicorn、httpx）。

### 方式二：示例脚本（仅监听本机回环）

```bash
cd AgentSecurity/AgentSSAS
uv run --extra http python examples/http_server_demo/start_server.py
```

demo 脚本固定监听 `http://127.0.0.1:8443`（仅本机访问），环境变量仍可覆盖。配套客户端示例 `examples/http_server_demo/client.py` 演示了健康检查与事件上报的完整流程。

### 服务端信息

- 应用标题：`AgentSSAS HTTP Server`（FastAPI，版本 0.1.0）。
- 交互式文档：启动后可访问 `http://<host>:<port>/docs`（FastAPI 自动生成的 OpenAPI 文档）。

## POST /api/v1/events

上报一个 raw_event，返回风险评估结果。对应 `AgentSSASBackendProtocol.report_event` 的 HTTP 映射，事件类型由 `raw_event.common.event_type` 与 `event_class` 字段区分。

### 请求

```text
POST /api/v1/events
Content-Type: application/json
```

请求体（`EventRequest`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `raw_event` | dict | 三层结构（common / payload / metadata）的原始事件，结构见[事件格式参考](./event-format.md) |

### 响应

响应体（`AssessmentResponse`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `assessment` | dict | `RiskAssessment` 序列化结果 |

`assessment` 内含 `RiskAssessment` 的 9 个字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `has_risk` | bool | 是否检出风险 |
| `risk_level` | str | 风险等级：safe / low / medium / high / critical |
| `risk_type` | str | 风险类型 |
| `risk_score` | float | 风险分数 |
| `confidence` | float | 置信度 |
| `detected_threats` | list[str] | 检出的威胁列表 |
| `recommended_actions` | list[str] | 建议动作列表（默认 `["log"]`） |
| `details` | dict | 详情 |
| `evidence` | dict | 证据 |

### curl 示例

```bash
curl -X POST http://localhost:8443/api/v1/events \
  -H "Content-Type: application/json" \
  -d '{
    "raw_event": {
      "common": {
        "source": "AgentSSASSecurityRail",
        "event_type": "invoke_start",
        "event_class": "lifecycle",
        "timestamp": 1767196800.0,
        "interaction_seq": 0,
        "session_id": "demo-session",
        "conversation_id": "demo-session",
        "agent_id": "demo-agent",
        "trace_id": "demo-trace",
        "context_id": "demo-ctx",
        "llm_call_seq": -1,
        "tool_call_seq": -1,
        "subsession_id": "",
        "tool_call_id": ""
      },
      "payload": {
        "content": { "query": "帮我列出当前目录下的文件" }
      },
      "metadata": {}
    }
  }'
```

响应示例：

```json
{
  "assessment": {
    "has_risk": false,
    "risk_level": "safe",
    "risk_type": "",
    "risk_score": 0.0,
    "confidence": 0.0,
    "detected_threats": [],
    "recommended_actions": ["log"],
    "details": {},
    "evidence": {}
  }
}
```

启用认证时携带 token：

```bash
curl -X POST http://localhost:8443/api/v1/events \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"raw_event": {...}}'
```

## GET /health

健康检查端点，**免认证**：

```bash
curl http://localhost:8443/health
```

响应：

```json
{"status": "ok"}
```

## Bearer token 认证

当 `AgentSSASConfig.http_token` 不为 `None` 时，服务端启用 Bearer token 认证中间件（`TokenAuthMiddleware`）：

- 除 `/health` 外的所有请求必须携带 `Authorization: Bearer <token>` 请求头，且 token 与配置值完全一致。
- 未携带、格式错误或不匹配时返回：

```text
HTTP/1.1 401 Unauthorized

{"detail": "Unauthorized"}
```

配置方式（config.yaml 或环境变量 `SSAS_HTTP_TOKEN`）：

```yaml
ssas:
  mode: http
  http_token: "my-secret-token"
```

`http_token` 为默认值 `None`（YAML 中留空）时不启用认证，所有请求直接放行。

## 懒初始化行为

服务启动时不立即创建分析后端。首次有请求到达 `POST /api/v1/events` 时，服务端才创建并初始化进程内 `AgentSSASBackend`（含检测模块扫描加载、存储初始化），之后的请求复用该实例。因此：

- 首个请求的响应时间会明显长于后续请求（包含后端初始化开销）。
- 检测模块的启用/禁用配置在首个请求到来前修改均可生效；初始化完成后需重启服务才能变更模块集合。
- 如需在启动阶段完成初始化（非懒初始化），可在自建服务时调用 `initialize_app(app, config)`（`http_server.py` 提供）。

## 客户端配置

AgentSSASSecurityRail 以 HTTP 模式运行时，通过以下配置连接服务端（见[配置参考](./configuration.md)）：

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `ssas.mode` | `inprocess` | 设为 `http` 启用客户端模式 |
| `ssas.http_endpoint` | `http://localhost:8443` | 服务端地址 |
| `ssas.http_timeout` | `5.0` | 客户端超时秒数 |
| `ssas.http_token` | `None` | 与服务端一致的 Bearer token |

## 相关文档

- [配置参考](./configuration.md)
- [事件格式参考](./event-format.md)
- [运行测试](../how-to/run-tests.md)
- [examples/http_server_demo](../../../examples/http_server_demo/)：服务端与客户端完整示例
