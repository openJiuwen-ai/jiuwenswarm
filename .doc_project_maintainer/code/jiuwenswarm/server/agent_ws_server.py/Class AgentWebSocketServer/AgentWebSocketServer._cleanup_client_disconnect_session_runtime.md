---
symbol: AgentWebSocketServer._cleanup_client_disconnect_session_runtime
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_cleanup_client_disconnect_session_runtime(self, request: AgentRequest) -> None"
health:
  overall: healthy
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: safe
  input_contract: clear
  output_contract: clear
  side_effects: explicit
  error_handling: clear
  state_mutation: shared
  dependency_coupling: medium
  test_coverage: covered
  observability: clear
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
issues: []
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._cleanup_client_disconnect_session_runtime`

## Actual Role

Performs best-effort in-memory session teardown after a Gateway-stamped client-disconnect cancel. It resolves the session from the envelope before the params fallback, no-ops when absent, normalizes an empty channel to `default`, asks `AgentManager` to clean that session across existing channel agents, and logs rather than propagates cleanup failures. It neither creates an agent nor deletes persisted session history.

## Key Signals

- Input: A normalized `AgentRequest`; `request.session_id` is authoritative, with `params.session_id` as compatibility fallback.
- Output: `None`; missing session IDs and cleanup failures do not disturb the surrounding cancel path.
- Side effects: Delegates shared in-memory teardown to `AgentManager.cleanup_session_runtime` and emits outcome/failure logs.
- Call chain: `_handle_message` invokes it only in the cancel branch's `finally`, after stream-task cleanup, when the internal disconnect classifier passed and intent is `cancel` or `supplement`.
- Boundary behavior: The manager visits existing agents only; adapter cleanup preserves history and defers removal while a session is active, executing, locked, or reconnecting.
- Tests/flow: AgentServer disconnect tests cover cleanup, absent runtime, send failure, stream cleanup failure, and manual/spoofed-source negatives. Gateway tests cover internal stamping, grace delay, and reconnect cancellation; the E2A flow still lists live WebSocket disconnect integration as pending.

## Detail Index

- Detail docs pending.
