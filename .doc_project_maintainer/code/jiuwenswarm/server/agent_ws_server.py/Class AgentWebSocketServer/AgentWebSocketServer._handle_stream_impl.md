---
symbol: AgentWebSocketServer._handle_stream_impl
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_stream_impl(self, ws, request, send_lock) -> None"
health:
  overall: unknown
  name_behavior_match: unknown
  responsibility_focus: unknown
  length: unknown
  complexity: unknown
  implementation_soundness: unknown
  boundary_safety: unknown
  input_contract: unknown
  output_contract: unknown
  side_effects: unknown
  error_handling: unknown
  state_mutation: unknown
  dependency_coupling: unknown
  test_coverage: unknown
  observability: unknown
  performance_risk: unknown
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  audited_symbol_hash: null
  confidence: unknown
  expired_reason: null
issues: []
confidence: confirmed
details: {}
---

# AgentWebSocketServer._handle_stream_impl

## Actual Role

Contains the existing stream resolution, agent invocation, heartbeat, bounded sending, task registration, and cleanup behavior after `_handle_stream` has established foreground scheduling priority.

## Key Signals

- Input: decoded request, WebSocket, and connection send lock.
- Output: none after stream completion or normalized terminal output.
- Main side effects: invokes the agent, emits stream frames, and mutates per-session task tracking.
- Main risk: inherited stream lifecycle risks require a fresh independent audit after the method split.
- Related tests: `test_agentserver_modes.py`, `test_ws_send.py`.

## Detail Index

- Detail docs pending.
