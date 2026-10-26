---
symbol: AgentWebSocketServer._handle_browser_runtime_restart
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_browser_runtime_restart`

## Actual Role

Handles unary `browser.runtime_restart` with no request parameters by synchronously calling OpenJiuwen's process-global local browser-runtime restart function on the AgentServer event loop. It reports the returned URL or `None` as `ok=true`, converts thrown exceptions to `ok=false`, and sends one response under `send_lock`; the Gateway config-save caller triggers it for browser-related environment changes but discards that response.

## Key Signals

- Input: No functional params; any routed `ReqMethod.BROWSER_RUNTIME_RESTART` invokes the global restart.
- Output: One response with `result` URL/`None`, or exception text; Gateway currently ignores success/failure.
- Main side effects: May terminate/spawn a subprocess and clear/replace dependency-owned process and URL globals.
- Main risk: Synchronous, unserialized global process mutation can block the loop or race, while its initiating caller can report config-save success after restart failure.
- Related tests: No direct RPC, dependency lifecycle, concurrency/responsiveness, or Gateway failure-propagation test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
