---
symbol: AgentWebSocketServer._handle_cancel
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_cancel`

## Actual Role

Resolves the runtime that should receive a `CHAT_CANCEL`: exact mode/project cache lookup, then any same-project cached runtime, then optional creation. It delegates interrupt semantics to `agent.process_message`, or returns a synthetic successful no-runtime result when creation is forbidden, and sends one encoded response under `send_lock`. Session stream cancellation and disconnect cleanup are intentionally owned by `_handle_message` before/after this call.

## Key Signals

- Input: `CHAT_CANCEL` request with dict-like `params`, connection/send lock, and `allow_create` policy.
- Output: Sends one encoded interrupt response; returns `None`.
- Main side effects: may create an agent, invokes its interrupt path, and writes a WebSocket frame.
- Main risk: fallback may interrupt an unrelated cached mode, while default policy creates a runtime when none exists.
- Test evidence: eight focused cancel cases in `test_agent_ws_connection_close.py` passed on current HEAD as part of the preceding 12-case router run; selection ambiguity and normal no-runtime creation remain untested.
- Related flow: `gateway-agentserver-e2a-chat` describes dispatch and response locking but does not model this specialized cancel-runtime selection.

## Detail Index

- Detail docs pending.
