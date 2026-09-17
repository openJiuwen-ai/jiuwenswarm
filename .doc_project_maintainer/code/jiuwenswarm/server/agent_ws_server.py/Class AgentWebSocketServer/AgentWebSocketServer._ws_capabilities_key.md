---
symbol: AgentWebSocketServer._ws_capabilities_key
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_ws_capabilities_key(ws) -> int"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: none
  error_handling: clear
  state_mutation: none
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
    dimension: boundary_safety
    severity: low
    status: open
    summary: "The integer key does not retain the WebSocket, so safety depends on lifecycle-paired cache cleanup."
    evidence: "Adjacent set/get helpers index _acp_client_capabilities_by_ws by id(ws); the normal runtime path clears that entry in _connection_handler.finally while the same ws object is still live. Calls outside that paired lifecycle could leave an integer key that Python may later reuse."
    suggested_action: "Keep all writes inside the connection lifecycle and add cleanup coverage; consider object-keyed weak storage if callers expand beyond that lifecycle."
  - id: ISSUE-002
    dimension: test_coverage
    severity: low
    status: open
    summary: "WebSocket isolation is covered, but key and disconnect-cleanup semantics lack direct assertions."
    evidence: "test_handle_message_uses_ws_scoped_acp_client_capabilities initializes two FakeWebSocket objects and verifies ws_b receives its own capabilities. No test directly calls _ws_capabilities_key or asserts that _connection_handler.finally removes the cached entry."
    suggested_action: "Add a focused set/get/clear test and a connection-finally assertion if this integer-key cache remains part of the lifecycle contract."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._ws_capabilities_key`

## Actual Role

Returns Python's object-identity integer for the supplied WebSocket. Adjacent helpers use that integer to store, retrieve, and clear per-connection ACP client capabilities; the key is stable while the WebSocket object is live, and the normal connection handler removes its cache entry in `finally`.

## Key Signals

- Input: WebSocket-like object accepted as `Any`.
- Output: Python object identity integer from `id(ws)`.
- Main side effects: None.
- Main risk: A write outside the paired connection lifecycle could leave a stale integer key that is vulnerable to later object-id reuse.
- Related tests: `tests/unit_tests/agentserver/test_agentserver_acp.py::test_handle_message_uses_ws_scoped_acp_client_capabilities` covers two-WebSocket capability isolation through the surrounding flow; direct key and disconnect-cleanup assertions are absent.

## Detail Index

- Detail docs pending.
