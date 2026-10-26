---
symbol: AgentWebSocketServer.host
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "host(self) -> str"
health:
  overall: healthy
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: safe
  input_contract: implicit
  output_contract: clear
  side_effects: none
  error_handling: clear
  state_mutation: none
  dependency_coupling: low
  test_coverage: missing
  observability: not_applicable
  performance_risk: low
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  confidence: confirmed
  expired_reason: null
issues: []
confidence: confirmed
details: {}
---

# `AgentWebSocketServer.host`

## Actual Role

Read-only property that returns the constructor-supplied WebSocket bind host stored in `self._host`. Repository search found no production consumer of this accessor; `start` binds with `_host` directly, so the property only exposes immutable configured state.

## Key Signals

- Input: Initialized `AgentWebSocketServer` instance.
- Output: Configured host string.
- Main side effects: None.
- Main risk: None material in the current call graph; a future consumer would rely on the constructor preserving the host value verbatim.
- Related tests: `tests/unit_tests/agentserver/test_agent_reload_scope.py` constructs the server with its default host but does not read `.host`; no direct accessor or custom-host assertion was found.

## Detail Index

- Detail docs pending.
