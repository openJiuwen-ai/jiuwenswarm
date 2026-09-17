---
symbol: AgentWebSocketServer._handle_command_workflows
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_workflows(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: medium
  complexity: medium
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: weak
  output_contract: partial
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: medium
  test_coverage: partial
  observability: clear
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:12Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:59273fdda666022d999100624ea0ec6c5467dceec3779fd7d1df394951e1a019
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "session_id is not validated before checkpoint metadata restore."
    evidence: "At HEAD 39feee89, request.session_id is reduced only to request.session_id or an empty string, then. See AgentWebSocketServer._handle_command_workflows/risks.md#issue-001."
    suggested_action: "Validate session_id as a safe session identifier before fallback restore, or use a metadata accessor that enforces."
  - id: ISSUE-002
    dimension: error_handling
    severity: medium
    status: open
    summary: "Snapshot and restore failures are reported as successful empty snapshots."
    evidence: "At HEAD 39feee89, checkpoint restore exceptions and live get_workflow_snapshot/serialization exceptions. See AgentWebSocketServer._handle_command_workflows/risks.md#issue-002."
    suggested_action: "Use the persisted snapshot after live-read failure when safe, and otherwise return diagnostic metadata or ok=false."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Fallback and dispatch behavior are not fully tested."
    evidence: "Nine direct tests cover empty/live snapshots, size bounds, live-handler failure, and defaults; none. See AgentWebSocketServer._handle_command_workflows/risks.md#issue-003."
    suggested_action: "Add tests for dispatcher routing, checkpoint restore success, restore exception, and unsafe or invalid session_id."
  - id: ISSUE-004
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Checkpoint fallback performs synchronous restore work in the async request path."
    evidence: "At HEAD 39feee89, restore_workflow_runs(session_id) is called synchronously inside the async WebSocket. See AgentWebSocketServer._handle_command_workflows/risks.md#issue-004."
    suggested_action: "Move checkpoint loading to an async storage boundary or a worker thread, with a bounded timeout."
---

# AgentWebSocketServer._handle_command_workflows

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_workflows/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_workflows/risks.md)
