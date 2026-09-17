---
symbol: AgentWebSocketServer._handle_extensions_list
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_extensions_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: partial
  input_contract: clear
  output_contract: weak
  side_effects: hidden
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:39:35Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:561818c83016193184652dbb6838286c6a4646b9341c1b997ec85e0737ca00d5
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: output_contract
    severity: high
    status: open
    summary: "The enabled field is configuration, not effective runtime state."
    evidence: "Current RailManager.list_extensions returns only cached RailExtension.to_dict records, whose enabled. See AgentWebSocketServer._handle_extensions_list/risks.md#issue-001."
    suggested_action: "Return configured and applied states separately with the last load error."
  - id: ISSUE-002
    dimension: error_handling
    severity: high
    status: open
    summary: "Unreadable or malformed configuration becomes a successful empty list."
    evidence: "On singleton initialization, RailManager._load_config catches any file/JSON/from_dict error, logs it. See AgentWebSocketServer._handle_extensions_list/risks.md#issue-002."
    suggested_action: "Preserve the load error and return a typed degraded state."
  - id: ISSUE-003
    dimension: performance_risk
    severity: medium
    status: open
    summary: "A read RPC performs synchronous initialization and emits an unbounded snapshot."
    evidence: "get_rail_manager first initialization synchronously mkdirs the extensions directory and reads/parses. See AgentWebSocketServer._handle_extensions_list/risks.md#issue-003."
    suggested_action: "Initialize off-loop and cap or paginate the response."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No list handler or RailManager list contract tests were found."
    evidence: "The located RailManager test checks only its directory path; an AgentServer startup test fakes a. See AgentWebSocketServer._handle_extensions_list/risks.md#issue-004."
    suggested_action: "Add RailManager, handler, and Web UI contracts for applied and degraded states."
---

# AgentWebSocketServer._handle_extensions_list

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_extensions_list/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_extensions_list/risks.md)
