---
symbol: AgentWebSocketServer._handle_command_btw
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_btw(ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: medium
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: medium
  test_coverage: partial
  observability: clear
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:41Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:9ec6c1dea1a4ca96e83d99d67593e60d29edd30aac92ea5f20e3555faa7a8b5b
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Long-running /btw calls have no handler-level timeout or cancellation alignment."
    evidence: "At HEAD 39feee89, the handler awaits generate_btw_answer without a local timeout. Existing boundary. See AgentWebSocketServer._handle_command_btw/risks.md#issue-001."
    suggested_action: "Align client/Gateway/AgentServer deadlines and propagate cancellation to the BTW model call."
  - id: ISSUE-002
    dimension: output_contract
    severity: low
    status: open
    summary: "Unknown adapter statuses are returned as successful transport payloads."
    evidence: "At HEAD 39feee89, any dict result is returned with ok=true without validating status or answer/error. See AgentWebSocketServer._handle_command_btw/risks.md#issue-002."
    suggested_action: "Validate the result schema and normalize unknown statuses or fields to a failed response."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: low
    status: open
    summary: "INFO logging includes the first 100 characters of the user question."
    evidence: "At HEAD 39feee89, the command.btw received INFO log records question[:100], exposing user-provided. See AgentWebSocketServer._handle_command_btw/risks.md#issue-003."
    suggested_action: "Log metadata or question length instead, or move redacted content to debug logging."
  - id: ISSUE-004
    dimension: test_coverage
    severity: low
    status: open
    summary: "Core paths are tested, but boundary handoffs are not fully covered."
    evidence: "Direct tests cover empty input, success, missing agent, adapter error, auto-harness mapping, no_context. See AgentWebSocketServer._handle_command_btw/risks.md#issue-004."
    suggested_action: "Add boundary tests for the remaining routing, schema, privacy-log, and timeout/cancellation behavior."
  - id: ISSUE-005
    dimension: input_contract
    severity: medium
    status: open
    summary: "Missing identity is silently redirected to default runtime context."
    evidence: "At HEAD 39feee89, blank session and channel values become default and an omitted mode becomes agent.plan. See AgentWebSocketServer._handle_command_btw/risks.md#issue-005."
    suggested_action: "Require canonical session/channel identity for context-bearing queries, validate params/question types, and make mode."
---

# AgentWebSocketServer._handle_command_btw

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_btw/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_btw/risks.md)
