---
symbol: AgentWebSocketServer._stop_scheduler
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_stop_scheduler(self) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
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
  confidence: confirmed
  expired_reason: null
issues:
  - id: ISSUE-001
    dimension: error_handling
    severity: medium
    status: open
    summary: "Failed scheduler stop discards the service reference."
    evidence: "_stop_scheduler catches any Exception from AutoHarnessService.stop_scheduler(), logs a warning, and still sets self._scheduler_service = None. The next schedule request then creates and starts a new service even though the old scheduler may still be running after its failed stop."
    suggested_action: "Clear the reference only after confirmed stop, or retain explicit failed-stop state so cleanup can be retried before another scheduler service is created."
  - id: ISSUE-002
    dimension: state_mutation
    severity: medium
    status: open
    summary: "Any WebSocket disconnect stops the server-wide scheduler service."
    evidence: "_scheduler_service is a single AgentWebSocketServer field lazily shared by schedule requests, but its only _stop_scheduler caller is each _connection_handler.finally. AgentWebSocketServer.stop() closes the listener and JiuwenBox runner without calling this helper."
    suggested_action: "Move scheduler cleanup into explicit server shutdown, or document and enforce a single-connection lifecycle if disconnect-scoped scheduling is intentional."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct tests cover scheduler stop success, failure, no-service, or lifecycle caller behavior."
    evidence: "No `_stop_scheduler` or `AutoHarnessService.stop_scheduler` reference was found under tests; existing connection-close and app shutdown tests do not assert scheduler-service cleanup."
    suggested_action: "Add focused async tests with a fake scheduler service for success, raised exception, no-op, and stop()/connection cleanup interaction."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._stop_scheduler`

## Actual Role

If the server-wide `_scheduler_service` reference is set, awaits that `AutoHarnessService` object's `stop_scheduler()`, logs success or a swallowed `Exception`, and then clears the reference in either outcome. If no service is set, it returns without logging or mutation.

## Key Signals

- Input: Implicit shared state in `self._scheduler_service`.
- Output: None.
- Main side effects: Awaits scheduler shutdown, writes logs, and mutates `self._scheduler_service`.
- Main risk: A failed stop can orphan a live scheduler before lazy re-creation, and the current per-connection caller can stop the shared scheduler while the server remains available.
- Related tests: No direct `_stop_scheduler`, service-stop, or connection/server lifecycle assertion was found.

## Detail Index

- Detail docs pending.
