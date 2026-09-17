---
symbol: AgentWebSocketServer._resolve_adapter
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_resolve_adapter(agent: Any) -> Any"
health:
  overall: watch
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: partial
  input_contract: implicit
  output_contract: implicit
  side_effects: none
  error_handling: clear
  state_mutation: none
  dependency_coupling: high
  test_coverage: missing
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
    summary: "Adapter resolution is tied to a private, protocol-external sandbox capability."
    evidence: "Lines 5156-5162 inspect private/legacy attributes and accept only objects having apply_sandbox_runtime_patch, which is absent from the runtime-checkable AgentAdapter protocol. A future protocol-compliant backend can therefore resolve to None, while an object with a non-callable attribute is accepted."
    suggested_action: "Expose the active adapter through a typed facade API and use explicit runtime-checkable capability protocols with callable validation per consumer."
  - id: ISSUE-002
    dimension: implementation_soundness
    severity: medium
    status: open
    summary: "A sandbox-specific selector is reused as a general public adapter resolver."
    evidence: "Sandbox context, policy display, and patching call this helper, but proactive_adapter.py:89 also uses public resolve_adapter for is_deep_agent_executing_for_session. That consumer can lose busy-session avoidance solely because an adapter lacks the unrelated sandbox patch method."
    suggested_action: "Separate facade unwrapping from feature capability checks, then validate sandbox and proactive methods at their own call sites."
  - id: ISSUE-003
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Resolution precedence and capability edge cases are untested."
    evidence: "No test references _resolve_adapter or resolve_adapter; None, _adapter/adapter/_active_adapter precedence, self fallback, non-callable attributes, and proactive reuse are uncovered."
    suggested_action: "Add table-driven resolver tests and a proactive test for adapters without sandbox capability."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._resolve_adapter`

## Actual Role

Unwraps the current Deep/Code runtime adapter from an agent-like facade by checking `_adapter`, `adapter`, and `_active_adapter` in that order, then accepting the agent itself as a fallback. A candidate is returned only when it exposes `apply_sandbox_runtime_patch`; otherwise the method returns `None`.

## Key Signals

- Input: Optional agent/facade object of unconstrained type.
- Output: First sandbox-patch-capable candidate, the agent itself, or `None`.
- Side effects: None; attribute access only.
- Callers: Sandbox project/code-context resolution, cached policy display, runtime patching, the public `resolve_adapter` wrapper, and proactive busy-session avoidance.
- Current compatibility: The only implemented harness Deep and Code adapters inherit the required method; the generic `AgentAdapter` protocol does not require it.
- Tests/flow: No direct resolver tests were found. Server-push flow mentions proactive events but does not verify adapter resolution; no sandbox flow exists.

## Detail Index

- Detail docs pending.
