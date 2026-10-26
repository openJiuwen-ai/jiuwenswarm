---
symbol: AgentWebSocketServer._handle_session_fork
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_session_fork(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: long
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: unsafe
  input_contract: weak
  output_contract: misleading
  side_effects: major
  error_handling: flawed
  state_mutation: external
  dependency_coupling: high
  test_coverage: weak
  observability: partial
  performance_risk: medium
audit:
  status: agent_audited
  auditor: codex
  audited_at: 2026-07-14T11:40:12Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:9cf86cc15020a7f9e8678df7cb83de7d5ca9ab829e7c21c3f5d8d58b7623fd87
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Source and target IDs can escape the sessions directory."
    evidence: "See AgentWebSocketServer._handle_session_fork/risks.md#issue-001 for full evidence."
    suggested_action: "Enforce the normalized session-ID grammar and verify resolved source/target."
  - id: ISSUE-002
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "Fork is neither atomic nor necessarily complete on success."
    evidence: "See AgentWebSocketServer._handle_session_fork/risks.md#issue-002 for full evidence."
    suggested_action: "Stage and verify history, metadata, context, state, and plan copies before a."
  - id: ISSUE-003
    dimension: dependency_coupling
    severity: high
    status: open
    summary: "State may be copied through the wrong agent variant."
    evidence: "See AgentWebSocketServer._handle_session_fork/risks.md#issue-003 for full evidence."
    suggested_action: "Resolve the exact source mode/sub-mode/project/card identity from authoritative."
  - id: ISSUE-004
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Full-history filesystem work runs on the event loop."
    evidence: "See AgentWebSocketServer._handle_session_fork/risks.md#issue-004 for full evidence."
    suggested_action: "Run bounded filesystem work in a worker/job and avoid full-history."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "No handler-level fork contract test was found."
    evidence: "See AgentWebSocketServer._handle_session_fork/risks.md#issue-005 for full evidence."
    suggested_action: "Add end-to-end handler/Gateway tests for success identity, active-source."
  - id: ISSUE-006
    dimension: state_mutation
    severity: high
    status: open
    summary: "A live source session is copied at inconsistent points in time."
    evidence: "See AgentWebSocketServer._handle_session_fork/risks.md#issue-006 for full evidence."
    suggested_action: "Acquire a per-source session lifecycle lock, quiesce or reject active work."
  - id: ISSUE-007
    dimension: boundary_safety
    severity: high
    status: open
    summary: "The handler does not authorize access to the source session."
    evidence: "See AgentWebSocketServer._handle_session_fork/risks.md#issue-007 for full evidence."
    suggested_action: "Resolve the authenticated principal and enforce source ownership plus."
---

# AgentWebSocketServer._handle_session_fork

## Actual Role

The reviewed behavior, contracts, side effects, callers, callees, tests, and documentation evidence are preserved in the linked detail pages.

## Audit Details

- [Reviewed behavior](AgentWebSocketServer._handle_session_fork/actual-behavior.md)
- [Full issue evidence](AgentWebSocketServer._handle_session_fork/risks.md)
