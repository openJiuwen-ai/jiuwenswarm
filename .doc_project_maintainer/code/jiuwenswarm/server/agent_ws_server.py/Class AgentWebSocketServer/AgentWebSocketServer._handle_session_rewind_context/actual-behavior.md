---
symbol: AgentWebSocketServer._handle_session_rewind_context
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_session_rewind_context`

## Actual Role

Handles unary `session.rewind_context` by resolving `session_id` and integer `turn_index`, rejecting missing/invalid input or an unavailable rewind-capable agent, then truncating persisted history/metadata/diff state before rebuilding the deep agent's context from the truncated records and attempting checkpointer persistence. It encodes one response under `send_lock`; helper `ValueError` becomes `BAD_REQUEST`, while other failures are logged and returned without a stable code.

## Key Signals

- Input: Params may override envelope `session_id`; `turn_index` must be int-convertible. Channel selects the currently available rewind agent.
- Output: One E2A response; success merges truncation counts/content with boolean `rewind_context`, including false as `ok=true`.
- Main side effects: Truncates history, queues metadata count, best-effort truncates file ops, clears/recreates in-memory context, wipes/rebuilds session state, attempts checkpointer saves, and sends a WebSocket frame.
- Main risk: Unchecked session paths and false-success semantics can leave history and context/checkpointer state inconsistent.
- Related tests: `test_compact_partial.py` covers selected context reconstruction/helper cases. No handler validation, agent resolution, wire contract, path containment, or partial-persistence test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
