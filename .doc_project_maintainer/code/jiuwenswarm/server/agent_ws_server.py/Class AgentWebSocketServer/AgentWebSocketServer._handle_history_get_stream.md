---
symbol: AgentWebSocketServer._handle_history_get_stream
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_history_get_stream(ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
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
  audited_at: 2026-07-14T11:38:13Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:87051df5f3981a359d6ee60ef408869d562fccd2f7cb90f7edf82b0c6fe1da2d
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Unvalidated session_id flows into session history path construction."
    evidence: "params.session_id is passed unchanged to get_conversation_history; history_exists/get_read_history_path. See AgentWebSocketServer._handle_history_get_stream/risks.md#issue-001."
    suggested_action: "Require a safe single-name ID, enforce resolved-root containment, make read paths non-creating, and add."
  - id: ISSUE-002
    dimension: error_handling
    severity: medium
    status: open
    summary: "Invalid stream history emits chat.error instead of a history-scoped terminal frame."
    evidence: "When get_conversation_history returns None, the method sends only an is_complete chat.error chunk.. See AgentWebSocketServer._handle_history_get_stream/risks.md#issue-002."
    suggested_action: "Emit a history-scoped terminal error/done frame or explicitly resolve pending page waiters on chat.error."
  - id: ISSUE-003
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Each page synchronously processes the full history on the event loop."
    evidence: "The handler synchronously calls get_conversation_history on the event loop; that helper reads/parses the. See AgentWebSocketServer._handle_history_get_stream/risks.md#issue-003."
    suggested_action: "Move reads off-loop and use reverse JSONL/cursor paging or cached pagination metadata for large sessions."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct stream-handler test evidence was found."
    evidence: "Tests cover get_conversation_history payload limits, gateway routing, and generic history.message codec. See AgentWebSocketServer._handle_history_get_stream/risks.md#issue-004."
    suggested_action: "Add fake-websocket tests for valid/invalid pages, sequence numbers, done publication, traversal, and send failure."
  - id: ISSUE-005
    dimension: output_contract
    severity: medium
    status: open
    summary: "An oversized record terminates the page without its history done frame."
    evidence: "If send_wire_payload replaces a record chunk and returns false, the method logs and returns immediately. See AgentWebSocketServer._handle_history_get_stream/risks.md#issue-005."
    suggested_action: "Define the replacement as an explicit terminal history error or emit a bounded history-scoped done/error frame, and."
---

# AgentWebSocketServer._handle_history_get_stream

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_history_get_stream/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_history_get_stream/risks.md)
