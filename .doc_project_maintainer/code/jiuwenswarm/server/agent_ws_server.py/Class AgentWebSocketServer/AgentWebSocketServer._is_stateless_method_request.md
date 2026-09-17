---
symbol: AgentWebSocketServer._is_stateless_method_request
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_is_stateless_method_request(request: AgentRequest) -> bool"
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
  dependency_coupling: medium
  test_coverage: missing
  observability: not_applicable
  performance_risk: low
audit:
  status: unaudited
  auditor: null
  audited_at: null
  audited_commit: null
  audited_source_hash: null
  audited_symbol_hash: null
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Missing direct tests for stateless predicate and unary bypass behavior."
    evidence: "No test invokes this predicate. test_skills_list_does_not_sync_code_mode covers the separate _should_sync_code_mode_state helper, while symphony/skill tests exercise downstream handlers rather than the unary/stream bypass at lines 2149 and 2202."
    suggested_action: "Add parameterized tests for None, chat.*, skills.*, plugins.*, symphony.*, and skilldev contract cases; add a fake-agent _handle_unary test proving stateless requests use mode='agent' and skip code-mode preparation."
  - id: ISSUE-002
    dimension: input_contract
    severity: medium
    status: open
    summary: "skilldev prefix is currently unreachable through normal ReqMethod parsing."
    evidence: "AgentRequest.req_method is ReqMethod | None and both wire paths construct ReqMethod(value). message.py defines skills/plugins/symphony values but no skilldev values, while skilldev/service.py references missing ReqMethod.SKILLDEV_* members."
    suggested_action: "Align the protocol contract: either add ReqMethod.SKILLDEV_* values plus tests, or remove/adjust skilldev from this stateless predicate until the enum and service dispatch are wired consistently."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._is_stateless_method_request`

## Actual Role

Pure routing predicate that returns true when an `AgentRequest` has a non-null `ReqMethod` whose value starts with `skills.`, `skilldev.`, `plugins.`, or `symphony.`. Unary and streaming handlers use it to obtain a lightweight stateless agent and bypass mode parsing, adapter rebuild, and code-mode synchronization.

## Key Signals

- Input: `AgentRequest`; `req_method` may be `ReqMethod` or `None`.
- Output: Boolean; true only for matching stateless RPC method prefixes.
- Main side effects: None.
- Main risk: The prefix list is hard-coded and can drift from `ReqMethod` and adapter route tables; `skilldev` is named here but no current `ReqMethod` skilldev values were found.
- Related tests: Adjacent tests cover skills code-mode-sync exclusion and downstream symphony behavior; no direct prefix matrix or unary/stream stateless-bypass test was found.

## Detail Index

- Detail docs pending.
