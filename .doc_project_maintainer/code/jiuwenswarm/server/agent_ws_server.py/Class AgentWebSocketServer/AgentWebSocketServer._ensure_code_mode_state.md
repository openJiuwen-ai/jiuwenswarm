---
symbol: AgentWebSocketServer._ensure_code_mode_state
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_ensure_code_mode_state(request: AgentRequest, mode: str, sub_mode: str, agent: Any) -> bool"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: long
  complexity: high
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: weak
  output_contract: clear
  side_effects: explicit
  error_handling: missing
  state_mutation: global
  dependency_coupling: high
  test_coverage: partial
  observability: partial
  performance_risk: medium
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: input_contract
    severity: medium
    status: open
    summary: "Default session normalization is inconsistent before checkpointer access."
    evidence: "_ensure_code_mode_state computes session_id = request.session_id or default, but passes raw request.session_id into create_agent_session; OpenJiuwen create_agent_session(None) creates a fresh UUID session."
    suggested_action: "Pass the normalized session_id variable into create_agent_session and add a no-session regression test for default-session plan-mode sync."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "The per-session lock does not cover the plan-exit detector that feeds this method's stale-reentry guard."
    evidence: "This method locks checkpoint work, but _check_post_process_plan_exit reads the same state and mutates _plan_exited_sessions unlocked; a new plan can start between its read and exit push."
    suggested_action: "Use the same lock around the post-process state read, flag mutation, and push decision, with an idempotent transition check."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Important guard branches lack direct tests."
    evidence: "Tests cover plan-to-normal sync, already-matching state, explicit /plan re-entry, team skip, and non-code skip; no direct test invokes the interrupt-resume or non-chat guards, either stale-reentry block, or activation-reminder injection."
    suggested_action: "Add focused async tests for those guards and both stale-reentry mechanisms, including params mode correction, plan.mode_exited push, and reminder injection."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._ensure_code_mode_state`

## Actual Role

Before unary or streaming processing, eligible code-mode chat turns load persisted plan state under a per-session lock and reconcile it with the requested sub-mode. The method persists switches, blocks stale plan re-entry unless `/plan` was explicit, skips interrupt resumes, may correct the request or inject a reminder, and reports plan-to-normal restoration.

## Key Signals

- Input: `AgentRequest`, resolved `mode` and `sub_mode`, and an agent exposing `get_instance()`.
- Output: Boolean; true only when persisted plan mode was `plan` and the requested sub-mode is `normal`.
- Main side effects: Reads/writes checkpointer state, mutates request params/query, uses module-global plan-exit and lock registries, and may send `plan.mode_exited`.
- Main risk: Default-session identity is inconsistent, and the matching post-process exit detector is outside this method's lock, permitting a stale exit flag/push race. The global lock registry also has no observed cleanup path.
- Related tests: Direct transition tests exist, but stale re-entry, interrupt-resume, non-chat, and activation-reminder branches are untested; the `agentserver-plan-mode-exit` flow is still pending.

## Detail Index

- Detail docs pending.
