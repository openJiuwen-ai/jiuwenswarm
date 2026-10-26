---
symbol: AgentWebSocketServer._handle_team_history_get
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_team_history_get(ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: weak
  output_contract: clear
  side_effects: hidden
  error_handling: partial
  state_mutation: global
  dependency_coupling: medium
  test_coverage: partial
  observability: partial
  performance_risk: high
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:12Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:10eb1791f49633d6f0ba6e902568cc79fffb4d0237fe3e65d53a6ffe856db8b3
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Pagination happens after full history load and full-record sanitization."
    evidence: "Current handler awaits read_team_history_records/read_member_history_records for the full session. See AgentWebSocketServer._handle_team_history_get/risks.md#issue-001."
    suggested_action: "Move pagination closer to storage or slice by cursor before expensive sanitization while preserving total and has_more."
  - id: ISSUE-002
    dimension: error_handling
    severity: medium
    status: open
    summary: "Read failures are indistinguishable from empty history to clients."
    evidence: "The broad read try/except catches team and member reader failures, logs one warning, assigns records=[]. See AgentWebSocketServer._handle_team_history_get/risks.md#issue-002."
    suggested_action: "Return ok=false or include an explicit non-fatal warning/error field for storage failures, with direct tests for that."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Unchecked session_id reaches creating filesystem helpers."
    evidence: "session_id is only type/blank checked and stripped before reaching get_read_history_path. That helper. See AgentWebSocketServer._handle_team_history_get/risks.md#issue-003."
    suggested_action: "Validate a safe ID, enforce sessions-root containment, and make reads non-creating."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Failure and boundary paths lack direct coverage."
    evidence: "test_history_payload_limits directly covers paging, cursor continuation, bounded/truncated records, and. See AgentWebSocketServer._handle_team_history_get/risks.md#issue-004."
    suggested_action: "Add handler tests for validation, read failure, member scope, and hostile paths."
---

# AgentWebSocketServer._handle_team_history_get

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_team_history_get/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_team_history_get/risks.md)
