# HTTP Service Mode

> This chapter explains how to run AgentSSAS as a standalone HTTP service: starting the server, running a client to report events, enabling Bearer token authentication, and switching JiuwenSwarm to HTTP mode. It is intended for users who need multiple agents to share the same analysis engine or to deploy SSAS independently.

## When to Choose HTTP Mode

AgentSSAS supports two run modes, with inprocess (in-process library mode) as the default. Consider HTTP mode in the following scenarios:

- **Multiple agent processes share the same analysis engine**: multiple JiuwenSwarm (or other agent) processes report events to the same SSAS service, and the detection data is persisted and analyzed centrally
- **Independent deployment**: SSAS and the agent runtime are deployed and upgraded separately, without affecting each other
- **Fault isolation**: the SSAS server runs independently, so its exceptions or restarts do not affect the agent processes (on top of the fail-open fallback)

See the table at the end of this chapter for a detailed comparison of the two modes.

## Starting the Server

HTTP mode requires the extra http extension dependencies (fastapi / uvicorn / httpx). From the AgentSSAS repository root, run:

```bash
uv run --extra http python examples/http_server_demo/start_server.py
```

By default the example server listens on `http://127.0.0.1:8443` (local loopback only, no internet access required); this can be overridden with environment variables:

| Environment Variable | Description |
| -------------------- | ----------- |
| `SSAS_HTTP_HOST` | Listen address |
| `SSAS_HTTP_PORT` | Listen port |
| `SSAS_HOME` | Storage root directory (default `~/.jiuwenswarm`) |

The server provides two endpoints:

- `POST /api/v1/events`: report an event; the request body is `{"raw_event": {...}}` and the response is `{"assessment": {...}}`
- `GET /health`: health check

The server lazily initializes the backend when the first event arrives (loading the detection modules). Events, alerts, and threat logs are persisted within the server process, by default under `~/.jiuwenswarm/ssas/`.

The backend's `initialize()` is idempotent - repeated or concurrent calls perform the actual loading only once, while the other calls wait for that same initialization to finish; `report_event` automatically waits when initialization has not completed (calling initialize in a fire-and-forget manner is harmless, and no detection is missed due to the subscription table not being ready yet).

For the complete endpoint definitions, see [HTTP API Reference](../reference/http-api.md).

## Running the Client

Open another terminal, and from the AgentSSAS repository root, run (the client uses only the Python standard library):

```bash
uv run python examples/http_server_demo/client.py
```

Expected output:

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

Consistent with in-process mode: `permission_interrupt_tool` is analyzed in the background by the `security_rail_detection` module in notify mode; `report_event` returns immediately (`safe`), and the detection conclusion is written later to the threat log and the alert table on the server side.

## Enabling Bearer Token Authentication (Optional)

The server supports Bearer token authentication. Modify the configuration in `start_server.py` by adding the following to `AgentSSASConfig.from_dict({...})`:

```python
"http_token": "your-secret-token",
```

From then on, requests without the `Authorization: Bearer your-secret-token` header will receive a 401 (except `/health`; the health check endpoint is always exempt from authentication).

## Switching JiuwenSwarm to HTTP Mode

First complete the integration as described in [Integrating JiuwenSwarm](./03-integrate-jiuwenswarm.md), then start the standalone SSAS server and modify the `ssas` section of `~/.jiuwenswarm/config/config.yaml`:

```yaml
ssas:
  enabled: true
  mode: http
  http_endpoint: "http://localhost:8443"   # Points to the standalone server address
  # If the server has authentication enabled, also configure:
  # http_token: "your-secret-token"
```

After JiuwenSwarm restarts, the confirmation line in the log changes to `mode=http`:

```text
[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail create success, mode=http, policy=observe_only
```

From then on, the events collected by AgentSSASSecurityRail are reported over HTTP to the standalone server; the detection results are returned to the Rail, and the data is persisted centrally on the server.

## Comparison with inprocess Mode

| Dimension | inprocess (in-process) | http (standalone service) |
| --------- | ---------------------- | ------------------------- |
| Deployment form | The analysis engine runs inside the jiuwenswarm process | A standalone FastAPI service process |
| Event reporting latency | Zero network latency (in-process calls) | Each report makes an HTTP round trip |
| Multi-agent sharing | Each agent process has its own engine and data | Multiple agent processes share the same engine and data |
| Fault isolation | The engine shares the lifecycle of the main process; exceptions are covered by the fail-open fallback | The server runs independently; its exceptions or restarts do not affect the agent processes (fail-open fallback) |
| Suitable scenarios | Single agent, low-latency requirements | Multi-agent sharing, independent deployment, centralized analysis |

## Related Resources

- [examples/http_server_demo/README.md](../../../examples/http_server_demo/README.md): the example that accompanies this tutorial
- [HTTP API Reference](../reference/http-api.md): the complete definitions of the endpoints, requests, and responses
- [View Threat Logs](../how-to/view-threat-logs.md): how to inspect the data persisted by the server
- [Architecture Principles](../explanation/architecture.md): the design background of the dual run modes
