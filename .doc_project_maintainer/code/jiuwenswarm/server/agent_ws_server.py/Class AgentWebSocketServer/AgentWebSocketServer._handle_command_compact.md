---
symbol: AgentWebSocketServer._handle_command_compact
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_compact(ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: mixed
  length: long
  complexity: medium
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: implicit
  error_handling: weak
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
  audited_symbol_hash: sha256:ba0c38e5a251a788b195faf464f15d4e7cb7da8c0b1e11872fcfba0c02747919
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: output_contract
    severity: medium
    status: open
    summary: "Emits an apparently unconsumed context.compressed push."
    evidence: "At HEAD 39feee89, every result == compressed with truthy stats sends context.compressed before any. See AgentWebSocketServer._handle_command_compact/risks.md#issue-001."
    suggested_action: "Align event names or remove the redundant push and rely on tested RPC stats/context.compression_state delivery."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Summary display depends on best-effort push delivery."
    evidence: "At HEAD 39feee89, the RPC returns compact_summary, but TUI suppresses local compact output when it is. See AgentWebSocketServer._handle_command_compact/risks.md#issue-002."
    suggested_action: "Render an RPC fallback or make push delivery observable so summary display does not depend on best-effort transport."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Important side effects and failure branches are under-tested."
    evidence: "Two direct tests cover a compressed response and one compression-state push; busy/noop, missing agent. See AgentWebSocketServer._handle_command_compact/risks.md#issue-003."
    suggested_action: "Add focused tests for those result, error, routing, delivery, and persistence branches."
  - id: ISSUE-004
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Post-compression side-effect failure can report failure after context was already mutated."
    evidence: "At HEAD 39feee89, agent.compress_context completes before append_compact_history_records and the second. See AgentWebSocketServer._handle_command_compact/risks.md#issue-004."
    suggested_action: "Separate the committed compression result from best-effort notification/history outcomes, make persistence atomic, and."
  - id: ISSUE-005
    dimension: input_contract
    severity: high
    status: open
    summary: "Mutation targeting is silently defaulted and advertised compact instructions are ignored."
    evidence: "At HEAD 39feee89, a missing/blank session_id is replaced with default and then used for context mutation. See AgentWebSocketServer._handle_command_compact/risks.md#issue-005."
    suggested_action: "Require a canonical explicit session ID, validate params as an object, and either implement instructions through the."
---

# AgentWebSocketServer._handle_command_compact

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_compact/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_compact/risks.md)
