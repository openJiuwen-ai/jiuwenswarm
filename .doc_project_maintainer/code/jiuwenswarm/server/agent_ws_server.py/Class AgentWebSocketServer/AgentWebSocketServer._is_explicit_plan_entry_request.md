---
symbol: AgentWebSocketServer._is_explicit_plan_entry_request
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_is_explicit_plan_entry_request(request: AgentRequest) -> bool"
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
  dependency_coupling: medium
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
    dimension: test_coverage
    severity: low
    status: open
    summary: "No direct unit test covers the predicate boundary cases."
    evidence: "No test invokes this predicate directly. test_ensure_code_mode_state_allows_explicit_plan_reentry_after_exit covers only the true path indirectly with plan_entry_source='slash_command'."
    suggested_action: "Add a direct unit test matrix for non-dict params, missing key, wrong value, and slash_command."
  - id: ISSUE-002
    dimension: input_contract
    severity: low
    status: open
    summary: "The cross-layer plan_entry_source contract is implicit and string-literal based."
    evidence: "TUI app-state.ts serializes pendingPlanEntrySource into plan_entry_source, while this method checks the independent literal 'slash_command'; no shared schema/constant or frontend-to-backend contract test was found."
    suggested_action: "Document or centralize accepted plan_entry_source values, or add an integration test pinning TUI slash-command serialization to backend behavior."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._is_explicit_plan_entry_request`

## Actual Role

Side-effect-free static predicate that returns true only when `request.params` is a dict whose `plan_entry_source` equals `"slash_command"`. `_ensure_code_mode_state` uses it during normal-to-plan synchronization to clear the stale exit flag and permit an explicit TUI `/plan` re-entry.

## Key Signals

- Input: `AgentRequest`, with unchecked `params` from the WebSocket payload.
- Output: Boolean only.
- Main side effects: None.
- Main risk: The cross-layer marker is an implicit string literal shared with TUI request serialization.
- Related tests: `test_ensure_code_mode_state_allows_explicit_plan_reentry_after_exit` covers the true path indirectly; no direct helper or TUI serialization test covers false cases and the cross-layer marker.

## Detail Index

- Detail docs pending.
