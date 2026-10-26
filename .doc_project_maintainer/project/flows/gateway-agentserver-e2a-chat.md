---
id: gateway-agentserver-e2a-chat
name: Gateway AgentServer E2A Chat
status: partial
confidence: confirmed
last_updated: 2026-08-01
user_visible_surface: "Channel, TUI, CLI, ACP, and web responses produced from AgentServer output."
source_of_truth: []
modules:
  - agentserver-runtime
  - gateway-and-channels
  - agent-harness
directories:
  - jiuwenswarm/server
  - jiuwenswarm/gateway/routing
code_symbols:
  - AgentWebSocketServer._handle_message
  - AgentWebSocketServer._handle_stream
  - AgentWebSocketServer._handle_unary
entrypoints:
  - jiuwenswarm/gateway/routing/agent_client.py
  - jiuwenswarm/server/agent_ws_server.py
---

# Gateway AgentServer E2A Chat

## Outcome

Gateway can send a normalized E2A or legacy-compatible request to AgentServer and receive either one final response or a stream of response chunks that the original channel/frontend can render.

## Causal Path

Gateway `WebSocketAgentServerClient` connects to AgentServer, waits for `connection.ack`, sends `E2AEnvelope.to_dict()` JSON, and receives E2A response wire frames. AgentServer `_connection_handler` creates a task per inbound frame. `_handle_message` decodes JSON, prefers `E2AEnvelope.from_dict`, falls back to legacy payload parsing when needed, injects ACP capability metadata for ACP requests, triggers before-chat hooks for chat-like methods, then dispatches by `ReqMethod`.

Requests with special methods go to local handlers. Other requests go to `_handle_stream` or `_handle_unary`. Chat-like methods first increment the warm-pool foreground guard, then their implementation helpers resolve mode and agent state through `AgentManager`, call `process_message_stream` or `process_message`, encode responses through E2A wire helpers, and send them under the connection send lock. A `finally` block releases the guard so lazy background warming resumes after the final active chat.

For newly allocated single-Agent sessions, adapter resolution first awaits the warm-pool preparation Future. A READY claim has already completed DeepAgent creation and interaction startup; a miss shares its foreground initialization task with the first chat and bypasses the background semaphore.

## State Classification

- Transient runtime state: WebSocket connection, per-request tasks, `_session_stream_tasks`, send lock, ACP client capability cache.
- Derived state: mode/sub-mode resolved onto request metadata and params.
- External integration state: Gateway response queues keyed by `request_id`.

## Replay, Restore, Or Reconstruction

Normal chat replay is not handled by this flow; session/history reconstruction belongs to `agentserver-session-lifecycle`.

## Contract

`E2AEnvelope` fields are the preferred request contract. Legacy `AgentRequest` payloads remain accepted through fallback parsing. Response wire frames are E2A response dicts or compatibility chunks with request ID alignment.

## Failure, Ordering, And Identity

`request_id` is the correlation key for Gateway queues. Writes are serialized by `send_lock`. Streaming uses a keepalive heartbeat while the agent is still running. JSON parse errors and handler exceptions are returned as error wire frames where possible.

## Verification

Evidence exists in `tests/unit_tests/agentserver/test_agentserver_modes.py`, `test_agentserver_acp.py`, `test_agentserver_cli_commands.py`, `test_agent_ws_connection_close.py`, and Gateway `test_agent_client.py`.

## Known Gaps

Full live WebSocket integration of real server handshake, concurrent frames, heartbeat, origin rejection, disconnect cleanup, and Gateway rendering is still pending.
