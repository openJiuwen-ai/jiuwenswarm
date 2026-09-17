---
symbol: AgentWebSocketServer._effective_files_from_adapter
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_effective_files_from_adapter(adapter: Any) -> dict[str, list[dict[str, str]]] | None"
health:
  overall: risky
  name_behavior_match: partial
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: questionable
  boundary_safety: risky
  input_contract: implicit
  output_contract: clear
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
    dimension: boundary_safety
    severity: high
    status: open
    summary: "Cached desired mounts can be presented as remotely effective policy."
    evidence: "The helper reads only adapter._sys_operation_card.gateway_config.launcher_config.extra_params.policy. apply_sandbox_runtime_patch replaces that cache at interface_deep.py:3051 before remote force-recreate, whose import/recreate failures are only logged; the caller then prefers this cached result over a rebuilt view."
    suggested_action: "Track last successfully applied policy/sandbox ID separately, or query remote state and label desired versus applied mounts explicitly."
  - id: ISSUE-002
    dimension: test_coverage
    severity: high
    status: open
    summary: "Adapter policy extraction and stale-cache behavior are untested."
    evidence: "No test references _effective_files_from_adapter or effective_files_from_policy, and no sandbox command test asserts effective_files after remote recreation failure."
    suggested_action: "Cover absent/malformed card layers, rw/ro conversion, deduplication, and desired-cache versus applied-policy failure behavior."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._effective_files_from_adapter`

## Actual Role

Safely traverses an adapter's private active SysOperationCard, extracts the launcher `extra_params.policy` mapping, and converts its cached filesystem bind mounts into the `allow_write`/`deny_write` display shape used by `/sandbox`. It returns `None` when any expected card/config/launcher/policy layer is absent so the caller can rebuild a view from runtime configuration.

## Key Signals

- Input: Adapter-like object with an optional private `_sys_operation_card` graph.
- Output: Display buckets derived from cached bind mounts, or `None` when no usable cached policy exists.
- Side effects: None; conversion may stat host paths to classify files versus directories.
- Call chain: `_attach_effective_sandbox_files` prefers this result and returns immediately; only `None` triggers its runtime-config fallback.
- Main risk: The source is adapter memory, not jiuwenbox state, so the display can overstate what was applied remotely.
- Tests/flow: No extraction or stale-cache tests were found, and project docs contain no dedicated sandbox flow.

## Detail Index

- Detail docs pending.
