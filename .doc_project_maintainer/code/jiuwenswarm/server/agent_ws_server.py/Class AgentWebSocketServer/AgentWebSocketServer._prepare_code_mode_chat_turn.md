---
symbol: AgentWebSocketServer._prepare_code_mode_chat_turn
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_prepare_code_mode_chat_turn(request: AgentRequest, channel_id: str) -> tuple[str, str | None, Any]"
health:
  overall: watch
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: medium
  test_coverage: partial
  observability: partial
  performance_risk: low
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
    dimension: test_coverage
    severity: medium
    status: fixed
    summary: "Direct tests now lock the relevant AgentManager selection arguments."
    evidence: "Team selection and stale agent/code-workspace tests assert channel_id, mode, project_dir, and sub_mode; auto_harness and None-agent boundaries remain separate pending coverage."
    suggested_action: "Retain the exact cache-identity assertions when extending mode handling."
  - id: ISSUE-002
    dimension: observability
    severity: low
    status: open
    summary: "No-agent failure loses selection context."
    evidence: "The method raises ValueError('Failed to get agent') after get_agent returns None; the outer handler reports only request_id plus the generic message."
    suggested_action: "Include channel_id, logical mode, agent_mode, sub_mode, and project_dir in the raised error or a structured log before propagating."
  - id: ISSUE-003
    dimension: name_behavior_match
    severity: low
    status: open
    summary: "The method name understates its all-mode selection role."
    evidence: "Unary and stream callers use it for every non-stateless chat turn; it resolves agent, team, code, and auto_harness modes rather than only code mode."
    suggested_action: "Rename it to describe general chat-turn agent selection, or narrow its callers and contract to code mode."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._prepare_code_mode_chat_turn`

## Actual Role

Reads locked Session `work_mode` (falling back to the request), canonicalizes single-Agent runtime identity, resolves the stable project directory, awaits any claimed prewarm task, and selects the matching AgentManager root. A stale `mode=agent` code request therefore selects `(code, normal, project)` rather than constructing a second Session child under another root.

## Key Signals

- Input: `AgentRequest` plus resolved `channel_id`.
- Output: Tuple of logical mode, sub-mode, and resolved agent.
- Main side effects: Canonicalizes `request.params["mode"]`; may create or reuse an agent through `AgentManager.get_agent`.
- Main risk: It performs an additional metadata read to restore canonical identity and still has a broad all-mode contract.
- Related tests: direct tests now assert exact AgentManager arguments for team and stale-agent/code-workspace requests; failure and auto-harness boundaries remain partial.

## Detail Index

- Detail docs pending.
