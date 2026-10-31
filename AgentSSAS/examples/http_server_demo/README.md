# HTTP 服务模式演示（http_server_demo）

演示 AgentSSAS HTTP 服务模式：SSAS 以独立进程运行，Agent 通过 HTTP 上报事件、获取风险评估。适合多 Agent 共享同一分析引擎的场景。

示例包含两个脚本，仅使用 localhost，不依赖外网。

## 运行

终端 1 —— 启动服务端（需 http 扩展依赖：fastapi / uvicorn）：

```bash
uv run --extra http python examples/http_server_demo/start_server.py
```

终端 2 —— 运行客户端（仅标准库）：

```bash
uv run python examples/http_server_demo/client.py
```

## 服务端

- 监听 `http://127.0.0.1:8443`（仅本机回环；可用环境变量 `SSAS_HTTP_HOST` / `SSAS_HTTP_PORT` 覆盖）
- 端点：
  - `POST /api/v1/events`：上报事件，请求体 `{"raw_event": {...}}`，响应 `{"assessment": {...}}`
  - `GET /health`：健康检查
- 首次事件到达时懒初始化后端（加载检测模块）
- 存储默认 `~/.jiuwenswarm/ssas`（可用 `SSAS_HOME` 覆盖），威胁日志与告警在服务端落盘

## 客户端预期输出

```text
[demo] GET /health -> {'status': 'ok'}
[demo] event=invoke_start               risk_level=safe     has_risk=False
[demo] event=llm_input                   risk_level=safe     has_risk=False
[demo] event=tool_input                  risk_level=safe     has_risk=False
[demo] event=tool_output                 risk_level=safe     has_risk=False
[demo] event=llm_output                  risk_level=safe     has_risk=False
[demo] event=invoke_end                  risk_level=safe     has_risk=False
[demo] event=permission_interrupt_tool   risk_level=safe     has_risk=False
[demo] 完成。事件与告警已落盘到服务端存储目录(默认 ~/.jiuwenswarm/ssas)。
```

`permission_interrupt_tool` 由 `security_rail_detection` 模块以 notify 模式后台分析，`report_event` 立即返回（safe），检测结论稍后写入服务端的威胁日志与告警表。

## 启用认证（可选）

服务端支持 Bearer token 认证。修改 `start_server.py` 中的配置，在 `AgentSSASConfig.from_dict({...})` 里加入：

```python
"http_token": "your-secret-token",
```

此后未携带 `Authorization: Bearer your-secret-token` 头的请求将返回 401（`/health` 除外）。

HTTP API 完整定义参见 [docs/zh/reference/http-api.md](../../docs/zh/reference/http-api.md)。
