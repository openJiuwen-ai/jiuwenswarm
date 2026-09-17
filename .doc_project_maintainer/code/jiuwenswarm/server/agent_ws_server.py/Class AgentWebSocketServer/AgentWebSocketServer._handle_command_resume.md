---
symbol: AgentWebSocketServer._handle_command_resume
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_resume(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: mismatch
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: low
  test_coverage: partial
  observability: partial
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:45Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:4022369dd672d7fb746ffe22051ca568829b8fd34d600f8ea486e0c40cd19ac3
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "The resume command reports success without resuming anything."
    evidence: "Current method has no session/history/agent dependency or state transition. Its normal branch always. See AgentWebSocketServer._handle_command_resume/risks.md#issue-001."
    suggested_action: "Implement the real session transition and history validation, or remove/deprecate command.resume and return an explicit."
  - id: ISSUE-002
    dimension: input_contract
    severity: high
    status: open
    summary: "Missing and invalid session selectors fabricate a successful mock session."
    evidence: "Missing, blank, or non-string params.query becomes session_id='sess_mock_resume'. A nonblank string is. See AgentWebSocketServer._handle_command_resume/risks.md#issue-002."
    suggested_action: "Require a normalized session_id, validate it through the canonical session service, and return structured."
  - id: ISSUE-003
    dimension: dependency_coupling
    severity: high
    status: open
    summary: "The routed public endpoint has drifted away from the real TUI resume flow."
    evidence: "COMMAND_RESUME remains in ReqMethod and AgentServer dispatch/forwarding, while the documented live. See AgentWebSocketServer._handle_command_resume/risks.md#issue-003."
    suggested_action: "Choose one canonical resume contract, migrate clients, then remove the dead mock route and forwarding entry."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "The only direct test codifies the mock response."
    evidence: "The only direct test, test_handle_command_resume_returns_mock_session, asserts resumed=true and the mock. See AgentWebSocketServer._handle_command_resume/risks.md#issue-004."
    suggested_action: "Replace the mock assertion with behavioral integration tests against the canonical session/history services and."
---

# AgentWebSocketServer._handle_command_resume

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_resume/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_resume/risks.md)
