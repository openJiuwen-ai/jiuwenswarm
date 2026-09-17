---
symbol: AgentWebSocketServer.__init__
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "__init__(self, host: str = \"127.0.0.1\", port: int = 18000, *, ping_interval: float | None = 30.0, ping_timeout: float | None = 300.0) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: missing
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
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
    dimension: side_effects
    severity: medium
    status: open
    summary: "Constructor overwrites a process-global ACP output callback and captures this instance."
    evidence: "agent_ws_server.py:947-949 installs a callback on the singleton AcpOutputManager that captures self and schedules self.send_push; reset_instance() (998-1000) only clears the class singleton, while stop() (1248-1260) closes the WebSocket server and jiuwenbox runner without clearing the callback."
    suggested_action: "Clear or replace the ACP callback during server stop/reset, or centralize callback registration in singleton lifecycle code."
  - id: ISSUE-002
    dimension: test_coverage
    severity: low
    status: open
    summary: "Constructor behavior is only indirectly covered."
    evidence: "AgentServer harnesses exercise construction indirectly and test_agentserver_acp.py resets the global ACP manager callback in an autouse fixture, but no focused test asserts bind/keepalive fields, capability/task caches, manager/runner wiring, or callback cleanup; test_app_agentserver.py replaces get_instance with a fake."
    suggested_action: "Add a focused constructor test for bind settings, mutable state, runtime manager wiring, jiuwenbox runner access, and ACP callback cleanup."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer.__init__`

## Actual Role

Initializes bind and keepalive settings plus the mutable runtime state used for the active Gateway connection, per-WebSocket ACP capabilities, session stream tasks, agent management, scheduled execution, model caching, sandbox process access, and proactive recommendations. It also replaces the process-global ACP output callback with a closure that schedules `send_push` on this instance.

## Key Signals

- Input: host, port, ping interval, and ping timeout values.
- Output: initialized in-memory server object.
- Main side effects: creates an `AgentManager`, obtains/possibly creates the singleton `JiuwenBoxRunner`, and replaces the singleton ACP output push callback.
- Main risk: global ACP callback can keep pointing at a stopped or test-created instance.
- Related tests: indirect construction in `test_agentserver_acp.py`, `test_agentserver_modes.py`, `test_agentserver_cli_commands.py`, and `test_agent_reload_scope.py`; `test_app_agentserver.py` fakes `get_instance`, and no direct constructor lifecycle test was found.

## Detail Index

- Detail docs pending.
