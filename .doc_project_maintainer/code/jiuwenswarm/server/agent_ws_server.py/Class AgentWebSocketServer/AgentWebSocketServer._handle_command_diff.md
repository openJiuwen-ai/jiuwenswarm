---
symbol: AgentWebSocketServer._handle_command_diff
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_command_diff(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: implicit
  output_contract: clear
  side_effects: hidden
  error_handling: partial
  state_mutation: global
  dependency_coupling: medium
  test_coverage: partial
  observability: clear
  performance_risk: high
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:38:42Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:e505c12eb06c80191703a2f23d27dc1fc10782041514f938e036f85931638341
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "Resolved project_dir is trusted before filesystem and git-diff reads."
    evidence: "resolve_request_project_dir accepts params/metadata project_dir or cwd and params.trusted_dirs[0]; line. See AgentWebSocketServer._handle_command_diff/risks.md#issue-001."
    suggested_action: "Validate canonical project_dir against trusted/session-bound directories, or derive it from server-side session."
  - id: ISSUE-002
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Direct coverage exercises only the empty default success path."
    evidence: "test_handle_command_diff_returns_summary_payload exercises the real singleton only for {type:list. See AgentWebSocketServer._handle_command_diff/risks.md#issue-002."
    suggested_action: "Add handler tests with a fake DiffService for turns, gitDiff, and error paths, plus a gateway/TUI forwarding smoke test."
  - id: ISSUE-003
    dimension: boundary_safety
    severity: high
    status: open
    summary: "session_id controls unchecked history and metadata paths."
    evidence: "request.session_id reaches load_history_records and get_agent_sessions_dir() / session_id /. See AgentWebSocketServer._handle_command_diff/risks.md#issue-003."
    suggested_action: "Validate a canonical ID, enforce sessions-root containment, and make reads non-creating."
  - id: ISSUE-004
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Turn-diff computation and payload construction are unbounded across session history."
    evidence: "get_turn_diffs reads full history and matching file-operation logs and returns every changed turn.. See AgentWebSocketServer._handle_command_diff/risks.md#issue-004."
    suggested_action: "Add cursor/limit or a total turn-diff budget before computation/payload construction, and return explicit truncation."
---

# AgentWebSocketServer._handle_command_diff

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_command_diff/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_command_diff/risks.md)
