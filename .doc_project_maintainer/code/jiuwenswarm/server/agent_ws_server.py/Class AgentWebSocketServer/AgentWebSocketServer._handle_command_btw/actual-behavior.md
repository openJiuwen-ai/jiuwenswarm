---
symbol: AgentWebSocketServer._handle_command_btw
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_command_btw`

## Actual Role

Adapts non-streaming `command.btw` into a read-only, single-model side query. It defaults session/channel/mode, logs the first 100 question characters, resolves a project- and submode-scoped agent (mapping auto_harness to agent), and delegates recent-context/system-prompt assembly plus the no-tool-execution model call to `generate_btw_answer`. Empty questions return application-level `status=failed` with `ok=true`; adapter dictionaries are passed through without schema validation, while lookup/model/type exceptions become `ok=false`.

## Key Signals

- Input: `params.question`, optional `params.mode`, request session/channel, and resolved project directory; params/question types are implicit, and blank identity falls back to `default`/`agent.plan`.
- Output: One locked response containing the adapter payload or `status=failed`. Empty validation and adapter-declared failures can use transport `ok:true`, while raised failures use `ok:false`.
- Main side effects: May create/select shared agent runtime state, performs a model call that consumes tokens, logs question/result/failure information, and sends one WebSocket frame; the adapter promises not to modify conversation history.
- Main risk: Deadline mismatch can orphan expensive work, while default identity and unvalidated adapter payloads can query or render the wrong semantic context.
- Related tests: Direct handler tests cover core validation/result/error/default branches and system tests cover forwarding shape. Project identity, malformed schema, unknown status, privacy logging, and deadline/cancellation edges remain. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
