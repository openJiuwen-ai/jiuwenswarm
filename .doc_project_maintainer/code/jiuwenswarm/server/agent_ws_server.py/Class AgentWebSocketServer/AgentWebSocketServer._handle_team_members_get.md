---
symbol: AgentWebSocketServer._handle_team_members_get
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_team_members_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: medium
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: hidden
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:11Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:e277b218fad25c3c49cefc5a58ad233971dd6e1214c0de3d49dee62a94c28b63
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: output_contract
    severity: medium
    status: open
    summary: "Zero seats, unavailable runtime, and query failure share one success shape."
    evidence: "See AgentWebSocketServer._handle_team_members_get/risks.md#issue-001 for full evidence."
    suggested_action: "Add ready/status/error fields and reserve members=[] for successful zero seats."
  - id: ISSUE-002
    dimension: input_contract
    severity: medium
    status: open
    summary: "Session/team identity is derived from unchecked caller strings."
    evidence: "See AgentWebSocketServer._handle_team_members_get/risks.md#issue-002 for full evidence."
    suggested_action: "Require a canonical non-empty session ID and resolve its authoritative team."
  - id: ISSUE-003
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Live member discovery has no local timeout."
    evidence: "See AgentWebSocketServer._handle_team_members_get/risks.md#issue-003 for full evidence."
    suggested_action: "Bound discovery and return an explicit unavailable status."
  - id: ISSUE-004
    dimension: implementation_soundness
    severity: low
    status: fixed
    summary: "The obsolete cross-channel monitor fallback was removed."
    evidence: "See AgentWebSocketServer._handle_team_members_get/risks.md#issue-004 for full evidence."
    suggested_action: "Retain coverage proving member lookup works while no monitor runtime is active."
  - id: ISSUE-005
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "The advertised resolved team name is only the caller's value echoed back."
    evidence: "See AgentWebSocketServer._handle_team_members_get/risks.md#issue-005 for full evidence."
    suggested_action: "Resolve and return the stored/session-authoritative team name, and compare it."
  - id: ISSUE-006
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Handler tests mock away the identity and storage behavior that carries the main risk."
    evidence: "See AgentWebSocketServer._handle_team_members_get/risks.md#issue-006 for full evidence."
    suggested_action: "Add integrated helper/handler/Gateway tests using authoritative session."
---

# AgentWebSocketServer._handle_team_members_get

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_team_members_get/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_team_members_get/risks.md)
