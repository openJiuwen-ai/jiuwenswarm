---
symbol: _run
kind: function
source: jiuwenswarm/server/app_agentserver.py
source_role: runtime_source
audit_scope: default_health_audit
signature: "_run(host: str, port: int) -> None"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: mixed
  length: medium
  complexity: medium
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: clear
  output_contract: clear
  side_effects: explicit
  error_handling: partial
  state_mutation: shared
  dependency_coupling: high
  test_coverage: partial
  observability: clear
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
    summary: "Startup cleanup around post-server initialization needs focused audit."
    evidence: "The WebSocket server starts before proactive initialization and before the long wait/finally shutdown block is entered."
    suggested_action: "Audit and test extension/proactive startup failure paths for server cleanup."
confidence: confirmed
details: {}
---

# `_run`

## Actual Role

Starts the standalone AgentServer runtime: loads extensions, obtains the `AgentWebSocketServer` singleton, starts the WebSocket server, initializes the proactive adapter, starts a teammate bootstrap daemon, waits for signal/cancel shutdown, then stops bootstrap, server, and team observability.

## Key Signals

- Input: bind host and port.
- Output: none; runs until stopped.
- Main side effects: WebSocket listener, extension registry, proactive engine, background bootstrap task, signal handlers, observability shutdown.
- Main risk: mixed startup and shutdown responsibilities with several cross-module imports.
- Related tests: `tests/unit_tests/test_app_agentserver.py`, system tests that launch `jiuwenswarm.server.app_agentserver`.

## Detail Index

- Detail docs pending.
