---
symbol: AgentWebSocketServer._should_sync_code_mode_state
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_should_sync_code_mode_state(request: AgentRequest) -> bool"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: safe
  input_contract: clear
  output_contract: clear
  side_effects: none
  error_handling: clear
  state_mutation: none
  dependency_coupling: low
  test_coverage: partial
  observability: not_applicable
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
    severity: low
    status: open
    summary: "Direct tests cover one excluded RPC but not the full allowlist or legacy fallback."
    evidence: "test_plan_approval.py::test_skills_list_does_not_sync_code_mode directly covers one excluded RPC. Plan-mode orchestration reaches the downstream sync path with CHAT_SEND, but no direct gate assertions cover CHAT_SEND, CHAT_RESUME, CHAT_ANSWER, or req_method=None."
    suggested_action: "Add a parameterized unit test covering None, CHAT_SEND, CHAT_RESUME, CHAT_ANSWER, SKILLS_LIST, and a representative command or history RPC."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._should_sync_code_mode_state`

## Actual Role

Pure gate called near the start of `_ensure_code_mode_state`, before session locking or checkpoint access. It permits legacy requests with no `req_method`, permits the three members of `_CODE_MODE_SYNC_METHODS` (`chat.send`, `chat.resume`, `chat.user_answer`), and rejects every background, command, history, and configuration RPC.

## Key Signals

- Input: `AgentRequest`; `req_method` may be `None`.
- Output: Boolean; true only for legacy/no-method requests or `_CODE_MODE_SYNC_METHODS`.
- Main side effects: None.
- Main risk: The permissive legacy `None` fallback and full allowlist are not directly pinned, so future method-set drift could reintroduce plan-state races.
- Related tests: `test_skills_list_does_not_sync_code_mode` directly verifies exclusion; `_ensure_code_mode_state` tests indirectly exercise `CHAT_SEND`, with no direct resume/answer/legacy assertions.

## Detail Index

- Detail docs pending.
