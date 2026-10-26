---
symbol: AgentWebSocketServer._set_ws_acp_client_capabilities
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_set_ws_acp_client_capabilities(self, ws: Any, capabilities: dict[str, Any] | None) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
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
  audited_symbol_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: medium
    status: open
    summary: "ACP capabilities are cached before ACP initialize is known to have succeeded."
    evidence: "_handle_initialize calls this setter at agent_ws_server.py:6225 before awaiting AgentManager.initialize at 6228-6231; its exception path sends an error without clearing the cache, while _handle_message reads the cached value at 1402-1409 for later non-initialize ACP requests."
    suggested_action: "Move the setter after successful initialize, or clear the per-WebSocket cache in the initialize exception path."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: low
    status: open
    summary: "Capability dicts are shallow-copied."
    evidence: "The setter uses dict(capabilities) at agent_ws_server.py:962, which copies only the outer mapping; the direct flow test passes nested values such as {'terminal': {'create': True}}."
    suggested_action: "Deep-copy or normalize the ACP capability schema if caller-side nested mutation is possible."
  - id: ISSUE-003
    dimension: test_coverage
    severity: low
    status: open
    summary: "Tests cover per-WebSocket selection but not cache clearing, non-dict clearing, or failed-initialize rollback."
    evidence: "test_handle_message_uses_ws_scoped_acp_client_capabilities verifies selection between two non-empty per-WebSocket maps, but no test directly exercises non-dict removal, _clear_ws_acp_client_capabilities, disconnect cleanup of this cache, or initialize-failure rollback."
    suggested_action: "Add focused tests for set/get/clear, non-dict input removal, connection-finally cleanup, and initialize failure rollback."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._set_ws_acp_client_capabilities`

## Actual Role

Stores a shallow copy of ACP `clientCapabilities` for one WebSocket identity, keyed by `id(ws)`. A non-dict value removes that WebSocket's entry so later ACP requests fall back to manager-level capabilities or empty metadata.

## Key Signals

- Input: WebSocket-like object and a capabilities dict or `None`; runtime non-dict values also take the removal branch.
- Output: None.
- Main side effects: Mutates `self._acp_client_capabilities_by_ws`.
- Main risk: Cache mutation happens before ACP initialize success is known, and nested capability values are only shallow-copied.
- Related tests: `tests/unit_tests/agentserver/test_agentserver_acp.py::test_handle_message_uses_ws_scoped_acp_client_capabilities`; cleanup and failure rollback tests are pending.

## Detail Index

- Detail docs pending.
