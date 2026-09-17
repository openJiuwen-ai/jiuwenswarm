---
symbol: AgentWebSocketServer.resolve_adapter
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "resolve_adapter(agent: Any) -> Any"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: implicit
  output_contract: weak
  side_effects: none
  error_handling: implicit
  state_mutation: none
  dependency_coupling: high
  test_coverage: missing
  observability: none
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
    dimension: boundary_safety
    severity: high
    status: open
    summary: "A sandbox capability is used to resolve the adapter for a concurrency guard."
    evidence: "The wrapper delegates to _resolve_adapter, which accepts only candidates with apply_sandbox_runtime_patch. Its sole external caller needs is_deep_agent_executing_for_session; if an adapter has the busy-check capability but not sandbox patching, resolution returns None and proactive delivery proceeds without the busy guard."
    suggested_action: "Resolve the capability the caller actually needs, or expose a typed facade method that performs a conservative session-busy check."
  - id: ISSUE-002
    dimension: output_contract
    severity: medium
    status: open
    summary: "The public name and Any return hide a narrow, order-dependent heuristic."
    evidence: "Resolution searches _adapter, adapter, then _active_adapter and falls back to the agent itself, but only when apply_sandbox_runtime_patch exists. None of this is expressed by the signature or one-line public docstring, and AgentAdapter Protocol does not define that capability."
    suggested_action: "Return AgentAdapter | None under a documented protocol, and name or parameterize the required capability."
  - id: ISSUE-003
    dimension: dependency_coupling
    severity: medium
    status: open
    summary: "The wrapper hard-codes base-class dispatch."
    evidence: "resolve_adapter calls AgentWebSocketServer._resolve_adapter(agent) directly. An inherited call on a subclass cannot honor a subclass override of the protected resolver."
    suggested_action: "Use a classmethod with cls dispatch, or move adapter resolution into a standalone typed utility owned by the adapter layer."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No public resolver or real proactive-call-site tests were found."
    evidence: "Tests do not cover candidate priority, capability-only adapters, agent fallback, None, property errors, subclass behavior, or fail-open busy checking in trigger_main_agent."
    suggested_action: "Add table-driven resolver tests and a proactive integration test that refuses delivery whenever busy state cannot be safely resolved."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer.resolve_adapter`

## Actual Role

Publishes the protected `_resolve_adapter` heuristic for code outside `AgentWebSocketServer`. The current external caller uses it in proactive delivery to locate an inner adapter for a session-busy check, although the delegated heuristic selects by sandbox-patching capability rather than the busy-check capability.

## Key Signals

- Input: Any facade/agent-like object.
- Output: First sandbox-patch-capable nested candidate, the agent itself, or `None`.
- Side effects: None.
- Main risk: A missing or differently capable adapter can disable proactive concurrency avoidance.
- Tests/flow: No direct tests found; proactive flow tests mock the trigger boundary rather than this resolver path.

## Detail Index

- Detail docs pending.
