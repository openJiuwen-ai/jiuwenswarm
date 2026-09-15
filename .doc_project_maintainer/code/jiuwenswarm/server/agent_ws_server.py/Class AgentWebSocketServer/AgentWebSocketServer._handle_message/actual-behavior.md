---
symbol: AgentWebSocketServer._handle_message
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_message`

## Actual Role

Acts as the WebSocket protocol boundary and central request router. It decodes one frame into an `AgentRequest`, enriches ACP metadata, invokes before-chat hooks, dispatches control/RPC families, and sends ordinary chat to `AgentRuntime`. Work/Code Normal stream execution is owned and cancelled by the Runtime Coordinator; the host task map is used only by Plan, Team, and background execution. Disconnect-scoped cancellation can additionally clean the Session runtime. JSON parse and guarded handler errors become wire responses when possible.

## Key Signals

- Input: raw WebSocket JSON frame and send lock.
- Output: response sent to WebSocket or delegated to another handler.
- Main side effects: mutates request metadata, invokes extension hooks and handlers, delegates single-Agent cancellation to Runtime, cancels/awaits host-owned Plan/Team/background tasks, and may clean session runtime.
- Main risks: a 377-line dispatch surface, conversion outside the normalized error boundary, request-overridable ACP capabilities, and unbounded cancel cleanup wait.
- Test evidence: the affected AgentServer/Gateway selection passed 192 tests on the 2026-09-08 working tree, including create/switch registration, cancellation order, connection close, and all user-channel stream identities.
- Related flow: `project/flows/gateway-agentserver-e2a-chat.md` correctly places this method at decode/dispatch before unary/stream processing, but does not narrow these method-level risks.

## Detail Index

- Detail docs pending.
