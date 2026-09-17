---
symbol: AgentWebSocketServer._handle_session_list
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_session_list(ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: medium
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:37:48Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:0da68bb3e9affdf2da2e9325b05f776882c572ba55d4f798b6bcbeafe8662d77
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: implementation_soundness
    severity: medium
    status: open
    summary: "Session list handler bypasses shared helper semantics."
    evidence: "Current HEAD 39feee89 still scans get_agent_sessions_dir() directly. In contrast. See AgentWebSocketServer._handle_session_list/risks.md#issue-001."
    suggested_action: "Share one cache-bust-capable listing helper with consistent exclusion, fallback, ordering, and totals."
  - id: ISSUE-002
    dimension: performance_risk
    severity: medium
    status: open
    summary: "The TUI filtering contract requires an unbounded full-list transfer."
    evidence: "The handler ignores request.params, including the Gateway/TUI limit, stats and cache-bust-reads every. See AgentWebSocketServer._handle_session_list/risks.md#issue-002."
    suggested_action: "Define equivalent server filters before pagination, or document and cap the full-list contract."
  - id: ISSUE-003
    dimension: error_handling
    severity: low
    status: open
    summary: "Directory scan failure is reported as ok=true."
    evidence: "One try covers exists(), iterdir(), sorting stats, per-entry is_dir/stat, and metadata reads. Any. See AgentWebSocketServer._handle_session_list/risks.md#issue-003."
    suggested_action: "Return ok=false for total failure, or include a partial-result warning."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct handler coverage found."
    evidence: "Current tests cover get_all_sessions_metadata sorting/pagination/fallback/heartbeat exclusion and. See AgentWebSocketServer._handle_session_list/risks.md#issue-004."
    suggested_action: "Add handler tests for filtering semantics, fallback metadata, scan errors, encoding, and locked send."
---

# AgentWebSocketServer._handle_session_list

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_session_list/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_session_list/risks.md)
