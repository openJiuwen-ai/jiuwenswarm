---
symbol: AgentWebSocketServer._handle_agents_tools_list
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_agents_tools_list`

## Actual Role

Handles unary `agents.tools_list` by accepting an otherwise unused `workspace_dir`, constructing `AgentConfigService`, and calling its static catalog builder. The builder imports private OpenJiuwen CLI display metadata plus local groups/descriptions/denylist, deduplicates by display name, infers missing internal names, and returns that static catalog; the handler does not inspect effective runtime abilities and sends one response under `send_lock`.

## Key Signals

- Input: Request metadata and an optional `workspace_dir` that does not affect the static result.
- Output: Static display/internal names, descriptions, groups, and subagent denylist, or exception text without a stable code.
- Main side effects: Performs synchronous UI/runtime module imports and catalog construction, logs failures, and sends a WebSocket frame.
- Main risk: The UI can present and persist capabilities that diverge from the runtime ToolCards.
- Related tests: Constant tests are adjacent, but no service/handler, effective-runtime comparison, import/map-drift, workspace-semantics, wire, or UI round-trip test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
