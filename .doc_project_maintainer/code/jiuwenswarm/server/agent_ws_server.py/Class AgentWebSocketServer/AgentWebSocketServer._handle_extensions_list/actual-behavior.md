---
symbol: AgentWebSocketServer._handle_extensions_list
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_extensions_list`

## Actual Role

Handles unary `extensions.list` by obtaining the process-wide RailManager singleton and returning every cached configured RailExtension as a dict under `send_lock`. First access synchronously creates the extension directory and loads JSON; parse failure is softened by the manager to an empty cache, and the response does not join effective `_registered_rails` state. Handler-level exceptions become error text.

## Key Signals

- Input: No business parameters; request identity is copied into the response.
- Output: Configured extension metadata or exception text; configured `enabled` is not confirmed applied state.
- Main side effects: First access initializes shared singleton/cache state, creates a directory, reads JSON, logs, and sends a WebSocket frame.
- Main risk: A successful list can hide config-load failure or present unapplied extensions as enabled.
- Related tests: Only a RailManager path test is adjacent; no manager-list, handler, corrupt-config, configured-vs-applied, large-output, or wire-failure test was found. Tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
