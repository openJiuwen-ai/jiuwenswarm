---
symbol: AgentWebSocketServer._handle_session_switch
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_session_switch(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: medium
  test_coverage: partial
  observability: partial
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:06Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:51ddc58507cc99b8b70e7c1e68741232ca0a88de5e32dfd58b2a3445b146c9da
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: input_contract
    severity: medium
    status: open
    summary: "The handler trusts client-supplied team mode and target identity."
    evidence: "Lines 2403-2429 accept params.session_id/request.session_id plus caller-declared team mode without. See AgentWebSocketServer._handle_session_switch/risks.md#issue-001."
    suggested_action: "Verify that the target exists and is a team session before invoking distributed cleanup."
  - id: ISSUE-002
    dimension: error_handling
    severity: low
    status: open
    summary: "Delegated switch failures fall through to the outer generic error path."
    evidence: "The await at lines 2427-2432 has no local exception mapping; _handle_message can only return the generic. See AgentWebSocketServer._handle_session_switch/risks.md#issue-002."
    suggested_action: "Catch delegated failures and return a switch-specific error code."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Validation and failure branches lack direct tests."
    evidence: "test_agentserver_acp.py covers team success and non-team rejection; TeamManager tests cover distributed. See AgentWebSocketServer._handle_session_switch/risks.md#issue-003."
    suggested_action: "Test those branches and metadata passthrough."
  - id: ISSUE-004
    dimension: output_contract
    severity: medium
    status: open
    summary: "The success response overstates the performed operation."
    evidence: "Lines 2433-2441 always return mode='team' and switched=true. prepare_session_switch is a local-runtime. See AgentWebSocketServer._handle_session_switch/risks.md#issue-004."
    suggested_action: "Report a preparation result with the requested canonical mode, or activate/verify the target before claiming."
---

# AgentWebSocketServer._handle_session_switch

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_session_switch/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_session_switch/risks.md)
