---
symbol: AgentWebSocketServer._handle_message
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_message(self, ws: Any, raw: str | bytes, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: overloaded
  length: excessive
  complexity: high
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: weak
  output_contract: implicit
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: clear
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:37:46Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:351c34df48a6e7b4775d3cbcb0fee48b8a6de29c20402f1a4ed7f977d791baa4
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: complexity
    severity: medium
    status: open
    summary: "Large method is the central routing table for many runtime RPC families."
    evidence: "Current lines 1341-1717 span 377 lines with 76 if nodes, 75 awaits, 68 req_method predicates, and 70. See AgentWebSocketServer._handle_message/risks.md#issue-001."
    suggested_action: "Separate decode/normalize, metadata enrichment, dispatch-table routing, and cancel orchestration behind tested helpers."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Malformed non-JSON-error payloads can escape before normalized error handling."
    evidence: "Lines 1365-1399 convert E2A/fallback payloads before the guarded dispatch block at 1401. JSON. See AgentWebSocketServer._handle_message/risks.md#issue-002."
    suggested_action: "Validate decoded JSON is a dict and wrap E2A/legacy conversion in the same error-normalization path."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Direct router coverage misses malformed converted payloads and many dispatch branches."
    evidence: "Direct tests cover open/closed invalid-JSON sends, closed unary handling, WebSocket-scoped ACP metadata. See AgentWebSocketServer._handle_message/risks.md#issue-003."
    suggested_action: "Add direct router tests for malformed E2A/legacy inputs, capability precedence, cancellation timeout, and selected."
  - id: ISSUE-004
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "An ACP request can override connection-scoped client capabilities."
    evidence: "Lines 1402-1409 copy request metadata and use metadata.setdefault('acp_client_capabilities', ws_caps or. See AgentWebSocketServer._handle_message/risks.md#issue-004."
    suggested_action: "Treat the WebSocket-scoped INITIALIZE record as authoritative and assign the capability field rather than preserving an."
  - id: ISSUE-005
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Cancel handling can wait indefinitely for a cancellation-resistant stream task."
    evidence: "Lines 1625-1659 cancel the per-session stream task and then await it without a timeout before disconnect. See AgentWebSocketServer._handle_message/risks.md#issue-005."
    suggested_action: "Bound the cleanup wait, log timeout diagnostics, and continue disconnect-scoped runtime cleanup even when the producer."
---

# AgentWebSocketServer._handle_message

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_message/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_message/risks.md)
