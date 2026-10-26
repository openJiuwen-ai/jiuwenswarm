---
symbol: AgentWebSocketServer._handle_command_resume
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_resume`

## Actual Role

Handles unary `command.resume` only as a mock response generator: it treats a nonblank string `params.query` as the returned session id, otherwise invents `sess_mock_resume`, and always claims the session resumed with a fixed preview. It neither validates nor reads, switches, or restores session/history/agent state; caught construction errors become an error payload and the frame is sent under `send_lock`.

## Key Signals

- Input: `params.query`; nonblank strings are accepted verbatim, all other values select a fabricated fallback id.
- Output: Normal execution always returns `ok=true`, `resumed=true`, and a fixed mock preview; only internal construction failure returns error text.
- Main side effects: Sends one WebSocket frame; no resume-related state changes occur.
- Main risk: Legacy/external clients can receive a false confirmation that an arbitrary session resumed.
- Related tests: One direct test pins the mock success response. No behavioral resume, invalid-input, authorization, missing-session, restoration, or parity test covers this route; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
