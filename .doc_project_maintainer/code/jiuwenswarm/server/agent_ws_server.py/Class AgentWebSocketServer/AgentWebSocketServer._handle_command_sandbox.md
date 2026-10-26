---
symbol: AgentWebSocketServer._handle_command_sandbox
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_sandbox(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:44Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:852271cd01a700fd3cea7b75285b6ffb1db38cba4543efd94a77383d5ba1d6c4
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: state_mutation
    severity: high
    status: open
    summary: "Sandbox writes can leave half-applied persistent state."
    evidence: "See AgentWebSocketServer._handle_command_sandbox/risks.md#issue-001 for full evidence."
    suggested_action: "Apply first then persist, or roll back/report degraded status when recreation."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "files.* remote sandbox refresh failures can be swallowed while responses show cached."
    evidence: "See AgentWebSocketServer._handle_command_sandbox/risks.md#issue-002 for full evidence."
    suggested_action: "Propagate files_changed remote recreate failures or include."
  - id: ISSUE-003
    dimension: error_handling
    severity: medium
    status: open
    summary: "enable/disable on a channel with no active agent can become an internal error or."
    evidence: "See AgentWebSocketServer._handle_command_sandbox/risks.md#issue-003 for full evidence."
    suggested_action: "Make no-active-agent a clear delayed-effect success or a structured bad-state."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No direct command.sandbox handler tests were found."
    evidence: "See AgentWebSocketServer._handle_command_sandbox/risks.md#issue-004 for full evidence."
    suggested_action: "Add handler tests for status, platform guard, unknown subcommands."
  - id: ISSUE-005
    dimension: state_mutation
    severity: high
    status: open
    summary: "Global sandbox policy is reconciled only against request-scoped runtime variants."
    evidence: "See AgentWebSocketServer._handle_command_sandbox/risks.md#issue-005 for full evidence."
    suggested_action: "Fan out reconciliation across all live variants, or version desired/applied."
  - id: ISSUE-006
    dimension: output_contract
    severity: high
    status: open
    summary: "Successful disable is not restart-stable for explicit internal startup."
    evidence: "See AgentWebSocketServer._handle_command_sandbox/risks.md#issue-006 for full evidence."
    suggested_action: "Define enabled as authoritative during bootstrap or return/document a."
  - id: ISSUE-007
    dimension: input_contract
    severity: medium
    status: open
    summary: "Malformed params can bypass the sandbox-specific error contract."
    evidence: "See AgentWebSocketServer._handle_command_sandbox/risks.md#issue-007 for full evidence."
    suggested_action: "Validate params as a mapping inside the protected block and return."
---

# AgentWebSocketServer._handle_command_sandbox

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_sandbox/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_sandbox/risks.md)
