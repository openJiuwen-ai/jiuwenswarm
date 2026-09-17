---
symbol: AgentWebSocketServer._handle_command_compact_partial
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_compact_partial(ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: medium
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:40Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:832c3981f728d32a69229e7fcba8ab20ad495a34bd680451642597d1f778f718
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "turn_index is not validated as a positive 1-based value."
    evidence: "See AgentWebSocketServer._handle_command_compact_partial/risks.md#issue-001 for full evidence."
    suggested_action: "Require a present integer turn_index >= 1 before resolving or calling an agent."
  - id: ISSUE-002
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "The delegated adapter reads only legacy history.json while current session history."
    evidence: "See AgentWebSocketServer._handle_command_compact_partial/risks.md#issue-002 for full evidence."
    suggested_action: "Use the shared session history resolver/load helpers and add JSONL integration."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: high
    status: open
    summary: "session_id controls an unchecked history read path."
    evidence: "See AgentWebSocketServer._handle_command_compact_partial/risks.md#issue-003 for full evidence."
    suggested_action: "Validate a canonical session ID and enforce containment under sessions_dir."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct handler tests cover compact_partial routing, validation, response, and error."
    evidence: "See AgentWebSocketServer._handle_command_compact_partial/risks.md#issue-004 for full evidence."
    suggested_action: "Add handler tests for success, missing/invalid turn_index, adapter failures."
  - id: ISSUE-005
    dimension: output_contract
    severity: medium
    status: open
    summary: "Adapter-level failed and no-turn results are reported as top-level success."
    evidence: "See AgentWebSocketServer._handle_command_compact_partial/risks.md#issue-005 for full evidence."
    suggested_action: "Map adapter status to a stable top-level ok/error contract and test each normal."
  - id: ISSUE-006
    dimension: error_handling
    severity: medium
    status: open
    summary: "The broad BaseException handler can swallow process-level exits."
    evidence: "See AgentWebSocketServer._handle_command_compact_partial/risks.md#issue-006 for full evidence."
    suggested_action: "Catch Exception, with an explicit CancelledError passthrough if required."
---

# AgentWebSocketServer._handle_command_compact_partial

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_compact_partial/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_compact_partial/risks.md)
