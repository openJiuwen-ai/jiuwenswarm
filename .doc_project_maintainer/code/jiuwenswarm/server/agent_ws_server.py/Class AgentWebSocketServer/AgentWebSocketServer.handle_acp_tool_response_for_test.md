---
symbol: AgentWebSocketServer.handle_acp_tool_response_for_test
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "handle_acp_tool_response_for_test(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: weak
  output_contract: partial
  side_effects: hidden
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: partial
  observability: partial
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
    dimension: boundary_safety
    severity: high
    status: open
    summary: "The public test alias exposes the underlying handler's unscoped global completion behavior without a guard."
    evidence: "The sole statement delegates arbitrary ws/request/lock values to _handle_acp_tool_response. That callee trusts caller-supplied jsonrpc_id to resolve a process-global pending future, without matching connection, channel, session, or response-body id, and accepts malformed response dictionaries. A for_test suffix is not an access boundary."
    suggested_action: "Fix ownership and schema validation in _handle_acp_tool_response; keep any test access inside the test harness rather than treating the alias as a guard."
  - id: ISSUE-002
    dimension: responsibility_focus
    severity: medium
    status: open
    summary: "A test-only seam is shipped as a public production method."
    evidence: "Repository search finds exactly two callers, both unit tests. The same test module defines initialize, session, message, and deletion wrappers on AgentWebSocketServerHarness, but this one exceptional wrapper resides on AgentWebSocketServer and therefore expands the production API and maintenance queue."
    suggested_action: "Move the delegator to AgentWebSocketServerHarness or test the private handler directly; production code should retain only the runtime dispatch path."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Tests through this shortcut do not prove the real ACP routing boundary."
    evidence: "The two tests cover accepted and unknown-id outcomes by invoking this alias directly. No test sends ACP_TOOL_RESPONSE through _handle_message/_handle_unary, so dispatch, hook behavior, connection metadata, and transport error propagation can regress while these tests stay green."
    suggested_action: "Add a wire-level dispatcher test and retain focused handler tests for ownership, malformed payloads, duplicate/late races, and send failure."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer.handle_acp_tool_response_for_test`

## Actual Role

Provides a production-class alias used only by two unit tests to call `_handle_acp_tool_response` with identical arguments. It adds no validation, error handling, or behavior.

## Key Signals

- Runtime callers: None; `_handle_unary` calls the private handler directly.
- Test callers: Valid-completion and unknown/late-id cases in `test_agentserver_acp.py`.
- Side effects: Inherited from the callee, including resolving global pending state and sending a response.
- Main risk: Tests bypass routing while the unnecessary public alias inherits the callee's trust-boundary flaws.
- Related flow: No dedicated ACP tool-response flow doc found; flow coverage remains pending.

## Detail Index

- Detail docs pending.
