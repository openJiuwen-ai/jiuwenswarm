---
symbol: AgentWebSocketServer._handle_acp_tool_response
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_acp_tool_response(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
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
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:40:12Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:13c0d3128299424a558c93059fcb80bab8f1b1cb977c4e6e7d3ff19030ed5a3c
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "A response can complete another session's pending ACP request."
    evidence: "At HEAD 39feee89, complete_jsonrpc_response is called on the process-global AcpOutputManager using only. See AgentWebSocketServer._handle_acp_tool_response/risks.md#issue-001."
    suggested_action: "Bind entries to authorized connection, channel, session, and body id; require all to match."
  - id: ISSUE-002
    dimension: input_contract
    severity: high
    status: open
    summary: "Malformed JSON-RPC payloads become successful tool responses."
    evidence: "At HEAD 39feee89, every non-dict params.response is coerced to {} before the pending lookup. A matching. See AgentWebSocketServer._handle_acp_tool_response/risks.md#issue-002."
    suggested_action: "Validate id and result/error schema before consuming the pending entry."
  - id: ISSUE-003
    dimension: dependency_coupling
    severity: medium
    status: open
    summary: "Correlation state is split across Gateway and AgentServer registries."
    evidence: "At HEAD 39feee89, Gateway removes its jsonrpc_id-to-session correlation before forwarding, while. See AgentWebSocketServer._handle_acp_tool_response/risks.md#issue-003."
    suggested_action: "Use one owner or retain correlation state until AgentServer acceptance."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Tests omit trust-boundary and lifecycle cases."
    evidence: "At HEAD 39feee89, test_agentserver_acp.py covers one valid completion and one unknown-id soft ignore. It. See AgentWebSocketServer._handle_acp_tool_response/risks.md#issue-004."
    suggested_action: "Add ownership, schema, race, transport-loss, and payload-boundary tests."
  - id: ISSUE-005
    dimension: error_handling
    severity: medium
    status: open
    summary: "Pending completion is irreversible before acknowledgment delivery."
    evidence: "At HEAD 39feee89, complete_jsonrpc_response removes/completes global pending state before response. See AgentWebSocketServer._handle_acp_tool_response/risks.md#issue-005."
    suggested_action: "Make acceptance idempotently replayable until acknowledged, or couple pending completion and Gateway correlation."
---

# AgentWebSocketServer._handle_acp_tool_response

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_acp_tool_response/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_acp_tool_response/risks.md)
