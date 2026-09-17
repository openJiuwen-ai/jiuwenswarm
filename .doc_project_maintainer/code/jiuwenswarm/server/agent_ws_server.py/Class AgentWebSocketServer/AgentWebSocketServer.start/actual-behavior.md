---
symbol: AgentWebSocketServer.start
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer.start`

## Actual Role

When `_server` is `None`, resets persisted harness-package activation state, requires the process-wide SQLite checkpointer, and binds an 8 MiB WebSocket listener with connection, origin-processing, and ping callbacks through `websockets.legacy.server.serve` or an ImportError fallback. It stores and logs the listener before synchronously awaiting best-effort internal JiuwenBox bootstrap. An existing `_server` makes the method warn and return without repeating any initialization.

## Key Signals

- Input: no explicit input; uses instance host, port, ping, and process config.
- Output: Returns `None`; `_server` is set to the bound listener object, or remains unchanged on duplicate start.
- Main side effects: resets harness-package state, initializes global checkpointer state, opens/stores a network listener, and may start JiuwenBox and rewrite sandbox endpoint/enabled config.
- Error boundary: reset is internally best-effort; checkpointer and listener failures propagate before a successful bind. Sandbox exceptions are softened, but cancellation is not.
- Main risk: the open listener is observable before optional bootstrap and later app readiness; bootstrap can delay return up to its downstream timeout, and cancellation can strand the listener.
- Related evidence: `app_agentserver._run` awaits `start()` before ProactiveEngine initialization and its ready log. `agentserver-sandbox-runtime` documents bootstrap/restart behavior. The app-level test uses a fake server; direct lifecycle coverage is absent.

## Detail Index

- Detail docs pending.
