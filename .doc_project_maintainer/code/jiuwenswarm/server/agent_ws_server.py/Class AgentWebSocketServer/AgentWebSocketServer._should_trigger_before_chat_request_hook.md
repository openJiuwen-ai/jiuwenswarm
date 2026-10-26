---
symbol: AgentWebSocketServer._should_trigger_before_chat_request_hook
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_should_trigger_before_chat_request_hook(request: AgentRequest) -> bool"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: safe
  input_contract: implicit
  output_contract: clear
  side_effects: none
  error_handling: clear
  state_mutation: none
  dependency_coupling: low
  test_coverage: missing
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
    summary: "Missing direct AgentServer tests for before-chat hook eligibility."
    evidence: "No test references either side's predicate. tests/unit_tests/gateway/test_message_handler_evolution.py overrides _trigger_before_chat_request_hook with a no-op, so it does not verify Gateway or AgentServer eligibility."
    suggested_action: "Add parameterized tests for CHAT_SEND, CHAT_RESUME, CHAT_ANSWER, and representative non-chat methods, plus a hook-gating test through _trigger_before_chat_request_hook."
  - id: ISSUE-002
    dimension: dependency_coupling
    severity: low
    status: open
    summary: "AgentServer and Gateway maintain duplicate before-chat hook eligibility lists."
    evidence: "AgentWebSocketServer and Gateway MessageHandler each hard-code the same current tuple: CHAT_SEND, CHAT_RESUME, and CHAT_ANSWER. They emit distinct AgentServerHookEvents and GatewayHookEvents but have no shared eligibility definition or parity test."
    suggested_action: "Centralize the eligible method set or add parity tests so future chat method additions do not drift between Gateway and AgentServer."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._should_trigger_before_chat_request_hook`

## Actual Role

Pure static predicate that reads `AgentRequest.req_method` and returns `True` exactly for `CHAT_SEND`, `CHAT_RESUME`, and `CHAT_ANSWER`. `_trigger_before_chat_request_hook` uses it to gate the AgentServer extension event before dispatch; Gateway independently applies the same three-method rule to its own hook event before forwarding.

## Key Signals

- Input: `AgentRequest` with `req_method` populated from `ReqMethod` or `None`.
- Output: Boolean; true only for the three chat-turn request methods.
- Main side effects: None.
- Main risk: Low implementation risk, but hook eligibility is duplicated with Gateway and lacks direct AgentServer tests.
- Related tests: No predicate or hook-gating test was found; the Gateway evolution harness overrides its analogous hook with a no-op.

## Detail Index

- Detail docs pending.
