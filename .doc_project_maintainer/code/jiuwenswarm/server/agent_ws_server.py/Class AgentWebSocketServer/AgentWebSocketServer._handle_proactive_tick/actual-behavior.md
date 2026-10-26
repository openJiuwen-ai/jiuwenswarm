---
symbol: AgentWebSocketServer._handle_proactive_tick
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_proactive_tick`

## Actual Role

Handles Gateway Cron `proactive.tick` requests by checking for an injected ProactiveEngine, forwarding optional `params.target_channel` to `tick_now`, mapping its delivered/not-delivered boolean plus `last_tick_at` into a status payload, and sending one E2A response. Recommendation, profile, and delivery work stays inside ProactiveEngine.

## Key Signals

- Input: `AgentRequest.params.target_channel`, websocket, and send lock.
- Output: One encoded AgentResponse: initialization and locally raised errors use `ok=false`; all returned engine booleans use `ok=true` with `success` and a status string that may embed `last_tick_at`.
- Main side effects: May run the proactive situation scan, model decision, delivery, and state update through ProactiveEngine, then sends a WebSocket frame.
- Main risk: Cross-process params are implicit, and the boolean engine contract conflates execution failure with benign skip outcomes.
- Related tests: ProactiveEngine flow tests cover recommendation, cooldown, quota, and busy-session behavior. Static search found no direct handler or Gateway Cron proactive-branch tests; no tests were run during this re-audit.

## Detail Index

- Detail docs pending.
