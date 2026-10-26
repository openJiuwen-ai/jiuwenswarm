---
symbol: AgentWebSocketServer._handle_config_cache_clear
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_config_cache_clear`

## Actual Role

Handles unary legacy `config.cache_clear` by importing the memory-config helper, setting only its process-global `_config_cache` reference to `None`, and sending `cleared=true` under `send_lock`. It does not reread configuration, reload agents, refresh existing memory managers, expose a scope/revision, or coordinate with concurrent cache loads; exceptions become error text.

## Key Signals

- Input: No functional params; any routed `CONFIG_CACHE_CLEAR` request invalidates the same process-global slot.
- Output: `cleared=true` confirms assignment only, not reread or runtime application.
- Main side effects: Mutates one global cache reference, logs failures, and sends a WebSocket frame.
- Main risk: The orphaned, narrowly named endpoint overstates refresh and can lose a race with in-progress loading.
- Related tests: No direct helper/RPC/lifecycle/concurrency test was found; Web/TUI helper tests target AGENT_RELOAD_CONFIG instead. Tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
