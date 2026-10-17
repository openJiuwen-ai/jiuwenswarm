---
symbol: JiuWenSwarmDeepAdapter.prepare_session
kind: method
source: jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
class: JiuWenSwarmDeepAdapter
audit:
  status: unaudited
---

# JiuWenSwarmDeepAdapter.prepare_session

## Actual Role

Creates or reuses the session-scoped adapter, completes `create_instance` and `start_interaction`, then delegates stable workspace, rails, tools, and prompt configuration through the child's public `configure_session_runtime` boundary. Blocking Git-ignore and runtime-state probes are dispatched to worker threads; the method still does not attach output or send input.

Prepared sessions mount the inert Rail but leave eternal conversation disabled. Only the real request-bound runtime path may enable it from the explicit frontend parameter.
