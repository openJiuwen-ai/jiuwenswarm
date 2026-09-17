---
symbol: AgentWebSocketServer._handle_schedule_request
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_schedule_request`

## Actual Role

Acts as the single, highly coupled router for nine scheduling actions and four issue-watcher actions. On the first request—including reads—it publishes and starts one server-wide AutoHarnessService. Selected mutating actions resolve or auto-create a request-scoped agent and overwrite the service's single mutable agent slot; create/run also resolve a model through a global name-only cache. The method then mixes synchronous config operations, persistent task/log mutations, autonomous execution controls, issue state operations, and read pagination behind one weakly typed params map, returning every normal service payload as transport success.

## Key Signals

- Input/routing: `_handle_message` passes 13 hard-coded action strings; each action reads a different unchecked subset of request params, mode/project/channel context, and model cache state.
- Output: One action payload or raw exception failure. Normal payload.error results, including unknown/not-found cases, remain top-level ok:true; logs can be unbounded.
- Main side effects: Lazily starts autonomous polling, may create/select agents, overwrites shared service agent identity, mutates scheduler config/tasks/runs/issue state, resolves cached models, and sends one WebSocket frame.
- Main risk: Persisted work lacks immutable execution ownership and model revision, so later/concurrent requests or restart can run autonomous work through the wrong agent, workspace, channel, or stale provider while clients receive misleading success.
- Related evidence: `agentserver-schedule-auto-harness` confirms source-of-truth files, restart reconstruction, mutable identity, model-cache, response, and pagination gaps. No direct handler RPC tests were found; tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
