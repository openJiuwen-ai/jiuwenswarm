---
symbol: AgentWebSocketServer._handle_agents_get
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_agents_get`

## Actual Role

Handles unary `agents.get` by taking unvalidated `name` and optional `workspace_dir`, constructing `AgentConfigService` with server-cwd fallback, synchronously loading/merging all builtin/local/user/project definitions, and selecting the active exact-name match. It sends the complete dataclass under `send_lock`; missing agents and caught exceptions become `ok=false` error text without stable codes.

## Key Signals

- Caller: `_handle_message` dispatches `ReqMethod.AGENTS_GET`; Web and TUI expose the forwarded method.
- Input: Optional `name` and `workspace_dir`; neither is normalized, and workspace identity is not authorized here.
- Output: Every `AgentDefinition` field, including full prompt and filesystem path.
- Side effects: Synchronous filesystem/config reads; the service mutates reused builtin definition objects.
- Related tests: Service tests cover basic get/list/precedence behavior, but no handler, invalid-name, workspace-boundary, disclosure, stale-state, failure, or wire-contract test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
