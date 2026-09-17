---
symbol: AgentWebSocketServer._handle_extensions_delete
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_extensions_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: persistent
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:36Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:d44e996877d0e8847ce4be01ad6bb6ed1974d36aa8bd3889b2c8b173042e34c2
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Deleting an enabled extension does not unregister its active Rail."
    evidence: "See AgentWebSocketServer._handle_extensions_delete/risks.md#issue-001 for full evidence."
    suggested_action: "Make deletion an async lifecycle operation that unregisters the exact live."
  - id: ISSUE-002
    dimension: state_mutation
    severity: high
    status: open
    summary: "Runtime state, folder deletion, registry mutation, and config persistence are not."
    evidence: "See AgentWebSocketServer._handle_extensions_delete/risks.md#issue-002 for full evidence."
    suggested_action: "Serialize extension lifecycle operations and use a recoverable."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: high
    status: open
    summary: "A registry key can escape the extensions directory and select another filesystem target."
    evidence: "See AgentWebSocketServer._handle_extensions_delete/risks.md#issue-003 for full evidence."
    suggested_action: "Validate loaded and requested names against the import identifier contract."
  - id: ISSUE-004
    dimension: boundary_safety
    severity: high
    status: open
    summary: "The destructive RPC has no server-side authorization or revision boundary."
    evidence: "See AgentWebSocketServer._handle_extensions_delete/risks.md#issue-004 for full evidence."
    suggested_action: "Require an authorized administrative principal plus a server-issued."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "The extension-delete lifecycle has no regression coverage."
    evidence: "See AgentWebSocketServer._handle_extensions_delete/risks.md#issue-005 for full evidence."
    suggested_action: "Add handler and manager lifecycle tests for unregister, traversal containment."
  - id: ISSUE-006
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Recursive filesystem deletion runs synchronously on the shared event loop."
    evidence: "See AgentWebSocketServer._handle_extensions_delete/risks.md#issue-006 for full evidence."
    suggested_action: "Move bounded filesystem work to a worker thread or asynchronous job, report."
---

# AgentWebSocketServer._handle_extensions_delete

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_extensions_delete/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_extensions_delete/risks.md)
