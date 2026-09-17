---
symbol: AgentWebSocketServer._resolve_active_is_code_agent
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_resolve_active_is_code_agent(self, channel_id: str) -> bool"
health:
  overall: watch
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: partial
  input_contract: implicit
  output_contract: clear
  side_effects: none
  error_handling: partial
  state_mutation: none
  dependency_coupling: high
  test_coverage: missing
  observability: partial
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
    severity: medium
    status: open
    summary: "The resolved flag currently has no downstream policy or display effect."
    evidence: "All four callers forward the result, but build_filesystem_policy and list_auto_managed_sandbox_paths delete is_code_agent; the other helpers only forward it. The docstring still claims Code-specific mount behavior."
    suggested_action: "Either remove the dead flag plumbing and stale mount-layout claims, or restore an explicit, tested Code-versus-Deep policy difference."
  - id: ISSUE-002
    dimension: dependency_coupling
    severity: medium
    status: open
    summary: "The selected adapter is not qualified by request project, mode, or sub-mode."
    evidence: "get_agent_nowait(channel_id) prefers a cached agent-mode instance and otherwise the first channel instance. Multiple projects or modes can select an adapter different from the request context."
    suggested_action: "Resolve the exact request cache identity or pass the already-selected adapter/project/mode context into this helper."
  - id: ISSUE-003
    dimension: error_handling
    severity: medium
    status: open
    summary: "Lookup failure and a genuine Deep adapter collapse to the same False result."
    evidence: "Manager exceptions, no agent/adapter, and an absent attribute all return False; only manager exceptions emit a debug log. Callers cannot distinguish unknown from confirmed non-Code identity."
    suggested_action: "Use an explicit unknown state or resolve the adapter before policy work, and surface identity failures where the distinction affects behavior."
  - id: ISSUE-004
    dimension: test_coverage
    severity: medium
    status: open
    summary: "No direct or caller-level tests cover adapter-flavor resolution."
    evidence: "No tests reference _resolve_active_is_code_agent or is_code_agent. Missing cases include Code/Deep adapters, manager failure, absent adapters, and channels with multiple project/mode instances."
    suggested_action: "Add focused resolver tests and downstream policy/display assertions before retaining or reviving this flag."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._resolve_active_is_code_agent`

## Actual Role

Fetches an unqualified cached agent, resolves its adapter, and returns the private `_is_code_agent` flag. Failures default to `False`; current policy and display helpers discard the forwarded result.

## Key Signals

- Input: Channel id only; no project, mode, or sub-mode identity.
- Output: `True` only for a resolved adapter with a truthy `_is_code_agent`; otherwise `False`.
- Side effects: None; manager exceptions produce a debug log.
- Main risk: A future reactivation of this currently inert flag could apply the wrong adapter flavor on multi-instance channels.
- Related tests/flow: No direct or adjacent tests found; sandbox runtime remains a pending build-plan slice and has no dedicated flow document.

## Detail Index

- Detail docs pending.
