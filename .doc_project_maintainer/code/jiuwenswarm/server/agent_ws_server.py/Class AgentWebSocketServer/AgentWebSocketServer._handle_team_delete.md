---
symbol: AgentWebSocketServer._handle_team_delete
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_team_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: long
  complexity: medium
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: clear
  output_contract: implicit
  side_effects: explicit
  error_handling: partial
  state_mutation: global
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:06Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:af1190adb788bb8ee907b15d89cb5eff964b939766f66b7fc0602a59648733d6
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: medium
    status: open
    summary: "Runner.delete_agent_team false result is ignored."
    evidence: "Current source awaits Runner.delete_agent_team(..., force=True) without assigning or checking its. See AgentWebSocketServer._handle_team_delete/risks.md#issue-001."
    suggested_action: "Check the returned value and return a stable failure response before deleting local session directories when persistent."
  - id: ISSUE-002
    dimension: error_handling
    severity: medium
    status: open
    summary: "Local session directory deletion failure is masked as successful team deletion."
    evidence: "Current local cleanup catches each shutil.rmtree exception, logs and continues, but the final payload. See AgentWebSocketServer._handle_team_delete/risks.md#issue-002."
    suggested_action: "Track failed local removals and return a partial/failure response, or retry/rollback according to a documented cleanup."
  - id: ISSUE-003
    dimension: test_coverage
    severity: low
    status: open
    summary: "Missing direct tests for partial and lower-level failure paths."
    evidence: "test_agentserver_acp has direct success, checkpointer-unavailable, non-team-mode, and missing-team-name. See AgentWebSocketServer._handle_team_delete/risks.md#issue-003."
    suggested_action: "Add tests for no matching sessions, Runner false/exception, and rmtree failure so destructive partial-cleanup behavior."
---

# AgentWebSocketServer._handle_team_delete

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_team_delete/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_team_delete/risks.md)
