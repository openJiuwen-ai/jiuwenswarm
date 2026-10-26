---
symbol: AgentWebSocketServer._handle_session_delete
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_session_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: long
  complexity: medium
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: weak
  output_contract: clear
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
  audited_symbol_hash: sha256:9b07813e7aeda9e4ab9dc109c7b1c914feb6cba5dd0eeed71fb8e2ccbeb0edf6
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Unvalidated session_id reaches recursive deletion."
    evidence: "Lines 2594-2606 only strip params.session_id before computing get_agent_sessions_dir() / target.. See AgentWebSocketServer._handle_session_delete/risks.md#issue-001."
    suggested_action: "Require a safe single-name ID, enforce resolved-root containment, and test traversal and absolute paths."
  - id: ISSUE-002
    dimension: state_mutation
    severity: medium
    status: open
    summary: "Delete leaves some session-scoped runtime uncoordinated."
    evidence: "The handler does not cancel/await _session_stream_tasks or remove _session_mode_sync_locks. For non-team. See AgentWebSocketServer._handle_session_delete/risks.md#issue-002."
    suggested_action: "Define idempotent ordering for active work, adapter cleanup, locks, and caches under concurrent chat traffic."
  - id: ISSUE-003
    dimension: error_handling
    severity: medium
    status: open
    summary: "Filesystem failure can leave a partial delete with a generic error."
    evidence: "Runtime/team cleanup is attempted first at lines 2630-2646, but shutil.rmtree at line 2657 is outside. See AgentWebSocketServer._handle_session_delete/risks.md#issue-003."
    suggested_action: "Map filesystem errors locally and make partial cleanup observable and retry-safe."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Direct tests cover only ordinary success and the checkpointer gate."
    evidence: "Two direct tests in test_agentserver_acp.py cover ordinary success and checkpointer rejection. Team. See AgentWebSocketServer._handle_session_delete/risks.md#issue-004."
    suggested_action: "Add handler tests for those branches and failure modes."
  - id: ISSUE-005
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Recursive filesystem deletion blocks the AgentServer event loop."
    evidence: "shutil.rmtree(session_dir) runs synchronously inside this async WebSocket handler. A large or slow. See AgentWebSocketServer._handle_session_delete/risks.md#issue-005."
    suggested_action: "Run bounded recursive deletion in a worker thread after containment validation, and expose progress/timeout behavior if."
---

# AgentWebSocketServer._handle_session_delete

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_session_delete/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_session_delete/risks.md)
