# MCP Server Configuration

JiuwenSwarm supports connecting to external tool services via MCP (Model Context Protocol). MCP lets the Agent call tools provided by third-party services, extending its capabilities.

---

## Two MCP Usage Scenarios

| Scenario | Configuration | Description |
|----------|--------------|-------------|
| **Browser runtime MCP** | Environment variables (`.env`) | MCP wrapper for Playwright browser automation; see [Browser Tools](Browser.md) |
| **General MCP servers** | `config.yaml` → `mcp.servers` | Connect any MCP service (stdio / sse / streamable-http); this document focuses on this |

---

## Configuring MCP Servers

Add MCP servers in `config/config.yaml`:

```yaml
mcp:
  servers:
    - name: my-local-tool
      enabled: true
      transport: stdio
      command: python
      args: ["path/to/server.py", "--transport", "stdio"]
      cwd: .
      env:
        LOG_LEVEL: INFO

    - name: remote-api
      enabled: false
      transport: sse
      url: http://127.0.0.1:9000/sse
      headers:
        Authorization: Bearer your-token-here
      timeout_s: 30

    - name: remote-streamable
      enabled: true
      transport: streamable-http
      url: http://127.0.0.1:8000/mcp
      timeout_s: 60
```

---

## Field Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Server identifier; must be globally unique |
| `enabled` | bool | Yes | Whether to enable this server |
| `transport` | string | Yes | Transport protocol: `stdio`, `sse`, or `streamable-http` |
| `command` | string | Required for stdio | Launch command (e.g. `python`, `npx`) |
| `args` | list | No | Command arguments |
| `cwd` | string | No | Working directory |
| `env` | map | No | Environment variables passed to the subprocess |
| `url` | string | Required for sse/streamable-http | Server URL |
| `headers` | map | No | Request headers (e.g. Authorization) |
| `timeout_s` | int | No | Request timeout in seconds |

---

## Transport Protocol Comparison

| Protocol | Use case | Description |
|----------|----------|-------------|
| **stdio** | Local tool servers | Agent launches a subprocess and communicates via stdin/stdout. Suitable for local Python/Node scripts |
| **sse** | Remote HTTP servers | Server-Sent Events protocol. Suitable for existing HTTP servers |
| **streamable-http** | Remote HTTP servers (recommended) | HTTP-based streaming protocol with better performance than SSE. Recommended for remote servers |

---

## Agent-Side Management Commands

Use `/mcp` commands during conversations to manage MCP servers:

```
/mcp list              # List configured MCP servers and their status
/mcp reload            # Reload all MCP servers
/mcp enable <name>     # Enable a specific server
/mcp disable <name>    # Disable a specific server
```

---

## Browser MCP Runtime

Browser automation uses a separate MCP runtime configured via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `BROWSER_RUNTIME_MCP_ENABLED` | Enable browser MCP runtime | `1` |
| `BROWSER_RUNTIME_MCP_CLIENT_TYPE` | Client type: `streamable-http` / `sse` / `stdio` | `streamable-http` |
| `BROWSER_RUNTIME_MCP_SERVER_PATH` | MCP server URL | `http://127.0.0.1:8940/mcp` |
| `BROWSER_RUNTIME_MCP_TIMEOUT_S` | Timeout in seconds | `300` |
| `BROWSER_RUNTIME_MCP_HOST` | Local wrapper host | `127.0.0.1` |
| `BROWSER_RUNTIME_MCP_PORT` | Local wrapper port | `8940` |

See [Browser Tools](Browser.md) for details.

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `BROWSER_RUNTIME_MCP_ENABLED` | Enable/disable browser MCP runtime |
| `BROWSER_RUNTIME_MCP_CLIENT_TYPE` | Browser MCP client type |
| `BROWSER_RUNTIME_MCP_SERVER_PATH` | Browser MCP server URL |
| `BROWSER_RUNTIME_MCP_TIMEOUT_S` | Browser MCP timeout |

---

## See Also

- [Browser Tools](Browser.md) — Browser MCP runtime detailed configuration
- [Configuration](Configuration.md) — Full configuration reference