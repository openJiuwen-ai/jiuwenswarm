---
symbol: AgentWebSocketServer._is_client_disconnect_cancel_request
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_is_client_disconnect_cancel_request(request: AgentRequest) -> bool"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: good
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: none
  error_handling: clear
  state_mutation: none
  dependency_coupling: low
  test_coverage: good
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
    dimension: boundary_safety
    severity: medium
    status: open
    summary: "The destructive-cleanup marker is trusted solely by channel_context value."
    evidence: "E2AEnvelope.from_dict preserves raw channel_context and e2a_to_agent_request copies it to request.metadata; this predicate accepts the exact internal key/value without authenticated provenance. Params, top-level metadata, and legacy metadata are filtered, but a direct E2A sender can construct channel_context."
    suggested_action: "Bind internal cancel provenance to an authenticated Gateway connection or a protected request field populated only after trusted ingress validation."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._is_client_disconnect_cancel_request`

## Actual Role

Classifies a `CHAT_CANCEL` request as Gateway-generated client-disconnect cleanup when its normalized metadata contains the exact internal cancel-source key/value. `_handle_message` uses the result to forbid agent creation and, for cancel/supplement intents, clean the session runtime after interrupt and stream-task cleanup.

## Key Signals

- Input: `AgentRequest`; non-dict metadata is safely treated as empty.
- Output: `True` only for the trimmed exact value `client_disconnect`; otherwise `False`.
- Side effects: None.
- Main risk: Correctness and cleanup authorization depend on trusted construction of E2A `channel_context` upstream.
- Tests/flow: AgentServer tests cover positive disconnect cleanup and negative params/top-level/legacy metadata cases; Gateway tests cover internal stamping, manual-cancel stripping, grace delay, and reconnect cancellation. The partial E2A chat flow includes disconnect cleanup only as a known integration gap.

## Detail Index

- Detail docs pending.
