---
symbol: AgentWebSocketServer._handle_command_mcp
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_mcp`

## Actual Role

Handles the complete `command.mcp` control plane in one method: list/show configured servers, add/update/enable/disable/remove persisted entries, inspect cached ToolMgr resources, and temporarily connect to MCP servers for tool count/listing. Mutation actions write `config.yaml` first and then reload all agents; responses distinguish request errors with MCP codes but express reload degradation through `applied=false` inside otherwise successful payloads.

## Key Signals

- Input: `params.action` (default `list`) plus action-specific server name, transport, command/args/cwd/env, URL/headers, enabled, and timeout fields.
- Output: One locked WebSocket response: typed list/detail/tools or mutation payloads; bad request/not found/internal exceptions use stable MCP codes, while reload failure remains `ok=true/applied=false`.
- Main side effects: Reads and rewrites global config, may spawn/connect/disconnect temporary MCP clients, reads private global ToolMgr state, reloads active agents, and sends a WebSocket frame.
- Main risk: Persisted/runtime divergence, silent runtime-registration skips, unbounded tool discovery, lossy normalization, and incomplete credential masking are concentrated in one high-coupling handler.
- Related tests: Handler tests cover list, add/reload, enable-not-found, remove, update, and an add-list-disable flow. They do not cover show/list_tools, timeouts, unchanged update, reload degradation/rollback, HTTP registration skip, broad masking, or TUI handling of `applied=false`; no tests were run during this re-audit.

## Detail Index

- Detail docs pending.
