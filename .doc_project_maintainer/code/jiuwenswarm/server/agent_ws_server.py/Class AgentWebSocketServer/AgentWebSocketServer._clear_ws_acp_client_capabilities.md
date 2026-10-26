---
symbol: AgentWebSocketServer._clear_ws_acp_client_capabilities
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_clear_ws_acp_client_capabilities(self, ws) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: safe
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: clear
  state_mutation: shared
  dependency_coupling: low
  test_coverage: partial
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
issues:
  - id: ISSUE-001
    dimension: test_coverage
    severity: low
    status: open
    summary: "No direct test asserts WebSocket capability cache cleanup."
    evidence: "tests/unit_tests/agentserver/test_agentserver_acp.py::test_handle_message_uses_ws_scoped_acp_client_capabilities covers per-WebSocket storage and reads, while connection-close tests exercise request cleanup rather than _connection_handler; no test found calls this helper or asserts capability-cache removal after disconnect."
    suggested_action: "Add a narrow idempotency/isolation test that seeds two WebSocket entries, clears one twice, and asserts the other entry remains; separately assert _connection_handler cleanup on disconnect if that lifecycle is testable without a live server."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._clear_ws_acp_client_capabilities`

## Actual Role

Idempotently removes the cached ACP client-capability dict keyed by the supplied WebSocket object's identity from `_acp_client_capabilities_by_ws`. `_connection_handler` invokes it from `finally` before cancelling connection tasks, making it the disconnect cleanup counterpart to initialize-time storage and request-time metadata injection.

## Key Signals

- Input: WebSocket-like object accepted as `Any`.
- Output: None.
- Main side effects: Removes one key from `self._acp_client_capabilities_by_ws` when present.
- Main risk: A regression in the disconnect lifecycle could retain capability data under an identity key; direct removal and idempotency are not test-asserted.
- Related tests: `tests/unit_tests/agentserver/test_agentserver_acp.py::test_handle_message_uses_ws_scoped_acp_client_capabilities` covers storage/read isolation; direct clear and `_connection_handler` disconnect cleanup tests are pending.

## Detail Index

- Detail docs pending.
