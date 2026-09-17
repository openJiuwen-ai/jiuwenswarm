---
symbol: AgentWebSocketServer._handle_acp_tool_response
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_acp_tool_response`

## Actual Role

Consumes ACP callback traffic by extracting caller-provided `jsonrpc_id` and `response`, coercing non-dict responses to an empty mapping, and asking the process-global AcpOutputManager to remove and complete the matching future. It does no JSON-RPC schema or ownership validation. A match yields `accepted:true`; a miss is logged and normalized to an `ok:true` soft-ignore payload. Only after the global future has been irreversibly completed does it encode and send the acknowledgment under `send_lock`.

## Key Signals

- Input/routing: `params.jsonrpc_id` plus `params.response`; `_handle_unary`, ACP adapters, and generic forwarding can route it, but connection/channel/session/method/body identity are ignored.
- Output: One `ok:true` acknowledgment distinguishing accepted from soft-ignored unknown/late response. Malformed bodies can still be accepted; request metadata is not copied.
- Main side effects: Pops and completes a process-global pending future, logs unknown IDs, and sends one WebSocket frame. Completion precedes transport acknowledgment and has no rollback/replay record.
- Main risk: An unscoped or malformed callback can satisfy another request, and split Gateway/AgentServer registries plus acknowledgment loss can permanently desynchronize correlation state.
- Related evidence: Tests cover valid and unknown IDs only; ownership, schema, races, duplicate/loss handling, and payload bounds remain untested. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
