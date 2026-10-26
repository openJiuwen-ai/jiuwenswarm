---
symbol: AgentWebSocketServer._handle_agents_delete
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_agents_delete`

## Actual Role

Coordinates three different deletion meanings in one RPC: it asks a workspace-aware AgentConfigService to resolve and unlink the highest-priority custom agent definition by name, independently removes that name from global subagent config, then reloads live agents from the new config. Definition absence does not stop config cleanup/reload, and post-unlink failures are softened into nested `applied` status rather than rolling back. All persistent file/config work is synchronous on the async request path.

## Key Signals

- Input: Unnormalized `params.name` and optional caller-selected `workspace_dir`; params is assumed mapping-like, with no source, authorization, or revision precondition.
- Output: One locked response. Outer service failures use top-level `ok:false`; a normal not-found delete still uses top-level `ok:true` with payload `ok:false`, while config/reload failures use `applied:false` and a raw `reload_error`.
- Main side effects: Synchronously unlinks an agent Markdown definition, rewrites global subagent config, synchronously reloads config, awaits global runtime reload, logs partial failure, and sends one WebSocket frame.
- Main risk: Non-transactional, cross-scope deletion can remove the wrong definition or leave file/config/runtime state divergent while exposing ambiguous nested success.
- Related evidence: Helper-level tests exist, but no handler/integration coverage or dedicated agent-configuration flow was found in the existing audit evidence. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
