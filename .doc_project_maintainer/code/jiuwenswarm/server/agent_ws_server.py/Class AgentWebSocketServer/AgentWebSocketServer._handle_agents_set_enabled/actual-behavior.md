---
symbol: AgentWebSocketServer._handle_agents_set_enabled
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_agents_set_enabled`

## Actual Role

Handles both unary `agents.enable` and `agents.disable` using the dispatcher-supplied boolean. It normalizes a required name, uses optional `workspace_dir` to synchronously resolve a non-builtin definition, writes the requested state into shared config, and globally reloads cached agents. Reload failure is softened to `ok=true, applied=false` without rollback; other failures return error text, and one response is sent under `send_lock`.

## Key Signals

- Input: Request `name`/optional `workspace_dir`, plus dispatcher-selected boolean state.
- Output: Name, requested durable state, application flag, and optional reload error; top-level success does not guarantee runtime convergence.
- Main side effects: Synchronous workspace/config reads, shared config write, broad runtime reload, logging, and WebSocket send.
- Main risk: Durable and runtime state can diverge while the user sees an unconditional success message.
- Related tests: AgentConfigService/config-helper tests are adjacent, but no handler validation/scope/failure/concurrency/wire or UI partial-success test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
