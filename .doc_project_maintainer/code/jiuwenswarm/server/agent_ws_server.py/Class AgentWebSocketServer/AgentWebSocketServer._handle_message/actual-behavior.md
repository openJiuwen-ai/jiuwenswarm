---
symbol: AgentWebSocketServer._handle_message
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_message`

## Actual Role

Acts as the WebSocket protocol boundary and central request router. It decodes one frame into E2A or legacy `AgentRequest`, enriches ACP metadata, invokes before-chat hooks, dispatches control/RPC families, and sends ordinary chat to unary or stream execution. Its cancel branch also cancels and awaits the active session stream and conditionally cleans disconnect-scoped runtime. JSON parse and guarded handler errors become wire responses when possible, but compatibility-conversion failures precede that guard.

## Key Signals

- Input: raw WebSocket JSON frame and send lock.
- Output: response sent to WebSocket or delegated to another handler.
- Main side effects: mutates request metadata, invokes extension hooks and handlers, cancels/awaits shared session tasks, and may clean session runtime.
- Main risks: a 377-line dispatch surface, conversion outside the normalized error boundary, request-overridable ACP capabilities, and unbounded cancel cleanup wait.
- Test evidence: 12 focused cases in `test_agent_ws_connection_close.py` and `test_agentserver_acp.py` passed on current HEAD (the command later hit its outer timeout during coverage teardown); broader modes/CLI suites mostly test delegated handlers.
- Related flow: `project/flows/gateway-agentserver-e2a-chat.md` correctly places this method at decode/dispatch before unary/stream processing, but does not narrow these method-level risks.

## Detail Index

- Detail docs pending.
