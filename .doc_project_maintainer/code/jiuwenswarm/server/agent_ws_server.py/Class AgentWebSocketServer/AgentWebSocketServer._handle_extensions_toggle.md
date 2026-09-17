---
symbol: AgentWebSocketServer._handle_extensions_toggle
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_extensions_toggle(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:37Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:6a6e1747f08e35d5fe826fe791a77791924d1a2edd918427bc6b986dd628eac0
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: input_contract
    severity: high
    status: open
    summary: "A missing enabled field silently means disable."
    evidence: "See AgentWebSocketServer._handle_extensions_toggle/risks.md#issue-001 for full evidence."
    suggested_action: "Require the key explicitly and reject missing values before mutation."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Non-boolean enabled values can invert intended behavior."
    evidence: "See AgentWebSocketServer._handle_extensions_toggle/risks.md#issue-002 for full evidence."
    suggested_action: "Accept only a JSON boolean and reject all other types."
  - id: ISSUE-003
    dimension: state_mutation
    severity: high
    status: open
    summary: "Persistent config is changed before runtime application and is not rolled back."
    evidence: "See AgentWebSocketServer._handle_extensions_toggle/risks.md#issue-003 for full evidence."
    suggested_action: "Restore prior memory/file state when runtime update fails."
  - id: ISSUE-004
    dimension: dependency_coupling
    severity: high
    status: open
    summary: "Hot reload targets an unqualified single Agent and tracks registration globally."
    evidence: "See AgentWebSocketServer._handle_extensions_toggle/risks.md#issue-004 for full evidence."
    suggested_action: "Track per-Agent registrations and update every intended runtime."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "No toggle handler or Rail hot-reload tests were found."
    evidence: "See AgentWebSocketServer._handle_extensions_toggle/risks.md#issue-005 for full evidence."
    suggested_action: "Test the handler/manager with temp config, multiple Agents, failures, and."
  - id: ISSUE-006
    dimension: state_mutation
    severity: high
    status: open
    summary: "Concurrent toggles can finish with config and registration in opposite states."
    evidence: "See AgentWebSocketServer._handle_extensions_toggle/risks.md#issue-006 for full evidence."
    suggested_action: "Serialize per-extension transitions and verify/reconcile configured and."
---

# AgentWebSocketServer._handle_extensions_toggle

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_extensions_toggle/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_extensions_toggle/risks.md)
