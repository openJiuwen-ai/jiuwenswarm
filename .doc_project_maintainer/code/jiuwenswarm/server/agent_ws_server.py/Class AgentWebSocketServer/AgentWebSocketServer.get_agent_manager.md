---
symbol: AgentWebSocketServer.get_agent_manager
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "get_agent_manager(self) -> AgentManager"
health:
  overall: watch
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: sound
  boundary_safety: partial
  input_contract: clear
  output_contract: clear
  side_effects: none
  error_handling: clear
  state_mutation: none
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
    dimension: dependency_coupling
    severity: medium
    status: open
    summary: "The accessor exposes the complete mutable AgentManager."
    evidence: "It returns the server-owned object directly. Production callers use concrete methods such as get_agent_nowait and get_agent (which can create Agents), while tests monkeypatch reload/get methods through the returned instance. No interface limits lifecycle or state mutation."
    suggested_action: "Expose narrow server operations or a read-only protocol for lookup callers, and inject explicit lifecycle services where mutation is required."
  - id: ISSUE-002
    dimension: test_coverage
    severity: low
    status: open
    summary: "The identity and lifetime contract is only indirectly exercised."
    evidence: "Many tests obtain the manager to patch downstream behavior, but none directly asserts that the accessor returns the server-owned manager consistently or defines behavior across server reset/recreation."
    suggested_action: "Add a small ownership/lifetime contract test if this accessor remains a supported public boundary."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer.get_agent_manager`

## Actual Role

Returns the exact `AgentManager` instance constructed and owned by this `AgentWebSocketServer`. It performs no validation, copying, synchronization, or lifecycle work; callers receive full access to the manager's mutable caches and operations.

## Key Signals

- Input: None beyond the server instance.
- Output: Stable live reference to `self._agent_manager`.
- Side effects: None in the accessor; subsequent caller operations may create, reload, or inspect Agents.
- Main risk: External modules become coupled to mutable AgentManager internals and can bypass server-level coordination boundaries.
- Tests/flow: Indirectly used by mode/CLI tests and remote-member tests; Gateway chat and session flows document AgentManager usage, not this ownership contract.

## Detail Index

- Detail docs pending.
