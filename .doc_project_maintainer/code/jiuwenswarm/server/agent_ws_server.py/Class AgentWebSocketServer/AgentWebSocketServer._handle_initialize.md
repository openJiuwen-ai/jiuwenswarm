---
symbol: AgentWebSocketServer._handle_initialize
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_initialize(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:39Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:8b1917c1574308d0a86942f3e8bbdba0bf07852375e6dfddb755f926d2e06870
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: high
    status: open
    summary: "ACP initialize replaces shared channel runtime without serialization."
    evidence: "See AgentWebSocketServer._handle_initialize/risks.md#issue-001 for full evidence."
    suggested_action: "Make initialization connection-scoped and idempotent, or serialize an atomic."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "The response does not represent client-visible ACP initialization reliably."
    evidence: "See AgentWebSocketServer._handle_initialize/risks.md#issue-002 for full evidence."
    suggested_action: "Use one handshake owner, await real initialization, propagate failure, and."
  - id: ISSUE-003
    dimension: error_handling
    severity: medium
    status: open
    summary: "Failure leaves capability state partially committed."
    evidence: "See AgentWebSocketServer._handle_initialize/risks.md#issue-003 for full evidence."
    suggested_action: "Validate first; commit caches only after successful creation or roll them back."
  - id: ISSUE-004
    dimension: input_contract
    severity: medium
    status: open
    summary: "Handshake fields lack schema and version negotiation."
    evidence: "See AgentWebSocketServer._handle_initialize/risks.md#issue-004 for full evidence."
    suggested_action: "Validate bounded capabilities and negotiate one canonical version."
  - id: ISSUE-005
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Tests omit real lifecycle safety."
    evidence: "See AgentWebSocketServer._handle_initialize/risks.md#issue-005 for full evidence."
    suggested_action: "Add real-manager lifecycle, concurrency, rollback, invalid-input, and."
  - id: ISSUE-006
    dimension: error_handling
    severity: high
    status: open
    summary: "Response delivery failure is treated as initialization failure after shared state is."
    evidence: "See AgentWebSocketServer._handle_initialize/risks.md#issue-006 for full evidence."
    suggested_action: "Separate committed initialization from transport delivery, make initialize."
---

# AgentWebSocketServer._handle_initialize

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_initialize/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_initialize/risks.md)
