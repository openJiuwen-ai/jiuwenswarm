---
symbol: AgentWebSocketServer._get_ws_acp_client_capabilities
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_get_ws_acp_client_capabilities(self, ws) -> dict[str, Any]"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: weak
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
    dimension: output_contract
    severity: medium
    status: open
    summary: "Getter cannot distinguish missing cache from explicitly empty capabilities."
    evidence: "The setter stores any dict, including an empty dict, but this getter returns {} for both an empty stored value and a miss. _handle_message then uses ws_caps or AgentManager.get_client_capabilities('acp'); that manager cache is channel-scoped and overwritten by each ACP initialize, so an explicitly capability-free connection can inherit another connection's latest non-empty map."
    suggested_action: "Return None for no entry or add a presence-aware helper, then fall back only when no WebSocket-scoped value exists."
  - id: ISSUE-002
    dimension: test_coverage
    severity: low
    status: open
    summary: "Existing coverage validates non-empty per-WebSocket behavior but not getter edge cases."
    evidence: "test_handle_message_uses_ws_scoped_acp_client_capabilities proves a non-empty ws_b map wins over the manager fallback. No direct getter tests were found for no entry, shallow-copy isolation, or an explicit empty map after another connection initializes non-empty capabilities."
    suggested_action: "Add focused helper tests for no entry and copy isolation, plus a two-connection explicit-empty regression test."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._get_ws_acp_client_capabilities`

## Actual Role

Looks up ACP client capabilities by the WebSocket's `id(ws)` key and returns a shallow top-level copy when the cached value is a dict; otherwise it returns a new empty dict. `_handle_message` uses this result to prefer connection-scoped capability metadata over AgentManager's channel-level fallback for later ACP requests.

## Key Signals

- Input: WebSocket-like object accepted as `Any`.
- Output: A copied capabilities dict, or `{}` when no dict is cached.
- Main side effects: None.
- Main risk: `{}` means both no cache entry and explicit empty capabilities; after another connection overwrites the channel-level manager cache, the caller can attach that other connection's capabilities.
- Related tests: `tests/unit_tests/agentserver/test_agentserver_acp.py::test_handle_message_uses_ws_scoped_acp_client_capabilities` covers the non-empty connection-scoped path; direct getter, copy-isolation, and explicit-empty regression tests are absent.

## Detail Index

- Detail docs pending.
