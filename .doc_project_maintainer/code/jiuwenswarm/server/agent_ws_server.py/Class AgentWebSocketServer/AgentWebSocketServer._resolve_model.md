---
symbol: AgentWebSocketServer._resolve_model
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_resolve_model(self, model_name: Optional[str] = None) -> Optional[Any]"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: flawed
  boundary_safety: watch
  input_contract: weak
  output_contract: weak
  side_effects: implicit
  error_handling: flawed
  state_mutation: isolated
  dependency_coupling: high
  test_coverage: missing
  observability: weak
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
    summary: "An invalid model can poison the lazy cache."
    evidence: "Lines 6764-6765 rebuild only an empty cache. The builder inserts before completion and does not catch per-entry errors; a later failure leaves entries but no default, preventing retry."
    suggested_action: "Build temporarily, validate entries, then atomically commit a usable cache/default."
  - id: ISSUE-002
    dimension: dependency_coupling
    severity: high
    status: open
    summary: "The copied resolver violates canonical selection semantics."
    evidence: "Lines 6782-6788 overwrite duplicate model names and ignore alias, #index, and is_default. interface_deep.py:2430-2477 preserves them."
    suggested_action: "Use the shared model registry instead of copied cache logic."
  - id: ISSUE-003
    dimension: state_mutation
    severity: high
    status: open
    summary: "Models stay stale after config or credential changes."
    evidence: "The cache is populated lazily, but no production path clears or fingerprints it; callers retain old endpoints, keys, and settings after reload."
    suggested_action: "Fingerprint or invalidate on config reload and close replaced clients."
  - id: ISSUE-004
    dimension: output_contract
    severity: medium
    status: open
    summary: "Unknown explicit names silently use the default."
    evidence: "Lines 6768-6770 fall back without telling schedule callers, silently changing provider, cost, or policy intent."
    suggested_action: "Reject unknown names or explicitly report fallback and resolved identity."
  - id: ISSUE-005
    dimension: test_coverage
    severity: high
    status: open
    summary: "No AgentServer resolver test was found."
    evidence: "No test references this resolver/builder; existing cache tests target DeepAdapter, not this duplicate or its callers."
    suggested_action: "Test aliases, duplicates, defaults, invalid recovery, reloads, and unknown names."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._resolve_model`

## Actual Role

Lazily builds a local model cache, returns an exact name hit, else the first cached model.

## Key Signals

- Callers: Agent configuration generation and schedule create/run/issue-watch operations.
- Mutation: Hidden lazy writes to `_model_cache` and `_default_model`.
- Failure: Contrary to its docstring, config/build errors can propagate or leave a poisoned partial cache.
- Runtime: Synchronous config load, credential resolution, imports, and model construction occur on the request event loop.
- Tests/flow: No direct tests or dedicated model-resolution flow found.

## Detail Index

- Detail docs pending.
