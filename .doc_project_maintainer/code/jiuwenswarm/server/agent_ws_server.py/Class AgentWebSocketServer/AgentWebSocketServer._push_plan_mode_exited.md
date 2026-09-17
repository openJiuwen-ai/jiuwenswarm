---
symbol: AgentWebSocketServer._push_plan_mode_exited
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_push_plan_mode_exited(self, request: AgentRequest) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: implicit
  output_contract: implicit
  side_effects: explicit
  error_handling: partial
  state_mutation: none
  dependency_coupling: medium
  test_coverage: missing
  observability: partial
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
    summary: "Missing direct tests for the plan-mode-exited push contract."
    evidence: "Plan-mode orchestration tests replace _push_plan_mode_exited with AsyncMock or only assert it was not awaited; generic push tests do not assert this helper's no-session branch or event/channel/session/mode payload."
    suggested_action: "Add focused async tests for missing session_id, explicit/default channel_id, and send_push payload shape for plan.mode_exited."
  - id: ISSUE-002
    dimension: output_contract
    severity: low
    status: open
    summary: "The emitted mode field is not preserved by the WebChannel structured-event path."
    evidence: "The helper sends payload.mode='code.normal', but web_connect.py:615-627 omits plan.mode_exited from its full-payload allowlist and its fallback retains only content/session fields. The TUI handler defaults a missing mode to code.normal, but no WebChannel contract test pins this behavior."
    suggested_action: "Either add plan.mode_exited to the structured WebChannel event allowlist with a gateway/frontend test, or document that frame.event alone is the contract."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._push_plan_mode_exited`

## Actual Role

Returns immediately when the request has no session id; otherwise sends a `plan.mode_exited` server-push for that session and channel, declaring `mode: code.normal`. Callers use it after persisted plan exit, restored normal mode, or stale plan re-entry rejection so the client can synchronize its mode.

## Key Signals

- Input: `AgentRequest`; requires `request.session_id`, uses `request.channel_id` or `"default"`.
- Output: None; emits a server-push payload containing `event_type=plan.mode_exited` and `mode=code.normal`.
- Main side effects: Calls `AgentWebSocketServer.send_push`, which writes an E2A server-push frame to the active Gateway WebSocket.
- Main risk: The direct payload contract is not pinned by tests, and one gateway path may drop `payload.mode`.
- Related tests: Plan-mode orchestration tests mock this helper or assert a no-call branch; no direct payload, missing-session, default-channel, or end-to-end consumer test was found.

## Detail Index

- Detail docs pending.
