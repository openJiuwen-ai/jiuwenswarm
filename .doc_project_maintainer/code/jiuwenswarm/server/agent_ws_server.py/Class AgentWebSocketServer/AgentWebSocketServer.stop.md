---
symbol: AgentWebSocketServer.stop
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "stop(self) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: clear
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: medium
  test_coverage: missing
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
    dimension: error_handling
    severity: medium
    status: open
    summary: "WebSocket close or wait failures can skip JiuwenBox cleanup."
    evidence: "server.close() and await server.wait_closed() run before self._server is cleared and before the best-effort _jiuwenbox_runner.stop() block. An exception or cancellation in either listener step bypasses runner cleanup and the final stopped log."
    suggested_action: "Use finally-style cleanup so runner shutdown is attempted even when close() or wait_closed() raises."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: low
    status: open
    summary: "The no-op branch skips JiuwenBox runner cleanup when _server is None."
    evidence: "stop() returns immediately if self._server is None, before checking whether the shared JiuwenBox runner needs cleanup."
    suggested_action: "Stop the runner independently, or document that cleanup requires a bound listener."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Missing direct lifecycle tests for stop cleanup and failure paths."
    evidence: "App-level tests use a fake start/stop; no direct test covers close/wait, no-op, runner calls or failures, or listener-close failure cleanup."
    suggested_action: "Add async lifecycle tests with fake listener and runner objects for normal, no-op, and failure paths."
  - id: ISSUE-004
    dimension: performance_risk
    severity: medium
    status: open
    summary: "Listener shutdown has no method-level deadline for connection cleanup."
    evidence: "stop() awaits wait_closed() without a timeout. Connection handlers await inflight cancellation, scheduler stop, team cancellation, and task gathering before completing, so stuck cleanup can delay this method indefinitely."
    suggested_action: "Apply an explicit shutdown deadline with diagnostics for unfinished cleanup, while preserving a controlled fallback for remaining tasks."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer.stop`

## Actual Role

With `_server` present, closes the listener, awaits server and connection-handler termination, clears the reference, then best-effort stops JiuwenBox and logs completion. Scheduler and request-task cleanup occur indirectly in connection-handler `finally` blocks. With no server, it returns without cleanup.

## Key Signals

- Input: Implicit runtime state in `self._server` and `self._jiuwenbox_runner`.
- Output: None.
- Main side effects: Closes the WebSocket listener, mutates `self._server`, may stop the JiuwenBox subprocess runner, and writes logs.
- Main risk: Cleanup is gated by `_server is None`; listener close failure or cancellation skips JiuwenBox cleanup, while unbounded downstream connection cleanup can delay return.
- Related tests: No direct `AgentWebSocketServer.stop` lifecycle tests were found; current app-level tests use fake or mocked server instances.

## Detail Index

- Detail docs pending.
