---
symbol: AgentWebSocketServer._handle_schedule_request
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_schedule_request(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock, action: str) -> None"
health:
  overall: critical
  name_behavior_match: partial
  responsibility_focus: mixed
  length: long
  complexity: high
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: hidden
  error_handling: flawed
  state_mutation: shared
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: high
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:40:16Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:3e89725a9a0f9a4b914a49cd5c58f7a47569c97b20de3c49183e804c72102a90
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: critical
    status: open
    summary: "Scheduled work can execute through another request's agent and workspace."
    evidence: "See AgentWebSocketServer._handle_schedule_request/risks.md#issue-001 for full evidence."
    suggested_action: "Persist immutable ownership per task, resolve its exact agent at execution, and."
  - id: ISSUE-002
    dimension: output_contract
    severity: high
    status: open
    summary: "Domain failures are encoded as successful responses."
    evidence: "See AgentWebSocketServer._handle_schedule_request/risks.md#issue-002 for full evidence."
    suggested_action: "Map service results to typed wire failures; reserve ok=true for completed."
  - id: ISSUE-003
    dimension: input_contract
    severity: high
    status: open
    summary: "Action parameters lack type, range, and response-size bounds."
    evidence: "See AgentWebSocketServer._handle_schedule_request/risks.md#issue-003 for full evidence."
    suggested_action: "Use per-action schemas, bounded strings/enums/pagination, and a hard wire."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No schedule/issue RPC handler test was found."
    evidence: "See AgentWebSocketServer._handle_schedule_request/risks.md#issue-004 for full evidence."
    suggested_action: "Add table-driven action, concurrency, restart, cross-project, failure, and."
  - id: ISSUE-005
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Lazy scheduler publication exposes half-initialized or permanently poisoned service state."
    evidence: "See AgentWebSocketServer._handle_schedule_request/risks.md#issue-005 for full evidence."
    suggested_action: "Initialize behind a shared lock/future, publish only after success, expose."
  - id: ISSUE-006
    dimension: dependency_coupling
    severity: high
    status: open
    summary: "Scheduled model selection can use stale or wrong same-name provider state."
    evidence: "See AgentWebSocketServer._handle_schedule_request/risks.md#issue-006 for full evidence."
    suggested_action: "Persist a stable provider/model revision per task and rebuild an atomically."
---

# AgentWebSocketServer._handle_schedule_request

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_schedule_request/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_schedule_request/risks.md)
