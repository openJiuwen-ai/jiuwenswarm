---
symbol: AgentWebSocketServer._handle_agent_reload_config
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_agent_reload_config`

## Actual Role

Parses a config snapshot, environment overrides, optional channel/session target, and reload scope list; routes selected domains to AgentManager and/or ProactiveEngine, then emits a single success/failure response. Omitted or invalidly shaped scopes select all domains, AgentManager applies environment overrides process-wide even for targeted reloads, and proactive state is rebuilt from locally resolved configuration rather than the supplied snapshot.

## Key Signals

- Inputs: Optional config/env snapshots, target channel/session, and reload scope list; scope shape and vocabulary are not validated.
- Routing: Empty scopes mean AgentManager plus proactive; known intersections select those domains, while unsupported non-empty scopes can select neither.
- Side effects: Process-global environment mutation, targeted or global agent/team reload, proactive configuration/model rebuild, logging, and one serialized WebSocket send.
- Failure model: Raised manager/config errors return `ok=false`; swallowed team/proactive failures and selected no-ops return `ok=true, reloaded=true` without component detail.
- Tests/flow: Three direct handler tests cover positive routing, with deeper manager targeting/dedupe/retry tests; malformed input, global-env scope, snapshot consistency, and degraded outcomes remain uncovered. No tests were run during this re-audit.

## Detail Index

- Detail docs pending.
