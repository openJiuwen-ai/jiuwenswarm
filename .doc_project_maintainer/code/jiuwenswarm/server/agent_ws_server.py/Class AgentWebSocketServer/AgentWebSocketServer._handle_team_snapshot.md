---
symbol: AgentWebSocketServer._handle_team_snapshot
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_team_snapshot(ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: weak
  side_effects: hidden
  error_handling: weak
  state_mutation: global
  dependency_coupling: medium
  test_coverage: partial
  observability: partial
  performance_risk: low
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:11Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:e42c1e22492b4c38e22e67d3e7e5fbe9d5004bbc7ee6afb6001aa4d667e2891d
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct handler or dispatch-route coverage for team.snapshot."
    evidence: "At HEAD 39feee89, tests/unit_tests/agentserver/test_monitor_handler.py covers TeamMonitorHandler payload. See AgentWebSocketServer._handle_team_snapshot/risks.md#issue-001."
    suggested_action: "Add async websocket-handler tests for active snapshot, missing/stopped monitor fallback, callee failure fallback, and."
  - id: ISSUE-002
    dimension: output_contract
    severity: medium
    status: open
    summary: "Different states collapse into the same successful empty response."
    evidence: "At HEAD 39feee89, a missing or stopped handler, a None/falsy snapshot, and a handler exception all. See AgentWebSocketServer._handle_team_snapshot/risks.md#issue-002."
    suggested_action: "Add snapshot_status metadata if clients need diagnostics, or document the refresh-tolerant empty response contract."
  - id: ISSUE-003
    dimension: state_mutation
    severity: low
    status: open
    summary: "Read-style snapshot requests can initialize global team runtime state."
    evidence: "At HEAD 39feee89, get_team_manager(channel_id) explicitly ignores channel_id and lazily creates the. See AgentWebSocketServer._handle_team_snapshot/risks.md#issue-003."
    suggested_action: "Use a non-creating lookup for read-only snapshots, or document singleton initialization as acceptable."
---

# AgentWebSocketServer._handle_team_snapshot

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_team_snapshot/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_team_snapshot/risks.md)
