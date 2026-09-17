---
symbol: AgentWebSocketServer._handle_history_get
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_history_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
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
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:09Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:f36aba55f7eeae472f28f8b852aaba7db061cf8cacc104201a6a148888f28965
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Unchecked session_id reaches creating filesystem helpers."
    evidence: "Lines 3010-3013 forward params.session_id to get_conversation_history. That helper only. See AgentWebSocketServer._handle_history_get/risks.md#issue-001."
    suggested_action: "Validate a single safe session ID, enforce resolved-root containment, and use create=False for all read paths."
  - id: ISSUE-002
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Each page load scans the whole history."
    evidence: "get_conversation_history lines 6140-6165 synchronously reads/parses the entire JSON/JSONL file, filters. See AgentWebSocketServer._handle_history_get/risks.md#issue-002."
    suggested_action: "Consider reverse JSONL/cursor paging or cached pagination metadata for large sessions."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct unary history handler tests were found."
    evidence: "test_history_payload_limits.py directly covers get_conversation_history sanitization for one large. See AgentWebSocketServer._handle_history_get/risks.md#issue-003."
    suggested_action: "Add async handler tests with fake ws, patched history helper, invalid params, and path-safety cases."
  - id: ISSUE-004
    dimension: error_handling
    severity: low
    status: open
    summary: "Failure responses are ambiguous and uncoded."
    evidence: "Lines 3014-3020 map invalid type/range, out-of-range page, missing history, non-list data, and caught. See AgentWebSocketServer._handle_history_get/risks.md#issue-004."
    suggested_action: "Return stable BAD_REQUEST/NOT_FOUND/HISTORY_IO codes and keep filesystem probing inside the local error boundary."
---

# AgentWebSocketServer._handle_history_get

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_history_get/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_history_get/risks.md)
