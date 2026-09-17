---
symbol: AgentWebSocketServer._handle_command_recap
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_recap(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: partial
  output_contract: partial
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: medium
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:41Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:849ae48b46b6abcf0204a8410c96a17772fb5c8b1df26c0af8ccaa1785dd8552
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct handler or adapter generate_recap status-path tests cover command.recap."
    evidence: "test_btw_command.py covers recap prompt construction and the shared _call_model_for_recap helper, but. See AgentWebSocketServer._handle_command_recap/risks.md#issue-001."
    suggested_action: "Add handler tests for success, no-turn, missing agent, exception, default session, auto_harness, and project_dir."
  - id: ISSUE-002
    dimension: observability
    severity: low
    status: open
    summary: "Success and adapter-level failed statuses are not logged at the handler boundary."
    evidence: "Lines 3690-3699 delegate and log only raised exceptions; normal ok, no_turn, and returned failed. See AgentWebSocketServer._handle_command_recap/risks.md#issue-002."
    suggested_action: "Log recap result status after generate_recap without recording summary content."
  - id: ISSUE-003
    dimension: error_handling
    severity: low
    status: open
    summary: "Raised business failures have no stable machine-readable error code."
    evidence: "Lines 3698-3707 collapse lookup, initialization, and unexpected recap exceptions into. See AgentWebSocketServer._handle_command_recap/risks.md#issue-003."
    suggested_action: "Map expected lookup and recap failures to stable error codes while preserving the documented application-level status."
---

# AgentWebSocketServer._handle_command_recap

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_recap/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_recap/risks.md)
