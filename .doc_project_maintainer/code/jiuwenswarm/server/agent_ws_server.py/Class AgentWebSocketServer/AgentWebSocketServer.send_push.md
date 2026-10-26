---
symbol: AgentWebSocketServer.send_push
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "send_push(self, msg) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: implicit
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: medium
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:38Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:dc4eadfc66c94484cf494d5422f8a62759976eecbd66b4760a9a1fc906af0d84
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Global connection replacement can misroute or disable pushes."
    evidence: "See AgentWebSocketServer.send_push/risks.md#issue-001 for full evidence."
    suggested_action: "Publish and snapshot an immutable connection object containing socket, lock."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "A push can violate the Gateway's mandatory first-frame ack contract."
    evidence: "See AgentWebSocketServer.send_push/risks.md#issue-002 for full evidence."
    suggested_action: "Serialize and successfully send ack before publishing a ready connection."
  - id: ISSUE-003
    dimension: output_contract
    severity: medium
    status: open
    summary: "Delivery failure is indistinguishable from success to callers."
    evidence: "See AgentWebSocketServer.send_push/risks.md#issue-003 for full evidence."
    suggested_action: "Return or raise a structured delivery result (sent, degraded, disconnected."
  - id: ISSUE-004
    dimension: performance_risk
    severity: medium
    status: open
    summary: "An unbounded send holds the lock shared with request responses."
    evidence: "See AgentWebSocketServer.send_push/risks.md#issue-004 for full evidence."
    suggested_action: "Bound send latency, close or quarantine a stalled connection generation, and."
  - id: ISSUE-005
    dimension: test_coverage
    severity: medium
    status: open
    summary: "The transport edge and wire branches lack direct tests."
    evidence: "See AgentWebSocketServer.send_push/risks.md#issue-005 for full evidence."
    suggested_action: "Add direct connection-generation, ack-race/failure, backpressure/result tests."
  - id: ISSUE-006
    dimension: input_contract
    severity: medium
    status: open
    summary: "The implicit push schema permits wire-success followed by downstream parse or routing."
    evidence: "See AgentWebSocketServer.send_push/risks.md#issue-006 for full evidence."
    suggested_action: "Define typed chunk and structured-push variants, validate required."
---

# AgentWebSocketServer.send_push

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer.send_push/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer.send_push/risks.md)
