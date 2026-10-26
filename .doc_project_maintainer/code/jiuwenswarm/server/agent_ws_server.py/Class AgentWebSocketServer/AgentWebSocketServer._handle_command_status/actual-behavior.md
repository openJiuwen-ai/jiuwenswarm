---
symbol: AgentWebSocketServer._handle_command_status
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_status`

## Actual Role

Handles unary `command.status` through `usage`, `config`, or default/unknown-action overview. Usage aggregates only the newest 500 metadata records while reporting the full session count; config reports config-path sources; overview combines version/session/cwd, effective default-model fields, MCP configuration, config paths, a hard-coded connected state, and project-memory diagnostics. It catches branch failures into one error response and sends under `send_lock`; overview clears and repopulates shared memory-discovery cache.

## Key Signals

- Input: Request identity plus optional `params.action`, `cwd`, and `trusted_dirs`.
- Output: One success/error response; unknown actions silently use overview. Several usage names/totals describe different scopes, and memory failure is represented as an empty warning list.
- Main side effects: Reads config, metadata, environment, and workspace files; clears/rebuilds shared project-memory cache; logs diagnostics; sends WebSocket data.
- Main risk: Operator-visible usage fields are materially inaccurate, while a read-only status call can block on uncached filesystem work.
- Related tests: No direct method/payload test was found. Gateway stream-cancel tests use COMMAND_STATUS only as a forwarded fake request; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
