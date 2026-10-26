---
symbol: AgentWebSocketServer._handle_command_chrome
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_chrome`

## Actual Role

Acknowledges a routed `command.chrome` request with an empty successful response. It does not inspect params or invoke browser integration. Gateway and AgentServer retain forwarding/dispatch support, but the matching TUI command factory is absent from the builtin registry.

## Key Signals

- Input: The request id and channel id are only used to form the response envelope.
- Output: One WebSocket response with `ok=true` and `{}` payload. Encoding/send failures escape the local guard and fall to `_handle_message`'s outer policy.
- Main side effects: Sends a WebSocket frame; the local exception log is reachable only for response-construction failures.
- Main risk: A callable command reports success without performing an action, while its frontend command is not registered.
- Related tests: Static search found no direct handler, dispatch, command-factory, or frontend builtin registration tests; no tests were run during this re-audit.

## Detail Index

- Detail docs pending.
