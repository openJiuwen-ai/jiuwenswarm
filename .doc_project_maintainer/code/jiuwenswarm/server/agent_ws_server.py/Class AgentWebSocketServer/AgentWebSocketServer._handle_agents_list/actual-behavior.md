---
symbol: AgentWebSocketServer._handle_agents_list
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_agents_list`

## Actual Role

Handles unary `agents.list` by taking only optional `params.workspace_dir`, constructing `AgentConfigService` (server cwd fallback), synchronously merging builtin/local/user/project definitions plus best-effort enabled state, serializing every full dataclass including shadowed definitions, and sending one response under `send_lock`. Service/config errors may be softened below the handler, while thrown failures become an error payload.

## Key Signals

- Input: Optional unvalidated `params.workspace_dir`; normal TUI calls do not supply it.
- Output: All dataclass fields for every active and shadowed definition, or exception text without a stable code.
- Main side effects: Synchronously reads host directories, Markdown definitions, and global config on the event loop, then sends a WebSocket frame.
- Main risk: Wrong/arbitrary workspace selection and unnecessary prompt/path disclosure.
- Related tests: Service unit tests cover list merge/precedence/sorting, but no handler, workspace-boundary, disclosure, size, failure, or cross-layer test was found; tests were not run in this re-audit.

## Detail Index

- Detail docs pending.
