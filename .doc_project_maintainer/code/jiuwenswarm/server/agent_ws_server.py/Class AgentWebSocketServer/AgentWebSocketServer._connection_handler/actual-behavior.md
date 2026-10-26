---
symbol: AgentWebSocketServer._connection_handler
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._connection_handler`

## Actual Role

Registers a Gateway WebSocket and its send lock as the process-wide server-push target, attempts `connection.ack`, then creates one concurrent `_handle_message` task per inbound frame. When iteration ends it clears socket/ACP/session state, cancels remaining connection tasks plus global agent and team work, stops the server scheduler, and gathers only tasks still retained in its local set.

## Key Signals

- Input: a WebSocket connection object.
- Output: no direct return value; sends ack, dispatches messages, and performs cleanup.
- Main side effects: replaces process-wide push state, sends ack, starts unbounded request tasks, and on disconnect cancels shared agent/team/scheduler work and clears ACP/session state.
- Main risk: overlapping connections can clear or cancel each other's state/work; completed task failures may be discarded, and frame intake has no concurrency bound.
- Related tests: `test_agent_ws_connection_close.py` exercises `_handle_message` close/send behavior, while CLI/system tests consume `connection.ack`; no test directly drives this method's ack ordering, ownership cleanup, scheduler shutdown, task-exception retrieval, or task saturation.

## Detail Index

- Detail docs pending.
