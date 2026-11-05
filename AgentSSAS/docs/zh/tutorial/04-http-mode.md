# HTTP 服务模式

> 本篇讲解如何以独立 HTTP 服务的方式运行 AgentSSAS：启动服务端、运行客户端上报事件、启用 Bearer token 认证，并让 JiuwenSwarm 切换到 HTTP 模式。适合需要多 Agent 共享同一分析引擎或独立部署 SSAS 的用户。

## 何时选择 HTTP 模式

AgentSSAS 支持两种运行模式，默认为 inprocess（进程内库模式）。在以下场景中考虑 HTTP 模式：

- **多 Agent 进程共享同一分析引擎**：多个 JiuwenSwarm（或其他 Agent）进程把事件上报给同一个 SSAS 服务，检测数据集中落盘、集中分析
- **独立部署**：SSAS 与 Agent 运行时分开部署、分开升级，互不影响
- **故障隔离**：SSAS 服务端独立运行，其异常或重启不会波及 Agent 进程（叠加 fail-open 兜底）

两种模式的详细对比见文末表格。

## 启动服务端

HTTP 模式需要额外安装 http 扩展依赖（fastapi / uvicorn / httpx）。在 AgentSSAS 仓库根目录执行：

```bash
uv run --extra http python examples/http_server_demo/start_server.py
```

示例服务端默认监听 `http://127.0.0.1:8443`（仅本机回环，不依赖外网），可用环境变量覆盖：

| 环境变量 | 说明 |
| -------- | ---- |
| `SSAS_HTTP_HOST` | 监听地址 |
| `SSAS_HTTP_PORT` | 监听端口 |
| `SSAS_HOME` | 存储根目录（默认 `~/.jiuwenswarm`） |

服务端提供两个端点：

- `POST /api/v1/events`：上报事件，请求体 `{"raw_event": {...}}`，响应 `{"assessment": {...}}`
- `GET /health`：健康检查

首次事件到达时服务端会懒初始化后端（加载检测模块）。事件、告警与威胁日志在服务端进程落盘，默认存储于 `~/.jiuwenswarm/ssas/`。

后端的 `initialize()` 是幂等的——重复或并发调用只执行一次实际加载，其余调用等待同一初始化完成；`report_event` 在初始化未完成时会自动等待（fire-and-forget 方式调用 initialize 无害，不会因订阅表尚未就绪而漏检测）。

端点的完整定义参见[HTTP API 参考](../reference/http-api.md)。

## 运行客户端

另开一个终端，在 AgentSSAS 仓库根目录执行（客户端仅使用 Python 标准库）：

```bash
uv run python examples/http_server_demo/client.py
```

预期输出：

```text
[demo] GET /health -> {'status': 'ok'}
[demo] event=invoke_start               risk_level=safe     has_risk=False
[demo] event=llm_input                  risk_level=safe     has_risk=False
[demo] event=tool_input                 risk_level=safe     has_risk=False
[demo] event=tool_output                risk_level=safe     has_risk=False
[demo] event=llm_output                 risk_level=safe     has_risk=False
[demo] event=invoke_end                 risk_level=safe     has_risk=False
[demo] event=permission_interrupt_tool  risk_level=safe     has_risk=False
[demo] 完成。事件与告警已落盘到服务端存储目录(默认 ~/.jiuwenswarm/ssas)。
```

与进程内模式一致：`permission_interrupt_tool` 由 `security_rail_detection` 模块以 notify 模式后台分析，`report_event` 立即返回（`safe`），检测结论稍后写入服务端的威胁日志与告警表。

## 启用 Bearer token 认证（可选）

服务端支持 Bearer token 认证。修改 `start_server.py` 中的配置，在 `AgentSSASConfig.from_dict({...})` 里加入：

```python
"http_token": "your-secret-token",
```

此后未携带 `Authorization: Bearer your-secret-token` 头的请求将返回 401（`/health` 除外，健康检查端点始终免认证）。

## 让 JiuwenSwarm 切换到 HTTP 模式

先按[集成 JiuwenSwarm](./03-integrate-jiuwenswarm.md)完成集成，然后启动独立 SSAS 服务端，并修改 `~/.jiuwenswarm/config/config.yaml` 的 `ssas` 段：

```yaml
ssas:
  enabled: true
  mode: http
  http_endpoint: "http://localhost:8443"   # 指向独立服务端地址
  # 若服务端启用了认证,还需配置:
  # http_token: "your-secret-token"
```

重启 JiuwenSwarm 后，日志中的启动确认行会变为 `mode=http`：

```text
[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail create success, mode=http, policy=observe_only
```

此后 AgentSSASSecurityRail 采集的事件经 HTTP 上报给独立服务端，检测结果返回给 Rail，数据统一落盘在服务端。

## 与 inprocess 模式对比

| 维度 | inprocess（进程内） | http（独立服务） |
| ---- | ------------------- | ---------------- |
| 部署形态 | 分析引擎运行在 jiuwenswarm 进程内 | 独立 FastAPI 服务进程 |
| 事件上报延迟 | 零网络延迟（进程内调用） | 每次上报经 HTTP 往返 |
| 多 Agent 共享 | 每个 Agent 进程各自一份引擎与数据 | 多个 Agent 进程共享同一引擎与数据 |
| 故障隔离 | 引擎与主进程同生命周期，异常由 fail-open 兜底 | 服务端独立运行，其异常或重启不影响 Agent 进程（fail-open 兜底） |
| 适用场景 | 单 Agent、追求低延迟 | 多 Agent 共享、独立部署、集中分析 |

## 相关资源

- [examples/http_server_demo/README.md](../../../examples/http_server_demo/README.md)：本教程对应的示例说明
- [HTTP API 参考](../reference/http-api.md)：端点、请求与响应的完整定义
- [查看威胁日志](../how-to/view-threat-logs.md)：服务端落盘数据的查看方法
- [架构解析](../explanation/architecture.md)：双运行模式的设计背景
