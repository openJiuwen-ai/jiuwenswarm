---
symbol: AgentWebSocketServer._handle_unary
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_handle_unary(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: clear
  performance_risk: low
audit:
  status: audit_expired
  auditor: codex
  audited_at: 2026-07-14T11:37:47Z
  audited_commit: 39feee89e00dc6b0b6a6b16ca80a527beb631bd7
  audited_source_hash: sha256:5fbbae5104a1791ca98014aeed0b81fea243b57dcd2faac3f8f37886833c4fa5
  audited_symbol_hash: sha256:9b7fd3efb8a44f6abdb8414dd5d632e92b304e1d3deb58ea45884b31562030d8
  confidence: confirmed
  expired_reason: "2026-08-01 source split into a foreground guard wrapper and _handle_unary_impl."
issues:
  - id: ISSUE-001
    dimension: error_handling
    severity: medium
    status: open
    summary: "Post-process plan-exit check can mask the original agent failure."
    evidence: "Current source awaits agent.process_message inside try and awaits _check_post_process_plan_exit unguarded in finally; if both raise, Python propagates the finally exception and _handle_message's outer error response loses the original agent failure."
    suggested_action: "Guard post-process plan-exit checking so it logs its own failure without masking process_message exceptions."
  - id: ISSUE-002
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct tests exercise the real _handle_unary success and send-failure branches."
    evidence: "test_agent_ws_connection_close overrides _handle_unary to raise, plan-mode tests cover helper methods, and test_ws_send directly exercises only _handle_stream; no located test invokes the real stateless or code-mode _handle_unary body."
    suggested_action: "Add direct fake-agent tests for stateless success, code-mode success, process_message exception, and send failure propagation through _handle_message."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._handle_unary`

## Actual Role

Wraps unary handling with the warm-pool foreground guard for chat-like methods. It increments the foreground count before delegating to `_handle_unary_impl` and releases it in `finally`; non-chat unary methods delegate without touching warm scheduling.

## Key Signals

- Input: non-stream `AgentRequest`, WebSocket, and the connection send lock.
- Output: `None` after delegation or after one normal/fallback bounded wire frame is sent.
- Main side effects: resolves/creates an agent, may persist mode and push plan-exit state, invokes the agent, fills a missing response `agent_ref`, and sends on the WebSocket.
- Main risk: plan-exit post-processing can replace the original `process_message` exception.
- Related tests: `test_plan_mode_orchestration.py` covers helpers, `test_agent_ws_connection_close.py` covers outer disconnect handling through an override, and `test_ws_send.py` covers stream/bounded sending. No direct real-body unary success, agent failure, post-process double-failure, or send-failure test was found. The linked Gateway-AgentServer flow remains partial.

## Detail Index

- Detail docs pending.
