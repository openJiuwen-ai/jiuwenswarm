---
symbol: AgentWebSocketServer._handle_agent_reload_config
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_agent_reload_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
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
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:35Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:6bef3b5986e99e3124fa375619f9d493d6acac1d56fd5482d9bec9002d411fb5
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: input_contract
    severity: high
    status: open
    summary: "Malformed reload_scopes can unexpectedly trigger a global reload."
    evidence: "See AgentWebSocketServer._handle_agent_reload_config/risks.md#issue-001 for full evidence."
    suggested_action: "Validate the container and known string values, and distinguish an omitted."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "Unknown scopes are accepted as a successful no-op."
    evidence: "See AgentWebSocketServer._handle_agent_reload_config/risks.md#issue-002 for full evidence."
    suggested_action: "Define the accepted scope vocabulary, reject unsupported values, and return."
  - id: ISSUE-003
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Proactive reload ignores the request's authoritative config snapshot."
    evidence: "See AgentWebSocketServer._handle_agent_reload_config/risks.md#issue-003 for full evidence."
    suggested_action: "Validate one effective request snapshot and use it, including its model."
  - id: ISSUE-004
    dimension: error_handling
    severity: high
    status: open
    summary: "Partial reload failures can still produce reloaded=true."
    evidence: "See AgentWebSocketServer._handle_agent_reload_config/risks.md#issue-004 for full evidence."
    suggested_action: "Return structured component outcomes and mark any selected but unapplied domain."
  - id: ISSUE-005
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Valid scope routing is tested, but failure and malformed contracts are not."
    evidence: "See AgentWebSocketServer._handle_agent_reload_config/risks.md#issue-005 for full evidence."
    suggested_action: "Add malformed/unsupported scope, global-env boundary, partial-failure, and."
  - id: ISSUE-006
    dimension: boundary_safety
    severity: high
    status: open
    summary: "A targeted reload still applies request environment overrides process-wide."
    evidence: "See AgentWebSocketServer._handle_agent_reload_config/risks.md#issue-006 for full evidence."
    suggested_action: "Separate process-global environment updates from channel/session reload."
---

# AgentWebSocketServer._handle_agent_reload_config

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_agent_reload_config/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_agent_reload_config/risks.md)
