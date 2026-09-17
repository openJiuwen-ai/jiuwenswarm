---
symbol: AgentWebSocketServer._build_model_cache
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_build_model_cache(self) -> None"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: medium
  complexity: medium
  implementation_soundness: flawed
  boundary_safety: partial
  input_contract: implicit
  output_contract: weak
  side_effects: internal
  error_handling: missing
  state_mutation: internal
  dependency_coupling: high
  test_coverage: missing
  observability: partial
  performance_risk: medium
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
    summary: "Duplicate model identities and aliases resolve to the wrong model."
    evidence: "Keys use only model_name, so later same-name entries overwrite the marked default; aliases/indexed identities are discarded. DeepAdapter now preserves model_name#index, aliases, and defaults, so this duplicated implementation has drifted and _resolve_model silently falls back."
    suggested_action: "Use a shared public registry preserving indexed identities, aliases, and defaults."
  - id: ISSUE-002
    dimension: error_handling
    severity: high
    status: open
    summary: "One invalid later entry can permanently leave a half-built cache."
    evidence: "It mutates the cache entry by entry. If a later build raises, _default_model remains unset; the next _resolve_model sees a nonempty cache and will not rebuild, so default/unknown requests return None until recreation."
    suggested_action: "Build locally, validate a default, atomically swap both fields, and clear on failure."
  - id: ISSUE-003
    dimension: state_mutation
    severity: high
    status: open
    summary: "The process-lifetime cache is never invalidated after model configuration changes."
    evidence: "It initializes only when the cache is empty. Config reloads update AgentManager but never clear/fingerprint this cache, so agent generation and schedule operations can retain removed models, endpoints, headers, or credentials."
    suggested_action: "Fingerprint config and atomically rebuild on every relevant update/reload."
  - id: ISSUE-004
    dimension: test_coverage
    severity: high
    status: open
    summary: "No AgentServer model-cache tests were found."
    evidence: "Tests cover DeepAdapter resolution, not this method. Duplicates, aliases/defaults, partial failure, fallbacks, reload, and consumers are unverified."
    suggested_action: "Test identity semantics, atomic failure, fallbacks, invalidation, and consumers."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._build_model_cache`

## Actual Role

Builds configured models through a private DeepAdapter method, keys them by name, and selects the first for agent-generation and schedule consumers.

## Key Signals

- Input: Process config plus decrypted model entries; no explicit arguments.
- Output: Mutates `_model_cache` and `_default_model`; exceptions propagate.
- Side effects: Retains model clients and endpoint/credential config for server lifetime.
- Main risks: Identity collapse, poisoned partial cache, stale configuration, and implementation drift.
- Related flow/tests: No dedicated model-cache flow or direct tests found.

## Detail Index

- Detail docs pending.
