---
symbol: AgentWebSocketServer.set_proactive_engine
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "set_proactive_engine(self, engine) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: explicit
  error_handling: clear
  state_mutation: isolated
  dependency_coupling: medium
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
    dimension: input_contract
    severity: low
    status: open
    summary: "Setter accepts Any while later handlers assume a ProactiveEngine-like surface."
    evidence: "The method stores the object directly, while proactive tick and reload paths later call tick_now, last_tick_at, reload_config, and rebuild_proactive_agent."
    suggested_action: "Type the setter as a ProactiveEngine or small Protocol, or document that only init_proactive_engine may call it."
  - id: ISSUE-002
    dimension: test_coverage
    severity: low
    status: open
    summary: "Server-level proactive engine injection and proactive.tick dispatch lack direct tests."
    evidence: "tests/unit_tests/agentserver/test_agent_reload_scope.py verifies reload_config through a MagicMock assigned directly to server._proactive_engine, and tests/symphony/test_proactive_recommendation_flow.py exercises ProactiveEngine.tick_now directly; no test was found that calls set_proactive_engine, init_proactive_engine, or AgentWebSocketServer._handle_proactive_tick."
    suggested_action: "Add a fake-engine test that injects through set_proactive_engine and covers proactive.tick success, missing-engine, and incompatible-engine responses."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer.set_proactive_engine`

## Actual Role

Stores a proactive recommendation engine instance on the server, replacing the constructor's default `None`; the startup adapter calls it only after constructing the engine and installing its callbacks. The setter does not initialize, validate, start, or wrap the object, while later tick and configuration-reload handlers expect `tick_now`, `last_tick_at`, `reload_config`, and `rebuild_proactive_agent`.

## Key Signals

- Input: ProactiveEngine-like object accepted as `Any`.
- Output: None.
- Main side effects: Updates `self._proactive_engine`.
- Main risk: A bad injected object fails later in tick or reload handlers because the setter does not enforce the expected engine surface.
- Related tests: `tests/unit_tests/agentserver/test_agent_reload_scope.py` indirectly covers the stored engine's reload use but bypasses this setter; `tests/symphony/test_proactive_recommendation_flow.py` covers the engine directly. Injection and `proactive.tick` handler tests are pending.

## Detail Index

- Detail docs pending.
