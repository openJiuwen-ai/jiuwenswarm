---
symbol: AgentWebSocketServer._handle_session_rewind_full
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_session_rewind_full(ws: Any, request: AgentRequest, send_lock: asyncio.Lock, restore_files: bool = False, compact: bool = False) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: overloaded
  length: excessive
  complexity: high
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: implicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:08Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:6a61a4ef47db6bc26e642d3e39841e22bd2779496bbf567e61c3171f7b9df2ca
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Compact-from rebuild omits the new summary records."
    evidence: "See AgentWebSocketServer._handle_session_rewind_full/risks.md#issue-001 for full evidence."
    suggested_action: "Append compact records before rebuilding context."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "Context convergence failure still returns success."
    evidence: "See AgentWebSocketServer._handle_session_rewind_full/risks.md#issue-002 for full evidence."
    suggested_action: "Return failure or explicit partial status unless durability is confirmed."
  - id: ISSUE-003
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Cross-store rewind is non-transactional."
    evidence: "See AgentWebSocketServer._handle_session_rewind_full/risks.md#issue-003 for full evidence."
    suggested_action: "Add prevalidation and atomic commit/rollback."
  - id: ISSUE-004
    dimension: input_contract
    severity: high
    status: open
    summary: "Invalid compact inputs bypass the intended BAD_REQUEST contract."
    evidence: "See AgentWebSocketServer._handle_session_rewind_full/risks.md#issue-004 for full evidence."
    suggested_action: "Validate compact fields before mutation in the BAD_REQUEST path."
  - id: ISSUE-005
    dimension: boundary_safety
    severity: high
    status: open
    summary: "session_id is an unchecked path component."
    evidence: "See AgentWebSocketServer._handle_session_rewind_full/risks.md#issue-005 for full evidence."
    suggested_action: "Validate the ID and enforce containment under sessions root."
  - id: ISSUE-006
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct tests cover the three rewind modes."
    evidence: "See AgentWebSocketServer._handle_session_rewind_full/risks.md#issue-006 for full evidence."
    suggested_action: "Test all modes, invalid inputs, missing agents, and failures."
---

# AgentWebSocketServer._handle_session_rewind_full

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_session_rewind_full/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_session_rewind_full/risks.md)
