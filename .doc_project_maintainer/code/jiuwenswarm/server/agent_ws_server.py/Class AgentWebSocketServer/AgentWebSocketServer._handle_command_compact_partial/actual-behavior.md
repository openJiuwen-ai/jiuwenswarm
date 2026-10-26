---
symbol: AgentWebSocketServer._handle_command_compact_partial
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_compact_partial`

## Actual Role

Handles unary `command.compact_partial` by defaulting session/direction, int-coercing the turn index, resolving mode/sub-mode/project identity to an agent, and delegating partial-summary generation. It returns any adapter result as top-level success, while caught failures become `ok=false/status=failed`; cancellation and KeyboardInterrupt propagate, and the response is sent under `send_lock`.

## Key Signals

- Input: `params.turn_index`, `params.direction`, optional `params.mode`, session id, channel id, and request project directory.
- Output: One response containing the adapter result or caught-exception failure; adapter `status=failed/no_turn` still has top-level `ok=true`.
- Main side effects: Calls the scoped runtime adapter and sends a websocket frame.
- Main risk: Invalid indexes and unchecked legacy-only paths can summarize the wrong turn, miss JSONL history, or escape the sessions root; normal adapter failures look successful.
- Related tests: Service/prompt tests exist, but no direct handler validation, routing, result-status, BaseException, or wire-send test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
