---
id: agentserver-server-push
name: AgentServer Server Push
status: partial
confidence: confirmed
last_updated: 2026-07-07
user_visible_surface: "Gateway-visible events emitted by AgentServer without an awaited request response."
source_of_truth: []
modules:
  - agentserver-runtime
  - gateway-and-channels
directories:
  - jiuwenswarm/server
  - jiuwenswarm/gateway/routing
code_symbols:
  - AgentWebSocketServer.send_push
entrypoints:
  - jiuwenswarm/server/gateway_push/wire.py
  - jiuwenswarm/server/agent_ws_server.py
---

# AgentServer Server Push

## Outcome

AgentServer can deliver downstream events to Gateway even when they are not the direct final response for a request. Examples include ACP output, proactive events, compression state updates, and team/evolution events.

## Causal Path

Runtime code calls a `send_push` callback or transport with a message dict. `build_server_push_wire` converts either a structured `response_kind` message or an `AgentResponseChunk`-like message into E2A response wire and marks metadata with the server-push key. `AgentWebSocketServer.send_push` sends the wire frame through the currently active Gateway WebSocket and send lock. Gateway client detects the metadata flag and routes the frame to a push handler instead of the request queue.

## State Classification

- Transient runtime state: `_current_ws` and `_current_send_lock` on the AgentWebSocketServer instance.
- Derived output: E2A response wire frame with server-push metadata.

## Contract

Message dicts may carry `response_kind`, `body`, `payload`, `request_id`, `channel_id`, `session_id`, `metadata`, and `is_complete`. Internal E2A metadata keys are stripped from user metadata in chunk-style pushes.

## Failure, Ordering, And Identity

If no Gateway WebSocket is active, `send_push` logs a warning and drops the push. Because the server stores one current WebSocket, multi-Gateway ownership semantics are not yet documented.

## Verification

`tests/unit/agentserver/test_gateway_push_transport.py` verifies default transport forwards messages to the AgentServer singleton. Gateway tests cover server-push handling adjacent to evolution events. Direct `build_server_push_wire` branch tests are pending.

## Known Gaps

Direct tests for `response_kind`, metadata filtering, server-push flagging, and session propagation are needed.
