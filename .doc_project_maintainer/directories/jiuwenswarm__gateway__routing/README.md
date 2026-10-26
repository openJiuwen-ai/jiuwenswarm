---
path: jiuwenswarm/gateway/routing
encoded: jiuwenswarm__gateway__routing
modules:
  - gateway-and-channels
confidence: confirmed
last_updated: 2026-07-07
read_when: "Working on Gateway-to-AgentServer transport, request queues, session maps, or routing helpers."
---

# `jiuwenswarm/gateway/routing`

## Purpose

Contains the Gateway-side client and helper state for talking to AgentServer. The most important file for the AgentServer slice is `agent_client.py`, which connects to the AgentServer WebSocket, sends E2A envelopes, dispatches response frames by request ID, and handles server-push frames outside normal RPC queues.

## Important Files

- `agent_client.py`: abstract and WebSocket AgentServer clients.
- `agent_request_timeout.py`: timeout policy.
- `session_map.py`: channel/session mapping support.
- `interaction_context.py`: pending interaction context support.
- `route_binding.py`: routing bindings.

## Related Flows

- `gateway-agentserver-e2a-chat`
- `agentserver-server-push`

## Coverage

Partial. Gateway client tests exist, but `session_map.py`, `interaction_context.py`, and `route_binding.py` need direct directory documentation and test-evidence review.
