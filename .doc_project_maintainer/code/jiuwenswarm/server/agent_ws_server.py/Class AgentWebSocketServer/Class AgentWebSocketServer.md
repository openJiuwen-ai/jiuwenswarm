---
symbol: AgentWebSocketServer
kind: class
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
signature: "class AgentWebSocketServer"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: overloaded
  length: excessive
  complexity: high
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: clear
  performance_risk: medium
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: responsibility_focus
    severity: medium
    status: open
    summary: "Class owns many unrelated runtime responsibilities."
    evidence: "One class handles WebSocket lifecycle, E2A dispatch, sessions, commands, MCP, sandbox, agents, extensions, scheduler, harness packages, and server push."
    suggested_action: "Continue documenting by handler family and consider extraction only with tests."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer`

## Actual Role

Singleton runtime server that accepts Gateway WebSocket connections, dispatches E2A or legacy AgentServer requests, manages per-session stream cancellation state, exposes many local command/session handlers, and sends server-originated push events back to Gateway.

## Key Signals

- Input: Gateway WebSocket connections and JSON request frames.
- Output: E2A response/chunk frames and server-push frames.
- Main side effects: agent runtime execution, session state, config and sandbox changes, scheduler service, push callbacks.
- Main risk: central mutable state and broad handler surface.
- Related tests: direct AgentServer mode, ACP, command, connection-close, history, team, workflow, and gateway-push tests.

## Detail Index

- Method detail docs pending beyond selected entry cards.
