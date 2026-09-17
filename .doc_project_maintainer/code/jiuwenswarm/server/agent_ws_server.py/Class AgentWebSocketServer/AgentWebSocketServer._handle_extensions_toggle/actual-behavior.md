---
symbol: AgentWebSocketServer._handle_extensions_toggle
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_extensions_toggle`

## Actual Role

Handles unary `extensions.toggle` by defaulting an omitted enabled value to false, obtaining the singleton RailManager, and optionally rebinding it to one unqualified current Agent instance. It persists the configured value first, then awaits register/unregister against the singleton runtime set, and returns the configured extension on success; validation/load/runtime failures become error text without rollback, and the response is sent under `send_lock`.

## Key Signals

- Input: Extension `name` plus nominal `enabled`; omission means disable and non-booleans are accepted.
- Output: Configured extension record after runtime hot reload, or exception text; no separate configured/applied state is returned.
- Main side effects: Rebinds singleton Agent state, mutates shared extension cache/JSON, dynamically loads plugins, registers/unregisters a Rail, logs, and sends a WebSocket frame.
- Main risk: Durable, UI, and per-Agent runtime states can disagree after either success or failure.
- Related tests: No handler/manager validation, identity, rollback, hot-reload failure, concurrency, configured-vs-applied, or wire test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
