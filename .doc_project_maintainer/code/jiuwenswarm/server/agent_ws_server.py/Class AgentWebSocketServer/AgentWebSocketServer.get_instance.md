---
symbol: AgentWebSocketServer.get_instance
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "get_instance(cls, *, host: str = '127.0.0.1', port: int = 18000, ping_interval: float | None = 30.0, ping_timeout: float | None = 300.0) -> AgentWebSocketServer"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: medium
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: implicit
  error_handling: missing
  state_mutation: global
  dependency_coupling: medium
  test_coverage: missing
  observability: missing
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
    dimension: input_contract
    severity: medium
    status: open
    summary: "First caller silently fixes server bind settings for the process."
    evidence: "agent_ws_server.py:986-988 returns an existing _instance without comparing kwargs. app_agentserver.py:156-159 supplies host/port; gateway push, remote bootstrap, send-file, and multi-session code contain no-argument calls."
    suggested_action: "Make the first-call contract explicit, or reject/log conflicting kwargs after initialization."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: low
    status: open
    summary: "Singleton mutation is unlocked."
    evidence: "agent_ws_server.py:986-995 performs an unlocked check-then-construct-then-assign sequence on the class-level _instance."
    suggested_action: "If this can be called from multiple threads, guard creation with a class lock; otherwise document the single-threaded asyncio boundary."
  - id: ISSUE-003
    dimension: state_mutation
    severity: low
    status: open
    summary: "Singleton lifecycle is decoupled from stop/reset cleanup."
    evidence: "reset_instance at agent_ws_server.py:998-1000 only assigns None; stop at 1248-1260 closes server/jiuwenbox resources without clearing _instance, while __init__ registers a process-global ACP callback capturing the created instance."
    suggested_action: "Add direct singleton lifecycle tests or cleanup guidance; consider clearing or replacing process-global callbacks as part of lifecycle reset."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer.get_instance`

## Actual Role

Classmethod lazy singleton factory for `AgentWebSocketServer`. It returns the existing class-level `_instance` or constructs one with bind and keepalive kwargs, which also runs constructor side effects such as AgentManager creation, JiuwenBox runner lookup, and global ACP push callback registration.

## Key Signals

- Input: Keyword-only bind and keepalive settings used only on first creation.
- Output: Process-global `AgentWebSocketServer` instance.
- Main side effects: Mutates `AgentWebSocketServer._instance`; first creation also invokes constructor side effects.
- Main risk: First-call-wins configuration is implicit, no-arg callers can create a default-bound singleton before configured startup, and creation is not locked.
- Related tests: `test_app_agentserver.py`, `test_gateway_push_transport.py`, and remote bootstrap tests replace or fake `get_instance`; no direct test was found for creation/reuse identity, first-call argument retention, reset, conflicting later kwargs, or post-stop reuse.

## Detail Index

- Detail docs pending.
