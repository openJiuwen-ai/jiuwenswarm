---
symbol: AgentWebSocketServer._handle_command_recap
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_recap`

## Actual Role

Handles `command.recap` by defaulting session/channel, resolving mode, sub-mode, and project scope, obtaining the scoped agent, delegating to `agent.generate_recap(session_id=...)`, and sending one response. Returned `ok`, `no_turn`, and `failed` statuses are application outcomes passed through with transport `ok=true`; raised handler/dependency exceptions become transport `ok=false/status=failed`.

## Key Signals

- Input: `session_id`, optional `params.mode`, channel id, and project directory routing.
- Output: One WebSocket response carrying the adapter recap payload unchanged; application statuses use `ok=true`, while raised exceptions use `ok=false/status=failed` with a raw error string.
- Main side effects: May create the scoped agent, reads in-memory or persisted history, can invoke a model, and sends a WebSocket frame; recap does not append to conversation history.
- Main risk: Status routing and error behavior are not directly tested, and normal application outcomes are not logged at this boundary.
- Related tests: Prompt and shared model-call helper tests exist; direct handler, dispatch, and adapter `ok/no_turn/failed` status-path tests are missing. No tests were run during this re-audit.

## Detail Index

- Detail docs pending.
