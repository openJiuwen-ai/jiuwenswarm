---
symbol: AgentWebSocketServer._mask_sensitive_fields
kind: method
source: jiuwenswarm/server/agent_ws_server.py
source_role: runtime_source
audit_scope: default_health_audit
class: AgentWebSocketServer
signature: "_mask_sensitive_fields(payload: Any) -> Any"
health:
  overall: risky
  name_behavior_match: good
  responsibility_focus: single
  length: short
  complexity: low
  implementation_soundness: partial
  boundary_safety: risky
  input_contract: implicit
  output_contract: clear
  side_effects: none
  error_handling: partial
  state_mutation: none
  dependency_coupling: low
  test_coverage: partial
  observability: not_applicable
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
    summary: "Common MCP credentials can be returned unmasked."
    evidence: "Only api_key, token, authorization, and secret substrings are matched. MCP accepts arbitrary headers/env, so x-api-key, password/passwd, credential, cookie, and access-key keys can pass through."
    suggested_action: "Normalize separators and expand credential vocabulary; preferably mask all values inside env and headers containers by policy."
  - id: ISSUE-002
    dimension: test_coverage
    severity: medium
    status: open
    summary: "Masking behavior has only narrow indirect coverage."
    evidence: "One command.mcp list test asserts env.TOKEN masking; no direct tests cover nested containers, response actions, headers, password-like keys, hyphenated API-key names, or safe fields."
    suggested_action: "Add direct table-driven tests and response-path tests for every command.mcp action."
confidence: confirmed
details: {}
---

# `AgentWebSocketServer._mask_sensitive_fields`

## Actual Role

Recursively copies dict/list payloads and replaces matched values with `***` based on key substrings or bearer/API-key/secret value markers. It is the response-side credential filter for `command.mcp` list, show, enable/disable, remove, and update results.

## Key Signals

- Input: any payload; practically nested MCP config dictionaries/lists containing headers, env, transport, and command fields.
- Output: Same JSON-like shape with matched sensitive values masked; primitives are returned unchanged.
- Main side effects: None.
- Main risk: denylist matching misses common arbitrary header/env credential names and can expose them over the websocket response.
- Related tests: One indirect command.mcp list test asserts `env.TOKEN` is masked.

## Detail Index

- Detail docs pending.
