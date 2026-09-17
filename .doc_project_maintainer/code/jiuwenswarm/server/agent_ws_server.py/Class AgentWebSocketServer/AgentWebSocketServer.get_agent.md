---
symbol: AgentWebSocketServer.get_agent
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "get_agent(self)"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: risky
  input_contract: weak
  output_contract: weak
  side_effects: none
  error_handling: missing
  state_mutation: none
  dependency_coupling: high
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
    dimension: implementation_soundness
    severity: high
    status: open
    summary: "The compatibility accessor returns an arbitrary cached variant, not a defined default agent."
    evidence: "It delegates to AgentManager.get_agent_nowait() with no arguments. That normalizes the channel to default, then its first unfiltered loop immediately returns the first dict value; the later branch intended to prefer mode=agent is unreachable whenever any entry exists. Multiple mode, sub-mode, and project cache keys can coexist per channel."
    suggested_action: "Define a unique default cache identity and request it explicitly, or remove this ambiguous accessor. Fix get_agent_nowait so unfiltered selection follows one documented rule."
  - id: ISSUE-002
    dimension: boundary_safety
    severity: high
    status: open
    summary: "The only caller can cross channel, mode, and project boundaries on fallback."
    evidence: "MultiSessionToolkit.notify first looks up self.channel_id, but if absent calls server.get_agent(), which silently switches to the default channel and omits mode/project identity. It can run a background-session final summary through an unrelated agent wrapper and workspace configuration."
    suggested_action: "Do not fall back across identities; retain the originating channel/mode/project key and emit the raw summary if that exact agent is unavailable."
  - id: ISSUE-003
    dimension: observability
    severity: medium
    status: open
    summary: "Ambiguous fallback selection is invisible."
    evidence: "The accessor neither accepts nor logs the requested identity or selected cache key. MultiSessionToolkit logs only when no wrapper exists, not when this fallback returns a mismatched wrapper."
    suggested_action: "Require explicit identity and record a structured warning whenever a compatibility fallback is attempted or rejected."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No direct accessor or background-notification fallback tests were found."
    evidence: "Related tests use fake get_agent_nowait methods but do not call AgentWebSocketServer.get_agent. No test covers multiple channels, modes, sub-modes, projects, insertion order, eviction, or MultiSessionToolkit fallback isolation."
    suggested_action: "Add direct selection tests and a multi-session completion test proving summaries never cross agent cache identities."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer.get_agent`

## Actual Role

Provides a legacy synchronous accessor that asks AgentManager for an existing agent without supplying any identity. It therefore reads the default channel and returns whichever cached variant was inserted first, or `None`.

## Key Signals

- Input: None; channel, mode, sub-mode, and project silently use unspecified defaults.
- Output: An arbitrary existing default-channel wrapper or `None`.
- Main side effects: None; reads shared AgentManager cache state.
- Main risk: Background completion can run through an unrelated channel/project/mode agent.
- Related tests/flow: No direct tests; no multi-session background-completion flow doc found, so flow remains pending.

## Detail Index

- Detail docs pending.
